"""User-friendly `skillbench codex ...` workflow surface.

This module wraps the existing daemon primitives in a vocabulary that is
easier on end users than ``daemon-scan`` / ``daemon-run`` / ``daemon-export``,
and adds three new helpers:

* ``codex collect`` — happy-path: scan + run-once + sanitized export.
* ``codex locate-sessions`` — diagnose ``scanned=0`` (no Codex sessions found).
* ``codex plugin-install`` — clone/refresh the marketplace and run the
  Codex CLI ``plugin marketplace add`` command for the user.

The module does not own argparse parsing. The CLI in ``skillbench/__init__.py``
builds the parser and forwards an ``args`` namespace into these handlers so
the surface stays consistent with every other ``skillbench`` command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .daemon import (
    CodexDaemonStore,
    _candidate_roots,
    export_daemon_sessions,
    iter_codex_session_files,
    ingest_codex_sessions,
)


# ---------------------------------------------------------------------------
# locate-sessions
# ---------------------------------------------------------------------------


@dataclass
class _RootProbe:
    path: Path
    exists: bool
    session_count: int


def probe_codex_roots(base_dir: Path | None = None) -> list[_RootProbe]:
    """Inspect well-known Codex session roots and report status.

    When ``base_dir`` is provided, we restrict the probe to that single
    path so the diagnostic matches what the daemon would actually scan.
    Otherwise we check every well-known location so users hitting
    ``scanned=0`` can see exactly which paths exist on their machine.
    """
    if base_dir is not None:
        candidates = [Path(base_dir)]
    else:
        candidates = [
            Path.home() / ".codex",
            Path.home() / ".codex-cli",
            Path.home() / ".openai-codex",
        ]

    probes: list[_RootProbe] = []
    for candidate in candidates:
        sessions_dir = candidate / "sessions"
        scan_root = sessions_dir if sessions_dir.exists() else candidate
        if candidate.exists():
            count = sum(
                1
                for p in scan_root.rglob("*")
                if p.is_file() and p.suffix in {".json", ".jsonl"}
            )
            probes.append(_RootProbe(path=candidate, exists=True, session_count=count))
        else:
            probes.append(_RootProbe(path=candidate, exists=False, session_count=0))
    return probes


def cmd_codex_locate_sessions(args) -> int:
    """Print a friendly per-path probe for users hitting ``scanned=0``."""
    base_dir = Path(args.base_dir) if getattr(args, "base_dir", None) else None
    probes = probe_codex_roots(base_dir)

    print("Codex session root probe:")
    print()
    total = 0
    for probe in probes:
        if probe.exists:
            label = "found"
            detail = f"{probe.session_count} session file(s)"
            total += probe.session_count
        else:
            label = "not found"
            detail = ""
        print(f"  {str(probe.path):<40}  {label}{(' — ' + detail) if detail else ''}")

    print()
    if total > 0:
        print(f"Total session files: {total}")
        print("Next: `skillbench codex collect --allowed-orgs <your-org>`")
        return 0

    print("No Codex sessions found in any known location.")
    print()
    print("Possible causes:")
    print("  - Codex has not created local sessions yet (open Codex and start one).")
    print("  - You're running under a different user account than the one that uses Codex.")
    print("  - Your platform stores sessions under a non-standard path.")
    print()
    print("You can point the daemon at a custom directory:")
    print("  skillbench codex scan --base-dir /path/to/codex/sessions")
    print("  skillbench config set codex.base_dir /path/to/codex/sessions")
    return 1


# ---------------------------------------------------------------------------
# collect (happy path)
# ---------------------------------------------------------------------------


@dataclass
class CollectResult:
    scanned: int
    inserted: int
    updated: int
    unchanged: int
    failed: int
    removed: int
    exported: int
    output_path: str
    sanitized: bool
    redactions: dict[str, int]


def run_codex_collect(
    *,
    db_path: Path | None,
    base_dir: Path | None,
    allowed_orgs: list[str],
    output_path: Path | None,
    sanitize: bool,
) -> CollectResult:
    """Compose ingest + export so users only run one command."""
    from . import normalize_allowed_orgs

    db_path = db_path or Path.home() / ".skillbench" / "codex_daemon.sqlite3"
    output_path = output_path or Path("dist") / "skillbench_daemon_export_sanitized.json"
    if not sanitize and output_path.name == "skillbench_daemon_export_sanitized.json":
        output_path = output_path.with_name("skillbench_daemon_export.json")

    ingest = ingest_codex_sessions(db_path=db_path, base_dir=base_dir)
    export = export_daemon_sessions(
        db_path=db_path,
        output_path=output_path,
        allowed_orgs=normalize_allowed_orgs(allowed_orgs) or None,
        sanitize=sanitize,
    )
    return CollectResult(
        scanned=ingest.scanned,
        inserted=ingest.inserted,
        updated=ingest.updated,
        unchanged=ingest.unchanged,
        failed=ingest.failed,
        removed=ingest.removed,
        exported=export["session_count"],
        output_path=export["output_path"],
        sanitized=export["sanitized"],
        redactions=export.get("redactions", {}),
    )


def cmd_codex_collect(args) -> int:
    """Implements ``skillbench codex collect``."""
    db_path = Path(args.db) if getattr(args, "db", None) else None
    base_dir = Path(args.base_dir) if getattr(args, "base_dir", None) else None
    output_path = Path(args.output) if getattr(args, "output", None) else None
    sanitize = not getattr(args, "raw", False)

    if not sanitize and not _raw_export_acknowledged(args):
        _print_raw_export_warning()
        return 2

    result = run_codex_collect(
        db_path=db_path,
        base_dir=base_dir,
        allowed_orgs=list(args.allowed_orgs or []),
        output_path=output_path,
        sanitize=sanitize,
    )

    print("SkillBench Codex collection complete.")
    print()
    print(f"  Scanned sessions:  {result.scanned}")
    print(
        f"  Daemon updates:    inserted={result.inserted} "
        f"updated={result.updated} unchanged={result.unchanged} "
        f"removed={result.removed} failed={result.failed}"
    )
    print(f"  Exported sessions: {result.exported}")
    print()
    label = "Sanitized" if result.sanitized else "Raw"
    print(f"  {label} export written to:")
    print(f"    {result.output_path}")
    if result.sanitized and result.redactions:
        print()
        print("  Redactions:")
        for name, count in sorted(result.redactions.items(), key=lambda kv: -kv[1]):
            print(f"    {name}: {count}")

    if result.scanned == 0:
        print()
        print("Heads up: scanned=0 — run `skillbench codex locate-sessions` for help.")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Raw export safety
# ---------------------------------------------------------------------------


RAW_OPT_IN_FLAG = "--i-understand-this-may-include-sensitive-data"
RAW_OPT_IN_ENV = "SKILLBENCH_ALLOW_RAW_EXPORT"


def _raw_export_acknowledged(args) -> bool:
    if getattr(args, "i_understand_this_may_include_sensitive_data", False):
        return True
    return os.environ.get(RAW_OPT_IN_ENV, "").lower() in {"1", "true", "yes"}


def _print_raw_export_warning() -> None:
    print(
        "Refusing to write a raw (un-sanitized) Codex export.\n"
        "\n"
        "Raw exports may contain secrets, API keys, PII, internal URLs,\n"
        "and complete tool/command output captured by Codex.\n"
        "\n"
        "If you really need a raw export, opt in explicitly:\n"
        f"  skillbench codex export --raw {RAW_OPT_IN_FLAG}\n"
        f"  {RAW_OPT_IN_ENV}=1 skillbench daemon-export --raw\n",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# plugin-install
# ---------------------------------------------------------------------------


DEFAULT_MARKETPLACE_REPO = "https://github.com/SkillBench-AI/skillmeter-codex-marketplace.git"


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _run(cmd: list[str], *, dry_run: bool) -> int:
    print("  $ " + " ".join(cmd))
    if dry_run:
        return 0
    completed = subprocess.run(cmd)
    return completed.returncode


def cmd_codex_plugin_install(args) -> int:
    """Clone/update the marketplace and register it with Codex."""
    repo_url = getattr(args, "marketplace_repo", None) or DEFAULT_MARKETPLACE_REPO
    target_root = Path(args.target_dir).expanduser() if getattr(args, "target_dir", None) else Path.cwd()
    target = target_root / "skillmeter-codex-marketplace"
    dry_run = bool(getattr(args, "dry_run", False))

    if _which("git") is None:
        print("ERROR: git is not on PATH. Install git and re-run.", file=sys.stderr)
        return 1

    codex_cli = _which("codex")
    if codex_cli is None and not dry_run:
        print(
            "ERROR: `codex` CLI not on PATH. Install Codex and re-run, or use --dry-run\n"
            "       to print the commands without executing them.",
            file=sys.stderr,
        )
        return 1

    print(f"SkillMeter Codex marketplace target: {target}")
    if target.exists():
        print("  Marketplace checkout exists — fetching latest changes")
        rc = _run(["git", "-C", str(target), "fetch", "--depth", "1", "origin"], dry_run=dry_run)
        if rc == 0:
            rc = _run(
                ["git", "-C", str(target), "reset", "--hard", "origin/HEAD"],
                dry_run=dry_run,
            )
        if rc != 0:
            print("WARN: git update failed; continuing with existing checkout", file=sys.stderr)
    else:
        print("  Cloning marketplace…")
        target_root.mkdir(parents=True, exist_ok=True)
        rc = _run(["git", "clone", "--depth", "1", repo_url, str(target)], dry_run=dry_run)
        if rc != 0:
            print("ERROR: git clone failed.", file=sys.stderr)
            return rc

    print()
    print("Registering with Codex…")
    rc = _run(["codex", "plugin", "marketplace", "add", str(target)], dry_run=dry_run)
    if rc != 0:
        print(
            "WARN: `codex plugin marketplace add` returned a non-zero exit code.\n"
            "      You can still install SkillMeter manually via `/plugins`.",
            file=sys.stderr,
        )

    print()
    print("Next steps:")
    print("  1. Open Codex")
    print("  2. Run /plugins")
    print("  3. Install SkillMeter from the SkillBench marketplace")
    print("  4. Start a fresh Codex thread")
    print("  5. Try `@skillmeter check my collector status`")
    return 0
