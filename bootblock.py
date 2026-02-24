#!/usr/bin/env python3
"""
SkillBench Boot Block Tool

Sits on top of CASS (coding_agent_session_search) to let users select
which project sessions to share with SkillBench for analysis.

Commands:
  scan   - Scan CASS index, check git/license, generate bootblock.txt
  export - Read bootblock.txt, extract sessions from CASS DB → sessions.json

Requires:
  - Python 3.9+
  - CASS installed and indexed (`cass index --full`)
  - git CLI
  - gh CLI (optional, for public/private detection of GitHub repos)
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# --- CASS database discovery ---

CASS_REQUIRED_TABLES = {"conversations", "messages", "workspaces", "agents"}


def find_cass_db() -> Optional[Path]:
    """Find the CASS SQLite database."""
    # Check env var first
    data_dir = os.environ.get("CASS_DATA_DIR")
    if data_dir:
        db = Path(data_dir) / "agent_search.db"
        if db.exists():
            return db

    # Platform-specific defaults
    system = platform.system()
    if system == "Darwin":
        candidates = [
            Path.home() / "Library" / "Application Support" / "coding-agent-search" / "coding-agent-search" / "agent_search.db",
            Path.home() / "Library" / "Application Support" / "coding-agent-search" / "agent_search.db",
        ]
    elif system == "Linux":
        candidates = [
            Path.home() / ".local" / "share" / "coding-agent-search" / "agent_search.db",
        ]
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates = [
                Path(appdata) / "coding-agent-search" / "coding-agent-search" / "agent_search.db",
            ]
        else:
            candidates = []
    else:
        candidates = []

    # Also check ./data (CASS fallback)
    candidates.append(Path("data") / "agent_search.db")

    for path in candidates:
        if path.exists():
            return path

    return None


def validate_cass_schema(db_path: Path) -> Optional[str]:
    """Check that the CASS DB has the tables we expect. Returns error message or None."""
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
    except sqlite3.Error as e:
        return f"Could not read database: {e}"

    missing = CASS_REQUIRED_TABLES - tables
    if missing:
        return (
            f"CASS database is missing expected tables: {', '.join(sorted(missing))}. "
            f"Found tables: {', '.join(sorted(tables))}. "
            f"Your version of CASS may use a different schema. "
            f"Try running: cass index --full"
        )
    return None


def open_cass_db(db_path: Path) -> sqlite3.Connection:
    """Open CASS DB with proper settings for concurrent access."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL mode allows reading while CASS might be writing
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            print("ERROR: CASS database is locked.", file=sys.stderr)
            print("CASS may be indexing. Wait for it to finish, or stop cass and retry.", file=sys.stderr)
            sys.exit(1)
        raise


# --- Dependency checks ---

def check_gh_cli() -> bool:
    """Check if gh CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# --- Git remote detection ---

def get_git_remote_url(folder: str) -> Optional[str]:
    """Get the origin remote URL for a git repo, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", folder, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def extract_github_owner_repo(remote_url: str) -> Optional[str]:
    """Extract owner/repo from a GitHub remote URL, or None."""
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", remote_url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def is_github_public(remote_url: str) -> Optional[bool]:
    """Check if a GitHub repo is public using gh CLI. Returns None if not determinable."""
    owner_repo = extract_github_owner_repo(remote_url)
    if not owner_repo:
        return None

    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}", "--jq", ".private"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() == "false"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


# --- License detection ---

LICENSE_FILES = [
    "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.rst",
    "LICENCE", "LICENCE.md", "LICENCE.txt",
    "COPYING", "COPYING.md", "COPYING.txt",
]

OSS_PATTERNS = {
    "MIT": [r"MIT License", r"Permission is hereby granted, free of charge"],
    "Apache-2.0": [r"Apache License", r"Version 2\.0"],
    "BSD-2-Clause": [r"BSD 2-Clause", r"Redistribution and use in source and binary"],
    "BSD-3-Clause": [r"BSD 3-Clause", r"Neither the name of"],
    "ISC": [r"ISC License", r"Permission to use, copy, modify"],
    "Unlicense": [r"This is free and unencumbered software"],
    "CC0-1.0": [r"CC0 1\.0 Universal", r"Creative Commons Zero"],
}


