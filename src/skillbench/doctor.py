"""`skillbench doctor` — single health-check command.

Surfaces actionable remediation when something is wrong instead of asking
the user to run several daemon-* commands and stitch the output together
themselves. The checks are deliberately read-only: doctor never writes to
the SQLite database or to ``dist/``.
"""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .daemon import CodexDaemonStore, _candidate_roots, iter_codex_session_files


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    remediation: list[str] = field(default_factory=list)


@dataclass
class DoctorReport:
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


def _check_python() -> CheckResult:
    info = sys.version_info
    ok = (info.major, info.minor) >= (3, 9)
    detail = f"{info.major}.{info.minor}.{info.micro}"
    if ok:
        return CheckResult("Python version", True, f"{detail} (>= 3.9)")
    return CheckResult(
        "Python version",
        False,
        f"{detail} (need >= 3.9)",
        [
            "Install a newer Python (>= 3.9):",
            "  macOS:  brew install python",
            "  Linux:  sudo apt install python3",
        ],
    )


def _check_cli_installed() -> CheckResult:
    found = shutil.which("skillbench")
    if found:
        return CheckResult("skillbench CLI on PATH", True, found)

    package_dir = Path(__file__).resolve().parent.parent.parent
    return CheckResult(
        "skillbench CLI on PATH",
        False,
        "skillbench is not on PATH (running via python -m or in-tree only)",
        [
            "Install with pipx so `skillbench` works from any shell:",
            f"  pipx install {package_dir}",
            "  pipx ensurepath",
        ],
    )


def _check_codex_session_dirs(base_dir: Path | None = None) -> CheckResult:
    candidates = [
        Path.home() / ".codex",
        Path.home() / ".codex-cli",
        Path.home() / ".openai-codex",
    ]
    if base_dir is not None:
        candidates = [Path(base_dir)]

    found = [c for c in candidates if c.exists()]
    if found:
        detail = ", ".join(str(c) for c in found)
        return CheckResult("Codex session directory", True, detail)
    checked = "\n    ".join(str(c) for c in candidates)
    return CheckResult(
        "Codex session directory",
        False,
        "no Codex session root found",
        [
            "We checked these paths and none exist:",
            f"    {checked}",
            "Open Codex and start (or finish) a session, then re-run.",
            "If your Codex sessions are stored elsewhere, pass --base-dir or",
            "set codex.base_dir via `skillbench config set codex.base_dir <path>`.",
        ],
    )


def _check_codex_sessions_detected(base_dir: Path | None = None) -> CheckResult:
    files = list(iter_codex_session_files(base_dir))
    if files:
        return CheckResult(
            "Codex sessions detected",
            True,
            f"{len(files)} session file(s)",
        )
    return CheckResult(
        "Codex sessions detected",
        False,
        "no .json/.jsonl session files found",
        [
            "Run `skillbench codex locate-sessions` for a per-path breakdown.",
            "If you've never used Codex on this machine, open Codex and",
            "start a session before running `skillbench codex collect`.",
        ],
    )


def _check_database_writable(db_path: Path) -> CheckResult:
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            "Local database writable",
            False,
            f"cannot create {db_path.parent}: {exc}",
            ["Check filesystem permissions on your home directory."],
        )

    try:
        # Touching CodexDaemonStore creates/initialises the DB; if it already
        # exists this is essentially a no-op.
        store = CodexDaemonStore(db_path)
        with store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except (sqlite3.DatabaseError, OSError) as exc:
        return CheckResult(
            "Local database writable",
            False,
            f"sqlite error: {exc}",
            [
                f"Inspect or remove the corrupt database at {db_path}",
                "and re-run `skillbench doctor`.",
            ],
        )
    return CheckResult("Local database writable", True, str(db_path))


def _check_dist_writable() -> CheckResult:
    target = Path.cwd() / "dist"
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target, delete=True):
            pass
    except OSError as exc:
        return CheckResult(
            "Sanitized export directory writable",
            False,
            f"cannot write to {target}: {exc}",
            ["Run from a directory where you have write permission."],
        )
    return CheckResult("Sanitized export directory writable", True, str(target))


def _check_plugin_marketplace_repo() -> CheckResult:
    """Best-effort: detect a SkillMeter Codex marketplace checkout nearby.

    This isn't required for collection — only `codex plugin marketplace add`
    is. We surface it as informational so users running the hot path see
    where the plugin came from.
    """
    candidates = [
        Path.cwd() / "skillmeter-codex-marketplace",
        Path.cwd().parent / "skillmeter-codex-marketplace",
        Path.home() / "skillmeter-codex-marketplace",
    ]
    for candidate in candidates:
        if candidate.exists() and (candidate / ".codex-plugin").exists() or (
            candidate.exists() and (candidate / "plugins" / "skillmeter").exists()
        ):
            return CheckResult(
                "SkillMeter marketplace checkout",
                True,
                str(candidate),
            )
    return CheckResult(
        "SkillMeter marketplace checkout",
        True,  # informational only — not a hard failure
        "not found locally (will be cloned by `skillbench codex plugin-install`)",
    )


def run_doctor(
    *,
    db_path: Path | str | None = None,
    base_dir: Path | None = None,
) -> DoctorReport:
    """Run all health checks and return a structured report."""
    db_target = Path(db_path) if db_path else Path.home() / ".skillbench" / "codex_daemon.sqlite3"

    checks: list[Callable[[], CheckResult]] = [
        _check_python,
        _check_cli_installed,
        lambda: _check_codex_session_dirs(base_dir),
        lambda: _check_codex_sessions_detected(base_dir),
        lambda: _check_database_writable(db_target),
        _check_dist_writable,
        _check_plugin_marketplace_repo,
    ]

    return DoctorReport(checks=[c() for c in checks])


def render_report(report: DoctorReport) -> str:
    """Format ``report`` for stdout. Uses simple ANSI color when on a TTY."""
    use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""
    green = "\033[32m" if use_color else ""
    red = "\033[31m" if use_color else ""
    reset = "\033[0m" if use_color else ""

    lines = [
        f"SkillBench doctor — {platform.system()} ({platform.machine()})",
        "",
    ]
    for check in report.checks:
        mark = f"{green}OK {reset}" if check.ok else f"{red}FAIL{reset}"
        suffix = f" — {check.detail}" if check.detail else ""
        lines.append(f"  [{mark}] {check.name}{suffix}")
        if not check.ok:
            for note in check.remediation:
                lines.append(f"        {note}")

    lines.append("")
    if report.passed:
        lines.append(f"{green}All checks passed.{reset}")
        lines.append("Next: `skillbench codex collect --allowed-orgs <your-org>`")
    else:
        lines.append(f"{red}Some checks failed — see remediation above.{reset}")
    return "\n".join(lines)
