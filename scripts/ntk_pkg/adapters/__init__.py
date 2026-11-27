"""Adapter registry and routing.

`all()` lists the registered adapters that are available on this machine;
`owner(id)` names the one that manages a capability id (bare ids belong to the
first, Claude Code; `<tool>:...` ids to the adapter named `<tool>`). The engine
talks to `get()`, a `Fleet`: it has the Adapter interface and sends each call to
the right adapter.

What routes:
- id calls (is_self, is_global, set_global, installable, missing_hint, describe,
  is_account_scoped) go to the id's owner;
- plan and apply split a row by owner, so each adapter only sees its own items
  and only runs its own acts;
- load_state returns a FleetState: the primary adapter's state (so code that
  reads it directly keeps working) carrying every other adapter's state;
  connectors_off, connectors_mixed and stale_entries route each part to its
  adapter;
- account_wide, list_plugins, list_available and discover_capabilities are
  joined across adapters (each adapter keeps only ids it owns).

From the primary adapter only: read_project (the SessionStart hook is Claude
Code's, and file-only) and account_info.
"""
from .base import Adapter  # noqa: F401
from .antigravity import AntigravityAdapter
from .claude_code import ClaudeCodeAdapter

_ADAPTERS = [ClaudeCodeAdapter(), AntigravityAdapter()]


def registered():
    """Every registered adapter, in priority order (the first owns bare ids)."""
    return list(_ADAPTERS)


def all():  # noqa: A001 (the plan's name for it)
    """Registered adapters whose tool is available here. The primary one is
    always included, so a machine without its CLI still gets a scan."""
    return [a for i, a in enumerate(_ADAPTERS) if i == 0 or a.available()]


active = all  # old name


def primary():
    return _ADAPTERS[0]


def owner(cid):
    """The adapter that manages `cid`, or None when no available adapter does."""
    for a in all():
        if a.owns(cid):
            return a
    return None


def managed(cid):
    """True when profiles enforce `cid`: the primary adapter owns it, or another
    available adapter owns it and sets `enforce` (see Adapter.enforce)."""
    o = owner(cid)
    return o is not None and (o is primary() or bool(o.enforce))


def tools():
    """What each available adapter can do, for the page: [{name, label, kinds,
    scopes, can_install, labels}]."""
    return [a.declaration() for a in all()]


def _view(row, ad):
    """`row` narrowed to the items `ad` owns (the row itself when it owns all)."""
    pl = [p for p in row["plugins"] if ad.owns(p["id"])]
    cn = [c for c in row["connectors"] if ad.owns(c["name"])]
    if len(pl) == len(row["plugins"]) and len(cn) == len(row["connectors"]):
        return row
    v = dict(row)
    v["plugins"], v["connectors"] = pl, cn
    return v


class FleetState(dict):
    """The primary adapter's state, plus `others`: {adapter name: its state}."""
    def __init__(self, primary_state, others=None):
        dict.__init__(self, primary_state or {})
        self.others = others or {}


def _state_for(state, a):
    if a is primary():
        return state
    return getattr(state, "others", {}).get(a.name)


def tool_key():
    """Names of the adapters in use, stored with cached lists: a list fetched
    with a different set of adapters is stale."""
    return [a.name for a in all()]


def _act_owner(a):
    return owner(a["item"])


