# AGENTS.md

## Project overview
Single-file Python CLI (`skillbench.py`) that classifies coding agent workspaces by GitHub visibility + OSS license, then computes agentic engineering metrics from CASS session data.

## Architecture
- `skillbench.py` — entire CLI: scan, analyze, gather, collect, push, dashboard commands
- `session_parser.py` — parses raw agent session files (Claude, Gemini, Codex)
- `sanitizer.py` — PII/secret redaction for exports
- `skills/sanitize-export/` — skill + script to redact sensitive data from exports
- `pyproject.toml` — packaging config (pip-installable, entry point: `skillbench:main`)
- `SPEC.md` — full design spec (read-only reference, do not modify without asking)
- `dist/` — all generated output (gitignored)

**CE Engine (separate repo):** Dashboard generation, CE metrics, and recommendations
live in `SkillBench-AI/ce-engine` (private, restricted access). Imported as `ce_engine`
package. This separation protects proprietary formulas from research participant
collaborators who have access to this repo.

## Privacy model (two levels)
1. **Workspace classification (bootblock).** `skillbench scan` auto-includes only public GitHub repos with a recognized OSS license. Everything else is auto-excluded (commented out in `dist/bootblock.txt`). Users can manually override. This ensures only openly-licensed project sessions are shared by default.
2. **Content sanitization (skill-driven).** Even for allowed workspaces, conversation text may contain secrets, PII, or infrastructure details. The `sanitize-export` skill (`skills/sanitize-export/SKILL.md`) guides an AI agent to discover and redact sensitive data from the raw export before sharing. This runs between `gather` (raw export) and `push` (upload).

## Key design decisions
- **License detection uses the GitHub API** (`gh repo view --json isPrivate,licenseInfo`), not local file parsing. GitHub's Licensee gem does the matching. Do not add local regex-based license detection for GitHub repos.
- **Auto-include requires both**: public GitHub repo AND a recognized OSS license (SPDX ID ≠ NOASSERTION) detected from a LICENSE file. No exceptions.
- **Non-GitHub repos cannot auto-include.** They fall back to manifest-only detection (pyproject.toml, package.json, etc.) which is informational only.
- **One `gh` call per repo** handles both visibility and license. Do not split into separate calls.
- **Gemini CLI hash resolution**: Gemini stores sessions under `~/.gemini/tmp/{sha256(project_root)}/`. During scan, `resolve_gemini_hashes()` reverses these hashes by computing SHA-256 of all known workspace paths and merges Gemini conversations into the real project entry. This runs before classification. Additionally, `analyze` and `gather` expand each bootblock path to also query its Gemini hash equivalent (`gemini_hash_for_path()`), so Gemini sessions are captured at query time even if they weren't merged during scan.

## Commands
```
skillbench scan     # classify workspaces → dist/bootblock.txt
skillbench analyze  # compute metrics from CASS data (--json for dist/skillbench_report.json)
skillbench gather   # export sessions → dist/skillbench_export.json
skillbench push     # upload sanitized data to SkillBench API (not yet implemented)
```

After `gather`, users should sanitize the export before sharing by using the
`sanitize-export` skill (`skills/sanitize-export/SKILL.md`) with their AI agent.

## Dependencies
- Python ≥ 3.9
- `ce-engine` package (git dependency, private repo: `SkillBench-AI/ce-engine`)
- External CLIs: `git`, `gh` (GitHub CLI), `cass` (coding_agent_session_search)
- CASS SQLite database must exist (user runs `cass index --full` first)

## Testing
```bash
# Syntax check
python3 -c "import ast; ast.parse(open('skillbench.py').read())"

# Run test suite (install dev deps first)
pip install -e ".[dev]"
pytest tests/ -v
```

Tests live in `tests/` with fixtures in `tests/fixtures/`. Each agent parser
has a dedicated test class in `test_parsers.py` plus parametrized schema
validation across all parsers.

## Conventions
- All generated files go to `dist/`. Never write output to the repo root.
- Commit messages use conventional commits (feat/fix/chore).
- Include `Co-Authored-By: Warp <agent@warp.dev>` in commits.
