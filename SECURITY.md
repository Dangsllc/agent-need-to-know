# Security

## What ntk is, for threat modeling

A local tool that reads and edits which plugins and connectors Claude Code loads per project folder. It handles no credentials and no project content, and makes no network calls of its own. Its power is configuration: turning a plugin or a claude.ai connector on for a folder widens what a session can reach, so the question is who can make ntk do that.

## What the server exposes

`ntk serve` (the page) listens on `127.0.0.1` only.

- Reads: the page (`GET /`) and the state (`GET /api/state`): folder names under your `root`, profile contents, plugin and connector names, org names and short ids from your Claude account files. No file contents, no credentials.
- Writes: `POST /api/*` can attach and detach profiles, edit profiles, set inline exceptions, run apply, and adopt, ignore or retire capabilities. These run the same code as the command line.
- Guards: every request must carry a `Host` of `127.0.0.1` or `localhost` (defeats DNS rebinding); every write must carry the per-run random token in `X-Ntk-Token`, compared in constant time. No CORS headers are sent, so another website cannot make a cross-origin write that carries the custom header. Responses are `no-store`.
- The token is embedded in the page served to local clients, so any process on your machine that can fetch `http://127.0.0.1:<port>/` can obtain it. That process could edit the same files directly, so this is not a privilege boundary; do not run the server on a machine with untrusted local users.
- A bad write payload returns 400; unexpected errors return 500 with a message and no stack trace.

## What it touches

- `claude plugin list|enable|disable|install --scope local` and `claude mcp list`, run with arguments ntk builds from your profiles (no shell, no string interpolation into a shell).
- `~/.claude.json`: only the claude.ai entries of `disabledMcpServers` for one project, after a backup, atomically, verified.
- Read-only: Claude settings files (`enabledPlugins`) and org-sync plugin manifests.
- Its own files in `data_dir` and `~/.claude/agent-need-to-know/`.

The SessionStart hook is file-only: it never runs `claude`, never opens a socket, and exits 0 on any error.

## Out of scope

Anyone who can write your data files or `~/.claude.json` already controls your agent configuration. ntk does not defend against that, and does not sandbox the plugins or connectors it enables.

## Reporting a vulnerability

Please report privately through GitHub security advisories: open the repository's **Security** tab at `https://github.com/Dangsllc/agent-need-to-know/security/advisories/new` and choose **Report a vulnerability**. Do not file a public issue for a security problem. Include the version, what you did and what you expected. Expect an acknowledgement within a few days; there is no bug bounty.

Supported versions: the latest release.
