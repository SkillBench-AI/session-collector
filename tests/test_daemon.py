import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skillbench  # noqa: E402
from skillbench.daemon import (  # noqa: E402
    CodexDaemonStore,
    export_daemon_sessions,
    ingest_codex_sessions,
    run_codex_daemon,
)


FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def _copy_fixture(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text())
    return dest


def test_ingest_codex_sessions_tracks_insert_update_and_unchanged(tmp_path):
    base_dir = tmp_path / ".codex"
    session_file = _copy_fixture(
        FIXTURES / "basic_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "basic.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"

    first = ingest_codex_sessions(db_path=db_path, base_dir=base_dir)
    assert (first.scanned, first.inserted, first.updated, first.unchanged) == (1, 1, 0, 0)

    store = CodexDaemonStore(db_path)
    status = store.status()
    assert status["session_count"] == 1
    assert status["workspace_count"] == 1
    assert status["message_count"] == 2

    second = ingest_codex_sessions(db_path=db_path, base_dir=base_dir)
    assert (second.scanned, second.inserted, second.updated, second.unchanged) == (1, 0, 0, 1)

    session_file.write_text(session_file.read_text() + "\n")
    third = ingest_codex_sessions(db_path=db_path, base_dir=base_dir)
    assert third.updated == 1
    assert third.unchanged == 0


def test_ingest_codex_sessions_counts_failures_for_malformed_files(tmp_path):
    base_dir = tmp_path / ".codex"
    broken = base_dir / "sessions" / "2026" / "04" / "broken.jsonl"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("NOT VALID JSON\n{{{{\n")
    db_path = tmp_path / "daemon.sqlite3"

    result = ingest_codex_sessions(db_path=db_path, base_dir=base_dir)
    assert result.scanned == 1
    assert result.failed == 1

    store = CodexDaemonStore(db_path)
    assert store.status()["session_count"] == 0


def test_run_codex_daemon_multiple_iterations(tmp_path):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "basic_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "basic.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"

    results = run_codex_daemon(
        db_path=db_path,
        base_dir=base_dir,
        interval_seconds=0.001,
        iterations=2,
    )

    assert len(results) == 2
    assert results[0].inserted == 1
    assert results[1].unchanged == 1


def test_export_daemon_sessions_filters_by_allowed_orgs_and_sanitizes(tmp_path):
    base_dir = tmp_path / ".codex"
    approved = _copy_fixture(
        FIXTURES / "live_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "approved.jsonl",
    )
    blocked = base_dir / "sessions" / "2026" / "04" / "blocked.jsonl"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text(
        approved.read_text().replace(
            "git@github.com:skillbench-ai/live-codex.git",
            "git@github.com:someone/private-repo.git",
        )
    )
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "sanitized.json"

    ingest_codex_sessions(db_path=db_path, base_dir=base_dir)
    result = export_daemon_sessions(
        db_path=db_path,
        output_path=output_path,
        allowed_orgs=["skillbench-ai"],
        sanitize=True,
    )

    export = json.loads(output_path.read_text())
    assert result["session_count"] == 1
    assert len(export) == 1
    assert export[0]["full_fidelity"] is True
    assert export[0]["git_remote"] == "git@github.com:skillbench-ai/live-codex.git"


def test_export_daemon_sessions_backfills_full_fidelity_for_legacy_records(tmp_path):
    db_path = tmp_path / "daemon.sqlite3"
    store = CodexDaemonStore(db_path)
    legacy_payload = {
        "session_id": "legacy-codex-session",
        "agent": "codex",
        "workspace": "/tmp/project",
        "git_remote": "git@github.com:skillbench-ai/legacy.git",
        "source_path": "~/.codex/sessions/legacy.jsonl",
        "started_at": 1,
        "ended_at": 2,
        "messages": [
            {
                "role": "assistant",
                "created_at": "2026-04-21T13:33:21+00:00",
                "content": [{"type": "text", "text": "legacy"}],
            }
        ],
    }
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (
                source_path, session_id, agent, workspace, git_remote,
                started_at, ended_at, message_count,
                file_mtime_ns, file_size, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_payload["source_path"],
                legacy_payload["session_id"],
                legacy_payload["agent"],
                legacy_payload["workspace"],
                legacy_payload["git_remote"],
                legacy_payload["started_at"],
                legacy_payload["ended_at"],
                len(legacy_payload["messages"]),
                1,
                1,
                json.dumps(legacy_payload),
                "2026-05-06T00:00:00+00:00",
            ),
        )

    output_path = tmp_path / "legacy-export.json"
    result = export_daemon_sessions(
        db_path=db_path,
        output_path=output_path,
        allowed_orgs=["skillbench-ai"],
        sanitize=False,
    )

    export = json.loads(output_path.read_text())
    assert result["session_count"] == 1
    assert export[0]["full_fidelity"] is True


def test_main_routes_daemon_commands(tmp_path, capsys):
    base_dir = tmp_path / ".codex"
    _copy_fixture(
        FIXTURES / "basic_session.jsonl",
        base_dir / "sessions" / "2026" / "04" / "basic.jsonl",
    )
    db_path = tmp_path / "daemon.sqlite3"
    output_path = tmp_path / "daemon-export.json"

    with patch.object(
        sys,
        "argv",
        [
            "skillbench",
            "daemon-scan",
            "--db",
            str(db_path),
            "--base-dir",
            str(base_dir),
        ],
    ):
        skillbench.main()

    with patch.object(
        sys,
        "argv",
        ["skillbench", "daemon-status", "--db", str(db_path)],
    ):
        skillbench.main()

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
        skillbench.main()

    output = capsys.readouterr().out
    assert "Codex daemon ingest complete." in output
    assert "Codex daemon status" in output
    assert "Exported 1 session(s)" in output
    assert output_path.exists()

    export = json.loads(output_path.read_text())
    assert export[0]["full_fidelity"] is True
