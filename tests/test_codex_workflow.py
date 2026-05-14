"""Tests for the user-friendly `skillbench codex ...` surface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skillbench  # noqa: E402
from skillbench.codex_workflow import (  # noqa: E402
    cmd_codex_locate_sessions,
    probe_codex_roots,
    run_codex_collect,
)


FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _copy_fixture(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    return dest


def test_run_codex_collect_writes_sanitized_export(tmp_path):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "live_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "live.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "sanitized.json"

    result = run_codex_collect(
        db_path=db_path,
        base_dir=base_dir,
        allowed_orgs=["skillbench-ai"],
        output_path=output_path,
        sanitize=True,
    )

    assert result.scanned == 1
    assert result.exported >= 1
    assert result.sanitized is True
    payload = json.loads(output_path.read_text())
    assert isinstance(payload, list)
    assert payload[0]["full_fidelity"] is True


def test_codex_collect_command_writes_export(tmp_path, capsys):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "live_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "live.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "sanitized.json"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "codex",
            "collect",
            "--db",
            str(db_path),
            "--base-dir",
            str(base_dir),
            "--output",
            str(output_path),
            "--allowed-orgs",
            "skillbench-ai",
        ],
    ):
        skillbench.main()

    out = capsys.readouterr().out
    assert "SkillBench Codex collection complete." in out
    assert output_path.exists()


def test_codex_collect_warns_when_no_sessions(tmp_path, capsys):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "sanitized.json"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "codex",
            "collect",
            "--db",
            str(db_path),
            "--base-dir",
            str(empty_dir),
            "--output",
            str(output_path),
            "--allowed-orgs",
            "skillbench-ai",
        ],
    ):
        try:
            skillbench.main()
        except SystemExit as exc:
            assert exc.code == 1
        else:  # pragma: no cover
            raise AssertionError("codex collect should exit non-zero on scanned=0")

    out = capsys.readouterr().out
    assert "scanned=0" in out
    assert "locate-sessions" in out


class _Args:
    """Minimal stand-in for argparse.Namespace used by cmd_* helpers."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_codex_locate_sessions_reports_total(tmp_path, capsys):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "basic_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "basic.jsonl",
    )

    rc = cmd_codex_locate_sessions(_Args(base_dir=str(base_dir)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total session files: 1" in out


def test_codex_locate_sessions_returns_error_when_empty(tmp_path, capsys):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    rc = cmd_codex_locate_sessions(_Args(base_dir=str(empty_dir)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "No Codex sessions found" in out


def test_raw_export_blocked_without_acknowledgment(tmp_path, capsys):
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "raw.json"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "daemon-export",
            "--db",
            str(db_path),
            "--output",
            str(output_path),
            "--raw",
        ],
    ):
        try:
            skillbench.main()
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover
            raise AssertionError("raw export must require explicit acknowledgment")

    err = capsys.readouterr().err
    assert "Refusing to write a raw" in err
    assert not output_path.exists()


def test_raw_export_allowed_via_env_var(tmp_path, monkeypatch):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "live_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "live.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "raw.json"

    # Seed the daemon DB so daemon-export has sessions to write.
    from skillbench.daemon import ingest_codex_sessions

    ingest_codex_sessions(db_path=db_path, base_dir=base_dir)

    monkeypatch.setenv("SKILLBENCH_ALLOW_RAW_EXPORT", "1")
    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "daemon-export",
            "--db",
            str(db_path),
            "--output",
            str(output_path),
            "--raw",
            "--allowed-orgs",
            "skillbench-ai",
        ],
    ):
        skillbench.main()

    assert output_path.exists()


def test_codex_plugin_install_dry_run_prints_commands(tmp_path, capsys):
    target_root = tmp_path / "marketplace-root"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "codex",
            "plugin-install",
            "--target-dir",
            str(target_root),
            "--dry-run",
        ],
    ):
        skillbench.main()

    out = capsys.readouterr().out
    assert "git clone" in out
    assert "codex plugin marketplace add" in out
    assert "Next steps:" in out


def test_codex_aliases_route_to_daemon_commands(tmp_path, capsys):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "basic_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "basic.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "codex",
            "scan",
            "--db",
            str(db_path),
            "--base-dir",
            str(base_dir),
        ],
    ):
        skillbench.main()
    assert "Codex daemon ingest complete." in capsys.readouterr().out

    with patch.object(
        sys,
        "argv",
        ["skillbench", "codex", "status", "--db", str(db_path)],
    ):
        skillbench.main()
    assert "Codex daemon status" in capsys.readouterr().out
