# Rules and file formats

## profiles.json
```json
{
  "schema_version": 2,
  "default_profiles": ["baseline"],
  "profiles": {
    "baseline":    {"group": "default", "description": "...", "plugins": [], "connectors": []},
    "lang-python": {"group": "code", "description": "...", "plugins": ["pyright-lsp@claude-plugins-official"], "connectors": []},
    "team-chat":   {"group": "connectors", "description": "...", "plugins": [], "connectors": ["Slack"],
                    "deny_plugins": [], "deny_connectors": []}
  }
}
```
- Plugin ids are `name@marketplace`, for example `engineering@anthropic-plugin-directory` or `pyright-lsp@claude-plugins-official`.
- Connector names are written **without** the `claude.ai ` prefix: `Slack`, not `claude.ai Slack`.
- Keys that start with `_` are comments. Keep them when you rewrite a file.

## projects.json
```json
{
  "schema_version": 2,
  "projects": {
    ".":       {"profiles": ["notes"]},
    "my-api":  {"profiles": ["lang-python", "secure-code"], "notes": "..."},
    "my-site": {"profiles": ["lang-typescript"], "recommended": ["marketing"],
                "inline": {"plugins_on": [], "plugins_off": [], "connectors_on": [], "connectors_off": []}}
  }
}
```
Keys are paths relative to `root`, and `.` is the root itself. A subfolder belongs to the longest key that contains it.

## Resolving what a project should load
1. **Attached** = `default_profiles` + the project's `profiles`, with duplicates removed. An unknown profile name is an error: report it and don't apply that project.
2. **Allowed plugins** = every plugin those profiles grant, plus `inline.plugins_on`.
3. **Denied plugins** = every `deny_plugins` in those profiles, plus `inline.plugins_off`.
4. **Desired plugins** = allowed minus denied. **A deny always wins.**
5. **Allowed connectors** = every connector those profiles grant, plus `inline.connectors_on`.
6. **Denied connectors** = every `deny_connectors`, plus `inline.connectors_off`.
7. **Connectors on** = allowed minus denied.
8. **Connectors off** = every available claude.ai connector (from `claude mcp list`) minus the connectors on. Connectors are **denied by default**, so a connector the account adds later starts off everywhere.

Record where each item comes from (`pyright-lsp ← lang-python`) so you can explain it.

## Comparing with what is actually loaded
- **Plugin needs enabling:** desired, installed, but `enabled` is false.
- **Plugin needs installing:** desired, but absent from `claude plugin list --json`.
  - `@claude-plugins-official`, `@anthropic-plugin-directory` or another added marketplace: it can be installed with `claude plugin install <id> --scope local`.
  - `@synced`: it comes only from a claude.ai account. Report it as missing on this account and don't try to install it.
- **Plugin needs disabling:** enabled in this project but not desired. Skip this assistant's own plugin, `agent-need-to-know@*`.
- **Connector drift:** the current `claude.ai ` entries in `disabledMcpServers` differ from **connectors off**.
