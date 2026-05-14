"""Tests for `skillbench doctor`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skillbench  # noqa: E402
from skillbench.doctor import render_report, run_doctor  # noqa: E402


FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _copy_fixture(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    return dest


def test_doctor_passes_when_codex_sessions_exist(tmp_path):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "basic_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "basic.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"

    report = run_doctor(db_path=db_path, base_dir=base_dir)

    by_name = {c.name: c for c in report.checks}
    assert by_name["Python version"].ok
    assert by_name["Codex session directory"].ok
    assert by_name["Codex sessions detected"].ok
    assert by_name["Local database writable"].ok
    rendered = render_report(report)
    assert "Codex session directory" in rendered


def test_doctor_reports_missing_sessions_with_remediation(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    db_path = tmp_path / "daemon.sqlite3"

    report = run_doctor(db_path=db_path, base_dir=empty_dir)
    by_name = {c.name: c for c in report.checks}

    sessions_check = by_name["Codex sessions detected"]
    assert sessions_check.ok is False
    rendered = render_report(report)
    assert "FAIL" in rendered
    assert "skillbench codex locate-sessions" in rendered


def test_doctor_main_exits_nonzero_on_failure(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    db_path = tmp_path / "daemon.sqlite3"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "doctor",
            "--db",
            str(db_path),
            "--base-dir",
            str(empty_dir),
        ],
    ):
        try:
            skillbench.main()
        except SystemExit as exc:
            assert exc.code == 1
        else:  # pragma: no cover - safety net
            raise AssertionError("doctor should exit non-zero when sessions are missing")
