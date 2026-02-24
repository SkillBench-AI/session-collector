# session-collector

Boot block tool + session-level analysis pipeline for SkillBench.

## What is this?

A local CLI tool that sits on top of [CASS](https://github.com/Dicklesworthstone/coding_agent_session_search) and lets users:
1. Browse their indexed coding agent sessions (Claude Code, Cursor, Copilot, Gemini, Aider, etc.)
2. Auto-classify projects by git visibility + OSS license (MIT/Apache = auto-include, proprietary/missing = exclude)
3. Review/edit an allowlist of folders to share
4. Push selected session data to SkillBench for analysis

Users get back a dashboard showing their agentic engineering metrics — processed by our server-side analysis pipeline.

## Status

**Spec phase.** See [SPEC.md](./SPEC.md) for the full design.

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
