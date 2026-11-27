"""The adapter interface.

An adapter is the only code that knows how one agent tool (Claude Code today)
stores its plugins and connectors. The core (model, engine, lifecycle, server,
cli) never runs a program and never reads or writes a tool's own files; it asks
an adapter. To add a tool, subclass Adapter, implement the methods below, give it
a unique `name`, and register it in adapters/__init__.py. See ARCHITECTURE.md.

Terms
- project path: absolute folder path.
- state: an opaque dict from load_state(); the core only hands it back to the adapter.
- row: one evaluated project (see engine.evaluate): desired vs actual per plugin and connector.
- act: a planned change, {"kind": "plugin"|"connector"|"report", "item", "old", "new", "cmd"}.
- ctx: a per-run dict the adapter may use to remember things (for example "backed_up").
"""


class Adapter(object):
    name = ""   # id prefix for this tool's own ids, e.g. "codex" for `codex:mcp:github`
    label = ""  # human name

    # --- what this adapter can do (the engine and page read these) ------------
    # kinds: of "plugin" (plugins, extensions), "mcp" (MCP servers, connectors),
    #   "skill" (skills, rules), "instructions" (instruction files, report only)
    # scopes: of "project" (per folder), "global" (one switch for every project),
    #   "account" (a user-level install of something scopable)
    kinds = ()
    scopes = ()
    can_install = False  # Apply can install a missing plugin
    # words the page and messages use for this tool's things; keys:
    #   plugin, plugins, connector, connectors, account, accounts;
    #   also account_short/accounts_short (the bare word, e.g. "org"), product
    #   (the tool's name in prose), brand (its short name), cli (its command),
    #   state_file (its per-user state file, named in "stale entry" reports)
    labels = {}

    def available(self):
        """True if this tool is installed here. Only adapters after the first are
        asked; the first is always used. File checks only, no programs run."""
        return True

    def declaration(self):
        """{name, label, kinds, scopes, can_install, labels}, for the page."""
        return {"name": self.name, "label": self.label, "kinds": list(self.kinds),
                "scopes": list(self.scopes), "can_install": bool(self.can_install),
                "labels": dict(self.labels)}

    def unreadable_hint(self):
        """Appended when this tool's plugin list can't be read, e.g. '(is X logged in?)'."""
        return ""

    # --- the id rule ----------------------------------------------------------
    def owns(self, cid):
        """True if this adapter manages the capability id. The first adapter owns
        bare ids; an adapter for another tool owns ids starting with `<name>:`."""
        raise NotImplementedError

    def is_self(self, pid):
        """True for this plugin's own id: always on, never drift."""
        return False

    # --- reading (what exists, what is set) -----------------------------------
    def list_available(self, root):
        """Connector names available to the account, or None if they cannot be read."""
        raise NotImplementedError

    def list_plugins(self, path):
        """[{"id", "enabled", "scope"}] for the project at `path`, or None on failure."""
        raise NotImplementedError

    def read_project(self, path):
        """File-only, fast, never shells out (the SessionStart hook relies on it):
        {"connectors_off": [...], "plugins_enabled": {id: bool}}."""
        raise NotImplementedError

    def load_state(self):
        """Opaque snapshot of the tool's global state, read once per scan."""
        return {}

    def connectors_off(self, state, path):
        """Connector names switched off for `path` according to `state`."""
        return []

    def account_info(self):
        """{"name", "id8", "orgs": [{"id8", "current", "plugins"}], "descriptions"}."""
        return {"descriptions": {}, "name": "", "id8": "", "orgs": []}

    def stale_entries(self, state, root):
        """Entries in the tool's state for folders under `root` that no longer exist."""
        return []

    def discover_capabilities(self, lists, account, connectors):
        """{id: {"kind": "plugin"|"connector", "orgs": set}} for the capability registry."""
        return {}

    def describe(self, cid, account):
        """Optional one-line description for the inbox, or ''."""
        return ""

    def is_account_scoped(self, pid):
        return False

    def is_global(self, pid):
        """True for a plugin the tool keeps one setting for, shared by every
        project (it cannot be switched per project). The core never plans
        per-project changes for these; it reports them once and, when every
        profile agrees, sets them once with set_global."""
        return False

    def account_wide(self):
        """Ordinary plugins switched on for every project at once (a user-level
        install), which profiles can't scope until the user decides. -> [id]."""
        return []

    def set_global(self, pid, on, out):
        """Switch a global plugin on or off for every project. -> rc."""
        raise NotImplementedError

    # --- planning and applying ------------------------------------------------
    def installable(self, pid):
        """True if a missing plugin can be installed from here."""
        return False

    def missing_hint(self, pid, account):
        """What the user should do about a wanted plugin that is absent."""
        return "not installed"

    def plan(self, row, only=None):
        """Acts that make actual match desired for one evaluated row (pure)."""
        raise NotImplementedError

    def apply(self, cfg, rel, row, acts, only, ctx, out):
        """Execute acts for one project, log each change, back up what it edits. -> rc (0 ok)."""
        raise NotImplementedError
