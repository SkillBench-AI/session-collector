# session-collector

Collect and analyze your AI coding sessions locally. See how you work with AI — metrics, patterns, and insights — without any data leaving your machine until you choose to share.

## Quick start

```bash
# Install
pip install -e .

# Collect, analyze, and sanitize — one command
skillbench collect
```

That's it. The `collect` command will:
1. **Scan** for coding agent sessions on your machine (Claude Code, Gemini CLI, Codex CLI)
2. **Classify** your workspaces — only public, OSS-licensed repos are included by default
3. **Analyze** your sessions and compute agentic engineering metrics
4. **Export** your session data
5. **Sanitize** the export automatically (redacts API keys, emails, private IPs, home paths, secrets)

Output goes to `dist/`. The sanitized export is `dist/skillbench_export_sanitized.json`.

### Share your results

Upload `dist/skillbench_export_sanitized.json` to the shared Google Drive folder provided by the SkillBench team. No raw or unsanitized data — only the scrubbed export.

### Prerequisites

- **Python 3.10+**
- **GitHub CLI (`gh`)** — used to check repo visibility and licensing. Install: `brew install gh` (macOS) or see [cli.github.com](https://cli.github.com/). If `gh` is not installed, workspaces are classified as private by default (safe fallback).

### Docker alternative

If you hit environment issues (Xcode, Rust conflicts, etc.):

```bash
make docker-collect      # public/OSS repos only
make docker-collect-all  # include private repos too
```

## Privacy & data policy

**All processing is local.** No data is sent anywhere unless you explicitly share the output file.

### Two-level privacy model

**Level 1 — Workspace filtering:** Only public GitHub repos with recognized OSS licenses are auto-included. Private repos, unlicensed projects, and non-GitHub repos are excluded by default. If no public repos are found, you'll be prompted to select private workspaces interactively.

**Level 2 — Content sanitization:** The export is automatically scrubbed using deterministic pattern matching. Redacted patterns include:
- API keys and tokens (AWS, GitHub, Anthropic, OpenAI, Slack, Stripe, etc.)
- Email addresses
- Private IP addresses
- Home directory paths (replaced with `~`)
- SSH keys, connection strings, passwords, bearer tokens
- `.env` file secrets

A summary of what was redacted is printed after collection.

### Additional guarantees
- **No telemetry, no auto-sync.** No background uploads, no analytics.
- **Network calls:** Only `gh` (GitHub CLI) during classification, and only for repos that have a GitHub remote.
- **You control what's shared.** Review `dist/skillbench_export_sanitized.json` before uploading.

### Agent fidelity

Full session fidelity (complete tool payloads, code diffs, command outputs) is currently supported for **Claude Code** sessions. Other agents (Gemini CLI, Codex CLI) are included with summary-level data (messages and metadata, tool blocks stubbed).

## What is this?

A local CLI tool that scans your coding agent session logs and lets you:
1. Browse indexed sessions across multiple AI coding agents
2. Auto-classify projects by git visibility + OSS license
3. Compute agentic engineering metrics locally
4. Export and sanitize session data for SkillBench analysis

## How the `collect` command works

The unified `collect` pipeline enforces a **two-level privacy model**: Level 1 filters *which projects* are included (workspace classification), and Level 2 filters *what content* is safe to share (automatic sanitization). Both levels run entirely on your machine.

### Step 1: Scan for sessions

Reads local log files directly from known agent directories:
- Claude Code: `~/.claude/projects/`
- Gemini CLI: `~/.gemini/tmp/`
- Codex CLI: standard log locations

No external dependencies needed — no CASS, no Rust toolchain.

### Step 2: Classify workspaces

For each workspace with sessions:

1. **Skip filtering.** Drops macOS temp dirs, Cursor internal dirs, git worktrees, bare home directory.
2. **GitHub classification.** Uses `gh repo view` to check visibility and license. Falls back gracefully if `gh` is not installed.
3. **Auto-include rule.** A project is included only when it is **public on GitHub** AND has a **recognized OSS license**.
4. **Interactive prompt.** If no public repos qualify, you're prompted to select private workspaces to include.

### Step 3: Analyze

Computes agentic engineering metrics across three tiers:
- **Tier 1 — Usage patterns:** sessions/week, active days, session duration, agent diversity
- **Tier 2 — Prompting sophistication:** prompt length, context provision rate, multi-step rate
- **Tier 3 — Iteration efficiency:** first-attempt success, correction rate, avg turns

### Step 4: Export

Exports full conversation data (messages, timestamps, agents) for allowed workspaces.

### Step 5: Sanitize

Runs the deterministic pattern-based sanitizer automatically. No manual step needed.

## Advanced: Manual pipeline (CASS-based)

For power users who want fine-grained control, you can run each step separately using the CASS-based workflow:

```bash
# Prerequisites: install CASS and index your sessions
cargo install cass
cass index --full

# Install skillbench
pip install -e .

# 1. Scan and classify → dist/bootblock.txt
skillbench scan

# 2. Review/edit dist/bootblock.txt, then compute metrics
skillbench analyze --json

# 3. Export session data
skillbench gather

# 4. Sanitize (automated — or use skills/sanitize-export/SKILL.md for AI-driven)
# The deterministic sanitizer in sanitizer.py handles most cases.

# 5. Upload (not yet implemented)
# skillbench push
```

### Gemini CLI note

Gemini CLI sessions are automatically detected and attributed to the correct project via SHA-256 hash resolution of project paths.

## How it fits with skillmeter

This is a **complementary data layer**, not a replacement:

| Layer | Tool | Granularity | What it tells you |
|-------|------|-------------|-------------------|
| Keystroke | skillmeter VS Code extension | Character-level | How much AI wrote vs. you |
| Session | session-collector (this repo) | Conversation-level | How effectively you direct AI |

## Status

See [SPEC.md](./SPEC.md) for the full design.
