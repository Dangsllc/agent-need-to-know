#!/usr/bin/env python3
"""devconsole: profiles and switches for the Claude Code plugins and claude.ai
connectors each project folder uses. Python 3 standard library only."""
import argparse
import concurrent.futures
import datetime
import glob
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from urllib.parse import urlparse, parse_qs

PREFIX = "claude.ai "
CACHE_TTL = 300
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build",
             "vendor", "target", "site-packages"}
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- paths/config
def claude_json_path():
    return os.environ.get("DEVCONSOLE_CLAUDE_JSON") or os.path.expanduser("~/.claude.json")


def config_path():
    return os.environ.get("DEVCONSOLE_CONFIG") or os.path.expanduser("~/.claude/devconsole/config.json")


def claude_home():
    return os.environ.get("DEVCONSOLE_CLAUDE_HOME") or os.path.expanduser("~/.claude")


def user_settings_path():
    return os.environ.get("DEVCONSOLE_USER_SETTINGS") or os.path.join(claude_home(), "settings.json")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write(path, text, mode=None):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".dc-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is None:
            try:
                mode = os.stat(path).st_mode & 0o777
            except OSError:
                mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_2(path, obj):
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


class Config:
    def __init__(self, root, data_dir):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))

    @property
    def profiles_path(self):
        return os.path.join(self.data_dir, "profiles.json")

    @property
    def projects_path(self):
        return os.path.join(self.data_dir, "projects.json")

    @property
    def cache_path(self):
        return os.path.join(self.data_dir, ".devconsole-cache.json")

    @property
    def log_path(self):
        return os.path.join(self.data_dir, "devconsole.log")


def load_config():
    c = read_json(config_path())
    if not isinstance(c, dict) or "root" not in c:
        return None
    return Config(c["root"], c.get("data_dir") or c["root"])


