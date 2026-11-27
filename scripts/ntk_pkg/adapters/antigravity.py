"""Antigravity adapter (Google's agent IDE and its `agy` CLI).

What it manages
- MCP servers, per folder: a deny rule `mcp(<server>/*)` in the permission grants
  of the Antigravity project(s) that contain the folder
  (~/.gemini/config/projects/<uuid>.json, key permissionGrants.permissionGrants.deny).
  A server that comes from a plugin is named `<plugin>_<server>` in the rule (the
  bare server name is not enforced). Servers from the global
  ~/.gemini/config/mcp_config.json use the key as-is (UNVERIFIED: how Antigravity
  names those in a rule has not been confirmed on a real install).
- Plugins: user-level plugins (~/.gemini/config/plugins/<name>/) have one switch for
  the whole account (`agy plugin enable|disable`), so they are GLOBAL in ntk terms.
  Workspace plugins (<folder>/.agents/plugins/) are out of scope for now.

Ids: `antigravity:plugin:<name>` and `antigravity:mcp:<internal server name>`.

Safety
- mcp_config.json files can hold tokens (env, headers). They are parsed only to take
  the server-name keys; no value is ever kept, logged, cached or printed.
- A project file is edited only at permissionGrants.permissionGrants.deny; every other
  key keeps its value and position. Each file is backed up once per run, written
  atomically and verified. ntk only removes a rule it added itself (recorded in
  data_dir/antigravity-owned.json); a rule the user wrote is never claimed or removed.

Never touches the real ~/.gemini in tests: NTK_GEMINI_HOME overrides the location.
Python standard library only."""
import datetime
import glob
import json
import os
import re

import subprocess
from urllib.parse import unquote

from .. import config
from ..config import atomic_write, env, log_change, read_json, write_json_2
from ..model import abs_of, foreign_tool
from .base import Adapter
from .claude_code import detect_indent

NAME = "antigravity"
RULE_RE = re.compile(r"^mcp\((.+)/\*\)$")
STAR = "mcp(*)"
KEEP_BACKUPS = 20


# ------------------------------------------------------------------ locations
def gemini_home():
    return env("GEMINI_HOME") or os.path.expanduser("~/.gemini")


def config_dir():
    return os.path.join(gemini_home(), "config")


def projects_dir():
    return os.path.join(config_dir(), "projects")


def plugins_dir():
    return os.path.join(config_dir(), "plugins")


def agy_cmd():
    p = os.path.join(gemini_home(), "bin", "agy")
    return p if os.path.isfile(p) else "agy"


def run(cmd, cwd, timeout=60):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def mcp_id(name):
    return "%s:mcp:%s" % (NAME, name)


def plugin_id(name):
    return "%s:plugin:%s" % (NAME, name)


def rule_for(internal):
    return "mcp(%s/*)" % internal


def _internal(cid):
    """`antigravity:mcp:<name>` -> <name> (the server name used in a deny rule)."""
    return cid.split(":", 2)[2]


def _kind(cid):
    p = cid.split(":", 2)
    return p[1] if len(p) == 3 and p[0] == NAME else None


def _rule_server(rule):
    m = RULE_RE.match(rule) if isinstance(rule, str) else None
    return m.group(1) if m else None


# ---------------------------------------------------------------- mcp configs
def server_names(path):
    """Server-name keys of an mcp_config.json. Empty, missing or invalid -> [].
    Values (env, headers, tokens) are dropped immediately."""
    try:
        d = read_json(path, None)
        s = d.get("mcpServers") if isinstance(d, dict) else None
        return sorted(k for k in s if isinstance(k, str) and k) if isinstance(s, dict) else []
    except Exception:
        return []


def plugin_names():
    try:
        return sorted(n for n in os.listdir(plugins_dir()) if os.path.isdir(os.path.join(plugins_dir(), n)))
    except OSError:
        return []


def known_servers():
    """Internal server names: plugins' servers as `<plugin>_<server>`, then the
    global ones as-is."""
    out = []
    for p in plugin_names():
        out += ["%s_%s" % (p, s) for s in server_names(os.path.join(plugins_dir(), p, "mcp_config.json"))]
    out += server_names(os.path.join(config_dir(), "mcp_config.json"))
    return sorted(set(out))


# ------------------------------------------------------------------- projects
def _folder(uri):
    if not isinstance(uri, str) or not uri:
        return None
    p = unquote(uri)
    if p.startswith("file://"):
        p = p[len("file://"):]
    return os.path.realpath(p) if p.startswith("/") else None


