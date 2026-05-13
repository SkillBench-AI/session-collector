from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .sanitizer import Sanitizer
from .session_parser import (
    Conversation,
    _parse_codex_json,
    _parse_codex_jsonl,
)

DEFAULT_DAEMON_DB = Path.home() / ".skillbench" / "codex_daemon.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_roots(base_dir: Path | None = None) -> list[Path]:
    candidates = [
        Path.home() / ".codex",
        Path.home() / ".codex-cli",
        Path.home() / ".openai-codex",
    ]
    if base_dir is not None:
        candidates = [Path(base_dir)]

    roots: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        sessions_dir = candidate / "sessions"
        roots.append(sessions_dir if sessions_dir.exists() else candidate)
    return roots


def iter_codex_session_files(base_dir: Path | None = None) -> Iterator[Path]:
    """Yield likely Codex session files in stable order."""
    for root in _candidate_roots(base_dir):
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in {".jsonl", ".json"}:
                continue
            yield path


def parse_codex_session_file(path: Path) -> Conversation | None:
    """Parse a single Codex session file into a normalized conversation."""
    if path.suffix == ".jsonl":
        return _parse_codex_jsonl(path)
    if path.suffix == ".json":
        return _parse_codex_json(path)
    return None


@dataclass
class IngestResult:
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    removed: int = 0


