# session-collector

Boot block tool + session-level analysis pipeline for SkillBench.

## What is this?

A local CLI tool that sits on top of [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) and lets users:
1. Browse their indexed coding agent sessions (Claude Code, Cursor, Copilot, Gemini, Aider, etc.)
2. Auto-classify projects by git visibility + OSS license
3. Review/edit an allowlist of folders to share
4. Compute agentic engineering metrics locally
5. Export selected session data for SkillBench analysis

## Auto-include algorithm

A project is auto-included only when **both** conditions are met:
- The repo is **public on GitHub** (checked via `gh repo view`)
- GitHub detects a **recognized OSS license** in the LICENSE file (via its built-in [Licensee](https://github.com/licensee/licensee) gem, returning a valid SPDX ID)

Projects that fail either check are excluded by default but can be manually uncommented in the bootblock.

## Usage

```bash
# Prerequisites: install CASS and index your sessions
cargo install cass
cass index --full

# Install skillbench
pip install -e .

# 1. Scan and classify all workspaces → dist/bootblock.txt
skillbench scan

# 2. Review/edit dist/bootblock.txt, then compute metrics
skillbench analyze --json

# 3. Export session data for allowed workspaces
skillbench push
```

All generated output goes to `dist/` (gitignored).

## Status

See [SPEC.md](./SPEC.md) for the full design.

Customer 0: Chris Sells (Gastown Discord).

## How it fits with skillmeter

This is a **complementary data layer**, not a replacement:

| Layer | Tool | Granularity | What it tells you |
|-------|------|-------------|-------------------|
| Keystroke | skillmeter VS Code extension | Character-level | How much AI wrote vs. you |
| Session | session-collector (this repo) | Conversation-level | How effectively you direct AI |

See the "Compatibility" section in SPEC.md for integration details.
