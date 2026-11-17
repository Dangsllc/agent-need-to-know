# devconsole

One place to see and switch which Claude Code plugins and claude.ai connectors are on in each project folder under a root directory. You define small profiles ("lang-python", "secure-code"), attach a few to each folder, and devconsole makes Claude's real settings match. macOS, Python 3 standard library only.

## Install

```
/plugin marketplace add <owner>/devconsole
/plugin install devconsole@devconsole
```

Local development: `claude plugin marketplace add /path/to/devconsole`.

## Configure

Create `~/.claude/devconsole/config.json` (set `DEVCONSOLE_CONFIG` to use another path):

```json
{"root": "~/code", "data_dir": "~/code/_devconsole"}
```

`root` is the folder whose projects you manage. `data_dir` holds your own files: `profiles.json` and `projects.json` (start from `templates/*.example.json`), plus generated `CONSOLE.md`, `CONSOLE.html`, a log, backups and a scan cache. Optional `plugin_costs.json` (`{"plugin-id": tokens}`) adds a token column.

## The model: projects are users, profiles are groups

devconsole follows the AWS IAM pattern.

- A **profile** is a small, single-purpose group: a list of `plugins` and `connectors` it grants, plus optional `deny_plugins` and `deny_connectors`. A profile has an optional `group` (for the page) and a `description`. Connector names are written without the `claude.ai ` prefix.
- A **project** (a folder under `root`) is a user. In `projects.json` it lists the `profiles` attached to it. Capability comes only from attached profiles.
- `default_profiles` in `profiles.json` are attached to every project automatically.
- **Allow is a union.** A plugin or connector is allowed when any attached profile grants it.
- **Explicit deny wins.** A `deny_*` entry in any attached profile removes the item, whatever else grants it.
- **Connectors are denied by default.** Every claude.ai connector that no attached profile grants is turned off, including ones a newer account adds later.
- **Inline exceptions** (`inline`: `plugins_on`, `plugins_off`, `connectors_on`, `connectors_off`) exist for one-off cases. They are flagged with an amber badge on the page and counted in `scan`, and they are discouraged: make or attach a profile instead. An inline `off` is a deny and wins; an inline `on` cannot override a profile deny.
- `recommended` lists profiles the page shows as one-click suggestions.
- The page and `scan` show where each item comes from ("from lang-python, code-core").
- Folders without an entry are `new`. Statuses: `ok`, `drift` (actual differs from desired), `missing` (a wanted plugin is absent from `claude plugin list` for that folder; the hint says how to fix it), `error` (plugin list failed or an unknown profile is attached), `new`, `gone`.
- v1 files (one `profile` per project, top-level extras, `extends`) still load; devconsole always writes the v2 shape.

## Launcher

`bin/devconsole` opens the page, starting the server in the background if it is not running. `bin/devconsole stop` stops it. Any other argument passes through to `scripts/devconsole.py`. Alias it in your shell: `alias devconsole="/path/to/devconsole/bin/devconsole"`.

## Commands

All run as `python3 scripts/devconsole.py <command>`; inside Claude Code use `/devconsole [scan|snapshot|apply <path>|...]`.

| Command | What it does |
|---|---|
| `scan [--json] [--fresh] [-v]` | Table of every project and its status. `-v` lists drift items, inline exceptions and missing-plugin hints |
| `check <path> [--hook]` | Fast, file-only check used by the SessionStart hook. Uses the cached connector list; with no cache it skips connector drift |
| `apply <path> [--dry-run]` | Make one project match its profiles |
| `apply --all [--dry-run]` | Reconcile every project that has an entry. Prints a per-project summary and totals, backs up `~/.claude.json` once, continues past failures |
| `attach <path> <profile> [--no-apply]` | Attach a profile (creates the entry for a new project), then apply |
| `detach <path> <profile> [--no-apply]` | Detach a profile, then apply |
| `profile <name> add\|remove plugin\|connector <id> [--dry-run] [--no-apply]` | Edit a profile, then apply every project it reaches. `--dry-run` lists affected projects and changes and writes nothing |
| `profile-new <name> [--description D] [--group G] [--from-project P]` | Create a profile. `--from-project` seeds it from that project's inline `on` items and moves them into an attachment |
| `profile-delete <name>` | Delete a profile; refused while it is attached anywhere |
| `inline <path> plugin\|connector <id> on\|off\|clear` | Set or clear an inline exception, then apply that item |
| `snapshot` | Write `CONSOLE.md` and a read-only `CONSOLE.html` |
| `serve [--port 8765] [--no-open]` | Local page: Projects and Profiles tabs. `#profiles` in the URL opens the Profiles tab |

Official-marketplace plugins that are wanted but absent are installed with `claude plugin install <id> --scope local`. Synced or other plugins that are absent are reported and skipped.

The SessionStart hook says nothing unless a folder is new or differs from its profiles. For new folders it suggests `/project-setup`, a skill you can add under `skills/project-setup/`.

## Safety

- The server binds to 127.0.0.1 only, checks the `Host` header, and requires a random per-run token on every write.
- Plugins change through `claude plugin enable|disable|install --scope local`. Connectors change by editing `disabledMcpServers` in `~/.claude.json` for that project only: backed up first (last 20 kept in `data_dir/backups`), written atomically, re-read to verify, all other keys preserved.
- Every change is appended to `data_dir/devconsole.log`.
- Nothing is ever deleted. Stale entries for folders that no longer exist are reported only.

## Tests

`python3 -m unittest discover -s tests`

## License

MIT
