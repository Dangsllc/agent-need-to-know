# Agent Need-to-Know (directory edition)

Minimum necessary, applied to AI agents. An agent session should reach the tools and data sources its task needs, and nothing more. Agent Need-to-Know sets that per project for the plugins and claude.ai connectors Claude Code loads, similar to managing permissions in cloud environments: projects are like users, profiles are like groups, and nothing is on unless a profile grants it.

This is the **directory edition**. It contains instructions only: two skills and one slash command. Claude does the work when you ask, and previews every change before making it. The **full edition** on GitHub adds a local console page with switches, a session-start check that flags new folders and drift, a capability inbox and a tested engine. Both editions use the same files, so you can switch at any time without migrating: https://github.com/Dangsllc/agent-need-to-know

## Why
- **Privacy:** a connector to a mailbox, a document store or a CRM is an access path to whatever sits behind it. Profiles let a privacy program apply its access rules to agent sessions, project by project. That fits HIPAA's minimum necessary standard (45 CFR 164.502(b), 164.514(d)) and the Part 2 limit on disclosing more than a purpose needs (42 CFR 2.13(a)).
- **Cost:** every enabled plugin adds its skill listings to every request in a session. Turning off what a project doesn't use shrinks every session that starts there.

## The rules
1. A project gets the plugins and connectors of every profile attached to it, plus the default profiles every project gets.
2. Connectors are off unless a profile grants them, so one your account adds later starts off everywhere.
3. A deny in a profile beats any grant. One-off per-project exceptions are possible, and they're flagged.

## Getting started
1. Install the plugin from the directory.
2. Run `/project-setup init`. Claude asks where your projects live and where to keep your profiles, then copies in starter profiles.
3. In any project, run `/project-setup`. Claude looks at the folder, asks a few questions and proposes a set of profiles.
4. Use `/ntk` any time: `/ntk` shows what the current project loads, `/ntk attach <profile>`, `/ntk apply`, `/ntk all`, `/ntk new`.

Changes take effect in the next session started in that folder.

## What it reads and changes
Disclosed in full, because it touches your Claude Code settings:
- **Reads:** your profiles and project list (in the data folder you choose), each project's `.claude/settings.local.json`, the `projects` section of `~/.claude.json`, and the output of `claude plugin list --json` and `claude mcp list`.
- **Changes, after showing you and getting a yes:**
  - plugins: runs `claude plugin install`, `enable` or `disable` with `--scope local` in the project folder;
  - connectors: sets `disabledMcpServers` for that project in `~/.claude.json`, changing only its `claude.ai ` entries, after copying the file to a timestamped backup in your data folder;
  - your own `profiles.json`, `projects.json` and a change log, `ntk.log`.
- **Never:** sends data anywhere, deletes entries or backups, or enables a plugin at user scope.

It governs which plugins and connectors a session loads. It does not restrict file access, encrypt data or replace a business associate agreement. Pair it with your agent tool's own permission settings.

## Uninstall
Remove the plugin from the directory or with `/plugin`. Your profiles and backups stay in your data folder. Per-project plugin switches and connector lists stay in Claude Code's settings until you change them.

## License
MIT