def _folders(data):
    pr = data.get("projectResources")
    res = pr.get("resources") if isinstance(pr, dict) else None
    out = []
    for r in res if isinstance(res, list) else []:
        if not isinstance(r, dict):
            continue
        uri = r.get("folderUri")
        if uri is None and isinstance(r.get("gitFolder"), dict):
            uri = r["gitFolder"].get("folderUri")
        f = _folder(uri)
        if f and f not in out:
            out.append(f)
    return out


def _deny_ref(data):
    """-> (ok, deny list or None). ok False when a level has an unexpected type."""
    if "permissionGrants" not in data:
        return True, None
    pg = data["permissionGrants"]
    if not isinstance(pg, dict):
        return False, None
    if "permissionGrants" not in pg:
        return True, None
    inner = pg["permissionGrants"]
    if not isinstance(inner, dict):
        return False, None
    if "deny" not in inner:
        return True, None
    return (isinstance(inner["deny"], list)), (inner["deny"] if isinstance(inner["deny"], list) else None)


def project_id(path, data):
    i = data.get("id") if isinstance(data, dict) else None
    return i if isinstance(i, str) and i else os.path.splitext(os.path.basename(path))[0]


def read_projects():
    out = []
    for f in sorted(glob.glob(os.path.join(projects_dir(), "*.json"))):
        d = read_json(f, None)
        if not isinstance(d, dict):
            out.append({"id": project_id(f, None), "name": os.path.basename(f), "file": f,
                        "folders": [], "deny": [], "ok": False})
            continue
        ok, deny = _deny_ref(d)
        name = d.get("name") if isinstance(d.get("name"), str) and d.get("name") else project_id(f, d)
        out.append({"id": project_id(f, d), "name": name, "file": f, "folders": _folders(d),
                    "deny": [x for x in (deny or []) if isinstance(x, str)], "ok": ok})
    return out


# ------------------------------------------------------------------ ownership
def owned_path(data_dir):
    return os.path.join(data_dir, "antigravity-owned.json")


def load_owned(data_dir):
    """{project id: {folder: [rules ntk added for that folder]}}"""
    d = read_json(owned_path(data_dir), {}) if data_dir else {}
    out = {}
    for pid, folders in (d.items() if isinstance(d, dict) else []):
        if not isinstance(folders, dict):
            continue
        for f, rules in folders.items():
            if isinstance(rules, list):
                out.setdefault(pid, {})[f] = [r for r in rules if isinstance(r, str)]
    return out


def save_owned(data_dir, owned):
    clean = {p: {f: sorted(set(r)) for f, r in fs.items() if r} for p, fs in owned.items()}
    clean = {p: fs for p, fs in clean.items() if fs}
    write_json_2(owned_path(data_dir), clean)


def _state_data_dir():
    try:
        c = config.load_config()
        return c.data_dir if c else None
    except Exception:
        return None


# ------------------------------------------------------------------- plugins
def fetch_plugin_list(path):
    """User-level plugins and whether they are enabled, or None on any failure."""
    try:
        r = run([agy_cmd(), "plugin", "list", "--json"], path, 60)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        if not isinstance(data, list):
            return None
        pdir = os.path.realpath(plugins_dir())
        out = []
        for e in data:
            if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"]:
                continue
            p = e.get("path")
            if isinstance(p, str) and p:
                if not (os.path.realpath(p) + os.sep).startswith(pdir + os.sep):
                    continue  # a workspace plugin: out of scope
            elif not os.path.isdir(os.path.join(pdir, e["name"])):
                continue
            out.append({"id": plugin_id(e["name"]), "enabled": bool(e.get("enabled")), "scope": "user"})
        return out
    except Exception:
        return None


def set_global(pid, on, out):
    cmd = [agy_cmd(), "plugin", "enable" if on else "disable", pid.split(":", 2)[2]]
    try:
        r = run(cmd, os.path.expanduser("~"), 60)
    except Exception as e:
        out("FAILED %s: %s" % (" ".join(cmd), str(e)[:200]))
        return 1
    if r.returncode != 0:
        out("FAILED %s: %s" % (" ".join(cmd), (r.stderr or r.stdout or "").strip()[:300]))
        return 1
    return 0


def plan_for(row, only=None):
    """Connector acts only; global plugins are handled by the core."""
    acts = []
    for c in row["connectors"]:
        if only and only != ("connector", c["name"]):
            continue
        if c.get("mixed"):
            continue
        if c["desired_off"] != c["actual_off"]:
            acts.append({"kind": "connector", "item": c["name"],
                         "old": "off" if c["actual_off"] else "on",
                         "new": "off" if c["desired_off"] else "on", "cmd": None})
    return acts


# --------------------------------------------------------------- reading state
def _denied_servers(deny, known):
    if STAR in deny:
        return set(known)
    return {s for s in (_rule_server(r) for r in deny) if s}