INLINE_KEYS = ("plugins_on", "plugins_off", "connectors_on", "connectors_off")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _uniq(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _strs(v):
    return [x for x in (v if isinstance(v, list) else []) if isinstance(x, str)]


def norm_conn(name):
    name = str(name)
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


# ------------------------------------------------------------ profiles file
def _norm_profile(raw, name):
    """One profile in v2 shape. v1 `extends` is flattened (ancestors first), v1
    connectors_on counts as a grant, connectors_off / suggested are ignored (v2 is
    an allow-list with implicit deny)."""
    chain, seen, n = [], set(), name
    while isinstance(n, str) and isinstance(raw.get(n), dict) and n not in seen:
        seen.add(n)
        chain.append(raw[n])
        n = raw[n].get("extends")
    chain.reverse()
    own = raw[name]
    plugins, conns, dp, dc_ = [], [], [], []
    for p in chain:
        plugins += _strs(p.get("plugins"))
        conns += [norm_conn(c) for c in _strs(p.get("connectors")) + _strs(p.get("connectors_on"))]
        dp += _strs(p.get("deny_plugins"))
        dc_ += [norm_conn(c) for c in _strs(p.get("deny_connectors"))]
    return {"group": own.get("group") if isinstance(own.get("group"), str) else "",
            "description": own.get("description") if isinstance(own.get("description"), str) else "",
            "plugins": _uniq(plugins), "connectors": _uniq(conns),
            "deny_plugins": _uniq(dp), "deny_connectors": _uniq(dc_)}


def _ser_profile(p):
    out = {}
    if p.get("group"):
        out["group"] = p["group"]
    out["description"] = p.get("description", "")
    out["plugins"] = list(p.get("plugins", []))
    out["connectors"] = list(p.get("connectors", []))
    for k in ("deny_plugins", "deny_connectors"):
        if p.get(k):
            out[k] = list(p[k])
    return out


def load_profiles(cfg):
    """(profiles, meta). profiles: {name: v2 profile}. meta: default_profiles,
    plugin_accounts (keys starting with _ dropped) and the raw top-level dict."""
    d = read_json(cfg.profiles_path, {})
    d = d if isinstance(d, dict) else {}
    raw = d.get("profiles") if isinstance(d.get("profiles"), dict) else {}
    profiles = {n: _norm_profile(raw, n) for n, v in raw.items()
                if not n.startswith("_") and isinstance(v, dict)}
    acc = d.get("plugin_accounts") if isinstance(d.get("plugin_accounts"), dict) else {}
    meta = {"default_profiles": _uniq(_strs(d.get("default_profiles"))),
            "plugin_accounts": {k: v for k, v in acc.items() if not k.startswith("_")},
            "raw": d}
    return profiles, meta


def save_profiles(cfg, profiles, meta):
    out = dict(meta.get("raw") or {})
    out["default_profiles"] = list(meta["default_profiles"])
    out["profiles"] = {n: _ser_profile(p) for n, p in profiles.items()}
    write_json_2(cfg.profiles_path, out)
    meta["raw"] = out


# ------------------------------------------------------------ projects file
def norm_entry(e):
    """One project in v2 shape. v1: `profile` string -> profiles, top-level
    plugins_on/off, connectors_on/off -> inline."""
    e = e if isinstance(e, dict) else {}
    profs = _strs(e.get("profiles"))
    if not profs and isinstance(e.get("profile"), str) and e["profile"]:
        profs = [e["profile"]]
    src = e.get("inline") if isinstance(e.get("inline"), dict) else {}
    inline = {}
    for k in INLINE_KEYS:
        vals = _strs(src.get(k)) + (_strs(e.get(k)) if k not in src else [])
        if k.startswith("connectors"):
            vals = [norm_conn(v) for v in vals]
        vals = _uniq(vals)
        if vals:
            inline[k] = vals
    return {"profiles": _uniq(profs), "recommended": _uniq(_strs(e.get("recommended"))),
            "inline": inline, "notes": e.get("notes") if isinstance(e.get("notes"), str) else ""}


def ser_entry(e):
    out = {"profiles": list(e.get("profiles", []))}
    if e.get("recommended"):
        out["recommended"] = list(e["recommended"])
    inline = {k: list(e["inline"][k]) for k in INLINE_KEYS if e.get("inline", {}).get(k)}
    if inline:
        out["inline"] = inline
    if e.get("notes"):
        out["notes"] = e["notes"]
    return out


def load_projects(cfg):
    d = read_json(cfg.projects_path, None)
    if not isinstance(d, dict):
        d = {}
    raw = d.get("projects") if isinstance(d.get("projects"), dict) else {}
    d["projects"] = {k: norm_entry(v) for k, v in raw.items() if not k.startswith("_")}
    return d


def save_projects(cfg, d):
    out = {k: v for k, v in d.items() if k != "projects"}
    out["projects"] = {k: ser_entry(v) for k, v in d["projects"].items()}
    write_json_2(cfg.projects_path, out)


def log_change(cfg, rel, item, old, new):
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(cfg.data_dir, exist_ok=True)
    with open(cfg.log_path, "a", encoding="utf-8") as f:
        f.write("%s\t%s\t%s\t%s -> %s\n" % (ts, rel, item, old, new))


# ----------------------------------------------------------------- resolution
def _add(d, k, who):
    lst = d.setdefault(k, [])
    if who not in lst:
        lst.append(who)


def resolve(profiles, defaults, entry, avail=None):
    """IAM evaluation. Allowed = union of default + attached profiles' grants plus
    inline on. Denied = union of their deny lists plus inline off. Deny wins.
    Connectors not allowed are off (implicit deny) among the `avail` names."""
    entry = entry or {}
    own = list(entry.get("profiles", []))
    inline = entry.get("inline", {}) or {}
    attached = _uniq(list(defaults) + own)
    unknown = [n for n in attached if n not in profiles]
    src = {"plugin": {}, "connector": {}}
    den = {"plugin": {}, "connector": {}}
    for n in attached:
        p = profiles.get(n)
        if not p:
            continue
        for i in p["plugins"]:
            _add(src["plugin"], i, n)
        for c in p["connectors"]:
            _add(src["connector"], norm_conn(c), n)
        for i in p["deny_plugins"]:
            _add(den["plugin"], i, n)
        for c in p["deny_connectors"]:
            _add(den["connector"], norm_conn(c), n)
    items = []
    for i in inline.get("plugins_on", []):
        _add(src["plugin"], i, "inline")
        items.append({"kind": "plugin", "id": i, "value": "on"})
    for i in inline.get("plugins_off", []):
        _add(den["plugin"], i, "inline")
        items.append({"kind": "plugin", "id": i, "value": "off"})
    for c in inline.get("connectors_on", []):
        _add(src["connector"], norm_conn(c), "inline")
        items.append({"kind": "connector", "id": norm_conn(c), "value": "on"})
    for c in inline.get("connectors_off", []):
        _add(den["connector"], norm_conn(c), "inline")
        items.append({"kind": "connector", "id": norm_conn(c), "value": "off"})
    plugins = [i for i in src["plugin"] if i not in den["plugin"]]
    conn_on = [c for c in src["connector"] if c not in den["connector"]]
    avail = [norm_conn(a) for a in (avail or [])]
    conn_off = _uniq([a for a in avail if a not in conn_on] +
                     [c for c in den["connector"] if c not in conn_on])
    return {"attached": attached, "own": own, "defaults": list(defaults), "unknown": unknown,
            "plugins": plugins, "connectors_on": conn_on, "connectors_off": conn_off,
            "sources": src, "denied_by": den, "inline_items": items, "avail_known": bool(avail)}



# ------------------------------------------------------------- claude state
def load_claude_json():
    d = read_json(claude_json_path(), {})
    return d if isinstance(d, dict) else {}


def project_entry_key(cj, abspath):
    projs = cj.get("projects") or {}
    for k in _uniq([abspath, os.path.realpath(abspath)]):
        if k in projs:
            return k
    return abspath


def actual_connectors_off(cj, abspath):
    projs = cj.get("projects") or {}
    e = projs.get(project_entry_key(cj, abspath)) or {}
    return [norm_conn(x) for x in e.get("disabledMcpServers", []) or [] if x.startswith(PREFIX)]


def _is_self(pid):
    """devconsole manages the others; it is always on and never counts as drift."""
    return str(pid).split("@")[0] == "devconsole"


def file_enabled_plugins(abspath):
    """enabledPlugins merged: user settings, project settings, project local."""
    merged = {}
    for p in (user_settings_path(),
              os.path.join(abspath, ".claude", "settings.json"),
              os.path.join(abspath, ".claude", "settings.local.json")):
        d = read_json(p, {}) or {}
        ep = d.get("enabledPlugins") if isinstance(d, dict) else None
        if isinstance(ep, dict):
            for k, v in ep.items():
                if not _is_self(k):
                    merged[k] = bool(v)
    return merged


def run(cmd, cwd, timeout=60):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def fetch_plugin_list(abspath):
    try:
        r = run(["claude", "plugin", "list", "--json"], abspath, 60)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        out = {}
        for e in data if isinstance(data, list) else []:
            i = e.get("id")
            if not i:
                continue
            cur = out.setdefault(i, {"id": i, "enabled": False, "scope": e.get("scope")})
            cur["enabled"] = cur["enabled"] or bool(e.get("enabled"))
        return list(out.values())
    except Exception:
        return None


def fetch_connectors(root):
    try:
        r = run(["claude", "mcp", "list"], root, 120)
    except Exception:
        return None
    names = []
    for line in r.stdout.splitlines():
        m = re.match(r"^(claude\.ai .+?):\s+https?://", line)
        if m:
            names.append(norm_conn(m.group(1)))
    return names


def load_cache(cfg):
    c = read_json(cfg.cache_path, {})
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


def orgs_info():
    cj = load_claude_json()
    acct = cj.get("oauthAccount") or {}
    cur = acct.get("organizationUuid") or ""
    orgs = {}
    for mf in glob.glob(os.path.join(claude_home(), "plugins", "synced", "*_*", "manifest.json")):
        d = os.path.dirname(mf)
        org = os.path.basename(d).split("_", 1)[0]
        m = read_json(mf, {}) or {}
        names = orgs.setdefault(org, set())
        for p in m.get("plugins", []) or []:
            n = p.get("name")
            if not n:
                continue
            pj = read_json(os.path.join(d, n, ".claude-plugin", "plugin.json"), {}) or {}
            names.add(pj.get("name") or n)
    return {
        "name": acct.get("organizationName") or "",
        "id8": cur[:8],
        "orgs": [{"id8": o[:8], "current": o == cur, "plugins": sorted(n)} for o, n in sorted(orgs.items())],
    }


# -------------------------------------------------------------- discovery
def _visible(name):
    return not name.startswith(".") and not name.startswith("_") and name not in SKIP_DIRS


def discover(cfg, assigned=()):
    """Root, visible top-level dirs, and second-level dirs that look like their own
    project (.git or .claude). Under a top-level dir that already has a profile, only
    second-level dirs with their own .claude/ count; a nested repo inherits its parent."""
    assigned = set(assigned)
    out = ["."]
    try:
        tops = sorted(os.listdir(cfg.root))
    except OSError:
        return out
    for n in tops:
        p = os.path.join(cfg.root, n)
        if not _visible(n) or not os.path.isdir(p):
            continue
        out.append(n)
        try:
            subs = sorted(os.listdir(p))
        except OSError:
            continue
        for s in subs:
            sp = os.path.join(p, s)
            if not _visible(s) or os.path.islink(sp) or not os.path.isdir(sp):
                continue
            has_claude = os.path.exists(os.path.join(sp, ".claude"))
            has_git = os.path.exists(os.path.join(sp, ".git"))
            if has_claude or (has_git and n not in assigned):
                out.append(n + "/" + s)
    return out


def abs_of(cfg, rel):
    return cfg.root if rel in (".", "") else os.path.join(cfg.root, rel)


def find_project_key(cfg, projects, path):
    """Longest matching key for path (None if not under root / no entry)."""
    ap = os.path.abspath(os.path.expanduser(path))
    rp = os.path.realpath(ap)
    for cand in _uniq([ap, rp]):
        if cand == cfg.root:
            rel = "."
        elif cand.startswith(cfg.root + os.sep):
            rel = os.path.relpath(cand, cfg.root)
        else:
            continue
        best = None
        for k in projects:
            if k == ".":
                if rel == ".":
                    best = "."
                continue
            if rel == k or rel.startswith(k + "/"):
                if best is None or len(k) > len(best):
                    best = k
        return rel, best
    return None, None


# --------------------------------------------------------------- evaluate
def missing_hint(pid, oi):
    nm = pid.split("@")[0]
    if pid.endswith("@claude-plugins-official"):
        return "not installed — Apply installs it"
    if pid.endswith("@synced"):
        have = [o["id8"] for o in (oi or {}).get("orgs", []) if not o["current"] and nm in o["plugins"]]
        if have:
            return ("add %s to this claude.ai account (claude.ai → Customize → plugins); "
                    "available on org %s" % (nm, ", ".join(have)))
        return "not synced to any claude.ai account here"
    return "not installed"


def evaluate(cfg, rel, entry, profiles, defaults, cj, plugin_list, connectors_avail, costs):
    ap = abs_of(cfg, rel)
    gone = not os.path.isdir(ap)
    des = resolve(profiles, defaults, entry, connectors_avail)
    res = {"path": rel, "abs": ap, "notes": (entry or {}).get("notes", ""),
           "attached": des["own"], "defaults": des["defaults"], "unknown_profiles": des["unknown"],
           "recommended": [r for r in (entry or {}).get("recommended", [])
                           if r in profiles and r not in des["attached"]],
           "inline_items": des["inline_items"], "drift": [], "missing": [], "missing_hints": {},
           "plugins": [], "connectors": [], "cost": None, "desired": des}
    actual_off = actual_connectors_off(cj, ap)
    enabled = {p["id"] for p in plugin_list or [] if p["enabled"] and not _is_self(p["id"])}
    present = {p["id"] for p in plugin_list or []}
    scanned = plugin_list is not None
    want_plugins = [i for i in des["plugins"] if not _is_self(i)]
    sp, dp = des["sources"]["plugin"], des["denied_by"]["plugin"]
    inl = (entry or {}).get("inline", {}) or {}
    ids = _uniq(want_plugins + sorted(enabled) + [i for i in list(sp) + list(dp) if not _is_self(i)])
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
        got = fetch_connectors(cfg.root)
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
    cj = load_claude_json()
    cache = load_cache(cfg)
    now = time.time()
    rels = _uniq(discover(cfg, projects.keys()) + list(projects.keys()))
    live = [r for r in rels if os.path.isdir(abs_of(cfg, r))]

    def need(r):
        c = cache["projects"].get(abs_of(cfg, r))
        return fresh or not c or now - c.get("ts", 0) > CACHE_TTL

    conn_c = cache.get("connectors") or {}
    if fresh or not conn_c or now - conn_c.get("ts", 0) > CACHE_TTL:
        names = fetch_connectors(cfg.root)
        if names:
            cache["connectors"] = {"ts": now, "names": names}
    connectors = (cache.get("connectors") or {}).get("names", [])
    todo = [r for r in live if need(r)]
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(fetch_plugin_list, abs_of(cfg, r)): r for r in todo}
            for f, r in futs.items():
                pl = f.result()
                if pl is not None:
                    cache["projects"][abs_of(cfg, r)] = {"ts": now, "plugins": pl}
    if todo or cache.get("connectors") != conn_c:
        save_cache(cfg, cache)
    costs = read_json(os.path.join(cfg.data_dir, "plugin_costs.json"), None)
    if not isinstance(costs, dict):
        costs = None
    oi = orgs_info()
    rows = []
    for r in rels:
        ap = abs_of(cfg, r)
        pl = (cache["projects"].get(ap) or {}).get("plugins") if r in live else None
        row = evaluate(cfg, r, projects.get(r), profiles, defaults, cj, pl, connectors, costs)
        row["missing_hints"] = {i: missing_hint(i, oi) for i in row["missing"]}
        rows.append(row)
    stale = []
    for k in (cj.get("projects") or {}):
        if (k == cfg.root or k.startswith(cfg.root + os.sep)) and not os.path.isdir(k):
            stale.append(k)
    miss = {}
    for row in rows:
        for i in row["missing"]:
            miss.setdefault(i, []).append(row["path"])
    miss_list = [{"id": i, "projects": ps, "hint": missing_hint(i, oi)} for i, ps in sorted(miss.items())]
    counts = {k: 0 for k in ("ok", "drift", "missing", "new", "gone", "error")}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    prof_out = {}
    for n, p in profiles.items():
        prof_out[n] = dict(p, default=n in defaults,
                           projects=[r for r, e in projects.items() if n in e.get("profiles", [])])
    seen_plugins = set()
    for r in rows:
        seen_plugins |= {p["id"] for p in r["plugins"]}
    known_plugins = sorted(set(meta["plugin_accounts"]) | seen_plugins |
                           {i for p in profiles.values() for i in p["plugins"]})
    return {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": cfg.root, "org": {"name": oi["name"], "id8": oi["id8"]}, "orgs": oi["orgs"],
        "default_profiles": defaults, "profiles": prof_out,
        "known_plugins": known_plugins, "available_connectors": connectors,
        "counts": counts, "projects": rows, "stale": stale, "missing": miss_list,
        "has_costs": costs is not None,
    }



