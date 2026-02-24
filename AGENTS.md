# AGENTS.md

## Project overview
Single-file Python CLI (`skillbench.py`) that classifies coding agent workspaces by GitHub visibility + OSS license, then computes agentic engineering metrics from CASS session data.

## Architecture
- `skillbench.py` — entire CLI: scan, analyze, push commands
- `pyproject.toml` — packaging config (pip-installable, entry point: `skillbench:main`)
- `SPEC.md` — full design spec (read-only reference, do not modify without asking)
- `dist/` — all generated output (gitignored)

## Key design decisions
- **License detection uses the GitHub API** (`gh repo view --json isPrivate,licenseInfo`), not local file parsing. GitHub's Licensee gem does the matching. Do not add local regex-based license detection for GitHub repos.
- **Auto-include requires both**: public GitHub repo AND a recognized OSS license (SPDX ID ≠ NOASSERTION) detected from a LICENSE file. No exceptions.
- **Non-GitHub repos cannot auto-include.** They fall back to manifest-only detection (pyproject.toml, package.json, etc.) which is informational only.
- **One `gh` call per repo** handles both visibility and license. Do not split into separate calls.

## Commands
```
skillbench scan     # classify workspaces → dist/bootblock.txt
skillbench analyze  # compute metrics from CASS data (--json for dist/skillbench_report.json)
skillbench push     # export sessions → dist/skillbench_export.json
```

## Dependencies
- Python ≥ 3.10 (stdlib only, no pip dependencies)
- External CLIs: `git`, `gh` (GitHub CLI), `cass` (coding_agent_session_search)
- CASS SQLite database must exist (user runs `cass index --full` first)

## Testing
No test framework yet. Validate changes with:
```bash
python3 -c "import ast; ast.parse(open('skillbench.py').read())"
```

## Conventions
- All generated files go to `dist/`. Never write output to the repo root.
- Commit messages use conventional commits (feat/fix/chore).
- Include `Co-Authored-By: Warp <agent@warp.dev>` in commits.