class Fleet(Adapter):
    """Every available adapter behind the one Adapter interface."""
    name = "*"
    label = "all tools"

    # --- routed by id -----------------------------------------------------
    def owns(self, cid):
        return owner(cid) is not None

    def is_self(self, pid):
        o = owner(pid)
        return bool(o and o.is_self(pid))

    def is_global(self, pid):
        o = owner(pid)
        return bool(o and o.is_global(pid))

    def is_account_scoped(self, pid):
        o = owner(pid)
        return bool(o and o.is_account_scoped(pid))

    def set_global(self, pid, on, out):
        o = owner(pid)
        if o is None:
            out("no adapter manages %s" % pid)
            return 1
        return o.set_global(pid, on, out)

    def installable(self, pid):
        o = owner(pid)
        return bool(o and o.installable(pid))

    def missing_hint(self, pid, account):
        o = owner(pid)
        return o.missing_hint(pid, account) if o else "no adapter for this tool"

    def describe(self, cid, account):
        o = owner(cid)
        return o.describe(cid, account) if o else ""

    def unreadable_hint(self, cid=None):
        o = owner(cid) if cid else primary()
        return o.unreadable_hint() if o else ""

    # --- joined across adapters -------------------------------------------
    def account_wide(self):
        out = []
        for a in all():
            out += [i for i in a.account_wide() if i not in out]
        return out

    def list_available(self, root):
        """Connector names: the primary adapter's, then each other adapter's ids.
        None when the primary list can't be read (a partial list would make its
        connectors look removed)."""
        ads = all()
        res = ads[0].list_available(root)
        if not res or len(ads) == 1:
            return res
        res = list(res)
        for a in ads[1:]:
            res += [n for n in (a.list_available(root) or []) if a.owns(n) and n not in res]
        return res

    def discover_capabilities(self, lists, account, connectors):
        seen = {}
        for a in all():
            for i, e in (a.discover_capabilities(lists, account, connectors) or {}).items():
                if owner(i) is a:
                    seen[i] = e
        return seen

    def load_state(self):
        ads = all()
        return FleetState(ads[0].load_state(), {a.name: a.load_state() for a in ads[1:]})

    def connectors_off_by(self, state, path):
        """{adapter name: connector names off for `path`, or None when that
        adapter can't control connectors there (e.g. no project for the folder)}."""
        return {a.name: a.connectors_off(_state_for(state, a), path) for a in all()}

    def connectors_off(self, state, path):
        out = []
        for v in self.connectors_off_by(state, path).values():
            out += [n for n in (v or []) if n not in out]
        return out

    def connectors_mixed(self, state, path):
        """Connectors forced off for `path` by another folder sharing the tool's
        project: reported as mixed, not as drift apply could fix."""
        out = set()
        for a in all():
            out |= set(a.connectors_mixed(_state_for(state, a), path) or ())
        return out

    def uncontrolled_hint(self, name):
        for a in all():
            if a.name == name:
                return a.uncontrolled_hint()
        return ""

    def stale_by(self, state, root):
        """{adapter name: its stale entries}, only adapters that have some."""
        out = {}
        for a in all():
            e = a.stale_entries(_state_for(state, a), root) or []
            if e:
                out[a.name] = list(e)
        return out

    def stale_entries(self, state, root):
        return [e for v in self.stale_by(state, root).values() for e in v]

    def list_plugins(self, path):
        """The primary adapter's list (None if it can't be read) plus every
        other available adapter's (skipped when unreadable)."""
        ads = all()
        res = ads[0].list_plugins(path)
        if res is None or len(ads) == 1:
            return res
        res = list(res)
        for a in ads[1:]:
            res += a.list_plugins(path) or []
        return res

    def plan(self, row, only=None):
        acts = []
        for a in all():
            v = _view(row, a)
            if v["plugins"] or v["connectors"]:
                acts += a.plan(v, only)
        return acts

    def apply(self, cfg, rel, row, acts, only, ctx, out):
        rc = 0
        for a in all():
            mine = [x for x in acts if _act_owner(x) is a]
            if mine:
                rc = a.apply(cfg, rel, _view(row, a), mine, only, ctx, out) or rc
        for x in acts:
            if _act_owner(x) is None:
                out("FAILED %s: no adapter manages it" % x["item"])
                rc = 1
        return rc

    # --- primary adapter only (see the module docstring) ------------------
    def read_project(self, path):
        return primary().read_project(path)

    def account_info(self):
        return primary().account_info()


FLEET = Fleet()


def get():
    """What the engine drives: the Fleet, which routes to every available adapter."""
    return FLEET