# ------------------------------------------------------------ plan / apply
def plan_for(row, only=None):
    """Actions that make actual match desired for one evaluated row."""
    acts = []
    for p in row["plugins"]:
        if only and only != ("plugin", p["id"]):
            continue
        i = p["id"]
        if p["state"] == "missing":
            if i.endswith("@claude-plugins-official"):
                acts.append({"kind": "plugin", "item": i, "old": "absent", "new": "installed+on",
                             "cmd": ["claude", "plugin", "install", i, "--scope", "local"]})
            else:
                acts.append({"kind": "report", "item": i, "old": "absent", "new": "missing (skipped)", "cmd": None})
        elif p["state"] == "needs-on":
            acts.append({"kind": "plugin", "item": i, "old": "off", "new": "on",
                         "cmd": ["claude", "plugin", "enable", i, "--scope", "local"]})
        elif p["state"] == "needs-off":
            acts.append({"kind": "plugin", "item": i, "old": "on", "new": "off",
                         "cmd": ["claude", "plugin", "disable", i, "--scope", "local"]})
    for c in row["connectors"]:
        if only and only != ("connector", c["name"]):
            continue
        if c["desired_off"] != c["actual_off"]:
            acts.append({"kind": "connector", "item": c["name"],
                         "old": "off" if c["actual_off"] else "on",
                         "new": "off" if c["desired_off"] else "on", "cmd": None})
    return acts