def detect_license(folder: str) -> Tuple[Optional[str], Optional[str]]:
    """Detect license type in a folder. Returns (license_type, license_file_path)."""
    folder_path = Path(folder)
    if not folder_path.exists():
        return None, None

    for name in LICENSE_FILES:
        license_path = folder_path / name
        if license_path.exists():
            try:
                content = license_path.read_text(errors="ignore")[:2000]
                for license_type, patterns in OSS_PATTERNS.items():
                    for pattern in patterns:
                        if re.search(pattern, content, re.IGNORECASE):
                            return license_type, str(license_path)
                # License file exists but doesn't match known OSS patterns
                return "unknown", str(license_path)
            except (OSError, PermissionError):
                return "unreadable", str(license_path)

    return None, None


# --- Workspace scanning ---

def scan_workspace(folder: str, has_gh: bool) -> Dict:
    """Classify a workspace folder by git visibility and license."""
    info = {
        "path": folder,
        "exists": Path(folder).exists(),
        "git_remote": None,
        "is_public": None,
        "license_type": None,
        "license_file": None,
        "auto_include": False,
        "reason": "",
    }

    if not info["exists"]:
        info["reason"] = "folder not found"
        return info

    # Check git remote
    remote = get_git_remote_url(folder)
    info["git_remote"] = remote

    if remote and has_gh:
        public = is_github_public(remote)
        info["is_public"] = public
    elif remote and not has_gh:
        info["is_public"] = None
    else:
        info["is_public"] = None

    # Check license
    license_type, license_file = detect_license(folder)
    info["license_type"] = license_type
    info["license_file"] = license_file

    # Auto-include logic: public repo + recognized OSS license
    if info["is_public"] and license_type in OSS_PATTERNS:
        info["auto_include"] = True
        info["reason"] = f"public + {license_type}"
    elif info["is_public"] and license_type is None:
        info["reason"] = "public but no LICENSE file"
    elif info["is_public"] is False:
        info["reason"] = "private repo"
    elif info["is_public"] is None and remote and not has_gh:
        info["reason"] = "gh CLI not available, cannot check visibility"
    elif info["is_public"] is None and remote:
        info["reason"] = "non-GitHub remote, cannot determine visibility"
    elif remote is None:
        info["reason"] = "no git remote"
    elif license_type == "unknown":
        info["reason"] = "unrecognized license"
    else:
        info["reason"] = "excluded"

    return info


# --- bootblock.txt generation ---

def generate_bootblock(workspaces: List[Dict], output_path: str = "bootblock.txt") -> str:
    """Generate the bootblock.txt allowlist file."""
    included = [w for w in workspaces if w["auto_include"]]
    excluded = [w for w in workspaces if not w["auto_include"]]

    lines = [
        "# SkillBench Boot Block",
        f"# Generated: {datetime.now(timezone.utc).isoformat()}",
        "#",
        "# This file controls which project sessions are shared with SkillBench.",
        "# - Uncommented paths will be included in the export.",
        "# - Commented paths (# prefix) will be excluded.",
        "# - Edit freely: add, remove, comment/uncomment as you see fit.",
        "# - Then run: python bootblock.py export",
        "#",
        "# NOTE: If a path contains '#', wrap it in double quotes.",
        "#",
        "",
    ]

    if included:
        lines.append("# AUTO-INCLUDED (public repo + OSS license):")
        for w in included:
            remote_note = ""
            if w["git_remote"] and "github.com" in w["git_remote"]:
                owner_repo = extract_github_owner_repo(w["git_remote"])
                if owner_repo:
                    remote_note = f", {owner_repo}"
            path_str = _format_path_for_bootblock(w["path"])
            lines.append(f"{path_str}    # {w['license_type']}{remote_note}")
        lines.append("")

    if excluded:
        lines.append("# EXCLUDED (uncomment to include):")
        for w in excluded:
            path_str = _format_path_for_bootblock(w["path"])
            lines.append(f"# {path_str}    # {w['reason']}")
        lines.append("")

    lines.extend([
        "# MANUAL ADDITIONS (paste paths here):",
        "",
    ])

    Path(output_path).write_text("\n".join(lines) + "\n")
    return output_path


def _format_path_for_bootblock(path: str) -> str:
    """Quote a path if it contains characters that would confuse the parser."""
    if "#" in path or path != path.strip():
        return f'"{path}"'
    return path


# --- bootblock.txt parsing ---

