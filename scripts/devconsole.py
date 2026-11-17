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


def load_profiles(cfg):
    d = read_json(cfg.profiles_path, {}) or {}
    return d.get("profiles", {}) or {}, d


def load_projects(cfg):
    d = read_json(cfg.projects_path, None)
    if not isinstance(d, dict):
        d = {"projects": {}}
    d.setdefault("projects", {})
    return d


def log_change(cfg, rel, item, old, new):
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(cfg.data_dir, exist_ok=True)
    with open(cfg.log_path, "a", encoding="utf-8") as f:
        f.write("%s\t%s\t%s\t%s -> %s\n" % (ts, rel, item, old, new))


# ------------------------------------------------------------------- profiles
def _uniq(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def resolve_profile(profiles, name):
    """Resolve a profile through `extends`. Child connectors_on wins over any
    ancestor's connectors_off."""
    chain, seen, n = [], set(), name
    while n and n in profiles and n not in seen:
        seen.add(n)
        chain.append(profiles[n])
        n = profiles[n].get("extends")
    chain.reverse()  # ancestors first
    plugins, off, on, sug = [], [], [], []
    for p in chain:
        plugins += p.get("plugins", []) or []
        off += p.get("connectors_off", []) or []
        on += p.get("connectors_on", []) or []
        sug += p.get("suggested", []) or []
    on_set = set(on)
    plugins = _uniq(plugins)
    return {
        "known": bool(chain),
        "plugins": plugins,
        "connectors_off": [c for c in _uniq(off) if c not in on_set],
        "suggested": [s for s in _uniq(sug) if s not in plugins],
    }


def norm_conn(name):
    return name[len(PREFIX):] if name.startswith(PREFIX) else name


def desired_state(profiles, entry):
    """Resolved profile, then project extras."""
    entry = entry or {}
    r = resolve_profile(profiles, entry.get("profile"))
    plugins = list(r["plugins"])
    for p in entry.get("plugins_on", []) or []:
        if p not in plugins:
            plugins.append(p)
    off_x = set(entry.get("plugins_off", []) or [])
    plugins = [p for p in plugins if p not in off_x]
    c_off = list(r["connectors_off"])
    on_x = {norm_conn(c) for c in entry.get("connectors_on", []) or []}
    c_off = [c for c in c_off if c not in on_x]
    for c in entry.get("connectors_off", []) or []:
        c = norm_conn(c)
        if c not in c_off:
            c_off.append(c)
    sug = [s for s in r["suggested"] if s not in plugins]
    return {"known": r["known"], "plugins": plugins, "connectors_off": c_off, "suggested": sug}


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


def discover(cfg):
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
            if os.path.exists(os.path.join(sp, ".git")) or os.path.exists(os.path.join(sp, ".claude")):
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
def evaluate(cfg, rel, entry, profiles, cj, plugin_list, connectors_avail, costs):
    ap = abs_of(cfg, rel)
    gone = not os.path.isdir(ap)
    res = {"path": rel, "abs": ap, "profile": (entry or {}).get("profile"),
           "notes": (entry or {}).get("notes", ""), "extras": {}, "drift": [],
           "missing": [], "plugins": [], "connectors": [], "cost": None}
    if entry:
        res["extras"] = {k: entry[k] for k in ("plugins_on", "plugins_off", "connectors_on", "connectors_off") if entry.get(k)}
    des = desired_state(profiles, entry)
    res["desired"] = des
    actual_off = actual_connectors_off(cj, ap)
    enabled = {p["id"] for p in plugin_list or [] if p["enabled"]}
    present = {p["id"] for p in plugin_list or []}
    scanned = plugin_list is not None
    ids = _uniq(des["plugins"] + des["suggested"] + sorted(enabled))
    for i in ids:
        want = i in des["plugins"]
        en = i in enabled
        pr = i in present
        if want and scanned and not pr:
            st = "missing"
            res["missing"].append(i)
        elif want and scanned and not en:
            st = "needs-on"
            res["drift"].append("plugin %s is off, profile wants it on" % i)
        elif want:
            st = "on" if scanned else "on?"
        elif en:
            st = "needs-off"
            res["drift"].append("plugin %s is on, profile does not include it" % i)
        elif i in des["suggested"]:
            st = "suggested"
        else:
            st = "off"
        res["plugins"].append({"id": i, "desired": want, "enabled": en, "present": pr, "state": st})
    known = set(connectors_avail or []) | set(des["connectors_off"]) | set(actual_off)
    for c in sorted(known):
        d_off = c in des["connectors_off"]
        a_off = c in actual_off
        if d_off != a_off:
            res["drift"].append("connector %s is %s, profile wants it %s" % (c, "off" if a_off else "on", "off" if d_off else "on"))
        res["connectors"].append({"name": c, "desired_off": d_off, "actual_off": a_off,
                                  "available": c in (connectors_avail or [])})
    if costs is not None:
        res["cost"] = sum(costs.get(p["id"], 0) for p in res["plugins"] if p["desired"])
    if not entry:  # no profile, so nothing is desired and nothing can drift
        res["drift"], res["missing"] = [], []
        for p in res["plugins"]:
            p["state"] = "on" if p["enabled"] else p["state"]
        for c in res["connectors"]:
            c["desired_off"] = c["actual_off"]
    if gone:
        res["status"] = "gone"
    elif not entry:
        res["status"] = "new"
    elif res["missing"]:
        res["status"] = "missing"
    elif res["drift"]:
        res["status"] = "drift"
    elif not scanned:
        res["status"] = "error"
    else:
        res["status"] = "ok"
    return res


def scan(cfg, fresh=False):
    profiles, praw = load_profiles(cfg)
    projects = load_projects(cfg)["projects"]
    cj = load_claude_json()
    cache = load_cache(cfg)
    now = time.time()
    rels = _uniq(discover(cfg) + list(projects.keys()))
    live = [r for r in rels if os.path.isdir(abs_of(cfg, r))]

    def need(r):
        c = cache["projects"].get(abs_of(cfg, r))
        return fresh or not c or now - c.get("ts", 0) > CACHE_TTL

    conn_c = cache.get("connectors") or {}
    if fresh or not conn_c or now - conn_c.get("ts", 0) > CACHE_TTL:
        names = fetch_connectors(cfg.root)
        if names is not None:
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
        save_cache(cfg, cache)
    costs = read_json(os.path.join(cfg.data_dir, "plugin_costs.json"), None)
    if not isinstance(costs, dict):
        costs = None
    rows = []
    for r in rels:
        ap = abs_of(cfg, r)
        pl = (cache["projects"].get(ap) or {}).get("plugins") if r in live else None
        rows.append(evaluate(cfg, r, projects.get(r), profiles, cj, pl, connectors, costs))
    stale = []
    for k in (cj.get("projects") or {}):
        if (k == cfg.root or k.startswith(cfg.root + os.sep)) and not os.path.isdir(k):
            stale.append(k)
    oi = orgs_info()
    miss = {}
    for row in rows:
        for i in row["missing"]:
            miss.setdefault(i, []).append(row["path"])
    miss_list = []
    for i, ps in sorted(miss.items()):
        nm = i.split("@")[0]
        have = [o["id8"] for o in oi["orgs"] if not o["current"] and nm in o["plugins"]] if i.endswith("@synced") else []
        miss_list.append({"id": i, "projects": ps, "other_orgs": have})
    counts = {k: 0 for k in ("ok", "drift", "missing", "new", "gone", "error")}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    known_plugins = sorted(set(sum([[p["id"] for p in r["plugins"]] for r in rows], [])) |
                           set((praw.get("plugin_accounts") or {}).keys()) - {"_about"})
    return {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": cfg.root, "org": {"name": oi["name"], "id8": oi["id8"]}, "orgs": oi["orgs"],
        "profiles": sorted(profiles.keys()), "profile_info": {k: v.get("description", "") for k, v in profiles.items()},
        "known_plugins": known_plugins, "connectors": connectors,
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


def do_apply(cfg, rel, dry=False, only=None, profile=None, out=print):
    projects = load_projects(cfg)
    if profile:
        profiles, _ = load_profiles(cfg)
        if profile not in profiles:
            out("unknown profile: %s" % profile)
            return 2
        e = projects["projects"].setdefault(rel, {})
        old = e.get("profile")
        if old != profile:
            if dry:
                out("would set profile %s -> %s" % (old, profile))
            else:
                e["profile"] = profile
                write_json_2(cfg.projects_path, projects)
                log_change(cfg, rel, "profile", old, profile)
        if dry:
            projects["projects"][rel] = dict(e, profile=profile)
    entry = projects["projects"].get(rel)
    if not entry:
        out("%s has no entry in projects.json; give it a profile first (--profile)" % rel)
        return 2
    ap = abs_of(cfg, rel)
    if not os.path.isdir(ap):
        out("folder is gone: %s" % ap)
        return 2
    profiles, _ = load_profiles(cfg)
    pl = fetch_plugin_list(ap)
    if pl is None:
        out("could not read the plugin list for %s (is `claude` on PATH and logged in?)" % rel)
        return 1
    conn = (load_cache(cfg).get("connectors") or {}).get("names") or []
    row = evaluate(cfg, rel, entry, profiles, load_claude_json(), pl, conn, None)
    acts = plan_for(row, only)
    if not acts:
        out("%s: nothing to change" % rel)
        return 0
    rc = 0
    conn_changes = [a for a in acts if a["kind"] == "connector"]
    for a in acts:
        if a["kind"] == "report":
            out("%s: MISSING %s (not available on this account); skipped" % (rel, a["item"]))
        elif dry:
            out("would %s %s: %s -> %s" % (a["kind"], a["item"], a["old"], a["new"]))
    if dry:
        return 0
    for a in acts:
        if a["kind"] == "plugin":
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
        backup_claude_json(cfg)
        desired_off = list(row["desired"]["connectors_off"])
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


def _enable_after_install(ap, pid, out):
    try:
        run(["claude", "plugin", "enable", pid, "--scope", "local"], ap, 60)
    except Exception:
        pass


def do_set(cfg, rel, kind, ident, value, out=print):
    on = value == "on"
    profiles, _ = load_profiles(cfg)
    projects = load_projects(cfg)
    e = projects["projects"].get(rel)
    if e is None:
        out("%s has no entry in projects.json; give it a profile first" % rel)
        return 2
    r = resolve_profile(profiles, e.get("profile"))
    if kind == "plugin":
        base_has = ident in r["plugins"]
        for k in ("plugins_on", "plugins_off"):
            if ident in e.get(k, []):
                e[k] = [x for x in e[k] if x != ident]
        if on != base_has:
            e.setdefault("plugins_on" if on else "plugins_off", []).append(ident)
    else:
        ident = norm_conn(ident)
        base_off = ident in r["connectors_off"]
        for k in ("connectors_on", "connectors_off"):
            if ident in [norm_conn(x) for x in e.get(k, [])]:
                e[k] = [x for x in e[k] if norm_conn(x) != ident]
        if (not on) != base_off:
            e.setdefault("connectors_off" if not on else "connectors_on", []).append(ident)
    for k in ("plugins_on", "plugins_off", "connectors_on", "connectors_off"):
        if k in e and not e[k]:
            del e[k]
    write_json_2(cfg.projects_path, projects)
    log_change(cfg, rel, "extra:%s:%s" % (kind, ident), "-", value)
    return do_apply(cfg, rel, only=(kind, ident), out=out)


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
    profiles, _ = load_profiles(cfg)
    if key is None:
        msg = ("devconsole: this folder (%s) has no plugin profile, so every claude.ai connector is on "
               "and no project plugins are set. Offer the user /project-setup." % rel)
        _emit(msg, hook, plain="devconsole: %s is new (no profile)" % rel)
        return
    entry = projects[key]
    des = desired_state(profiles, entry)
    ap = abs_of(cfg, key)
    cj = load_claude_json()
    ep = file_enabled_plugins(ap)
    plug_on = [p for p in des["plugins"] if not ep.get(p)]
    plug_off = [p for p, v in ep.items() if v and p not in des["plugins"]]
    want_off = {c for c in des["connectors_off"]}
    act_off = set(actual_connectors_off(cj, ap))
    c_off = sorted(want_off - act_off)
    c_on = sorted(act_off - want_off)
    bits = []
    if plug_on:
        bits.append("%d plugin(s) to enable" % len(plug_on))
    if plug_off:
        bits.append("%d plugin(s) to disable" % len(plug_off))
    if c_off:
        bits.append("%d connector(s) to turn off" % len(c_off))
    if c_on:
        bits.append("%d connector(s) to turn on" % len(c_on))
    if bits:
        _emit("devconsole: %s (profile %s) differs from its profile: %s. Run /devconsole to review."
              % (key, entry.get("profile"), ", ".join(bits)), hook)
    elif not hook:
        print("devconsole: %s (profile %s): ok" % (key, entry.get("profile")))


def _emit(msg, hook, plain=None):
    if hook:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}))
    else:
        print(plain or msg)