def backup_claude_json(cfg):
    src = claude_json_path()
    if not os.path.exists(src):
        return None
    bdir = os.path.join(cfg.data_dir, "backups")
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, "claude.json." + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(f.read())
    os.chmod(dst, 0o600)
    old = sorted(glob.glob(os.path.join(bdir, "claude.json.*")))
    for o in old[:-20]:
        try:
            os.unlink(o)
        except OSError:
            pass
    return dst


def detect_indent(text):
    m = re.match(r"\{\r?\n( +|\t)\S", text)
    if not m:
        return None if "\n" not in text.strip() else 2
    return "\t" if m.group(1) == "\t" else len(m.group(1))


def write_disabled_servers(abspath, desired_off_names, cfg=None):
    """Set projects[abs].disabledMcpServers to the desired claude.ai list,
    preserving non-claude.ai entries and every other key. Atomic + verified."""
    path = claude_json_path()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = json.loads(text)
    projs = data.setdefault("projects", {})
    key = project_entry_key(data, abspath)
    entry = projs.setdefault(key, {})
    cur = entry.get("disabledMcpServers", []) or []
    want = [PREFIX + norm_conn(n) for n in desired_off_names]
    keep = [x for x in cur if (not x.startswith(PREFIX)) or x in want]
    new = keep + [w for w in want if w not in keep]
    if new == cur and "disabledMcpServers" in entry:
        return False
    entry["disabledMcpServers"] = new
    out = json.dumps(data, indent=detect_indent(text), ensure_ascii=False)
    if text.endswith("\n"):
        out += "\n"
    atomic_write(path, out)
    check = read_json(path, {})
    got = (check.get("projects", {}).get(key, {}) or {}).get("disabledMcpServers")
    if got != new:
        raise RuntimeError("verification of %s failed after write" % path)
    return True

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
    pl = lists[rel] if lists is not None and rel in lists else fetch_plugin_list(ap)
    if pl is None:
        return None, [], ("could not read the plugin list for %s (is `claude` on PATH and logged in?)" % rel, 1)
    profiles, meta = load_profiles(cfg)
    row = evaluate(cfg, rel, entry, profiles, meta["default_profiles"], load_claude_json(), pl,
                   get_connectors(cfg, ctx), None)
    if row["unknown_profiles"]:
        return row, [], ("%s: unknown profile(s): %s" % (rel, ", ".join(row["unknown_profiles"])), 2)
    return row, plan_for(row, only), None