def _matching(state, path):
    ap = os.path.realpath(path)
    return ap, [p for p in state.get("projects", []) if ap in p["folders"]]


def _known_for(state):
    known = set(state.get("servers", []))
    for fs in (state.get("owned") or {}).values():
        for rules in fs.values():
            known |= {s for s in (_rule_server(r) for r in rules) if s}
    return known


def actual_off(state, path):
    if not state:
        return None
    _, ms = _matching(state, path)
    if not ms:
        return None
    known = _known_for(state)
    off = None
    for p in ms:
        d = _denied_servers(p["deny"], known) & known
        off = d if off is None else off & d
    return sorted(mcp_id(s) for s in off or ())


def mixed_servers(state, path):
    if not state:
        return []
    ap, ms = _matching(state, path)
    owned = state.get("owned") or {}
    out = []
    for cid in actual_off(state, path) or []:
        r = rule_for(_internal(cid))
        mine = any(r in owned.get(p["id"], {}).get(ap, []) for p in ms)
        theirs = any(r in rules for p in ms for f, rules in owned.get(p["id"], {}).items() if f != ap)
        if theirs and not mine:
            out.append(cid)
    return out


# --------------------------------------------------------------------- writing
def _backup(cfg, path, ctx):
    done = ctx.setdefault("antigravity_backed_up", set())
    if path in done:
        return
    bdir = os.path.join(cfg.data_dir, "backups", NAME)
    os.makedirs(bdir, exist_ok=True)
    base = os.path.basename(path)
    dst = os.path.join(bdir, "%s.%s" % (base, datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
    with open(path, "rb") as f, open(dst, "wb") as g:
        g.write(f.read())
    os.chmod(dst, 0o600)
    for o in sorted(glob.glob(os.path.join(glob.escape(bdir), glob.escape(base) + ".*")))[:-KEEP_BACKUPS]:
        try:
            os.unlink(o)
        except OSError:
            pass
    done.add(path)


def _edit_project(cfg, path, ap, want, owned, ctx, out):
    """Make this folder's rules in one project file match `want` (a set of rules).
    -> (ok, final deny list or None). Mutates `owned` (that project's {folder: rules})."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
    except Exception:
        out("skipped %s: unexpected shape" % path)
        return False, None
    ok, cur = (False, None) if not isinstance(data, dict) else _deny_ref(data)
    if not ok:
        out("skipped %s: unexpected shape" % path)
        return False, None
    deny = list(cur or [])
    mine = set(owned.get(ap, []))
    others = set()
    for f, rules in owned.items():
        if f != ap:
            others |= set(rules)
    star = STAR in deny
    new_owned = set()
    for r in sorted(want):
        if r in mine:
            new_owned.add(r)
            if r not in deny and not star:
                deny.append(r)
        elif r in deny:
            if r in others:
                new_owned.add(r)       # ntk's own rule, shared with another folder
        elif not star:
            deny.append(r)
            new_owned.add(r)
    for r in mine - set(want):
        if r not in others:
            deny = [x for x in deny if x != r]
    if new_owned:
        owned[ap] = sorted(new_owned)
    else:
        owned.pop(ap, None)
    if deny == (cur or []) and cur is not None:
        return True, deny
    if deny == [] and cur is None:
        return True, deny
    _backup(cfg, path, ctx)
    if "permissionGrants" not in data:
        # the shape every real project file and the verified experiment use
        data["permissionGrants"] = {"permissionGrants": {}, "v2Migrated": True}
    pg = data["permissionGrants"]
    inner = pg.setdefault("permissionGrants", {})
    inner["deny"] = deny
    body = json.dumps(data, indent=detect_indent(text), ensure_ascii=False)
    if text.endswith("\n"):
        body += "\n"
    atomic_write(path, body)
    chk = read_json(path, {})
    try:
        got = chk["permissionGrants"]["permissionGrants"]["deny"]
    except Exception:
        got = None
    if got != deny:
        raise RuntimeError("verification of %s failed after write" % path)
    return True, deny


def execute_acts(cfg, rel, row, acts, only, ctx, out):
    acts = [a for a in acts if a["kind"] == "connector"]
    if not acts:
        return 0
    ap = os.path.realpath(abs_of(cfg, rel))
    projects = [p for p in read_projects() if ap in p["folders"]]
    if not projects:
        out("%s: no Antigravity project has this folder; nothing written" % rel)
        return 1
    owned = load_owned(cfg.data_dir)
    desired = {c["name"]: c["desired_off"] for c in row["connectors"]}
    want_all = {rule_for(_internal(n)) for n, off in desired.items() if off}
    rc, finals, changed_owned = 0, {}, False
    for p in projects:
        po = owned.setdefault(p["id"], {})
        before = {f: list(r) for f, r in po.items()}
        if only:
            r = rule_for(_internal(only[1]))
            want = set(po.get(ap, []))
            want = (want | {r}) if desired.get(only[1]) else (want - {r})
        else:
            want = want_all
        try:
            ok, deny = _edit_project(cfg, p["file"], ap, want, po, ctx, out)
        except Exception as e:
            out("FAILED %s: %s" % (p["file"], str(e)[:200]))
            ok, deny = False, None
        if not ok:
            rc = 1
            po.clear()
            po.update(before)  # ownership unchanged for a file we did not write
        else:
            finals[p["id"]] = deny
        changed_owned = changed_owned or po != before
    if changed_owned:
        save_owned(cfg.data_dir, owned)
    if rc:
        return rc
    for a in acts:
        r = rule_for(_internal(a["item"]))
        present = [r in d or STAR in d for d in finals.values()]
        done = all(present) if a["new"] == "off" else not any(r in d for d in finals.values())
        if done:
            log_change(cfg, rel, "connector:" + a["item"], a["old"], a["new"])
            out("%s: connector %s %s -> %s" % (rel, a["item"], a["old"], a["new"]))
        else:
            out("%s: connector %s left %s: the deny rule is not one ntk added, so it was not touched"
                % (rel, a["item"], a["old"]))
    return 0


# --------------------------------------------------------------------- adapter
class AntigravityAdapter(Adapter):
    """Thin wrappers over the module functions so tests can patch `run` and
    `fetch_plugin_list`."""
    name = NAME
    label = "Antigravity"
    kinds = ("plugin", "mcp")
    scopes = ("project", "global")
    can_install = False
    enforce = True
    labels = {"plugin": "plugin", "plugins": "plugins",
              "connector": "MCP server", "connectors": "MCP servers",
              "account": "Google account", "accounts": "Google accounts",
              "account_short": "account", "accounts_short": "accounts",
              "product": "Antigravity", "brand": "Antigravity", "cli": "agy",
              "state_file": "~/.gemini/config/projects"}

    def available(self):
        return os.path.isdir(config_dir())

    def unreadable_hint(self):
        return "(is Antigravity's `agy` CLI installed?)"

    def owns(self, cid):
        return foreign_tool(cid) == NAME

    def list_available(self, root):
        return sorted(mcp_id(s) for s in known_servers()) if self.available() else []

    def load_state(self):
        dd = _state_data_dir()
        return {"projects": read_projects(), "servers": known_servers(),
                "owned": load_owned(dd) if dd else {}}

    def connectors_off(self, state, path):
        return actual_off(state, path)

    def connectors_mixed(self, state, path):
        return mixed_servers(state, path)

    def uncontrolled_hint(self):
        return "Antigravity: no Antigravity project has this folder, so its MCP servers aren't managed here"

    def stale_entries(self, state, root):
        r = os.path.realpath(root)
        out = []
        for p in (state or {}).get("projects", []):
            fs = p["folders"]
            if fs and all((f == r or f.startswith(r + os.sep)) and not os.path.exists(f) for f in fs):
                out += ["%s (Antigravity project %s)" % (f, p["name"]) for f in fs]
        return out

    def list_plugins(self, path):
        return fetch_plugin_list(path)

    def is_global(self, pid):
        return self.owns(pid) and _kind(pid) == "plugin"

    def set_global(self, pid, on, out):
        return set_global(pid, on, out)

    def account_wide(self):
        return []

    def installable(self, pid):
        return False

    def missing_hint(self, pid, account):
        return "not installed in Antigravity (agy plugin install %s)" % (pid.split(":", 2)[2] if pid.count(":") >= 2 else pid)

    def describe(self, cid, account):
        if self.owns(cid) and _kind(cid) == "plugin":
            d = read_json(os.path.join(plugins_dir(), cid.split(":", 2)[2], "plugin.json"), {})
            desc = d.get("description") if isinstance(d, dict) else None
            return desc if isinstance(desc, str) else ""
        return ""

    def discover_capabilities(self, lists, account, connectors):
        seen = {}
        for pl in lists or []:
            for p in pl or []:
                if self.owns(p["id"]):
                    seen.setdefault(p["id"], {"kind": "plugin", "orgs": set()})
        for c in connectors or []:
            if self.owns(c):
                seen.setdefault(c, {"kind": "connector", "orgs": set()})
        return seen

    def plan(self, row, only=None):
        return plan_for(row, only)

    def apply(self, cfg, rel, row, acts, only, ctx, out):
        return execute_acts(cfg, rel, row, acts, only, ctx, out)
