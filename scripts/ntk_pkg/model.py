"""Data model: profiles and projects files (with schema versions), the capability
id rule, and IAM-style resolution of what a project is allowed. Pure data: this
module never runs a program and never touches an agent tool's own files."""
import os
import re

from .config import read_json, write_json_2

SCHEMA_VERSION = 2
PREFIX = "claude.ai "  # how Claude Code names claude.ai connectors; stripped from ids
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "dist", "build",
             "vendor", "target", "site-packages"}



# ------------------------------------------------------------ schema contract
class SchemaTooNew(Exception):
    """A data file declares a schema_version newer than this ntk understands."""


WARNINGS = []  # read-time notices (de-duplicated); the CLI prints them to stderr


def file_version(d):
    """schema_version of a parsed data file. Missing (or not an integer) means 2,
    the shape every file written before the field existed already has."""
    v = d.get("schema_version") if isinstance(d, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else SCHEMA_VERSION


def too_new_message(path, d):
    v = file_version(d)
    if v <= SCHEMA_VERSION:
        return None
    return ("%s has schema_version %d, written by a newer ntk (this one supports up to %d); "
            "upgrade ntk" % (path, v, SCHEMA_VERSION))


def read_versioned(path, default):
    """read_json plus a recorded warning when the file is from a newer ntk.
    Reading stays best effort; writing is refused (see write_versioned)."""
    d = read_json(path, default)
    msg = too_new_message(path, d)
    if msg and msg not in WARNINGS:
        WARNINGS.append(msg)
    return d


def check_schema_file(path):
    """Raise SchemaTooNew if the file currently on disk is from a newer ntk."""
    msg = too_new_message(path, read_json(path, None))
    if msg:
        raise SchemaTooNew(msg)


def assert_writable(cfg):
    """Raise SchemaTooNew if any data file is from a newer ntk. Mutating
    commands call this first so they never act on data they cannot fully read."""
    for p in (cfg.profiles_path, cfg.projects_path, cfg.caps_path):
        check_schema_file(p)


def with_version(obj):
    """Copy of obj with schema_version set to the current version, placed right
    after `_about` (or first) so files stay readable. Other keys keep their order."""
    out = {}
    if "_about" in obj:
        out["_about"] = obj["_about"]
    out["schema_version"] = SCHEMA_VERSION
    for k, v in obj.items():
        if k not in ("_about", "schema_version"):
            out[k] = v
    return out


def write_versioned(path, obj):
    check_schema_file(path)
    write_json_2(path, with_version(obj))


# ------------------------------------------------------------ capability ids
FOREIGN_RE = re.compile(r"^[a-z0-9-]+:")


def foreign_tool(cid):
    """Id rule. A bare id (`name@marketplace` for plugins, a plain name for
    connectors) means Claude Code. An id starting with `<tool>:` (lowercase letters,
    digits and hyphens, then a colon, e.g. `codex:mcp:github`) belongs to another
    tool: -> that tool's name, else None. The Claude adapter ignores such ids."""
    m = FOREIGN_RE.match(str(cid))
    return m.group(0)[:-1] if m else None


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
    return {"_extra": {k: v for k, v in own.items() if k.startswith("_")},
            "group": own.get("group") if isinstance(own.get("group"), str) else "",
            "description": own.get("description") if isinstance(own.get("description"), str) else "",
            "plugins": _uniq(plugins), "connectors": _uniq(conns),
            "deny_plugins": _uniq(dp), "deny_connectors": _uniq(dc_)}


def _ser_profile(p):
    out = dict(p.get("_extra") or {})
    if p.get("group"):
        out["group"] = p["group"]
    out["description"] = p.get("description", "")
    out["plugins"] = list(p.get("plugins", []))
    out["connectors"] = list(p.get("connectors", []))
    for k in ("deny_plugins", "deny_connectors"):
        if p.get(k):
            out[k] = list(p[k])
    return out


# What to do with a plugin switched on for the whole account (not a built-in):
# ask = report it once and let the user choose; contain = apply turns it off
# account-wide and on only where profiles grant it; allow = leave it on everywhere.
AW_POLICIES = ("ask", "contain", "allow")


def load_profiles(cfg):
    """(profiles, meta). profiles: {name: v2 profile}. meta: default_profiles,
    plugin_accounts (keys starting with _ dropped) and the raw top-level dict."""
    d = read_versioned(cfg.profiles_path, {})
    d = d if isinstance(d, dict) else {}
    raw = d.get("profiles") if isinstance(d.get("profiles"), dict) else {}
    profiles = {n: _norm_profile(raw, n) for n, v in raw.items()
                if not n.startswith("_") and isinstance(v, dict)}
    acc = d.get("plugin_accounts") if isinstance(d.get("plugin_accounts"), dict) else {}
    meta = {"default_profiles": _uniq(_strs(d.get("default_profiles"))),
            "plugin_accounts": {k: v for k, v in acc.items() if not k.startswith("_")},
            "account_wide_policy": d.get("account_wide_policy") if d.get("account_wide_policy") in AW_POLICIES else "ask",
            "raw": d}
    return profiles, meta


def save_profiles(cfg, profiles, meta):
    out = dict(meta.get("raw") or {})
    out["default_profiles"] = list(meta["default_profiles"])
    if meta.get("account_wide_policy", "ask") != "ask" or "account_wide_policy" in out:
        out["account_wide_policy"] = meta.get("account_wide_policy", "ask")
    out["profiles"] = {n: _ser_profile(p) for n, p in profiles.items()}
    write_versioned(cfg.profiles_path, out)
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
    d = read_versioned(cfg.projects_path, None)
    if not isinstance(d, dict):
        d = {}
    raw = d.get("projects") if isinstance(d.get("projects"), dict) else {}
    d["projects"] = {k: norm_entry(v) for k, v in raw.items() if not k.startswith("_")}
    return d


def save_projects(cfg, d):
    out = {k: v for k, v in d.items() if k != "projects"}
    out["projects"] = {k: ser_entry(v) for k, v in d["projects"].items()}
    write_versioned(cfg.projects_path, out)



# ----------------------------------------------------------------- resolution
def _add(d, k, who):
    lst = d.setdefault(k, [])
    if who not in lst:
        lst.append(who)


def resolve(profiles, defaults, entry, avail=None, managed=None):
    """IAM evaluation. Allowed = union of default + attached profiles' grants plus
    inline on. Denied = union of their deny lists plus inline off. Deny wins.
    Connectors not allowed are off (implicit deny) among the `avail` names.
    `managed(id)` says whether an adapter enforces an id of another tool; ids it
    doesn't cover stay in `foreign`, shown but not enforced."""
    entry = entry or {}
    own = list(entry.get("profiles", []))
    inline = entry.get("inline", {}) or {}
    attached = _uniq(list(defaults) + own)
    unknown = [n for n in attached if n not in profiles]
    src = {"plugin": {}, "connector": {}}
    den = {"plugin": {}, "connector": {}}
    foreign = {}  # ids that belong to another tool: kept for display, never enforced

    def _put(d, k, who):
        if foreign_tool(k) and not (managed and managed(k)):
            e = foreign.setdefault(k, {"id": k, "tool": foreign_tool(k),
                                       "kind": "plugin" if d is src["plugin"] or d is den["plugin"] else "connector",
                                       "sources": []})
            if who not in e["sources"]:
                e["sources"].append(who)
        else:
            _add(d, k, who)

    for n in attached:
        p = profiles.get(n)
        if not p:
            continue
        for i in p["plugins"]:
            _put(src["plugin"], i, n)
        for c in p["connectors"]:
            _put(src["connector"], norm_conn(c), n)
        for i in p["deny_plugins"]:
            _put(den["plugin"], i, n)
        for c in p["deny_connectors"]:
            _put(den["connector"], norm_conn(c), n)
    items = []
    for i in inline.get("plugins_on", []):
        _put(src["plugin"], i, "inline")
        items.append({"kind": "plugin", "id": i, "value": "on"})
    for i in inline.get("plugins_off", []):
        _put(den["plugin"], i, "inline")
        items.append({"kind": "plugin", "id": i, "value": "off"})
    for c in inline.get("connectors_on", []):
        _put(src["connector"], norm_conn(c), "inline")
        items.append({"kind": "connector", "id": norm_conn(c), "value": "on"})
    for c in inline.get("connectors_off", []):
        _put(den["connector"], norm_conn(c), "inline")
        items.append({"kind": "connector", "id": norm_conn(c), "value": "off"})
    plugins = [i for i in src["plugin"] if i not in den["plugin"]]
    conn_on = [c for c in src["connector"] if c not in den["connector"]]
    avail = [norm_conn(a) for a in (avail or [])]
    conn_off = _uniq([a for a in avail if a not in conn_on] +
                     [c for c in den["connector"] if c not in conn_on])
    return {"attached": attached, "own": own, "defaults": list(defaults), "unknown": unknown,
            "plugins": plugins, "connectors_on": conn_on, "connectors_off": conn_off,
            "sources": src, "denied_by": den, "inline_items": items, "avail_known": bool(avail),
            "foreign": list(foreign.values())}


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

