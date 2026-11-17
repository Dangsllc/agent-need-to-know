# Changelog

## 1.0.0

First public release.

- **Composable profiles** on the IAM model: projects are users, profiles are groups, allow is a union, explicit deny wins, `default_profiles` apply everywhere, inline exceptions are flagged and discouraged.
- **Deny-by-default connectors:** a claude.ai connector no attached profile grants is turned off for that project, including connectors added to the account later.
- **Console page** (`/devconsole`): projects with the reason behind every switch, a profile editor with a preview of the projects a change touches, and the capabilities inbox. Local only, with a per-run token and a Host check.
- **`/project-setup` interview:** inspects a folder, asks a few questions, proposes a set of small profiles and attaches them. `/project-setup init` creates the config and starter files.
- **SessionStart hook:** file-only, fast and quiet; reports new folders, drift and new capabilities.
- **Capability lifecycle:** `capabilities.json` registry, an inbox of new and gone plugins and connectors, and `adopt`, `ignore` and `retire`.
- **Workspaces** are ordinary projects with their own profiles; a session loads only what its starting folder allows.
- **Backups and logging:** `~/.claude.json` is copied to `data_dir/backups` before connector changes, writes are atomic and verified, and every change is logged.
- **Data-format contract:** `schema_version: 2` in `profiles.json`, `projects.json` and `capabilities.json` (missing reads as 2, a newer version is refused by commands that write). Ids of the form `<tool>:...` are reserved for future adapters and ignored by the Claude adapter.
- **Code layout:** `scripts/devconsole.py` is the entry point over the `devconsole_pkg` package with a Claude Code adapter behind an adapter interface (see ARCHITECTURE.md).
- Release tooling: unit tests and a leak check (`scripts/check_leaks.py`) run in CI on Linux and macOS, Python 3.9 through 3.13.
- `check` now defaults to the current directory, so the hook command needs no shell variable other than `CLAUDE_PLUGIN_ROOT`.

## 0.x (pre-release)

Private iterations before the public release: a single `profile` per project (v1 files still load), then composable profiles and the capability lifecycle.
