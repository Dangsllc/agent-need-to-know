# Changelog

## Unreleased

- **Fix: no false drift after a global or account-wide switch.** Such a switch changes what every project sees, so ntk now forgets its cached plugin lists (up to 5 minutes old) right after one. Before, the page's re-scan showed the old state as drift in every project. History labels for page actions now include the choice (for example `project-only`) and the policy value.
- **Plugins switched on for the whole account.** An ordinary plugin installed at user level (Claude Code: `true` in the user settings' `enabledPlugins`) is on in every project, so profiles can't scope it. It used to show as drift in every project whose profiles don't grant it. Now:
  - it's reported once (`account_wide` in `scan --json`, an `account-wide:` line, an "On for the whole account" strip in the page and an inbox item), with no per-project drift;
  - you decide with `ntk account-wide <id> project-only` (off for the account, on in each project whose profiles grant it) or `everywhere` (added to the first default profile), or with the buttons on the page;
  - a policy covers new ones: `ntk policy account-wide ask|contain|allow` (default `ask`, stored in `profiles.json`). With `contain`, `apply --all` makes them project-only; with `allow` they're only reported;
  - account-wide switches go into history and the change log under `(account-wide)`, and undo and redo switch them back. `apply` is recorded when it changes something.
- **Capabilities tab** marks global plugins (🌐) and plugins on for the whole account.
- **History tab and undo per change.** The page has a History tab: every recorded change, newest first, with the switches it made (from the change log). Each line's menu offers **Undo just this change**, which keeps later changes and is refused with the reason when a later change touched the same thing, and **Go back to before this**, which also reverts the later ones. An undo line offers **Redo**. The full change log is below, each line linked to the change it was part of. CLI: `ntk history -v` and `ntk undo <id> --only`.
- **Global plugins.** Claude Code's built-in plugins (`<name>@builtin`) have one switch shared by every project: `claude plugin enable|disable --scope local` on one of them writes the user settings. ntk used to toggle them per project, so applying one project flipped them for all, and a built-in that Claude Code stopped offering was reported as missing in every project and sent to `claude plugin install`. Now:
  - they never count as per-project drift or missing, and nothing installs or switches them per project (`check` ignores them too);
  - `scan` reports each one once (`globals` in `--json`; a `global:` line when it needs attention): ok, needs on/off, split (profiles disagree, so nothing changes) or unavailable;
  - `apply --all` sets each one once, at user scope, only when every profile agrees; `apply <path>` explains instead of switching it;
  - the console shows them once, in a Global plugins strip above the projects, instead of in every row;
  - a profile edit, attach, detach, inline or capability change sets affected global plugins right away, with one line of output instead of one per project.
- **Warning when a profile grants an ignored or retired capability.**
- **Undo.** Every command and page action that changes `profiles.json`, `projects.json` or a capability's status (not `last_seen` churn from scans) is recorded in `data_dir/history/` with the files as they were before. `ntk history` lists them; `ntk undo` reverts the newest change and re-applies every project; `ntk undo <id>` goes back to before that change, reverting everything after it. An undo is itself recorded, so it can be redone. The page has an Undo button with the change it will undo in its tooltip.
- **Fix: switching between claude.ai accounts no longer marks capabilities gone.** Each login only sees its own org's synced plugins and connectors. `seen_on_orgs` is now merged across scans instead of replaced, and an item seen only on another account is left active when it is absent from the current login. Items an earlier scan wrongly marked gone return to active on the next scan, so `capability retire` on the gone list can't strip profile grants that belong to the other account.

## 1.0.0

First public release, as **Agent Need-to-Know** (short command `ntk`). The project was developed under the working name `devconsole`; every user-facing name changed at release:

- Plugin, marketplace and repository: `agent-need-to-know`. Slash command `/ntk`, entry script `scripts/ntk.py`, optional launcher `scripts/ntk` (moved out of `bin/`, which the plugin directory does not accept on claude.ai and Cowork), package `ntk_pkg`.
- Config directory `~/.claude/agent-need-to-know/`, environment prefix `NTK_`, cache `.ntk-cache.json`, log `ntk.log`, hook messages prefixed `ntk:`.
- Compatibility for the working-name install: the old config directory is still read (with a note in `scan` and the page footer, nothing moved automatically), `DEVCONSOLE_*` variables still work (deprecated), `.devconsole-cache.json` and `devconsole.log` are still read and appended to, and `devconsole@*` plugin ids are still treated as this plugin.

- **Composable profiles**, modeled on cloud permission management: projects are users, profiles are groups, allow is a union, explicit deny wins, `default_profiles` apply everywhere, inline exceptions are flagged and discouraged.
- **Deny-by-default connectors:** a claude.ai connector no attached profile grants is turned off for that project, including connectors added to the account later.
- **Console page** (`/ntk`): projects with the reason behind every switch, a profile editor with a preview of the projects a change touches, and the capabilities inbox. Local only, with a per-run token and a Host check.
- **`/project-setup` interview:** inspects a folder, asks a few questions, proposes a set of small profiles and attaches them. `/project-setup init` creates the config and starter files.
- **SessionStart hook:** file-only, fast and quiet; reports new folders, drift and new capabilities.
- **Capability lifecycle:** `capabilities.json` registry, an inbox of new and gone plugins and connectors, and `adopt`, `ignore` and `retire`.
- **Workspaces** are ordinary projects with their own profiles; a session loads only what its starting folder allows.
- **Backups and logging:** `~/.claude.json` is copied to `data_dir/backups` before connector changes, writes are atomic and verified, and every change is logged.
- **Data-format contract:** `schema_version: 2` in `profiles.json`, `projects.json` and `capabilities.json` (missing reads as 2, a newer version is refused by commands that write). Ids of the form `<tool>:...` are reserved for future adapters and ignored by the Claude adapter.
- **Code layout:** `scripts/ntk.py` is the entry point over the `ntk_pkg` package with a Claude Code adapter behind an adapter interface (see ARCHITECTURE.md).
- Release tooling: unit tests and a leak check (`scripts/check_leaks.py`) run in CI on Linux and macOS, Python 3.9 through 3.13.
- `check` now defaults to the current directory, so the hook command needs no shell variable other than `CLAUDE_PLUGIN_ROOT`.

## 0.x (pre-release)

Private iterations before the public release, under the name devconsole: a single `profile` per project (v1 files still load), then composable profiles and the capability lifecycle.
