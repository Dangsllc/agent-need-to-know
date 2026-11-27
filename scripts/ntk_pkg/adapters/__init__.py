"""Adapter registry and routing.

`all()` lists the registered adapters that are available on this machine;
`owner(id)` names the one that manages a capability id (bare ids belong to the
first, Claude Code; `<tool>:...` ids to the adapter named `<tool>`). The engine
talks to `get()`, a `Fleet`: it has the Adapter interface and sends each call to
the right adapter.

What routes today:
- id calls (is_self, is_global, set_global, installable, missing_hint, describe,
  is_account_scoped) go to the id's owner;
- plan and apply split a row by owner, so each adapter only sees its own items
  and only runs its own acts;
- account_wide and list_plugins are joined across adapters.

Still read from the primary adapter only (until a second adapter defines its
own state, P1): load_state, connectors_off, read_project, list_available,
account_info, stale_entries, discover_capabilities. Connectors are plain names,
which the id rule gives to the primary adapter.
"""
from .base import Adapter  # noqa: F401
from .claude_code import ClaudeCodeAdapter

_ADAPTERS = [ClaudeCodeAdapter()]


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
    """True when an available adapter manages `cid`: profiles enforce it."""
    return owner(cid) is not None


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


def _act_owner(a):
    return owner(a["item"])


class Fleet(Adapter):
    """Every available adapter behind the one Adapter interface."""
    name = "*"
    label = "all tools"

    # --- routed by id -----------------------------------------------------
    def owns(self, cid):
        return managed(cid)

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
    def list_available(self, root):
        return primary().list_available(root)

    def read_project(self, path):
        return primary().read_project(path)

    def load_state(self):
        return primary().load_state()

    def connectors_off(self, state, path):
        return primary().connectors_off(state, path)

    def account_info(self):
        return primary().account_info()

    def stale_entries(self, state, root):
        return primary().stale_entries(state, root)

    def discover_capabilities(self, lists, account, connectors):
        return primary().discover_capabilities(lists, account, connectors)


FLEET = Fleet()


def get():
    """What the engine drives: the Fleet, which routes to every available adapter."""
    return FLEET