def execute_acts(cfg, rel, row, acts, only, ctx, out):
    ap = abs_of(cfg, rel)
    rc = 0
    conn_changes = [a for a in acts if a["kind"] == "connector"]
    for a in acts:
        if a["kind"] != "plugin":
            continue
        r = run(a["cmd"], ap, 120)
        if r.returncode != 0:
            out("FAILED %s: %s" % (" ".join(a["cmd"]), (r.stderr or r.stdout).strip()[:200]))
            rc = 1
            continue
        log_change(cfg, rel, "plugin:" + a["item"], a["old"], a["new"])
        out("%s: plugin %s %s -> %s" % (rel, a["item"], a["old"], a["new"]))
        if a["old"] == "absent":
            _enable_after_install(ap, a["item"], out)
    if conn_changes:
        if not ctx.get("backed_up"):
            backup_claude_json(cfg)
            ctx["backed_up"] = True
        desired_off = [c["name"] for c in row["connectors"] if c["desired_off"]]
        if only:  # only this connector: start from the actual list
            actual = actual_connectors_off(load_claude_json(), ap)
            name = only[1]
            desired_off = [x for x in actual if x != name] + ([name] if name in row["desired"]["connectors_off"] else [])
        write_disabled_servers(ap, desired_off)
        for a in conn_changes:
            log_change(cfg, rel, "connector:" + a["item"], a["old"], a["new"])
            out("%s: connector %s %s -> %s" % (rel, a["item"], a["old"], a["new"]))
    invalidate_cache(cfg, ap)
    return rc


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
    return execute_acts(cfg, rel, row, acts, only, ctx, out)


