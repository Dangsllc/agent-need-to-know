"""Orchestration: evaluate desired vs actual, scan, plan and apply, profile and
project edits, the SessionStart check and snapshots. Talks to agent tools only
through the adapter."""
import concurrent.futures
import datetime
import json
import os
import time

from . import adapters, lifecycle, model
from .config import (SCRIPTS_DIR, atomic_write, config_path, legacy_notes, load_config, log_change,
                     read_json, state_read, state_write, write_json_2)
from .model import (NAME_RE, _uniq, foreign_tool, _visible, abs_of, discover, find_project_key, load_profiles,
                    load_projects, norm_conn, norm_entry, resolve, save_profiles, save_projects)


CACHE_TTL = 300


def A():
    return adapters.get()


def load_cache(cfg):
    c = read_json(cfg.cache_read_path, {})
    if not isinstance(c, dict):
        c = {}
    c.setdefault("projects", {})
    return c


def save_cache(cfg, cache):
    try:
        write_json_2(cfg.cache_path, cache)
    except Exception:
        pass


def invalidate_cache(cfg, abspath):
    c = load_cache(cfg)
    c["projects"].pop(abspath, None)
    save_cache(cfg, c)



def evaluate(cfg, rel, entry, profiles, defaults, cj, plugin_list, connectors_avail, costs):
    ap = abs_of(cfg, rel)
    gone = not os.path.isdir(ap)
    des = resolve(profiles, defaults, entry, connectors_avail)
    res = {"path": rel, "abs": ap, "notes": (entry or {}).get("notes", ""),
           "attached": des["own"], "defaults": des["defaults"], "unknown_profiles": des["unknown"],
           "recommended": [r for r in (entry or {}).get("recommended", [])
                           if r in profiles and r not in des["attached"]],
           "inline_items": des["inline_items"], "drift": [], "missing": [], "missing_hints": {},
           "plugins": [], "connectors": [], "cost": None, "desired": des,
           "foreign": des["foreign"]}
    ad = A()
    actual_off = ad.connectors_off(cj, ap)
    enabled = {p["id"] for p in plugin_list or [] if p["enabled"] and not ad.is_self(p["id"])}
    present = {p["id"] for p in plugin_list or []}
    scanned = plugin_list is not None
    want_plugins = [i for i in des["plugins"] if not ad.is_self(i)]
    sp, dp = des["sources"]["plugin"], des["denied_by"]["plugin"]
    inl = (entry or {}).get("inline", {}) or {}
    ids = _uniq(want_plugins + sorted(enabled) + [i for i in list(sp) + list(dp) if not ad.is_self(i)])
    for i in ids:
        want = i in want_plugins
        en, pr = i in enabled, i in present
        if want and scanned and not pr:
            st = "missing"
            res["missing"].append(i)
        elif want and scanned and not en:
            st = "needs-on"
            res["drift"].append("plugin %s is off, profiles want it on" % i)
        elif want:
            st = "on" if scanned else "on?"
        elif en:
            st = "needs-off"
            res["drift"].append("plugin %s is on, profiles do not grant it" % i)
        else:
            st = "off"
        iv = "on" if i in inl.get("plugins_on", []) else ("off" if i in inl.get("plugins_off", []) else None)
        res["plugins"].append({"id": i, "desired": want, "enabled": en, "present": pr, "state": st,
                               "sources": sp.get(i, []), "denied_by": dp.get(i, []), "inline": iv})
    avail = set(connectors_avail or [])
    c_on, c_off = set(des["connectors_on"]), set(des["connectors_off"])
    sc, dc_ = des["sources"]["connector"], des["denied_by"]["connector"]
    for c in sorted(avail | c_on | c_off | set(actual_off)):
        a_off = c in actual_off
        d_off = (c in c_off) if (c in c_on or c in c_off) else a_off  # undecided: leave as is
        if d_off != a_off:
            res["drift"].append("connector %s is %s, profiles want it %s" % (c, "off" if a_off else "on", "off" if d_off else "on"))
        iv = "on" if c in inl.get("connectors_on", []) else ("off" if c in inl.get("connectors_off", []) else None)
        res["connectors"].append({"name": c, "desired_off": d_off, "actual_off": a_off, "available": c in avail,
                                  "sources": sc.get(c, []), "denied_by": dc_.get(c, []), "inline": iv})
    if costs is not None:
        res["cost"] = sum(costs.get(p["id"], 0) for p in res["plugins"] if p["desired"])
    if not entry:  # no entry, so nothing is desired and nothing can drift
        res["drift"], res["missing"] = [], []
        for p in res["plugins"]:
            p["state"] = "on" if p["enabled"] else p["state"]
        for c in res["connectors"]:
            c["desired_off"] = c["actual_off"]
    if gone:
        res["status"] = "gone"
    elif not entry:
        res["status"] = "new"
    elif des["unknown"] or not scanned:
        res["status"] = "error"
        res["error"] = ("unknown profile(s): " + ", ".join(des["unknown"])) if des["unknown"] else "plugin list unavailable"
    elif res["missing"]:
        res["status"] = "missing"
    elif res["drift"]:
        res["status"] = "drift"
    else:
        res["status"] = "ok"
    return res


