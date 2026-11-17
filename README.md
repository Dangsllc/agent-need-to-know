# devconsole

One place to see and switch which Claude Code plugins and claude.ai connectors are on in each project folder under a root directory. You name a few profiles ("python-app", "static-site"), assign one to each folder, and devconsole makes Claude's real settings match. macOS, Python 3 standard library only.

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

## How profiles and extras work

- A profile lists `plugins` (applied), `suggested` (shown, not applied), `connectors_off` and optionally `extends`. Plugins are the union up the chain. A child's `connectors_on` beats any ancestor's `connectors_off`. Connector names are written without the `claude.ai ` prefix.
- A project in `projects.json` picks a profile and may add extras: `plugins_on`, `plugins_off`, `connectors_on`, `connectors_off`.
- Desired state is the resolved profile, then the extras.
- Folders without an entry are `new`. Statuses: `ok`, `drift` (actual differs from desired), `missing` (a wanted plugin is not on this account), `new`, `gone`.

## Launcher

`bin/devconsole` opens the page, starting the server in the background if it is not running. `bin/devconsole stop` stops it. Any other argument passes through to `scripts/devconsole.py`. Alias it in your shell: `alias devconsole="/path/to/devconsole/bin/devconsole"`.

## Commands

All run as `python3 scripts/devconsole.py <command>`; inside Claude Code use `/devconsole [scan|snapshot|apply <path>]`.

| Command | What it does |
|---|---|
| `scan [--json] [--fresh] [-v]` | Table of every project and its status |
| `check <path> [--hook]` | Fast, file-only check used by the SessionStart hook |
| `apply <path> [--profile P] [--dry-run]` | Make a project match its desired state |
| `set <path> plugin\|connector <name> on\|off` | Record an extra and apply it |
| `snapshot` | Write `CONSOLE.md` and a read-only `CONSOLE.html` |
| `serve [--port 8765] [--no-open]` | Local page with switches |

The SessionStart hook says nothing unless a folder is new or differs from its profile. For new folders it suggests `/project-setup`, a skill you can add under `skills/project-setup/`.

## Safety

- The server binds to 127.0.0.1 only, checks the `Host` header, and requires a random per-run token on every write.
- Plugins change through `claude plugin enable|disable|install --scope local`. Connectors change by editing `disabledMcpServers` in `~/.claude.json` for that project only: backed up first (last 20 kept in `data_dir/backups`), written atomically, re-read to verify, all other keys preserved.
- Every change is appended to `data_dir/devconsole.log`.
- Nothing is ever deleted. Stale entries for folders that no longer exist are reported only.

## Tests

`python3 -m unittest discover -s tests`

## License

MIT