def _enable_after_install(ap, pid, out):
    try:
        run(["claude", "plugin", "enable", pid, "--scope", "local"], ap, 60)
    except Exception:
        pass


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
        futs = {r: ex.submit(fetch_plugin_list, abs_of(cfg, r)) for r in rels}
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
                rc = execute_acts(cfg, rel, row, acts, None, ctx,
                                  lambda m: out("  " + m) if m.startswith("FAILED") else None)
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
    cj, conn, res = load_claude_json(), get_connectors(cfg), []
    for r in rels:
        if r not in lists or lists[r] is None:
            res.append({"path": r, "changes": [], "note": "not scanned"})
            continue
        row = evaluate(cfg, r, projects[r], profiles, defaults, cj, lists[r], conn, None)
        res.append({"path": r, "changes": [describe_act(a) for a in plan_for(row)]})
    return res


def profile_edit(cfg, name, op, kind, ident, dry=False, apply=True, out=print):
    """-> (rc, affected). Edits profiles.json, then applies every affected project."""
    profiles, meta = load_profiles(cfg)
    if not _need_profile(profiles, name, out):
        return 2, []
    if op not in ("add", "remove") or kind not in ("plugin", "connector") or not ident:
        out("usage: profile <name> add|remove plugin|connector <id>")
        return 2, []
    ident = norm_conn(ident) if kind == "connector" else ident
    key = "plugins" if kind == "plugin" else "connectors"
    new_profiles = {n: dict(p, plugins=list(p["plugins"]), connectors=list(p["connectors"])) for n, p in profiles.items()}
    cur = new_profiles[name][key]
    if op == "add":
        if ident in cur:
            out("%s already has %s %s" % (name, kind, ident))
            return 0, []
        cur.append(ident)
    else:
        if ident not in cur:
            out("%s does not have %s %s" % (name, kind, ident))
            return 2, []
        new_profiles[name][key] = [x for x in cur if x != ident]
    projects = load_projects(cfg)["projects"]
    rels = _affected(projects, meta["default_profiles"], name)
    if dry:
        affected = _project_changes(cfg, rels, projects, new_profiles, meta["default_profiles"])
        out("would %s %s %s %s %s; affects %d project(s)" % (op, kind, ident, "to" if op == "add" else "from", name, len(rels)))
        for a in affected:
            out("  %s: %s" % (a["path"], ", ".join(a["changes"]) or a.get("note") or "no change"))
        return 0, affected
    save_profiles(cfg, new_profiles, meta)
    log_change(cfg, "(profile %s)" % name, "%s:%s" % (kind, ident), "-" if op == "add" else "granted", "granted" if op == "add" else "removed")
    out("%s: %s %s %s" % (name, "added" if op == "add" else "removed", kind, ident))
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