def get_connectors(cfg, ctx=None, fresh=False):
    """Available claude.ai connector names: cache first, fetched when absent/old."""
    if ctx is not None and "conn" in ctx and not fresh:
        return ctx["conn"]
    cache = load_cache(cfg)
    c = cache.get("connectors") or {}
    names = c.get("names") or []
    if fresh or not names or time.time() - c.get("ts", 0) > 86400:
        got = A().list_available(cfg.root)
        if got:
            cache["connectors"] = {"ts": time.time(), "names": got}
            save_cache(cfg, cache)
            names = got
    if ctx is not None:
        ctx["conn"] = names
    return names


def scan(cfg, fresh=False):
    profiles, meta = load_profiles(cfg)
    defaults = meta["default_profiles"]
    projects = load_projects(cfg)["projects"]
    cj = A().load_state()
    cache = load_cache(cfg)
    now = time.time()
    rels = _uniq(discover(cfg, projects.keys()) + list(projects.keys()))
    live = [r for r in rels if os.path.isdir(abs_of(cfg, r))]

    def need(r):
        c = cache["projects"].get(abs_of(cfg, r))
        return fresh or not c or now - c.get("ts", 0) > CACHE_TTL

    conn_c = cache.get("connectors") or {}
    if fresh or not conn_c or now - conn_c.get("ts", 0) > CACHE_TTL:
        names = A().list_available(cfg.root)
        if names:
            cache["connectors"] = {"ts": now, "names": names}
    connectors = (cache.get("connectors") or {}).get("names", [])
    todo = [r for r in live if need(r)]
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(A().list_plugins, abs_of(cfg, r)): r for r in todo}
            for f, r in futs.items():
                pl = f.result()
                if pl is not None:
                    cache["projects"][abs_of(cfg, r)] = {"ts": now, "plugins": pl}
    if todo or cache.get("connectors") != conn_c:
        save_cache(cfg, cache)
    costs = read_json(os.path.join(cfg.data_dir, "plugin_costs.json"), None)
    if not isinstance(costs, dict):
        costs = None
    oi = A().account_info()
    rows = []
    for r in rels:
        ap = abs_of(cfg, r)
        pl = (cache["projects"].get(ap) or {}).get("plugins") if r in live else None
        row = evaluate(cfg, r, projects.get(r), profiles, defaults, cj, pl, connectors, costs)
        row["missing_hints"] = {i: A().missing_hint(i, oi) for i in row["missing"]}
        rows.append(row)
    stale = A().stale_entries(cj, cfg.root)
    complete = bool(connectors) and all(
        (cache["projects"].get(abs_of(cfg, r)) or {}).get("plugins") is not None for r in live)
    lists = [(cache["projects"].get(abs_of(cfg, r)) or {}).get("plugins") for r in live]
    caps, seen_ids = lifecycle.update_capabilities(cfg, lists, oi, connectors, profiles, complete)
    miss = {}
    for row in rows:
        for i in row["missing"]:
            miss.setdefault(i, []).append(row["path"])
    miss_list = [{"id": i, "projects": ps, "hint": A().missing_hint(i, oi)} for i, ps in sorted(miss.items())]
    counts = {k: 0 for k in ("ok", "drift", "missing", "new", "gone", "error")}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    prof_out = {}
    for n, p in profiles.items():
        prof_out[n] = dict({k: v for k, v in p.items() if k != "_extra"}, default=n in defaults,
                           projects=[r for r, e in projects.items() if n in e.get("profiles", [])])
    seen_plugins = set()
    for r in rows:
        seen_plugins |= {p["id"] for p in r["plugins"]}
    known_plugins = sorted(i for i in (set(meta["plugin_accounts"]) | seen_plugins |
                                       {i for p in profiles.values() for i in p["plugins"]})
                           if not foreign_tool(i))
    return {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": cfg.root, "org": {"name": oi["name"], "id8": oi["id8"]}, "orgs": oi["orgs"],
        "default_profiles": defaults, "profiles": prof_out,
        "known_plugins": known_plugins, "available_connectors": connectors,
        "counts": counts, "projects": rows, "stale": stale, "missing": miss_list,
        "has_costs": costs is not None,
        "capabilities": lifecycle.caps_view(caps, rows, profiles, oi, seen_ids),
        "warnings": list(model.WARNINGS), "notes": legacy_notes(),
        "inbox": {st: sorted(i for i, e in caps["capabilities"].items() if e["status"] == st) for st in ("new", "gone")},
    }




