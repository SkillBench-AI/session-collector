#!/usr/bin/env python3
"""
SkillBench Boot Block Tool

Sits on top of CASS (coding_agent_session_search) to let users select
which project sessions to share with SkillBench for analysis.

Commands:
  scan   - Scan CASS index, check git/license, generate bootblock.txt
  export - Read bootblock.txt, extract sessions from CASS DB → sessions.json

Requires: CASS installed and indexed (`cass index --full`)
"""

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


# --- CASS database discovery ---

def find_cass_db() -> Path | None:
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


# --- Git remote detection ---

def get_git_remote_url(folder: str) -> str | None:
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


def is_github_public(remote_url: str) -> bool | None:
    """Check if a GitHub repo is public using gh CLI. Returns None if not determinable."""
    # Extract owner/repo from GitHub URL
    patterns = [
        r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$",
    ]
    owner_repo = None
    for pattern in patterns:
        match = re.search(pattern, remote_url)
        if match:
            owner_repo = f"{match.group(1)}/{match.group(2)}"
            break

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


def detect_license(folder: str) -> tuple[str | None, str | None]:
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

def scan_workspace(folder: str) -> dict:
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

    if remote:
        public = is_github_public(remote)
        info["is_public"] = public
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

def generate_bootblock(workspaces: list[dict], output_path: str = "bootblock.txt"):
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
        "",
    ]

    if included:
        lines.append("# AUTO-INCLUDED (public repo + OSS license):")
        for w in included:
            remote_note = ""
            if w["git_remote"] and "github.com" in w["git_remote"]:
                match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", w["git_remote"])
                if match:
                    remote_note = f", {match.group(1)}"
            lines.append(f"{w['path']}    # {w['license_type']}{remote_note}")
        lines.append("")

    if excluded:
        lines.append("# EXCLUDED (uncomment to include):")
        for w in excluded:
            lines.append(f"# {w['path']}    # {w['reason']}")
        lines.append("")

    lines.extend([
        "# MANUAL ADDITIONS (paste paths here):",
        "",
    ])

    Path(output_path).write_text("\n".join(lines) + "\n")
    return output_path


# --- bootblock.txt parsing ---

def parse_bootblock(path: str = "bootblock.txt") -> list[str]:
    """Read bootblock.txt, return list of included folder paths."""
    included = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comments
        path_part = line.split("#")[0].strip()
        if path_part:
            included.append(path_part)
    return included


# --- Session export ---

def export_sessions(db_path: Path, folders: list[str], output_path: str = "sessions.json"):
    """Export conversations from CASS DB for selected workspace folders."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    all_conversations = []

    for folder in folders:
        # Find conversations matching this workspace
        # CASS stores workspace as a path — try exact match and prefix match
        rows = conn.execute("""
            SELECT c.*, w.path as workspace_path, a.slug as agent_slug, a.name as agent_name
            FROM conversations c
            LEFT JOIN workspaces w ON c.workspace_id = w.id
            LEFT JOIN agents a ON c.agent_id = a.id
            WHERE w.path = ? OR w.path LIKE ?
        """, (folder, folder + "%")).fetchall()

        for row in rows:
            conv_id = row["id"]
            # Get messages for this conversation
            messages = conn.execute("""
                SELECT role, author, created_at, content, extra_json
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC
            """, (conv_id,)).fetchall()

            # Get code snippets for this conversation's messages
            message_ids = [m["rowid"] if "rowid" in m.keys() else None for m in messages]

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
    return len(all_conversations)


# --- CLI ---

def cmd_scan(args):
    """Scan CASS index and generate bootblock.txt."""
    db_path = find_cass_db()
    if not db_path:
        print("ERROR: Could not find CASS database.", file=sys.stderr)
        print("Make sure CASS is installed and you've run: cass index --full", file=sys.stderr)
        print("Or set CASS_DATA_DIR to your CASS data directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Found CASS database: {db_path}")

    # Query all workspaces
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT DISTINCT path FROM workspaces ORDER BY path").fetchall()
    conn.close()

    if not rows:
        print("No workspaces found in CASS index.", file=sys.stderr)
        print("Have you run: cass index --full ?", file=sys.stderr)
        sys.exit(1)

    folders = [row["path"] for row in rows]
    print(f"Found {len(folders)} project folders. Scanning git remotes and licenses...")

    # Scan each workspace
    workspaces = []
    for i, folder in enumerate(folders, 1):
        print(f"  [{i}/{len(folders)}] {folder}", end="", flush=True)
        info = scan_workspace(folder)
        tag = "INCLUDE" if info["auto_include"] else "exclude"
        print(f" → {tag} ({info['reason']})")
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
    print(f"Next: review {output}, then run: python bootblock.py export")


def cmd_export(args):
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

    print(f"Exporting sessions for {len(folders)} folders...")
    for f in folders:
        print(f"  {f}")

    output = args.output or "sessions.json"
    count = export_sessions(db_path, folders, output)

    size_mb = Path(output).stat().st_size / (1024 * 1024)
    print()
    print(f"Exported: {output}")
    print(f"  {count} conversations")
    print(f"  {size_mb:.1f} MB")
    print()
    print("Send this file to the SkillBench team for analysis.")


def main():
    parser = argparse.ArgumentParser(
        description="SkillBench Boot Block Tool — select and export coding agent sessions",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan CASS index, generate bootblock.txt")
    scan_parser.add_argument("-o", "--output", help="Output file (default: bootblock.txt)")

    # export
    export_parser = subparsers.add_parser("export", help="Export sessions for selected folders")
    export_parser.add_argument("-b", "--bootblock", help="Path to bootblock.txt (default: bootblock.txt)")
    export_parser.add_argument("-o", "--output", help="Output file (default: sessions.json)")

    args = parser.parse_args()
    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "export":
        cmd_export(args)


if __name__ == "__main__":
    main()