class CodexDaemonStore:
    """SQLite-backed state store for daemonized Codex session ingestion."""

    def __init__(self, db_path: Path | str = DEFAULT_DAEMON_DB):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    source_path TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    git_remote TEXT,
                    started_at INTEGER,
                    ended_at INTEGER,
                    message_count INTEGER NOT NULL,
                    file_mtime_ns INTEGER NOT NULL,
                    file_size INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_workspace
                ON sessions(workspace);

                CREATE INDEX IF NOT EXISTS idx_sessions_session_id
                ON sessions(session_id);
                """
            )

    def get_session_record(self, source_path: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT source_path, file_mtime_ns, file_size
                FROM sessions
                WHERE source_path = ?
                """,
                (source_path,),
            ).fetchone()
        return row

    def upsert_session(self, conv: Conversation, stat_result) -> str:
        payload = conv.to_dict()
        payload_json = json.dumps(payload, sort_keys=True)
        now = _utc_now()

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE source_path = ?",
                (conv.source_path,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO sessions (
                    source_path, session_id, agent, workspace, git_remote,
                    started_at, ended_at, message_count,
                    file_mtime_ns, file_size, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    session_id = excluded.session_id,
                    agent = excluded.agent,
                    workspace = excluded.workspace,
                    git_remote = excluded.git_remote,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    message_count = excluded.message_count,
                    file_mtime_ns = excluded.file_mtime_ns,
                    file_size = excluded.file_size,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    conv.source_path,
                    conv.session_id,
                    conv.agent,
                    conv.workspace,
                    conv.git_remote,
                    conv.started_at,
                    conv.ended_at,
                    len(conv.messages),
                    stat_result.st_mtime_ns,
                    stat_result.st_size,
                    payload_json,
                    now,
                ),
            )
        return "updated" if existing else "inserted"

    def delete_missing(self, existing_paths: set[str], active_roots: list[Path] | None = None) -> int:
        with self.connect() as conn:
            rows = conn.execute("SELECT source_path FROM sessions").fetchall()
            stale = []
            for row in rows:
                source_path = row["source_path"]
                if source_path in existing_paths:
                    continue
                # Only delete if path is under one of the active scan roots
                if active_roots:
                    try:
                        resolved_path = Path(source_path).resolve()
                        resolved_roots = [root.resolve() for root in active_roots]
                        under_active_root = any(
                            resolved_path.is_relative_to(root) for root in resolved_roots
                        )
                        if not under_active_root:
                            continue
                    except (ValueError, OSError):
                        # Skip paths that can't be resolved
                        continue
                stale.append(source_path)
            for source_path in stale:
                conn.execute("DELETE FROM sessions WHERE source_path = ?", (source_path,))
        return len(stale)

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO metadata(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_metadata(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def load_sessions(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM sessions ORDER BY COALESCE(started_at, 0), session_id"
            ).fetchall()
        sessions = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            # Backfill legacy daemon rows written before full_fidelity was
            # included in parser-based exports so existing databases export
            # consistently without requiring a forced reingest.
            payload.setdefault("full_fidelity", True)
            sessions.append(payload)
        return sessions

    def status(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS session_count,
                    COUNT(DISTINCT workspace) AS workspace_count,
                    COALESCE(SUM(message_count), 0) AS message_count,
                    MAX(updated_at) AS last_ingested_at
                FROM sessions
                """
            ).fetchone()
        return {
            "db_path": str(self.path),
            "session_count": row["session_count"],
            "workspace_count": row["workspace_count"],
            "message_count": row["message_count"],
            "last_ingested_at": row["last_ingested_at"],
            "last_scan_at": self.get_metadata("last_scan_at"),
        }


def ingest_codex_sessions(
    *,
    db_path: Path | str = DEFAULT_DAEMON_DB,
    base_dir: Path | None = None,
) -> IngestResult:
    store = CodexDaemonStore(db_path)
    result = IngestResult()
    seen_paths: set[str] = set()
    active_roots = _candidate_roots(base_dir)

    for path in iter_codex_session_files(base_dir):
        result.scanned += 1
        seen_paths.add(str(path))

        try:
            stat_result = path.stat()
        except (OSError, IOError):
            result.failed += 1
            continue

        try:
            existing = store.get_session_record(str(path))
        except Exception:
            result.failed += 1
            continue

        if (
            existing is not None
            and existing["file_mtime_ns"] == stat_result.st_mtime_ns
            and existing["file_size"] == stat_result.st_size
        ):
            result.unchanged += 1
            continue

        try:
            conv = parse_codex_session_file(path)
        except Exception:
            result.failed += 1
            continue

        if conv is None or not conv.messages:
            result.failed += 1
            continue

        action = store.upsert_session(conv, stat_result)
        if action == "inserted":
            result.inserted += 1
        else:
            result.updated += 1

    result.removed = store.delete_missing(seen_paths, active_roots)
    store.set_metadata("last_scan_at", _utc_now())
    return result


def run_codex_daemon(
    *,
    db_path: Path | str = DEFAULT_DAEMON_DB,
    base_dir: Path | None = None,
    interval_seconds: float = 30.0,
    iterations: int | None = None,
) -> list[IngestResult]:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    results: list[IngestResult] = []
    loops = iterations if iterations is not None else -1
    iteration = 0
    while loops == -1 or iteration < loops:
        results.append(ingest_codex_sessions(db_path=db_path, base_dir=base_dir))
        iteration += 1
        if loops != -1 and iteration >= loops:
            break
        time.sleep(interval_seconds)
    return results


def export_daemon_sessions(
    *,
    db_path: Path | str = DEFAULT_DAEMON_DB,
    output_path: Path | str,
    allowed_orgs: list[str] | None = None,
    sanitize: bool = True,
) -> dict:
    from . import PILOT_ALLOWED_GITHUB_ORGS, get_repo_scope_decision

    store = CodexDaemonStore(db_path)
    sessions = store.load_sessions()
    allowed_orgs = allowed_orgs or PILOT_ALLOWED_GITHUB_ORGS.copy()

    filtered = [
        session
        for session in sessions
        if get_repo_scope_decision(session.get("git_remote"), allowed_orgs)["allowed"]
    ]

    output_sessions = filtered
    sanitizer = None
    if sanitize:
        sanitizer = Sanitizer()
        output_sessions = sanitizer.sanitize_export(filtered)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output_sessions, indent=2) + "\n")

    return {
        "output_path": str(output_path),
        "session_count": len(output_sessions),
        "sanitized": sanitize,
        "redactions": dict(sanitizer.stats) if sanitizer else {},
    }