def describe_act(a):
    if a["kind"] == "plugin":
        if a["old"] == "absent":
            return "+plugin %s (install)" % a["item"]
        return ("+plugin %s" if a["new"] == "on" else "-plugin %s") % a["item"]
    if a["kind"] == "connector":
        return "connector %s %s->%s" % (a["item"], a["old"], a["new"])
    return "missing %s (skipped)" % a["item"]


def plan_project(cfg, rel, ctx, only=None):
    """-> (row, acts, err) where err is (message, rc) or None."""
    projects = ctx["projects"] if "projects" in ctx else load_projects(cfg)["projects"]
    entry = projects.get(rel)
    if not entry:
        return None, [], ("%s has no entry in projects.json; attach a profile first" % rel, 2)
    ap = abs_of(cfg, rel)
    if not os.path.isdir(ap):
        return None, [], ("folder is gone: %s" % ap, 2)
    lists = ctx.get("lists")
    pl = lists[rel] if lists is not None and rel in lists else A().list_plugins(ap)
    if pl is None:
        return None, [], ("could not read the plugin list for %s (is `claude` on PATH and logged in?)" % rel, 1)
    profiles, meta = load_profiles(cfg)
    row = evaluate(cfg, rel, entry, profiles, meta["default_profiles"], A().load_state(), pl,
                   get_connectors(cfg, ctx), None)
    if row["unknown_profiles"]:
        return row, [], ("%s: unknown profile(s): %s" % (rel, ", ".join(row["unknown_profiles"])), 2)
    return row, A().plan(row, only), None



def do_apply(cfg, rel, dry=False, only=None, out=print, ctx=None):
    ctx = ctx if ctx is not None else {}
    row, acts, err = plan_project(cfg, rel, ctx, only)
    if err:
        out(err[0])
        return err[1]
    if not acts:
        out("%s: nothing to change" % rel)
        return 0
    for a in acts:
        if a["kind"] == "report":
            out("%s: MISSING %s (not available on this account); skipped" % (rel, a["item"]))
        elif dry:
            out("would %s %s: %s -> %s" % (a["kind"], a["item"], a["old"], a["new"]))
    if dry:
        return 0
    rc = A().apply(cfg, rel, row, acts, only, ctx, out)
    invalidate_cache(cfg, abs_of(cfg, rel))
    return rc


def _tally(acts, t):
    for a in acts:
        if a["kind"] == "plugin":
            k = "installs" if a["old"] == "absent" else ("enables" if a["new"] == "on" else "disables")
        elif a["kind"] == "connector":
            k = "connector changes"
        else:
            k = "missing (skipped)"
        t[k] = t.get(k, 0) + 1


def _fetch_lists(cfg, rels):
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {r: ex.submit(A().list_plugins, abs_of(cfg, r)) for r in rels}
        for r, f in futs.items():
            out[r] = f.result()
    return out


