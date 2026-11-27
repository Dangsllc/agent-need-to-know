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

Defined in `adapters/base.py`; `adapters.get()` returns the adapter the engine drives. Methods:

| Method | Purpose |
|---|---|
| `owns(id)`, `is_self(id)` | the id rule; ntk's own plugin is never managed |
| `list_available(root)` | connector names available to the account, or `None` |
| `list_plugins(path)` | plugins and their enabled state for a project, or `None` |
| `read_project(path)` | file-only, fast: connectors off and enabled plugins (the hook uses only this) |
| `load_state()`, `connectors_off(state, path)` | one read of the tool's global state per scan |
| `account_info()`, `stale_entries()`, `describe()` | org names, folders the tool still remembers, descriptions |
| `discover_capabilities(...)` | everything installed or available, for the registry |
| `installable(id)`, `missing_hint(id, account)` | what Apply can install, what to tell the user otherwise |
| `is_global(id)`, `set_global(id, on, out)` | plugins the tool keeps one switch for, shared by every project (Claude Code's `@builtin` plugins); the core reports them once and sets them once, never per project |
| `plan(row, only)` | pure: acts that make actual match desired for one evaluated project |
| `apply(cfg, rel, row, acts, only, ctx, out)` | execute acts, log each change, back up what it edits |

A row (see `engine.evaluate`) holds desired and actual state per plugin and connector; an act is `{"kind", "item", "old", "new", "cmd"}`.

## Adding Codex CLI, Gemini CLI or another tool

1. Create `adapters/<tool>.py` with a subclass of `Adapter`. Set `name` to the id prefix (`codex`, `gemini`) so it owns ids such as `codex:mcp:github`.
2. Implement reading (`list_*`, `read_project`) from that tool's CLI or config files, and `plan`/`apply` to make its settings match the resolved allow list. Back up any file you edit and log through `config.log_change`.
3. Register it in `adapters/__init__.py`.
4. Make the engine route each capability id to the adapter that owns it. Today it drives `adapters.get()`, one adapter, and the Claude adapter ignores ids with a `<tool>:` prefix. Dropping the single-adapter assumption in `engine.py` is the main core change a second adapter needs; the data files already hold such ids.

Profiles stay tool-neutral: a profile may list `a@market` (Claude Code), `Docs` (a connector) and `codex:mcp:github` side by side.

## Data files and the schema contract

All live in `data_dir` (from `~/.claude/agent-need-to-know/config.json`, or the old `~/.claude/devconsole/config.json` when the new one is absent; `NTK_*` environment variables override paths, with deprecated `DEVCONSOLE_*` fallbacks).

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
