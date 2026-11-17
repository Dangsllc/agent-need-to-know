# Applying changes safely

Do these steps in order for each project. Stop and report if any step fails.

## 1. Preview
List every action and ask for a yes:
```
my-api
  + install  pyright-lsp@claude-plugins-official
  + enable   engineering@anthropic-plugin-directory
  - disable  marketing@anthropic-plugin-directory
  connectors off: Gmail, Google Drive, Slack (+2 more)   was: none
  skipped:   finance@synced (not on this account)
```
If nothing differs, say the project is already in line and stop.

## 2. Plugins
Run each command from the project folder (`cd "<absolute project path>"` first):
- `claude plugin install <id> --scope local`, then `claude plugin enable <id> --scope local`
- `claude plugin enable <id> --scope local`
- `claude plugin disable <id> --scope local`

If the CLI says the plugin is already in the requested state, treat that as done.

## 3. Connectors
Before writing, copy the file:
```
mkdir -p "<data_dir>/backups" && cp ~/.claude.json "<data_dir>/backups/claude.json.$(date +%Y%m%d-%H%M%S)"
```
Then set the project's list. This keeps every other key and every entry that isn't a claude.ai connector, writes atomically and checks the result. Pass the absolute project path and the connectors-off names as a JSON array:
```
python3 - "<absolute project path>" '["Gmail","Google Drive","Slack"]' <<'PY'
import json, os, re, sys, tempfile
path = os.path.expanduser("~/.claude.json")
proj, want = sys.argv[1], json.loads(sys.argv[2])
text = open(path, encoding="utf-8").read()
data = json.loads(text)
m = re.match(r"\{\r?\n( +|\t)\S", text)
indent = None if not m else ("\t" if m.group(1) == "\t" else len(m.group(1)))
entry = data.setdefault("projects", {}).setdefault(proj, {})
keep = [x for x in (entry.get("disabledMcpServers") or []) if not x.startswith("claude.ai ")]
entry["disabledMcpServers"] = keep + ["claude.ai " + n for n in want]
out = json.dumps(data, indent=indent, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(out)
os.chmod(tmp, os.stat(path).st_mode & 0o777)
os.replace(tmp, path)
check = json.load(open(path, encoding="utf-8"))
assert check["projects"][proj]["disabledMcpServers"] == entry["disabledMcpServers"]
print("connectors updated")
PY
```
Keep the newest 20 backups and leave older ones for the user to remove.

## 4. Verify
Re-read `claude plugin list --json` in the project folder, and the project's `disabledMcpServers`. Confirm that they match the preview.

## 5. Log
Append one line per change to `<data_dir>/ntk.log`, tab-separated:
```
2026-11-17T14:02:11-08:00	my-api	plugin:pyright-lsp@claude-plugins-official	absent -> installed+on
```

## 6. Tell the user
Summarize what changed, and remind them that it takes effect in the next session started in that folder.