def apply_all(cfg, dry=False, out=print):
    projects = load_projects(cfg)["projects"]
    rels = [r for r in projects if os.path.isdir(abs_of(cfg, r))]
    ctx = {"projects": projects, "lists": _fetch_lists(cfg, rels)}
    get_connectors(cfg, ctx)
    if dry:
        out("(dry run: nothing will be changed)")
    tot, failures, changed = {}, [], 0
    for rel in projects:
        if rel not in rels:
            out("%s: folder is gone (skipped)" % rel)
            continue
        try:
            row, acts, err = plan_project(cfg, rel, ctx)
            if err:
                failures.append("%s: %s" % (rel, err[0]))
                out("%s: FAILED %s" % (rel, err[0]))
                continue
            out("%s: %s" % (rel, ", ".join(describe_act(a) for a in acts) or "ok"))
            if acts:
                changed += 1
                _tally(acts, tot)
            if acts and not dry:
                rc = A().apply(cfg, rel, row, acts, None, ctx,
                               lambda m: out("  " + m) if m.startswith("FAILED") else None)
                invalidate_cache(cfg, abs_of(cfg, rel))
                if rc:
                    failures.append("%s: a plugin command failed" % rel)
        except Exception as e:
            failures.append("%s: %s" % (rel, e))
            out("%s: FAILED %s" % (rel, e))
    parts = ["%d %s" % (v, k) for k, v in sorted(tot.items())]
    out("totals: %d of %d project(s) %s%s" % (changed, len(projects), "would change" if dry else "changed",
                                              (": " + ", ".join(parts)) if parts else ""))
    for f in failures:
        out("failure: " + f)
    return 1 if failures else 0


# ------------------------------------------------- attach / detach / profile edit
def _need_profile(profiles, name, out):
    if name not in profiles:
        out("unknown profile: %s" % name)
        return False
    return True


def do_attach(cfg, rel, profile, out=print, apply=True, attach=True):
    profiles, meta = load_profiles(cfg)
    if not _need_profile(profiles, profile, out):
        return 2
    d = load_projects(cfg)
    e = d["projects"].get(rel)
    if attach:
        if e is None:
            e = d["projects"][rel] = norm_entry({})
        if profile in e["profiles"]:
            out("%s already has %s attached" % (rel, profile))
            return 0
        e["profiles"].append(profile)
        if profile in e.get("recommended", []):
            e["recommended"] = [r for r in e["recommended"] if r != profile]
    else:
        if e is None or profile not in e["profiles"]:
            out("%s has no %s attached%s" % (rel, profile,
                                              " (it is a default profile; remove it from default_profiles)" if profile in meta["default_profiles"] else ""))
            return 2
        e["profiles"] = [p for p in e["profiles"] if p != profile]
    save_projects(cfg, d)
    log_change(cfg, rel, "profile:" + profile, "-" if attach else "attached", "attached" if attach else "detached")
    out("%s: %s %s" % (rel, "attached" if attach else "detached", profile))
    return do_apply(cfg, rel, out=out) if apply else 0


def _affected(projects, defaults, name):
    if name in defaults:
        return list(projects)
    return [r for r, e in projects.items() if name in e.get("profiles", [])]


def _cached_lists(cfg, rels):
    cache = load_cache(cfg)
    lists, missing = {}, []
    for r in rels:
        c = cache["projects"].get(abs_of(cfg, r))
        if c and c.get("plugins") is not None:
            lists[r] = c["plugins"]
        elif os.path.isdir(abs_of(cfg, r)):
            missing.append(r)
    if missing:
        lists.update(_fetch_lists(cfg, missing))
    return lists


def _project_changes(cfg, rels, projects, profiles, defaults):
    """What apply would do for each project under the given (possibly edited) profiles."""
    lists = _cached_lists(cfg, rels)
    cj, conn, res = A().load_state(), get_connectors(cfg), []
    for r in rels:
        if r not in lists or lists[r] is None:
            res.append({"path": r, "changes": [], "note": "not scanned"})
            continue
        row = evaluate(cfg, r, projects[r], profiles, defaults, cj, lists[r], conn, None)
        res.append({"path": r, "changes": [describe_act(a) for a in A().plan(row)]})
    return res


