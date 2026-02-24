# session-collector

Boot block tool + session-level analysis pipeline for SkillBench.

## What is this?

A local CLI tool that sits on top of [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) and lets users:
1. Browse their indexed coding agent sessions (Claude Code, Cursor, Copilot, Gemini, Aider, etc.)
2. Auto-classify projects by git visibility + OSS license (MIT/Apache = auto-include, proprietary/missing = exclude)
3. Review/edit an allowlist of folders to share
4. Export selected session data for SkillBench analysis

## Quick start

### Prerequisites
- Python 3.9+
- [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) installed and indexed
- `git` CLI
- `gh` CLI (optional but recommended — enables public/private repo detection)

### Step 1: Index your sessions with CASS
```bash
cass index --full
```

### Step 2: Scan and classify your projects
```bash
python bootblock.py scan
```
This checks each project for git remotes and OSS licenses, then generates `bootblock.txt` — an editable allowlist. Public repos with MIT/Apache/BSD licenses are auto-included. Everything else is excluded by default.

### Step 3: Review and edit
Open `bootblock.txt` in any editor. Uncomment projects you're willing to share. Comment out anything you want to exclude. ~2 minutes.

### Step 4: Export
```bash
python bootblock.py export
```
This extracts full conversation data (all agents, all messages) for your selected folders into `sessions.json`.

### Step 5: Upload
Upload `sessions.json` to the shared Google Drive folder (https://drive.google.com/drive/folders/1rjZnWp3xio6a9zVfD7YdOJt9fIPPySsL?usp=sharing) and ping Matt in Slack.

## Status

**Boot block tool is functional.** Server-side analysis pipeline is next.

See [SPEC.md](./SPEC.md) for the full design (architecture, metrics, dashboard wireframes, compatibility with skillmeter).

Customer 0: Chris Sells (Gastown Discord).

## How it fits with skillmeter

This is a **complementary data layer**, not a replacement:

| Layer | Tool | Granularity | What it tells you |
|-------|------|-------------|-------------------|
| Keystroke | skillmeter VS Code extension | Character-level | How much AI wrote vs. you |
| Session | session-collector (this repo) | Conversation-level | How effectively you direct AI |

See the "Compatibility" section in SPEC.md for integration details.

## Team

Questions, concerns, strong opinions — open an issue or comment on the spec. This is intentionally rough. The point is to get something concrete everyone can react to.
