# Privacy & data policy

**All processing is local.** No data is sent anywhere unless you explicitly
share the output file.

## Two-level privacy model

### Level 1 — Workspace filtering

Public GitHub repos with recognized OSS licenses are auto-included. Private
repos, unlicensed projects, and non-GitHub repos are excluded by default. If
no public repos qualify, you'll be prompted to select private workspaces
manually. Use `--allowed-orgs` to restrict collection to specific GitHub orgs
(e.g. for a pilot program).

### Level 2 — Content sanitization

The export is automatically scrubbed using deterministic pattern matching.
Redacted patterns include:

- API keys and tokens (AWS, GitHub, Anthropic, OpenAI, Slack, Stripe, etc.)
- Email addresses
- Private IP addresses
- Home directory paths (replaced with `~`)
- SSH keys, connection strings, passwords, bearer tokens
- `.env` file secrets

A summary of what was redacted is printed after collection.

## Additional guarantees

- **No telemetry, no auto-sync.** No background uploads, no analytics.
- **Network calls:** Only `gh` (GitHub CLI) during classification, and only
  for repos that have a GitHub remote.
- **You control what's shared.** Review `dist/skillbench_export_sanitized.json`
  before uploading.

## Agent fidelity

Full session fidelity (complete tool payloads, code diffs, command outputs)
is currently supported for **Claude Code** sessions. Other agents (Gemini
CLI, Codex CLI) are included with summary-level data (messages and metadata,
tool blocks stubbed).