# ------------------------------------------------------------------ snapshot
def render_table(state):
    cols = ("path", "profile", "status", "plugins on", "connectors off")
    rows = []
    for p in state["projects"]:
        rows.append((p["path"], p["profile"] or "-", p["status"],
                     ", ".join(x["id"].split("@")[0] for x in p["plugins"] if x["enabled"]) or "-",
                     ", ".join(c["name"] for c in p["connectors"] if c["actual_off"]) or "-"))
    w = [max(len(str(r[i])) for r in rows + [cols]) for i in range(len(cols))]
    w = [min(x, 48) for x in w]
    fmt = "  ".join("%%-%ds" % x for x in w)
    lines = [fmt % cols]
    for r in rows:
        lines.append(fmt % tuple(str(c)[:48] for c in r))
    return "\n".join(lines)


def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def snapshot(cfg):
    st = scan(cfg)
    lines = ["# Console", "", "Generated %s. Org: %s (%s)." % (st["generated"], st["org"]["name"] or "unknown", st["org"]["id8"] or "-"),
             "", "| project | profile | status | plugins on | connectors off | notes |", "|---|---|---|---|---|---|"]
    for p in st["projects"]:
        lines.append("| %s |" % " | ".join(md_escape(x) for x in (
            p["path"], p["profile"] or "-", p["status"],
            ", ".join(x["id"] for x in p["plugins"] if x["enabled"]) or "-",
            ", ".join(c["name"] for c in p["connectors"] if c["actual_off"]) or "-",
            p["notes"] or "")))
    if st["missing"]:
        lines += ["", "## Missing on this account", ""]
        for m in st["missing"]:
            lines.append("- %s in %s%s" % (m["id"], ", ".join(m["projects"]),
                                          (" (available in org %s)" % ", ".join(m["other_orgs"])) if m["other_orgs"] else ""))
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
                msgs = []
                rel = body.get("path")
                if u.path not in ("/api/set", "/api/apply") or not isinstance(rel, str):
                    return self._send(400, '{"error":"bad request"}')
                if rel not in load_projects(cfg)["projects"] and not os.path.isdir(abs_of(cfg, rel)):
                    return self._send(400, '{"error":"unknown project"}')
                if ".." in rel.split("/"):
                    return self._send(400, '{"error":"bad path"}')
                with lock:
                    if u.path == "/api/set":
                        if body.get("kind") not in ("plugin", "connector") or body.get("value") not in ("on", "off") \
                                or not isinstance(body.get("id"), str):
                            return self._send(400, '{"error":"bad request"}')
                        rc = do_set(cfg, rel, body["kind"], body["id"], body["value"], out=msgs.append)
                    else:
                        prof = body.get("profile")
                        if prof is not None and not isinstance(prof, str):
                            return self._send(400, '{"error":"bad request"}')
                        rc = do_apply(cfg, rel, profile=prof, out=msgs.append)
                with state_lock:
                    st = scan(cfg)
                self._send(200, json.dumps({"ok": rc == 0, "messages": msgs, "state": st}))
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="devconsole")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--json", action="store_true")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true", help="list each drift item")
    c = sub.add_parser("check")
    c.add_argument("path")
    c.add_argument("--hook", action="store_true")
    a = sub.add_parser("apply")
    a.add_argument("relpath")
    a.add_argument("--profile")
    a.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("set")
    t.add_argument("relpath")
    t.add_argument("kind", choices=["plugin", "connector"])
    t.add_argument("ident")
    t.add_argument("value", choices=["on", "off"])
    sub.add_parser("snapshot")
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-open", action="store_true")
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
        for p in st["projects"]:
            for d in p["drift"] if args.verbose else []:
                print("  %s: %s" % (p["path"], d))
        for m in st["missing"]:
            print("missing: %s in %s%s" % (m["id"], ", ".join(m["projects"]),
                                           "  (org %s has it)" % ", ".join(m["other_orgs"]) if m["other_orgs"] else ""))
        for k in st["stale"]:
            print("stale ~/.claude.json entry (folder gone): %s" % k)
        return 0
    if args.cmd == "apply":
        return do_apply(cfg, args.relpath, dry=args.dry_run, profile=args.profile)
    if args.cmd == "set":
        return do_set(cfg, args.relpath, args.kind, args.ident, args.value)
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