def profile_edit(cfg, name, op, kind, ident, dry=False, apply=True, out=print):
    """-> (rc, affected). Edits profiles.json, then applies every affected project."""
    profiles, meta = load_profiles(cfg)
    if not _need_profile(profiles, name, out):
        return 2, []
    if op not in ("add", "remove", "deny", "undeny") or kind not in ("plugin", "connector") or not ident:
        out("usage: profile <name> add|remove|deny|undeny plugin|connector <id>")
        return 2, []
    ident = norm_conn(ident) if kind == "connector" else ident
    key = ("deny_" if op in ("deny", "undeny") else "") + ("plugins" if kind == "plugin" else "connectors")
    new_profiles = {n: dict(p, **{k: list(p[k]) for k in ("plugins", "connectors", "deny_plugins", "deny_connectors")})
                    for n, p in profiles.items()}
    cur = new_profiles[name][key]
    verb = {"add": "added", "remove": "removed", "deny": "denied", "undeny": "un-denied"}[op]
    if op in ("add", "deny"):
        if ident in cur:
            out("%s already %s %s %s" % (name, "denies" if op == "deny" else "has", kind, ident))
            return 0, []
        cur.append(ident)
    else:
        if ident not in cur:
            out("%s does not %s %s %s" % (name, "deny" if op == "undeny" else "have", kind, ident))
            return 2, []
        new_profiles[name][key] = [x for x in cur if x != ident]
    projects = load_projects(cfg)["projects"]
    rels = _affected(projects, meta["default_profiles"], name)
    if dry:
        affected = _project_changes(cfg, rels, projects, new_profiles, meta["default_profiles"])
        out("would %s %s %s %s %s; affects %d project(s)" % (op, kind, ident, "to" if op in ("add", "deny") else "from", name, len(rels)))
        for a in affected:
            out("  %s: %s" % (a["path"], ", ".join(a["changes"]) or a.get("note") or "no change"))
        return 0, affected
    save_profiles(cfg, new_profiles, meta)
    log_change(cfg, "(profile %s)" % name, "%s:%s" % (kind, ident), "-", verb)
    out("%s: %s %s %s" % (name, verb, kind, ident))
    rc = 0
    if apply:
        ctx = {}
        for r in rels:
            if os.path.isdir(abs_of(cfg, r)):
                rc = do_apply(cfg, r, out=out, ctx=ctx) or rc
    return rc, []


def profile_new(cfg, name, description="", group="", from_project=None, out=print):
    profiles, meta = load_profiles(cfg)
    if not NAME_RE.match(name or ""):
        out("invalid profile name: %r" % name)
        return 2
    if name in profiles:
        out("profile %s already exists" % name)
        return 2
    p = {"group": group or "", "description": description or "", "plugins": [], "connectors": [],
         "deny_plugins": [], "deny_connectors": []}
    d = None
    if from_project:
        d = load_projects(cfg)
        e = d["projects"].get(from_project)
        if e is None:
            out("%s has no entry in projects.json" % from_project)
            return 2
        inl = e.get("inline", {})
        p["plugins"] = list(inl.get("plugins_on", []))
        p["connectors"] = list(inl.get("connectors_on", []))
        for k in ("plugins_on", "connectors_on"):
            inl.pop(k, None)
        if name not in e["profiles"]:
            e["profiles"].append(name)
    profiles[name] = p
    save_profiles(cfg, profiles, meta)
    log_change(cfg, "(profile %s)" % name, "profile", "-", "created")
    out("created profile %s" % name)
    if d is not None:
        save_projects(cfg, d)
        log_change(cfg, from_project, "profile:" + name, "-", "attached (moved from inline)")
        out("%s: moved %d plugin(s), %d connector(s) from inline into %s" % (from_project, len(p["plugins"]), len(p["connectors"]), name))
    return 0


def profile_delete(cfg, name, out=print):
    profiles, meta = load_profiles(cfg)
    if not _need_profile(profiles, name, out):
        return 2
    d = load_projects(cfg)
    where = [r for r, e in d["projects"].items() if name in e["profiles"]]
    if name in meta["default_profiles"]:
        where.insert(0, "(default_profiles)")
    if where:
        out("refusing to delete %s: attached to %s" % (name, ", ".join(where)))
        return 2
    del profiles[name]
    save_profiles(cfg, profiles, meta)
    for e in d["projects"].values():
        e["recommended"] = [r for r in e.get("recommended", []) if r != name]
    save_projects(cfg, d)
    log_change(cfg, "(profile %s)" % name, "profile", "exists", "deleted")
    out("deleted profile %s" % name)
    return 0


