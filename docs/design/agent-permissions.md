# Design: agent permissions as a capability (scope expansion)

Status: proposal (2026-11-27). Builds on the neutral core (PR #5) and the Antigravity adapter (P1). Nothing here is built yet.

## Why

Today ntk decides what each project folder may **load**: plugins and connectors. Agent tools also decide what an agent may **do** in a folder: run commands, read and write files, fetch URLs, call MCP tools. A least-privilege story for clients ("each project's agents can do what its work needs, with an audit trail") has to cover both. The same profiles, deny-wins rule, history, undo and change log should cover permissions too.

## What we verified (Antigravity 1.5.15, 2026-11-27)

Docs: [Permissions](https://antigravity.google/docs/permissions/), [Projects](https://antigravity.google/docs/projects/), [MCP](https://antigravity.google/docs/mcp/).

- **Rule syntax:** `action(target)` in allow, ask and deny lists. Actions: `read_file`, `write_file`, `read_url`, `execute_url`, `download_url`, `command`, `mcp`. Within one rule set, Deny > Ask > Allow.
- **Projects:** an Antigravity project is a set of folders. Its rules are in `~/.gemini/config/projects/<uuid>.json` under `permissionGrants.permissionGrants`, layered on the global rules in `~/.gemini/config/config.json` (`userSettings.globalPermissionGrants`). The App, IDE and CLI share this folder.
- **Experiment:** throwaway project, rules hand-edited, run headless with `agy --project=<id> -p`.

| Test | Result |
|---|---|
| global allow `command(echo)`, no project rule | ran |
| plus project deny `command(echo)` | blocked |
| no rule anywhere, `printf` | ran |
| project allow and deny `command(printf)` | blocked |
| project deny `mcp(chrome-devtools/*)` (the plugin's server name) | **not** blocked |
| project deny `mcp(chrome-devtools-plugin_chrome-devtools/*)` | blocked |
| project deny `mcp(*)`, with global allow `mcp(*)` | blocked |

What this shows:
1. Hand edits to the project file are honored, and Antigravity did not rewrite the file.
2. A project deny beats a global allow.
3. Unconfigured actions ran headless, so **deny is the lever that enforces**; allow only avoids prompts. This may differ in interactive sessions (not tested).
4. MCP rules for a plugin's server use the internal name `<plugin>_<server>`, not the name the plugin lists. The adapter must translate.

Not known yet: whether a running App picks up an edit or overwrites it (Settings > Projects was greyed out in the App on the test machine). The project file format is observed, not documented.

## Model

A fourth kind next to plugins and connectors (the plan's "kinds" declaration gains `permission`):

```json
"profiles": {
  "secure-code": {
    "deny_actions": ["command(rm -rf)", "write_file(/etc)", "read_url(*)"]
  },
  "web-research": {
    "actions": ["read_url(*)"]
  }
}
```

- **Grammar:** profiles use the `action(target)` grammar above, which is tool-neutral enough to be the canonical form. Each adapter translates to its tool:
  - Antigravity: as is.
  - Claude Code: `command(git)` becomes `Bash(git:*)`, `read_file(p)` becomes `Read(p)`, `write_file(p)` becomes `Edit(p)`/`Write(p)`, and `mcp(server/tool)` becomes `mcp__server__tool`, all in the project's `.claude/settings.local.json` `permissions`.
  - An adapter returns `None` for a rule it can't express, and the console shows that rule as not enforced for that tool.
- **Resolution:** the same as today. Allowed is the union of `actions` across attached and default profiles plus inline. Denied is the union of `deny_actions` plus inline. Deny wins. Rules are compared as strings after normalising whitespace. ntk does not try to decide whether one rule covers another.
- **MCP from connectors:** a connector or MCP server that a project's profiles don't grant becomes a deny rule for tools that can't switch MCP per project but can deny it. For Antigravity that is `mcp(<internal-name>/*)`. This gives Antigravity real per-project MCP control even though its MCP config is global.
- **Ownership:** ntk changes only the rules it put there. The last set it applied per tool project is kept in `data_dir/applied.json`, so a rule a person added in the tool's UI is left alone and reported as "unmanaged". Removing a grant removes only ntk's rule.

## Antigravity specifics

- **Folders to projects is many-to-many.** On the test machine one folder is in four projects. ntk applies a folder's rules to every Antigravity project that lists it.
- **A project with several folders** gets the union of the denies of its folders. Its allows are those that every folder allows. When they differ, the console flags the project as "mixed".
- **Stale projects:** a project whose folders no longer exist is reported, never edited.
- **Writes:** change only `permissionGrants.permissionGrants`, keep every other key, write atomically, back up the file first, log each rule as `permission:<rule>`. Skip and report if the JSON shape is unexpected.
- **Headless reality:** the console says plainly that allow rules reduce prompts but don't restrict anything; restriction comes from deny rules.

## Console and CLI

- A Permissions section per project (allowed, denied, unmanaged, not enforceable per tool), and an `actions` editor on profiles with the same deny-wins badges.
- `ntk scan` reports permission drift like connector drift. `ntk check` (the SessionStart hook) stays file-only.
- History and undo cover it unchanged, because they snapshot ntk's data files and re-apply.
- The compliance export (P3) lists permission changes with who and when.

## Phasing (separate PRs)

1. **Core kind, report only:** the profile fields, `resolve()`, rows and scan output, page display, and tests. No adapter writes.
2. **Antigravity writes:** project discovery from `config/projects`, folder matching, deny and allow writing with backups and ownership, the MCP name translation, and tests against fixtures copied from a real install, with secrets stripped.
3. **Claude Code mapping:** write `permissions` in `.claude/settings.local.json` through the adapter.

## Open questions

1. Do the canonical rules stay as Antigravity's grammar, or should ntk keep its own neutral names?
2. Ask lists: support them, or leave ask as the default for everything unconfigured?
3. Should a `deny_actions` entry in a default profile be locked against inline exceptions (a "guardrail" profile)?
4. App behaviour when it is open during an edit: verify before phase 2 ships.