# --------------------------------------------------------------------- check
def check_path(path, hook=False):
    cfg = load_config()
    if cfg is None:
        hint = ("devconsole: no config found. Create %s with "
                '{"root": "~/code", "data_dir": "~/code/_devconsole"} to get started.' % config_path())
        hinted = os.path.join(os.path.dirname(config_path()), ".hinted")
        if not os.path.exists(hinted):
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
        msg = ("devconsole: this folder (%s) has no plugin profile, so every claude.ai connector is on "
               "and no project plugins are set. Offer the user /project-setup." % rel)
        _emit(msg, hook, plain="devconsole: %s is new (no profile)" % rel)
        return
    entry = projects[key]
    conn = (load_cache(cfg).get("connectors") or {}).get("names") or None  # no cache: skip connector drift
    des = resolve(profiles, meta["default_profiles"], entry, conn)
    ap = abs_of(cfg, key)
    cj = load_claude_json()
    ep = file_enabled_plugins(ap)
    want = [p for p in des["plugins"] if not _is_self(p)]
    plug_on = [p for p in want if not ep.get(p)]
    plug_off = [p for p, v in ep.items() if v and p not in want]
    act_off = set(actual_connectors_off(cj, ap))
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
        _emit("devconsole: %s (%s) differs from its profiles: %s. Run /devconsole to review."
              % (key, label, ", ".join(bits)), hook)
    elif not hook:
        print("devconsole: %s (%s): ok" % (key, label))


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
    with open(os.path.join(HERE, "console.html"), encoding="utf-8") as f:
        html = f.read()
    boot = {"token": token, "readonly": readonly, "state": state}
    blob = json.dumps(boot).replace("</", "<\\/")
    return html.replace("/*__BOOT__*/null", blob)


# --------------------------------------------------------------------- serve
def _bad(msg="bad request"):
    return 400, {"error": msg}


def handle_post(cfg, path, body, msgs):
    """Mutating API call (caller holds the lock). -> (status, payload). payload gets
    messages and state added by the caller unless it is an error."""
    S = lambda v: isinstance(v, str) and v
    projects = load_projects(cfg)["projects"]

    def proj():
        rel = body.get("path")
        if not S(rel) or rel.startswith("/") or ".." in rel.split("/"):
            return None
        if rel not in projects and not os.path.isdir(abs_of(cfg, rel)):
            return None
        return rel

    if path in ("/api/attach", "/api/detach"):
        rel = proj()
        if rel is None or not S(body.get("profile")):
            return _bad()
        return 200, {"rc": do_attach(cfg, rel, body["profile"], out=msgs.append, attach=path == "/api/attach")}
    if path == "/api/inline":
        rel = proj()
        if rel is None or body.get("kind") not in ("plugin", "connector") or not S(body.get("id")) \
                or body.get("value") not in ("on", "off", "clear"):
            return _bad()
        return 200, {"rc": do_inline(cfg, rel, body["kind"], body["id"], body["value"], out=msgs.append)}
    if path == "/api/apply":
        rel = proj()
        if rel is None:
            return _bad()
        return 200, {"rc": do_apply(cfg, rel, out=msgs.append)}
    if path == "/api/profile":
        if not S(body.get("name")) or body.get("op") not in ("add", "remove") \
                or body.get("kind") not in ("plugin", "connector") or not S(body.get("id")):
            return _bad()
        dry = bool(body.get("dry_run"))
        rc, affected = profile_edit(cfg, body["name"], body["op"], body["kind"], body["id"], dry=dry, out=msgs.append)
        out = {"rc": rc}
        if dry:
            out["affected"] = affected
        return 200, out
    if path == "/api/profile-new":
        if not S(body.get("name")) or not NAME_RE.match(body["name"]):
            return _bad("invalid profile name")
        fp = body.get("from_project")
        if fp is not None and (not S(fp) or fp not in projects):
            return _bad("unknown project")
        return 200, {"rc": profile_new(cfg, body["name"], str(body.get("description") or ""),
                                        str(body.get("group") or ""), fp, out=msgs.append)}
    return 404, {"error": "not found"}


def make_handler(cfg, token):
    lock = threading.Lock()
    state_lock = threading.Lock()

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "devconsole"

        def log_message(self, *a):
            pass

        def _host_ok(self):
            h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return h in ("127.0.0.1", "localhost")

        def _send(self, code, body, ctype="application/json"):
            b = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, '{"error":"bad host"}')
            u = urlparse(self.path)
            try:
                if u.path == "/":
                    return self._send(200, load_page(token), "text/html")
                if u.path == "/api/state":
                    fresh = parse_qs(u.query).get("fresh", ["0"])[0] == "1"
                    with state_lock:
                        return self._send(200, json.dumps(scan(cfg, fresh=fresh)))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, '{"error":"bad host"}')
            if not secrets.compare_digest(self.headers.get("X-Devconsole-Token") or "", token):
                return self._send(403, '{"error":"bad token"}')
            u = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    return self._send(400, '{"error":"bad request"}')
                msgs = []
                with lock:
                    code, payload = handle_post(cfg, u.path, body, msgs)
                if code != 200:
                    return self._send(code, json.dumps(payload))
                rc = payload.pop("rc")
                payload.update({"ok": rc == 0, "messages": msgs})
                if "affected" not in payload:  # a dry run writes nothing, so no new state
                    with state_lock:
                        payload["state"] = scan(cfg)
                self._send(200, json.dumps(payload))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))

    return H


