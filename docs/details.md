# session-collector — details

Technical reference for the `collect` pipeline. For install instructions,
see [../README.md](../README.md). For privacy policy, see [privacy.md](privacy.md).
For using a token instead of the `gh` CLI, see [gh-token.md](gh-token.md).

## What is this?

A local CLI tool that scans your coding agent session logs and lets you:

1. Browse indexed sessions across multiple AI coding agents
2. Auto-classify projects by git visibility + OSS license
3. Compute agentic engineering metrics locally
4. Export and sanitize session data for SkillBench analysis

## How the `collect` command works

The unified `collect` pipeline enforces the two-level privacy model described
in [privacy.md](privacy.md). Both levels run entirely on your machine.

### Step 1: Scan for sessions

Reads local log files directly from known agent directories:

- Claude Code: `~/.claude/projects/`
- Gemini CLI: `~/.gemini/tmp/`
- Codex CLI: standard log locations

No external dependencies needed — no CASS, no Rust toolchain.

### Step 2: Classify workspaces

For each workspace with sessions:

1. **Skip filtering.** Drops macOS temp dirs, Cursor internal dirs, git
   worktrees, bare home directory.
2. **GitHub classification.** Uses `gh repo view` to check visibility and
   license. Falls back gracefully if `gh` is not installed.
3. **Auto-include rule.** A project is included only when it is **public on
   GitHub** AND has a **recognized OSS license**.
4. **Interactive prompt.** If no public repos qualify, you're prompted to
   select private workspaces to include. The prompt shows the detected GitHub
   org and only offers repos that match the active allowlist.

To restrict collection to specific GitHub orgs (e.g. for a pilot), pass
`--allowed-orgs`:

```bash
skillbench collect --allowed-orgs andela-technology woven-teams woven-reviews
```

### Step 3: Analyze

Computes agentic engineering metrics across three tiers:

- **Tier 1 — Usage patterns:** sessions/week, active days, session duration,
  agent diversity
- **Tier 2 — Prompting sophistication:** prompt length, context provision
  rate, multi-step rate
- **Tier 3 — Iteration efficiency:** first-attempt success, correction rate,
  avg turns

### Step 4: Export

Exports full conversation data (messages, timestamps, agents) for allowed
workspaces. Files are split by ISO week by default (`--split weekly`),
producing one file per week (e.g. `skillbench_export_sanitized_2026_W12.json`).
Use `--split session` for per-session files, or `--split none` for a single
combined file.

### Step 5: Sanitize

Runs the deterministic pattern-based sanitizer automatically. No manual
step needed.

## `collect` flags

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output` | `dist/skillbench_export_sanitized.json` | Output file path for the sanitized export |
| `-y, --yes` | off | Skip interactive confirmation prompts |
| `--split` | `weekly` | How to split export files: `weekly` (one file per ISO week), `session` (one file per session), or `none` (single combined file) |
| `--include-excluded` | off | Include workspaces from the allowed org scope even if they're private or lack an OSS license (use when no public repos qualify) |
| `--upload-guide` | off | Print upload instructions at the end of the run |
| `--allowed-orgs` | all orgs (`*`) | Space-separated list of GitHub orgs to restrict collection to. Omit to collect from all orgs. |
| `--write-report` | off | Also write `dist/skillbench_report.json` (local-only summary; **not** a shareable artifact and not expected by the upload UI) |

## Makefile knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOWED_ORGS` | (unset) | Space-separated GitHub orgs, forwarded to `--allowed-orgs`. |
| `YES` | `0` | Set `YES=1` to pass `-y` (skips all interactive prompts, including private-repo selection). |
| `INCLUDE_EXCLUDED` | `0` | Set `1` to pass `--include-excluded` (opt-in to private/unlicensed). `make docker-collect-all` is a shortcut. |
| `ALLOW_NO_GH` | `0` | Set `1` to bypass the `gh` preflight check (manual selection only). |
| `GH_TOKEN` / `GITHUB_TOKEN` | (unset) | If set, skips the preflight `gh auth` check and is forwarded into the container. See [gh-token.md](gh-token.md). |

## Advanced: Manual pipeline (CASS-based)

For power users who want fine-grained control, you can run each step
separately using the CASS-based workflow:

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

## Gemini CLI note

Gemini CLI sessions are automatically detected and attributed to the correct
project via SHA-256 hash resolution of project paths.

## How it fits with skillmeter

This is a **complementary data layer**, not a replacement:

| Layer | Tool | Granularity | What it tells you |
|-------|------|-------------|-------------------|
| Keystroke | skillmeter VS Code extension | Character-level | How much AI wrote vs. you |
| Session | session-collector (this repo) | Conversation-level | How effectively you direct AI |

## Design spec

See [../SPEC.md](../SPEC.md) for the full design.
