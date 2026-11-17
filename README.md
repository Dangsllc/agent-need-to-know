# Agent Need-to-Know

Agent Need-to-Know (`ntk`) is privacy and least-privilege control for the plugins and connectors your AI agent sessions load. Today it manages Claude Code; adapters for other agent CLIs are planned (see [Roadmap](#roadmap)).

You describe small profiles ("lang-python", "secure-code"), attach a few to each project folder, and ntk makes Claude's real settings match. Connectors are **denied unless a profile grants them**, so a session in a folder only sees the tools that folder needs.

**Why**

- **Minimum necessary.** Regulated work (for example HIPAA's minimum-necessary standard) is easier to defend when a session in a client folder cannot reach your mail, drive or CRM connectors because nothing granted them.
- **Smaller context.** Every enabled plugin and connector adds tool descriptions to the context. Loading only what a folder needs leaves room for the work.
- **One place to see it.** A table and a local page show, per folder, what is on, why (which profile granted it) and where it has drifted from the plan.

A visual walkthrough is in [docs/overview.html](docs/overview.html).

## Requirements

- Claude Code with `claude` on your `PATH` and logged in.
- Python 3.9 or newer, standard library only. Unit tests run on 3.9 through 3.13 on Linux and macOS in CI.
- macOS or Linux. Windows is not supported (the hook calls `python3`, the launcher is a shell script).

## Install

```
/plugin marketplace add Dangsllc/agent-need-to-know
/plugin install agent-need-to-know@agent-need-to-know
```

Local checkout: `claude plugin marketplace add /path/to/agent-need-to-know`.

## First run

In a Claude Code session run `/project-setup init`. It asks for two folders, then writes `~/.claude/agent-need-to-know/config.json` and copies the starter `profiles.json` and `projects.json` from `templates/` into your data folder:

| Setting | Meaning |
|---|---|
| `root` | The folder whose projects you manage, such as `~/code`. |
| `data_dir` | Where your `profiles.json`, `projects.json` and generated files live. A folder you back up or keep in git is a good choice. |

You can write the config by hand instead (set `NTK_CONFIG` to use another path):

```json
{"root": "~/code", "data_dir": "~/code/_ntk"}
```

**Names, paths and the old working name.** The config lives in `~/.claude/agent-need-to-know/`. Installs from the working name `devconsole` keep working: if the new folder has no `config.json` and `~/.claude/devconsole/config.json` exists, it is read from there (nothing is moved automatically; `scan` and the page footer print a note, and you can move it by copying `config.json`). In `data_dir` the cache is `.ntk-cache.json` and the log `ntk.log`; an existing `.devconsole-cache.json` is read and an existing `devconsole.log` is still appended to. Environment overrides use the prefix `NTK_` (`NTK_CONFIG`, `NTK_CLAUDE_JSON`, `NTK_CLAUDE_HOME`, `NTK_USER_SETTINGS`, `NTK_PORT`); the old `DEVCONSOLE_*` names still work but are deprecated.

Then run `/ntk` (or `python3 scripts/ntk.py scan`). Folders without a profile show as `new`; `/project-setup` interviews you about a folder and attaches the right profiles.

## Daily use

- `/ntk` opens the console page and prints a summary.
- A SessionStart hook says nothing when a folder matches its profiles. When a folder is new or has drifted, it adds one line to the session so Claude can offer to fix it.
- `/project-setup` in a new folder: a short interview, then profiles are attached and applied.
- Change a profile on the page (or with `profile ...`) and every project that uses it is updated.

## The model: projects are users, profiles are groups

ntk follows the pattern used for managing permissions in cloud environments.

- A **profile** is a small, single-purpose group: `plugins` and `connectors` it grants, plus optional `deny_plugins` and `deny_connectors`. It has an optional `group` (for the page) and a `description`. Connector names are written without the `claude.ai ` prefix.
- A **project** (a folder under `root`) is a user. In `projects.json` it lists the `profiles` attached to it. Capability comes only from attached profiles.
- `default_profiles` in `profiles.json` attach to every project automatically.
- **Allow is a union.** An item is allowed when any attached profile grants it.
- **Explicit deny wins.** A `deny_*` entry in any attached profile removes the item, whatever else grants it.
- **Connectors are denied by default.** Every claude.ai connector that no attached profile grants is turned off, including ones your account adds later.
- **Inline exceptions** (`inline`: `plugins_on`, `plugins_off`, `connectors_on`, `connectors_off`) are for one-offs. The page flags them and `scan` counts them. Prefer a profile. An inline `off` is a deny and wins; an inline `on` cannot override a profile deny.
- `recommended` lists profiles the page offers as one-click suggestions.
- Statuses: `ok`; `drift` (actual differs from desired); `missing` (a wanted plugin is not present, with a hint); `error` (plugin list failed, or an unknown profile is attached); `new` (no entry); `gone` (folder removed).

## Data files and the id contract

`profiles.json`, `projects.json` and `capabilities.json` live in `data_dir` and carry `"schema_version": 2`.

- A missing `schema_version` is read as 2, so files written before the field existed keep working. Older v1 shapes (one `profile` per project, `extends`) still load; ntk always writes v2 and keeps `_about` and other `_` keys.
- A file with a version greater than 2 was written by a newer ntk. Commands that change anything refuse (exit code 3, nothing written) and tell you to upgrade; read-only commands (`scan`, `inbox`, `snapshot`, `capability list`, the page) warn and carry on without rewriting it.

**Capability ids.** A bare id means Claude Code: `name@marketplace` for plugins, the plain name for connectors. An id that starts with `<tool>:` (lowercase letters, digits and hyphens, then a colon, for example `codex:mcp:github` or `gemini:ext:foo`) is reserved for future adapters. The Claude adapter ignores such ids: it never installs or enables them, `scan` does not count them as missing, and the page shows them greyed as "not supported yet". You can store them in profiles today so a profile is ready when its adapter ships.

## Lifecycle

Plugins and connectors appear and disappear. `scan` keeps `data_dir/capabilities.json` up to date with every plugin in any project's `claude plugin list`, every synced plugin in any org manifest on the machine, and every claude.ai connector.

- The first time the file is created, everything currently seen (and everything a profile names) is `active`, so day one has no inbox.
- After that, an id seen for the first time is `new` and waits in the inbox (`ntk inbox`, or the Capabilities tab) until you `adopt` it into profiles or `ignore` it.
- An `active` item that was seen before but is no longer seen anywhere becomes `gone` (`last_seen` is kept, nothing is deleted) and returns to `active` if it reappears. A scan that could not read every list never marks anything gone.
- `retire` removes an id from every profile and inline exception, applies the change and marks it `retired`.
- The hook adds one line when the inbox has new items, at most once a day.

## Workspaces

A workspace is a folder under `root` that exists to give one session access to several projects, for work that crosses them. To ntk it is an ordinary project: it has its own profiles, and a session only loads what its starting folder allows. You make a folder a workspace through Claude Code's own settings (the `additionalDirectories` list in the folder's `.claude/settings.json`). ntk does not write that setting. When a session starts in a folder that adds other directories, Claude Code may ask you to confirm that you trust them; that prompt belongs to Claude Code and ntk neither answers nor suppresses it.

## Commands

All run as `python3 scripts/ntk.py <command>` (or `ntk <command>` once the launcher is aliased). Inside Claude Code use `/ntk [scan|snapshot|apply <path>|...]`.

| Command | What it does |
|---|---|
| `scan [--json] [--fresh] [-v]` | Table of every project and its status. `-v` lists drift, inline exceptions and missing-plugin hints |
| `check [path] [--hook]` | Fast, file-only check (default: current directory). Used by the SessionStart hook |
| `apply <path> [--dry-run]` | Make one project match its profiles |
| `apply --all [--dry-run]` | Reconcile every project with an entry; per-project summary and totals; continues past failures |
| `attach <path> <profile> [--no-apply]` | Attach a profile (creates the entry for a new project), then apply |
| `detach <path> <profile> [--no-apply]` | Detach a profile, then apply |
| `profile <name> add\|remove\|deny\|undeny plugin\|connector <id> [--dry-run] [--no-apply]` | Edit a profile, then apply every project it reaches. `--dry-run` lists what would change and writes nothing |
| `profile-default add\|remove <name> [--dry-run] [--no-apply]` | Edit `default_profiles` (every project is affected) |
| `profile-new <name> [--description D] [--group G] [--from-project P]` | Create a profile, optionally seeded from a project's inline `on` items |
| `profile-delete <name>` | Delete a profile; refused while attached anywhere |
| `inline <path> plugin\|connector <id> on\|off\|clear` | Set or clear an inline exception, then apply that item |
| `inbox` | List new and gone capabilities |
| `capability adopt <id> --profiles a,b [--dry-run]` | Add a capability to profiles, apply, mark active |
| `capability ignore <id>` | Remove it from the inbox |
| `capability retire <id> [--dry-run]` | Remove it everywhere, apply, mark retired |
| `capability list [--status S]` | List the registry |
| `snapshot` | Write `CONSOLE.md` and a read-only `CONSOLE.html` to `data_dir` |
| `serve [--port 8765] [--no-open]` | The local page (Projects, Profiles, Capabilities tabs) |

Missing plugins that come from a marketplace are installed with `claude plugin install <id> --scope local`. Plugins that only exist on a claude.ai account (`@synced`) cannot be installed from here and are reported with a hint.

`scripts/ntk` is an optional POSIX shell launcher: it opens the page, starting the server in the background if needed (`ntk stop` stops it), and passes any other argument to `ntk.py`. Alias it to use it from your own shell: `alias ntk="<plugin path>/scripts/ntk"`. The `/ntk` slash command does the same work in Claude Code and needs no alias.

## The page

`/ntk` serves a page on `127.0.0.1:8765` with three tabs. **Projects**: every folder, its status, and switches for each plugin and connector with the reason behind each one. **Profiles**: edit grants and denies, preview which projects a change touches before saving. **Capabilities**: the inbox of new and gone items. See [docs/overview.html](docs/overview.html) for pictures.

## Safety and security

- The server binds to `127.0.0.1` only, rejects any request whose `Host` header is not `127.0.0.1` or `localhost`, and requires a random per-run token (header `X-Ntk-Token`) on every write. It sends no CORS headers.
- ntk itself opens no outbound network connections and has no telemetry. It runs `claude plugin list`, `claude mcp list` and, for changes, `claude plugin enable|disable|install --scope local`; whatever network access those commands need (listing connectors, installing from a marketplace) is Claude Code's.
- Plugins change only through those `claude plugin` commands. Connectors change by editing the claude.ai entries of `disabledMcpServers` for that one project in `~/.claude.json`: a timestamped copy goes to `data_dir/backups` first (once per run, last 20 kept), the write is atomic, the result is re-read to verify, and every other key is preserved.
- The launcher (`scripts/ntk`) probes `127.0.0.1` with `curl`, starts or stops its own background server (`pkill -f "ntk.py serve"` is the fallback for stop), and opens the page with `open` or `xdg-open`. `serve` opens it with Python's `webbrowser` unless `--no-open` is given.
- Every change is appended to `data_dir/ntk.log`.
- It never uninstalls a plugin, deletes a folder or removes a settings key it does not manage. Stale entries for folders that no longer exist are only reported.
- The SessionStart hook reads files only: it never runs `claude`, finishes in well under a second (the manifest allows 5 s), prints nothing for folders outside `root`, exits 0 on any error, and stays silent when a folder matches. The one exception is a single "no config found" hint, shown once on a machine with no config.
- Details and reporting: see [SECURITY.md](SECURITY.md).

## Uninstall

1. Remove the plugin: `/plugin uninstall agent-need-to-know@agent-need-to-know` (or `claude plugin uninstall agent-need-to-know@agent-need-to-know`).
2. Optional: delete `~/.claude/agent-need-to-know/` (config and hint markers) and your `data_dir` (profiles, projects, log, backups).

ntk leaves the settings it applied in place, which is what you want if you are only removing the tool. To hand a project back to Claude Code's defaults, undo these by hand:

- **Plugins.** Each project's `.claude/settings.local.json` has `enabledPlugins` entries written by `claude plugin enable|disable|install --scope local`. Delete the ones you do not want, or run `claude plugin uninstall <id> --scope local` in that folder.
- **Connectors.** `~/.claude.json` has, under `projects` > your folder, a `disabledMcpServers` list. Remove the entries that start with `claude.ai ` (with Claude Code closed), or re-enable each connector from `/mcp`. A copy of the file from before ntk's last change is in `data_dir/backups`.

`ntk scan -v` and `ntk apply <path> --dry-run` (run before you uninstall) show what ntk currently sets for each folder. There is no `release` command: a safe reset needs to edit Claude's files, which ntk only does for the connector list it manages.

## Roadmap

Adapters for Codex CLI and Gemini CLI, behind the same profiles and the `<tool>:` id prefix described above. No dates promised. The interface is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Contributing

Issues and pull requests are welcome, and adapters for other agent tools most of all: start from `scripts/ntk_pkg/adapters/base.py` and [ARCHITECTURE.md](ARCHITECTURE.md). Run the tests with `python3 -m unittest discover -s tests` and the leak check with `python3 scripts/check_leaks.py` (CI runs both). Keep the code to the standard library, and keep personal paths, addresses and project names out of the repository.

## License

MIT, see [LICENSE](LICENSE).
