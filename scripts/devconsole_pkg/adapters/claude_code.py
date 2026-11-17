"""Claude Code adapter: every `claude` CLI call, every read or write of Claude's own
files (~/.claude.json, settings files, org-sync manifests), the install rules and
the missing-plugin hints. Python standard library only."""
import datetime
import glob
import json
import os
import re
import subprocess

from ..config import atomic_write, log_change, read_json
from ..model import PREFIX, _uniq, abs_of, foreign_tool, norm_conn
from .base import Adapter


def claude_json_path():
    return os.environ.get("DEVCONSOLE_CLAUDE_JSON") or os.path.expanduser("~/.claude.json")



def claude_home():
    return os.environ.get("DEVCONSOLE_CLAUDE_HOME") or os.path.expanduser("~/.claude")


def user_settings_path():
    return os.environ.get("DEVCONSOLE_USER_SETTINGS") or os.path.join(claude_home(), "settings.json")



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



def orgs_info():
    cj = load_claude_json()
    acct = cj.get("oauthAccount") or {}
    cur = acct.get("organizationUuid") or ""
    orgs, descs = {}, {}
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
            if pj.get("description"):
                descs.setdefault(pj.get("name") or n, pj["description"])
    return {
        "descriptions": descs,
        "name": acct.get("organizationName") or "",
        "id8": cur[:8],
        "orgs": [{"id8": o[:8], "current": o == cur, "plugins": sorted(n)} for o, n in sorted(orgs.items())],
    }



def installable(pid):
    """Marketplace plugins (official, the Anthropic plugin directory, any added
    marketplace) install per project. Synced plugins come only from a claude.ai
    account, so they can't be installed from here."""
    return "@" in pid and not pid.endswith("@synced")


def missing_hint(pid, oi):
    nm = pid.split("@")[0]
    if installable(pid):
        return "not installed — Apply installs it"
    if pid.endswith("@synced"):
        have = [o["id8"] for o in (oi or {}).get("orgs", []) if not o["current"] and nm in o["plugins"]]
        if have:
            return ("add %s to this claude.ai account (claude.ai → Customize → plugins); "
                    "available on org %s" % (nm, ", ".join(have)))
        return "not synced to any claude.ai account here"
    return "not installed"



def plan_for(row, only=None):
    """Actions that make actual match desired for one evaluated row."""
    acts = []
    for p in row["plugins"]:
        if only and only != ("plugin", p["id"]):
            continue
        i = p["id"]
        if p["state"] == "missing":
            if installable(i):
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

def _enable_after_install(ap, pid, out):
    try:
        run(["claude", "plugin", "enable", pid, "--scope", "local"], ap, 60)
    except Exception:
        pass


def execute_acts(cfg, rel, row, acts, only, ctx, out):
    """Run planned actions for one project. Plugins go through the `claude` CLI;
    connectors are written to ~/.claude.json (backed up once per run). -> rc."""
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
    return rc


def stale_entries(cj, root):
    """~/.claude.json project entries under root whose folder no longer exists."""
    return [k for k in (cj.get("projects") or {})
            if (k == root or k.startswith(root + os.sep)) and not os.path.isdir(k)]


def collect_seen(lists, oi, connectors):
    """Every capability this machine knows about: plugins in any project's list,
    synced plugins in any org manifest, and the claude.ai connectors."""
    seen = {}

    def add(i, kind, org=None):
        e = seen.setdefault(i, {"kind": kind, "orgs": set()})
        if org:
            e["orgs"].add(org)
    cur = oi.get("id8")
    for pl in lists:
        for p in pl or []:
            if not _is_self(p["id"]):
                add(p["id"], "plugin", cur if p["id"].endswith("@synced") and cur else None)
    for o in oi.get("orgs", []):
        for n in o["plugins"]:
            add(n + "@synced", "plugin", o["id8"])
    for c in connectors or []:
        add(c, "connector", cur or None)
    return seen


def describe(cid, oi):
    """One-line description for synced plugins (from the org manifest), else ''."""
    if cid.endswith("@synced"):
        return (oi or {}).get("descriptions", {}).get(cid.split("@")[0], "")
    return ""


class ClaudeCodeAdapter(Adapter):
    """Adapter for Claude Code (plugins per project via `claude plugin ... --scope
    local`; claude.ai connectors via disabledMcpServers in ~/.claude.json). Thin
    wrappers over the module functions above so tests can patch `run` and friends."""
    name = "claude"
    label = "Claude Code"

    def owns(self, cid):
        return foreign_tool(cid) is None

    def is_self(self, pid):
        return _is_self(pid)

    def load_state(self):
        return load_claude_json()

    def connectors_off(self, state, path):
        return actual_connectors_off(state, path)

    def read_project(self, path):
        cj = load_claude_json()
        return {"connectors_off": actual_connectors_off(cj, path), "plugins_enabled": file_enabled_plugins(path)}

    def list_available(self, root):
        return fetch_connectors(root)

    def list_plugins(self, path):
        return fetch_plugin_list(path)

    def account_info(self):
        return orgs_info()

    def stale_entries(self, state, root):
        return stale_entries(state, root)

    def discover_capabilities(self, lists, account, connectors):
        return collect_seen(lists, account, connectors)

    def describe(self, cid, account):
        return describe(cid, account)

    def is_account_scoped(self, pid):
        return pid.endswith("@synced")

    def installable(self, pid):
        return installable(pid)

    def missing_hint(self, pid, account):
        return missing_hint(pid, account)

    def plan(self, row, only=None):
        return plan_for(row, only)

    def apply(self, cfg, rel, row, acts, only, ctx, out):
        return execute_acts(cfg, rel, row, acts, only, ctx, out)