def do_inline(cfg, rel, kind, ident, value, out=print, apply=True):
    """Set (on/off) or clear an inline exception, then apply just that item."""
    profiles, meta = load_profiles(cfg)
    d = load_projects(cfg)
    e = d["projects"].get(rel)
    if e is None:
        out("%s has no entry in projects.json; attach a profile first" % rel)
        return 2
    if value not in ("on", "off", "clear"):
        out("value must be on, off or clear")
        return 2
    ident = norm_conn(ident) if kind == "connector" else ident
    if foreign_tool(ident):
        out("%s is reserved for another tool (%s:) and is not supported yet; "
            "inline exceptions only apply to Claude Code ids" % (ident, foreign_tool(ident)))
        return 2
    on_k, off_k = ("plugins_on", "plugins_off") if kind == "plugin" else ("connectors_on", "connectors_off")
    inl = e.setdefault("inline", {})
    for k in (on_k, off_k):
        if ident in inl.get(k, []):
            inl[k] = [x for x in inl[k] if x != ident]
    if value != "clear":
        base = resolve(profiles, meta["default_profiles"], e, None)  # item's own inline already removed
        if kind == "plugin":
            granted, denied = ident in base["sources"]["plugin"], ident in base["denied_by"]["plugin"]
        else:
            granted, denied = ident in base["sources"]["connector"], ident in base["denied_by"]["connector"]
        if value == "on" and denied:
            out("%s is denied by %s; deny wins over an inline exception" % (ident, ", ".join(base["denied_by"][kind][ident])))
            return 2
        on_now = granted and not denied
        if (value == "on") != on_now:
            inl.setdefault(on_k if value == "on" else off_k, []).append(ident)
        else:
            out("%s is already %s via its profiles; no exception needed" % (ident, value))
    for k in list(inl):
        if not inl[k]:
            del inl[k]
    save_projects(cfg, d)
    log_change(cfg, rel, "inline:%s:%s" % (kind, ident), "-", value)
    return do_apply(cfg, rel, only=(kind, ident), out=out) if apply else 0


# ------------------------------------------------------------- default profiles
def profile_default(cfg, op, name, dry=False, apply=True, out=print):
    """-> (rc, affected). Edit default_profiles; every project is affected."""
    profiles, meta = load_profiles(cfg)
    if not _need_profile(profiles, name, out):
        return 2, []
    cur = list(meta["default_profiles"])
    if op == "add":
        if name in cur:
            out("%s is already a default profile" % name)
            return 0, []
        cur.append(name)
    else:
        if name not in cur:
            out("%s is not a default profile" % name)
            return 2, []
        cur.remove(name)
    projects = load_projects(cfg)["projects"]
    rels = list(projects)
    if dry:
        affected = _project_changes(cfg, rels, projects, profiles, cur)
        out("would %s default profile %s; affects %d project(s)" % (op, name, len(rels)))
        for a in affected:
            out("  %s: %s" % (a["path"], ", ".join(a["changes"]) or a.get("note") or "no change"))
        return 0, affected
    meta["default_profiles"] = cur
    save_profiles(cfg, profiles, meta)
    log_change(cfg, "(default_profiles)", name, "-", "added" if op == "add" else "removed")
    out("%s: %s default profile" % (name, "added" if op == "add" else "removed"))
    rc = 0
    if apply:
        ctx = {}
        for r in rels:
            if os.path.isdir(abs_of(cfg, r)):
                rc = do_apply(cfg, r, out=out, ctx=ctx) or rc
    return rc, []