def serve(cfg, port, open_browser):
    token = secrets.token_urlsafe(24)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, token))
    url = "http://127.0.0.1:%d/" % port
    print("devconsole serving on %s (Ctrl-C to stop)" % url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# ----------------------------------------------------------------------- cli
def need_config():
    cfg = load_config()
    if cfg is None:
        sys.exit("devconsole: no config at %s. Create it with {\"root\": \"~/code\", \"data_dir\": \"~/code/_devconsole\"}." % config_path())
    return cfg


def build_parser():
    ap = argparse.ArgumentParser(prog="devconsole")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="list drift items, inline exceptions, missing hints")
    c = sub.add_parser("check")
    c.add_argument("path")
    c.add_argument("--hook", action="store_true")
    a = sub.add_parser("apply", help="reconcile one project, or --all")
    a.add_argument("relpath", nargs="?")
    a.add_argument("--all", action="store_true")
    a.add_argument("--dry-run", action="store_true")
    for nm in ("attach", "detach"):
        t = sub.add_parser(nm)
        t.add_argument("relpath")
        t.add_argument("profile")
        t.add_argument("--no-apply", action="store_true")
    p = sub.add_parser("profile", help="profile <name> add|remove plugin|connector <id>")
    p.add_argument("name")
    p.add_argument("op", choices=["add", "remove"])
    p.add_argument("kind", choices=["plugin", "connector"])
    p.add_argument("ident")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-apply", action="store_true")
    pn = sub.add_parser("profile-new")
    pn.add_argument("name")
    pn.add_argument("--description", default="")
    pn.add_argument("--group", default="")
    pn.add_argument("--from-project")
    pd = sub.add_parser("profile-delete")
    pd.add_argument("name")
    i = sub.add_parser("inline", help="inline <path> plugin|connector <id> on|off|clear")
    i.add_argument("relpath")
    i.add_argument("kind", choices=["plugin", "connector"])
    i.add_argument("ident")
    i.add_argument("value", choices=["on", "off", "clear"])
    i.add_argument("--no-apply", action="store_true")
    sub.add_parser("snapshot")
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-open", action="store_true")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cmd == "check":
        try:
            check_path(args.path, args.hook)
        except Exception:
            pass
        return 0
    cfg = need_config()
    if args.cmd == "scan":
        st = scan(cfg, fresh=args.fresh)
        if args.json:
            print(json.dumps(st, indent=2))
            return 0
        print("Org: %s (%s)  root: %s" % (st["org"]["name"] or "unknown", st["org"]["id8"] or "-", st["root"]))
        print(render_table(st))
        print("\n" + "  ".join("%s=%d" % kv for kv in st["counts"].items() if kv[1]))
        if args.verbose:
            for p in st["projects"]:
                for d in p["drift"]:
                    print("  %s: drift: %s" % (p["path"], d))
                for it in p["inline_items"]:
                    print("  %s: inline %s %s %s" % (p["path"], it["kind"], it["id"], it["value"]))
                for i, h in p["missing_hints"].items():
                    print("  %s: missing %s: %s" % (p["path"], i, h))
                if p.get("error"):
                    print("  %s: error: %s" % (p["path"], p["error"]))
        for m in st["missing"]:
            print("missing: %s in %s: %s" % (m["id"], ", ".join(m["projects"]), m["hint"]))
        for k in st["stale"]:
            print("stale ~/.claude.json entry (folder gone): %s" % k)
        return 0
    if args.cmd == "apply":
        if args.all:
            return apply_all(cfg, dry=args.dry_run)
        if not args.relpath:
            ap.error("apply needs a path or --all")
        return do_apply(cfg, args.relpath, dry=args.dry_run)
    if args.cmd in ("attach", "detach"):
        return do_attach(cfg, args.relpath, args.profile, apply=not args.no_apply, attach=args.cmd == "attach")
    if args.cmd == "profile":
        return profile_edit(cfg, args.name, args.op, args.kind, args.ident, dry=args.dry_run, apply=not args.no_apply)[0]
    if args.cmd == "profile-new":
        return profile_new(cfg, args.name, args.description, args.group, args.from_project)
    if args.cmd == "profile-delete":
        return profile_delete(cfg, args.name)
    if args.cmd == "inline":
        return do_inline(cfg, args.relpath, args.kind, args.ident, args.value, apply=not args.no_apply)
    if args.cmd == "snapshot":
        snapshot(cfg)
        print("saved %s and %s" % (os.path.join(cfg.data_dir, "CONSOLE.md"), os.path.join(cfg.data_dir, "CONSOLE.html")))
        return 0
    if args.cmd == "serve":
        serve(cfg, args.port, not args.no_open)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except Exception as e:
        if len(sys.argv) > 1 and sys.argv[1] == "check":
            sys.exit(0)
        raise
