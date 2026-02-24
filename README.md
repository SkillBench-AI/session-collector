# session-collector

Boot block tool + session-level analysis pipeline for SkillBench.

## What is this?

A local CLI tool that sits on top of [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) and lets users:
1. Browse their indexed coding agent sessions (Claude Code, Cursor, Copilot, Gemini, Aider, etc.)
2. Auto-classify projects by git visibility + OSS license
3. Review/edit an allowlist of folders to share
4. Compute agentic engineering metrics locally
5. Export selected session data for SkillBench analysis

## How it works

### 1. `skillbench scan` — classify workspaces

Queries the CASS SQLite database for all indexed workspace paths, then classifies each one:

1. **Gemini hash resolution.** Gemini CLI stores sessions in `~/.gemini/tmp/{sha256(project_path)}/` instead of the project directory. CASS indexes these as workspace paths, so Gemini conversations appear under opaque hash directories. Before classification, `scan` reverses the hashes by computing SHA-256 for every known real workspace path, matches them to Gemini entries, and merges conversation counts, agent lists, and timestamps into the real project entries. Unresolved hashes (Gemini-only projects with no other agent session) are skipped.

2. **Skip filtering.** Paths matching known non-project patterns are dropped: macOS temp dirs (`/private/var/folders/`), Cursor internal dirs, git worktrees, bare home directory, and any remaining `.gemini/` paths.

3. **GitHub classification.** For each surviving path, a single `gh repo view --json isPrivate,licenseInfo` call checks both visibility and license. GitHub's built-in [Licensee](https://github.com/licensee/licensee) gem matches the LICENSE file against known OSS licenses and returns an SPDX ID.

4. **Auto-include rule.** A project is auto-included only when **both** conditions are met:
   - The repo is **public on GitHub**
   - GitHub detects a **recognized OSS license** (SPDX key ≠ `other`)

   Projects that fail either check are excluded by default but can be manually uncommented in the bootblock.

5. **Output.** Writes `dist/bootblock.txt` — an editable allowlist of project paths with inline comments showing license, agents used, and conversation counts.

### 2. `skillbench analyze` — compute metrics

Reads the bootblock for allowed paths, then queries CASS for conversations and messages in those workspaces. For each allowed path, the query also includes its Gemini hash equivalent (`~/.gemini/tmp/{sha256(path)}`) so that Gemini CLI sessions are captured alongside sessions from other agents.

Computes agentic engineering metrics across three tiers:
- **Tier 1 — Usage patterns:** sessions/week, active days, session duration, agent diversity
- **Tier 2 — Prompting sophistication:** prompt length, context provision rate, multi-step rate
- **Tier 3 — Iteration efficiency:** first-attempt success, correction rate, avg turns

Places you on an agentic engineering ladder (L1 Dabbler → L5 Maestro) with personalized level-up suggestions. Use `--json` to also write `dist/skillbench_report.json`.

### 3. `skillbench push` — export session data

Same bootblock + Gemini hash expansion as `analyze`. Exports full conversation data (messages, timestamps, agents) for allowed workspaces to `dist/skillbench_export.json`.

## Quick start

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

### Gemini CLI note

Gemini CLI sessions are automatically detected and attributed to the correct project. The hash resolution in `scan` and the query expansion in `analyze`/`push` both use `gemini_hash_for_path()` — a SHA-256 of the absolute project path, matching Gemini CLI's own `getProjectHash()` implementation.

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