def parse_bootblock(path: str = "bootblock.txt") -> List[str]:
    """Read bootblock.txt, return list of included folder paths."""
    included = []
    for line_num, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Handle quoted paths (which may contain #)
        if line.startswith('"'):
            end_quote = line.find('"', 1)
            if end_quote == -1:
                print(f"WARNING: Unclosed quote on line {line_num} of {path}, skipping", file=sys.stderr)
                continue
            path_part = line[1:end_quote]
        else:
            # Strip inline comments (first # that's preceded by whitespace)
            comment_match = re.search(r"\s+#", line)
            if comment_match:
                path_part = line[:comment_match.start()].strip()
            else:
                path_part = line.strip()

        if path_part:
            included.append(path_part)
    return included


# --- Session export ---

def _escape_like(s: str) -> str:
    """Escape special characters for SQL LIKE with ESCAPE '\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def export_sessions(db_path: Path, folders: List[str], output_path: str = "sessions.json") -> Dict:
    """Export conversations from CASS DB for selected workspace folders.

    Returns dict with per-folder counts and total.
    """
    conn = open_cass_db(db_path)

    all_conversations = []
    seen_conv_ids = set()
    folder_counts: Dict[str, int] = {}

    for folder in folders:
        folder_count = 0
        escaped = _escape_like(folder)

        try:
            rows = conn.execute("""
                SELECT c.id, c.title, c.source_path, c.started_at, c.ended_at, c.approx_tokens,
                       w.path as workspace_path, a.slug as agent_slug, a.name as agent_name
                FROM conversations c
                LEFT JOIN workspaces w ON c.workspace_id = w.id
                LEFT JOIN agents a ON c.agent_id = a.id
                WHERE w.path = ? OR w.path LIKE ? ESCAPE '\\'
            """, (folder, escaped + "%")).fetchall()
        except sqlite3.OperationalError as e:
            print(f"  WARNING: Query failed for {folder}: {e}", file=sys.stderr)
            folder_counts[folder] = 0
            continue

        for row in rows:
            conv_id = row["id"]

            # Deduplicate: nested workspace paths can match the same conversation
            if conv_id in seen_conv_ids:
                continue
            seen_conv_ids.add(conv_id)

            try:
                messages = conn.execute("""
                    SELECT role, author, created_at, content
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC
                """, (conv_id,)).fetchall()
            except sqlite3.OperationalError as e:
                print(f"  WARNING: Could not read messages for conversation {conv_id}: {e}", file=sys.stderr)
                continue

            conv_data = {
                "id": conv_id,
                "workspace": row["workspace_path"],
                "agent": row["agent_slug"] or row["agent_name"],
                "title": row["title"],
                "source_path": row["source_path"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "approx_tokens": row["approx_tokens"],
                "messages": [
                    {
                        "role": m["role"],
                        "author": m["author"],
                        "created_at": m["created_at"],
                        "content": m["content"],
                    }
                    for m in messages
                ],
            }
            all_conversations.append(conv_data)
            folder_count += 1

        folder_counts[folder] = folder_count

    conn.close()

    export_data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tool": "skillbench-bootblock",
        "version": "0.1.0",
        "folders_included": folders,
        "conversation_count": len(all_conversations),
        "conversations": all_conversations,
    }

    Path(output_path).write_text(json.dumps(export_data, indent=2, default=str))
    return folder_counts


# --- CLI ---

def cmd_scan(args: argparse.Namespace) -> None:
    """Scan CASS index and generate bootblock.txt."""
    db_path = find_cass_db()
    if not db_path:
        print("ERROR: Could not find CASS database.", file=sys.stderr)
        print("Make sure CASS is installed and you've run: cass index --full", file=sys.stderr)
        print("Or set CASS_DATA_DIR to your CASS data directory.", file=sys.stderr)
        sys.exit(1)

    # Validate schema
    schema_err = validate_cass_schema(db_path)
    if schema_err:
        print(f"ERROR: {schema_err}", file=sys.stderr)
        sys.exit(1)

    print(f"Found CASS database: {db_path}")

    # Check gh CLI availability
    has_gh = check_gh_cli()
    if not has_gh:
        print("NOTE: gh CLI not found or not authenticated.")
        print("      Will skip public/private detection for GitHub repos.")
        print("      Install: https://cli.github.com  then run: gh auth login")
        print()

    # Query all workspaces
    conn = open_cass_db(db_path)
    rows = conn.execute("SELECT DISTINCT path FROM workspaces WHERE path IS NOT NULL ORDER BY path").fetchall()
    conn.close()

    if not rows:
        print("No workspaces found in CASS index.", file=sys.stderr)
        print("Have you run: cass index --full ?", file=sys.stderr)
        sys.exit(1)

    folders = [row["path"] for row in rows if row["path"]]
    print(f"Found {len(folders)} project folders. Scanning git remotes and licenses...")
    print()

    # Scan each workspace
    workspaces = []
    for i, folder in enumerate(folders, 1):
        short_path = folder
        if len(short_path) > 60:
            short_path = "..." + short_path[-57:]
        print(f"  [{i}/{len(folders)}] {short_path}", end="", flush=True)
        info = scan_workspace(folder, has_gh)
        tag = "INCLUDE" if info["auto_include"] else "exclude"
        print(f"  →  {tag} ({info['reason']})")
        workspaces.append(info)

    # Generate bootblock.txt
    output = args.output or "bootblock.txt"
    generate_bootblock(workspaces, output)

    included = sum(1 for w in workspaces if w["auto_include"])
    excluded = len(workspaces) - included

    print()
    print(f"Generated: {output}")
    print(f"  {included} auto-included (public + OSS license)")
    print(f"  {excluded} excluded (edit the file to include any you want)")
    print()
    print(f"Next steps:")
    print(f"  1. Review {output} — uncomment any private projects you're willing to share")
    print(f"  2. Run: python bootblock.py export")


def cmd_export(args: argparse.Namespace) -> None:
    """Read bootblock.txt and export sessions from CASS DB."""
    bootblock_path = args.bootblock or "bootblock.txt"
    if not Path(bootblock_path).exists():
        print(f"ERROR: {bootblock_path} not found.", file=sys.stderr)
        print("Run 'python bootblock.py scan' first.", file=sys.stderr)
        sys.exit(1)

    folders = parse_bootblock(bootblock_path)
    if not folders:
        print("No folders included in bootblock.txt.", file=sys.stderr)
        print("Uncomment some folders and try again.", file=sys.stderr)
        sys.exit(1)

    db_path = find_cass_db()
    if not db_path:
        print("ERROR: Could not find CASS database.", file=sys.stderr)
        sys.exit(1)

    schema_err = validate_cass_schema(db_path)
    if schema_err:
        print(f"ERROR: {schema_err}", file=sys.stderr)
        sys.exit(1)

    print(f"Exporting sessions for {len(folders)} folders...")
    print()

    output = args.output or "sessions.json"
    folder_counts = export_sessions(db_path, folders, output)

    # Report per-folder counts
    total = 0
    for folder, count in folder_counts.items():
        short = folder if len(folder) <= 60 else "..." + folder[-57:]
        print(f"  {short}: {count} conversations")
        total += count

    size_bytes = Path(output).stat().st_size
    if size_bytes > 1024 * 1024:
        size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        size_str = f"{size_bytes / 1024:.0f} KB"

    print()
    print(f"Exported: {output}")
    print(f"  {total} conversations total")
    print(f"  {size_str}")
    print()
    print("Send this file to the SkillBench team for analysis.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SkillBench Boot Block Tool — select and export coding agent sessions",
        epilog="Requires CASS (https://github.com/Dicklesworthstone/coding_agent_session_search)",
    )
    subparsers = parser.add_subparsers(dest="command")

    # scan
    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan CASS index, check git/license, generate bootblock.txt",
    )
    scan_parser.add_argument("-o", "--output", help="Output file (default: bootblock.txt)")

    # export
    export_parser = subparsers.add_parser(
        "export",
        help="Export sessions for selected folders → sessions.json",
    )
    export_parser.add_argument("-b", "--bootblock", help="Path to bootblock.txt (default: bootblock.txt)")
    export_parser.add_argument("-o", "--output", help="Output file (default: sessions.json)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        print()
        print("Quick start:")
        print("  1. python bootblock.py scan       # find and classify your projects")
        print("  2. edit bootblock.txt              # choose what to share")
        print("  3. python bootblock.py export      # export sessions → sessions.json")
        sys.exit(0)

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "export":
        cmd_export(args)


if __name__ == "__main__":
    main()
