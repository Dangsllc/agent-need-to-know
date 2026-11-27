# Architecture

Agent Need-to-Know (`ntk`) decides what each project folder is allowed to load (plugins, connectors) and asks an **adapter** to make the agent tool match. The core knows nothing about any tool's files or CLI. Python standard library only.

## Layout

```
scripts/ntk.py              entry point (the hook, the launcher and the slash command call this path)
scripts/ntk                 optional POSIX sh launcher (opens the page, passes other commands through)
scripts/ntk_pkg/
  config.py      paths, NTK_* env overrides, config.json, atomic writes, change log
  model.py       profiles.json / projects.json read+write with schema_version,
                 the capability id rule, resolve() (allow-union, deny-wins evaluation), project discovery
  engine.py      evaluate desired vs actual, scan, plan/apply, profile and project edits,
                 the SessionStart check, snapshot
  lifecycle.py   capabilities.json registry, inbox, adopt / ignore / retire
  history.py     undo: records the data files before each change, restores them
  server.py      HTTP server for the page: 127.0.0.1, Host check, per-run token, API routes
  cli.py         argparse and subcommand dispatch
  adapters/
    base.py          Adapter interface (documented)
    claude_code.py   the only module that runs `claude` or touches Claude's files
scripts/console.html           the page (single file, no network resources)
scripts/check_leaks.py         pre-publish check, also run in CI
```

Dependencies point one way: `config` <- `model` <- `adapters` <- `lifecycle`, `engine` <- `server`, `cli`. `engine` and `lifecycle` call each other (scan updates the registry; adopt and retire edit profiles), so `lifecycle` imports `engine` lazily.

Rule: core modules never shell out and never read or write an agent tool's own files. Only an adapter does. Tests patch the adapter's `run`, `fetch_plugin_list` and `fetch_connectors`, so no test starts `claude`.

## The adapter interface

Defined in `adapters/base.py`. The registry in `adapters/__init__.py` holds every adapter: `all()` lists the available ones (the first, Claude Code, is always included), `owner(id)` names the one that manages an id, and `get()` returns the `Fleet` the engine drives. The Fleet has the same interface and routes:

- calls about one id (`is_self`, `is_global`, `set_global`, `installable`, `missing_hint`, `describe`) go to that id's owner;
- `plan` and `apply` split each row by owner, so an adapter only sees and changes its own items;
- `account_wide` and `list_plugins` are joined across adapters;
- `load_state`, `connectors_off`, `read_project`, `list_available`, `account_info`, `stale_entries` and `discover_capabilities` still come from the first adapter only, until a second adapter defines its own state (P1).

Profiles enforce a `<tool>:...` id only when an available adapter owns it (`model.resolve(..., managed=)`); otherwise it stays in `foreign`, shown but not enforced. Each plugin and connector in a row carries `tool`, and `scan()` returns `tools`: each adapter's declaration.

Declarations (class attributes): `kinds` (`plugin`, `mcp`, `skill`, `instructions`), `scopes` (`project`, `global`, `account`), `can_install`, and `labels` (the words the page and messages use for this tool's plugins, connectors and accounts). Methods:

| Method | Purpose |
|---|---|
| `available()`, `declaration()` | whether the tool is installed here (file checks only); the declaration for the page |
| `owns(id)`, `is_self(id)` | the id rule; ntk's own plugin is never managed |
| `list_available(root)` | connector names available to the account, or `None` |
| `list_plugins(path)` | plugins and their enabled state for a project, or `None` |
| `read_project(path)` | file-only, fast: connectors off and enabled plugins (the hook uses only this) |
| `load_state()`, `connectors_off(state, path)` | one read of the tool's global state per scan |
| `account_info()`, `stale_entries()`, `describe()` | org names, folders the tool still remembers, descriptions |
| `discover_capabilities(...)` | everything installed or available, for the registry |
| `installable(id)`, `missing_hint(id, account)`, `unreadable_hint()` | what Apply can install, what to tell the user otherwise, what to suggest when the tool can't be read |
| `is_global(id)`, `set_global(id, on, out)` | plugins the tool keeps one switch for, shared by every project (Claude Code's `@builtin` plugins); the core reports them once and sets them once, never per project |
| `plan(row, only)` | pure: acts that make actual match desired for one evaluated project |
| `apply(cfg, rel, row, acts, only, ctx, out)` | execute acts, log each change, back up what it edits |

A row (see `engine.evaluate`) holds desired and actual state per plugin and connector; an act is `{"kind", "item", "old", "new", "cmd"}`.

## Adding Codex CLI, Gemini CLI or another tool

1. Create `adapters/<tool>.py` with a subclass of `Adapter`. Set `name` to the id prefix (`codex`, `gemini`) so it owns ids such as `codex:mcp:github`.
2. Implement reading (`list_*`, `read_project`) from that tool's CLI or config files, and `plan`/`apply` to make its settings match the resolved allow list. Back up any file you edit and log through `config.log_change`.
3. Set `kinds`, `scopes`, `can_install` and `labels`, and implement `available()`.
4. Register it in `adapters/__init__.py`. The Fleet then routes its ids to it (see above); `tests/test_routing.py` shows a fake second adapter end to end.
5. Give it its own state and connectors: the Fleet methods that still read the first adapter only are where P1 work goes.

Profiles stay tool-neutral: a profile may list `a@market` (Claude Code), `Docs` (a connector) and `codex:mcp:github` side by side.

## Data files and the schema contract

All live in `data_dir` (from `config.json` in `$XDG_CONFIG_HOME/ntk/` (default `~/.config/ntk/`; `%APPDATA%\ntk\` on Windows), else the first of `~/.claude/agent-need-to-know/` and the older `~/.claude/devconsole/` that has one, with state files written next to wherever config was found; `NTK_*` environment variables override paths, with deprecated `DEVCONSOLE_*` fallbacks).

| File | Holds |
|---|---|
| `profiles.json` | `default_profiles`, `plugin_accounts` (hints for the page), `profiles` (`plugins`, `connectors`, `deny_plugins`, `deny_connectors`, `group`, `description`) |
| `projects.json` | `projects`: path relative to `root` -> `profiles`, `recommended`, `inline`, `notes` |
| `capabilities.json` | the registry: id -> kind, status (`new`, `active`, `ignored`, `retired`, `gone`), first and last seen |
| `.ntk-cache.json`, `plugin_costs.json` | scan cache (an old `.devconsole-cache.json` is still read); optional token costs per plugin |
| `ntk.log` (an existing `devconsole.log` keeps being appended to), `backups/`, `CONSOLE.md`, `CONSOLE.html` | change log, copies of `~/.claude.json`, generated snapshots |

Contract:

- `schema_version` (integer) is in `profiles.json`, `projects.json` and `capabilities.json`. The current version is 2.
- Missing means 2. v1 shapes (a `profile` string per project, `extends`) are normalized on read.
- Writes always set `schema_version: 2` and keep `_about` and other `_` keys.
- Greater than 2: `model.assert_writable` makes every mutating command exit 3 without writing; read-only commands warn (stderr, and `warnings` in the state the page shows) and never rewrite the file.
- Id rule: a bare id (`name@marketplace`, or a plain connector name) is Claude Code; `<tool>:...` (`^[a-z0-9-]+:`) belongs to another tool. Implemented once, in `model.foreign_tool`.
- Changing a shape in a way an older ntk cannot read means bumping `SCHEMA_VERSION` in `model.py` and adding a migration on read.
