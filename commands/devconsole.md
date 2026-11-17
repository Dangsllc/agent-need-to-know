---
description: Open the devconsole page, or run scan, snapshot or apply for project profiles
---

Arguments: `$ARGUMENTS`

Run devconsole according to the arguments.

- No arguments: start the console in the background with
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devconsole.py" serve` (run it with `run_in_background`; it opens the browser on 127.0.0.1:8765). Then run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devconsole.py" scan` and reply with a short summary: the status counts, which projects are `new`, `drift` or `missing`, and which plugins are missing on this account.
- `scan`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devconsole.py" scan` (add `--fresh` if asked) and summarize the table.
- `snapshot`: run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devconsole.py" snapshot` and say where CONSOLE.md and CONSOLE.html were written.
- `apply <path>` (optionally `--profile NAME`): first run it with `--dry-run`, show the planned changes, and ask before running it for real:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devconsole.py" apply <path> [--profile NAME] [--dry-run]`.
  `<path>` is relative to the configured root.

Never edit the user's Claude config files by hand; the script takes backups and writes them atomically.
