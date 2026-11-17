---
description: Open the Agent Need-to-Know (ntk) page, or run scan, snapshot, apply or profile edits for project profiles
---

Arguments: `$ARGUMENTS`

Run ntk according to the arguments.

- No arguments: start the console in the background with
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntk.py" serve` (run it with `run_in_background`; it opens the browser on 127.0.0.1:8765). Then run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntk.py" scan` and reply with a short summary: the status counts, which projects are `new`, `drift` or `missing`, and which plugins are missing on this account.
- `scan`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntk.py" scan` (add `--fresh` if asked) and summarize the table.
- `snapshot`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntk.py" snapshot` and say where CONSOLE.md and CONSOLE.html were written.
- `apply <path>` or `apply --all`: first run it with `--dry-run`, show the planned changes, and ask before running it for real:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ntk.py" apply <path>|--all [--dry-run]`.
  `<path>` is relative to the configured root.
- Profiles are groups attached to projects (deny wins; connectors are denied unless a profile grants them). Other subcommands, all with the same script; edits write the user's data files and then apply, so show what they will touch and ask first:
  - `attach <path> <profile>` and `detach <path> <profile>` (`--no-apply` to only edit)
  - `profile <name> add|remove plugin|connector <id>` (run with `--dry-run` first to list affected projects)
  - `profile-new <name> [--description D] [--group G] [--from-project PATH]` and `profile-delete <name>`
  - `profile <name> deny|undeny plugin|connector <id>` and `profile-default add|remove <name>` (both support `--dry-run`)
  - `inbox` (new and gone plugins/connectors), `capability adopt <id> --profiles a,b [--dry-run]`, `capability ignore <id>`, `capability retire <id> [--dry-run]`, `capability list [--status S]`
  - `inline <path> plugin|connector <id> on|off|clear` (inline exceptions are flagged; prefer a profile)

Never edit the user's Claude config files by hand; the script takes backups and writes them atomically.

This command needs no alias. To also run the launcher from your own shell, alias it: `alias ntk="<plugin path>/scripts/ntk"`.