# --------------------------------------------------------------------- check
def check_path(path, hook=False):
    cfg = load_config()
    if cfg is None:
        hint = ("ntk: no config found. Create %s with "
                '{"root": "~/code", "data_dir": "~/code/_ntk"} to get started.' % config_path())
        if not os.path.exists(state_read(".hinted")):
            hinted = state_write(".hinted")
            os.makedirs(os.path.dirname(hinted), exist_ok=True)
            with open(hinted, "w") as hf:
                hf.write("shown\n")
            _emit(hint, hook)
        return
    projects = load_projects(cfg)["projects"]
    rel, key = find_project_key(cfg, projects, path)
    if rel is None:
        return
    profiles, meta = load_profiles(cfg)
    if key is None and rel != "." and not _visible(rel.split(os.sep)[0]):
        return  # hidden/underscore folders (_archive, .git, node_modules…) are never "new"
    if key is None:
        msg = ("ntk: this folder (%s) has no plugin profile, so every claude.ai connector is on "
               "and no project plugins are set. Offer the user /project-setup." % rel)
        _emit(msg + lifecycle._caps_hint(cfg, hook), hook, plain="ntk: %s is new (no profile)" % rel)
        return
    entry = projects[key]
    conn = (load_cache(cfg).get("connectors") or {}).get("names") or None  # no cache: skip connector drift
    des = resolve(profiles, meta["default_profiles"], entry, conn)
    ap = abs_of(cfg, key)
    st = A().read_project(ap)
    ep = st["plugins_enabled"]
    want = [p for p in des["plugins"] if not A().is_self(p)]
    plug_on = [p for p in want if not ep.get(p)]
    plug_off = [p for p, v in ep.items() if v and p not in want]
    act_off = set(st["connectors_off"])
    c_off = sorted(set(des["connectors_off"]) - act_off)
    c_on = sorted(act_off & set(des["connectors_on"]))
    bits = []
    if des["unknown"]:
        bits.append("unknown profile(s) %s" % ", ".join(des["unknown"]))
    if plug_on:
        bits.append("%d plugin(s) to enable" % len(plug_on))
    if plug_off:
        bits.append("%d plugin(s) to disable" % len(plug_off))
    if c_off:
        bits.append("%d connector(s) to turn off" % len(c_off))
    if c_on:
        bits.append("%d connector(s) to turn on" % len(c_on))
    label = "profiles %s" % (", ".join(des["own"]) or "none")
    if bits:
        _emit("ntk: %s (%s) differs from its profiles: %s. Run /ntk to review."
              % (key, label, ", ".join(bits)) + lifecycle._caps_hint(cfg, hook), hook)
    elif not hook:
        print("ntk: %s (%s): ok" % (key, label))
    else:
        h = lifecycle._caps_hint(cfg, hook)
        if h:
            _emit(h.strip(), hook)


def _emit(msg, hook, plain=None):
    if hook:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}))
    else:
        print(plain or msg)


# ------------------------------------------------------------------ snapshot
def render_table(state):
    cols = ("path", "profiles", "status", "plugins on", "connectors on", "inline")
    rows = []
    for p in state["projects"]:
        rows.append((p["path"], ",".join(p["attached"]) or "-", p["status"],
                     ", ".join(x["id"].split("@")[0] for x in p["plugins"] if x["enabled"]) or "-",
                     ", ".join(c["name"] for c in p["connectors"] if not c["actual_off"]) or "-",
                     str(len(p["inline_items"])) if p["inline_items"] else "-"))
    w = [min(max(len(str(r[i])) for r in rows + [cols]), 56) for i in range(len(cols))]
    fmt = "  ".join("%%-%ds" % x for x in w)
    lines = [fmt % cols]
    for r in rows:
        lines.append(fmt % tuple(str(c)[:56] for c in r))
    return "\n".join(lines)


def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def snapshot(cfg):
    st = scan(cfg)
    lines = ["# Console", "", "Generated %s. Org: %s (%s)." % (st["generated"], st["org"]["name"] or "unknown", st["org"]["id8"] or "-"),
             "", "| project | profiles | status | plugins on | connectors on | inline | notes |", "|---|---|---|---|---|---|---|"]
    for p in st["projects"]:
        lines.append("| %s |" % " | ".join(md_escape(x) for x in (
            p["path"], ", ".join(p["attached"]) or "-", p["status"],
            ", ".join(x["id"] for x in p["plugins"] if x["enabled"]) or "-",
            ", ".join(c["name"] for c in p["connectors"] if not c["actual_off"]) or "-",
            ", ".join("%s %s %s" % (i["kind"], i["id"], i["value"]) for i in p["inline_items"]) or "-",
            p["notes"] or "")))
    if st["missing"]:
        lines += ["", "## Missing on this account", ""]
        for m in st["missing"]:
            lines.append("- %s in %s: %s" % (m["id"], ", ".join(m["projects"]), m["hint"]))
    atomic_write(os.path.join(cfg.data_dir, "CONSOLE.md"), "\n".join(lines) + "\n", 0o644)
    page = load_page(token="", readonly=True, state=st)
    atomic_write(os.path.join(cfg.data_dir, "CONSOLE.html"), page, 0o644)
    return st



def load_page(token, readonly=False, state=None):
    with open(os.path.join(SCRIPTS_DIR, "console.html"), encoding="utf-8") as f:
        html = f.read()
    boot = {"token": token, "readonly": readonly, "state": state}
    blob = json.dumps(boot).replace("</", "<\\/")
    return html.replace("/*__BOOT__*/null", blob)

