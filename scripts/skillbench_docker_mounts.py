#!/usr/bin/env python3
"""
Generate a minimal set of Docker bind-mount args for SkillBench collect.

Why this exists:
- `skillbench.py collect` reads sessions from ~/.claude, ~/.gemini, ~/.codex...
- It also checks that each session's workspace path exists, then runs `git -C`
  and `gh repo view` against those workspaces.

So a container needs read-only access to:
1) agent session stores (handled in Makefile via fixed mounts)
2) the *workspace folders referenced by sessions* (computed here)

Output:
  A single line of `-v ...:...:ro` args suitable for shell evaluation.
"""

from __future__ import annotations

import shlex
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    # Import from repo without requiring installation.
    # Package lives under src/skillbench/ per the project layout.
    import sys

    sys.path.insert(0, str(repo_root / "src"))
    from skillbench.session_parser import scan_sessions  # noqa: E402

    scanner = scan_sessions(verbose=False)
    summaries = scanner.get_workspace_summary()

    gemini_tmp = str(Path.home() / ".gemini" / "tmp")
    mounts: list[str] = []
    seen: set[str] = set()

    for s in summaries:
        ws = s.get("workspace")
        if not ws or not isinstance(ws, str):
            continue

        # Gemini hash folders are mounted separately (fixed mount in Makefile).
        if gemini_tmp in ws:
            continue

        p = Path(ws)
        if not p.is_dir():
            continue

        host = str(p)
        if host in seen:
            continue
        seen.add(host)

        # Mount workspace into the same absolute path inside the container.
        mounts.append(f"-v {shlex.quote(host)}:{shlex.quote(host)}:ro")

    print(" ".join(mounts))


if __name__ == "__main__":
    main()

