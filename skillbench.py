#!/usr/bin/env python3
"""SkillBench session-collector CLI.

Boot block tool + session-level analysis pipeline.
Sits on top of CASS (coding_agent_session_search) and lets users:
  1. Browse indexed coding agent sessions
  2. Auto-classify projects by git visibility + OSS license
  3. Review/edit an allowlist of folders to share
  4. Compute agentic engineering metrics locally
"""

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from configparser import ConfigParser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASS_DB_DEFAULT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "com.coding-agent-search.coding-agent-search"
    / "agent_search.db"
)

DIST_DIR = Path("dist")
BOOTBLOCK_FILE = DIST_DIR / "bootblock.txt"

GEMINI_TMP_DIR = Path.home() / ".gemini" / "tmp"

# Paths to skip (after Gemini hash resolution)
SKIP_PATTERNS = [
    r"\.gemini/",          # unresolved Gemini hash dirs
    r"/private/var/folders/",
    r"^/tmp/",             # system /tmp only
    r"\.cursor/projects/",
    r"\.worktrees/",
    r"/worktrees/",
]

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def find_cass_db() -> Path:
    """Locate the CASS SQLite database."""
    # Allow overriding the CASS data directory explicitly.
    data_dir = os.environ.get("CASS_DATA_DIR")
    if data_dir:
        env_db = Path(data_dir) / "agent_search.db"
        if env_db.exists():
            return env_db

    candidates: list[Path] = []

    system = platform.system()
    if system == "Darwin":
        candidates.extend([
            # Current default used by this repo
            CASS_DB_DEFAULT,
            # Common CASS locations on macOS (older/newer layouts)
            Path.home() / "Library" / "Application Support" / "coding-agent-search" / "coding-agent-search" / "agent_search.db",
            Path.home() / "Library" / "Application Support" / "coding-agent-search" / "agent_search.db",
        ])
    elif system == "Linux":
        xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        candidates.append(Path(xdg) / "coding-agent-search" / "agent_search.db")
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "coding-agent-search" / "coding-agent-search" / "agent_search.db")

    # Project-local fallback (some CASS builds use ./data/)
    candidates.append(Path("data") / "agent_search.db")

    for p in candidates:
        if p.exists():
            return p

    return None


def get_workspaces(db_path: Path) -> list[dict]:
    """Return all workspaces with conversation counts and agent info."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            w.path                              AS workspace,
            a.slug                              AS agent,
            COUNT(c.id)                         AS conversations,
            COUNT(DISTINCT m_user.id)           AS user_messages,
            MIN(c.started_at)                   AS first_session,
            MAX(c.ended_at)                     AS last_session
        FROM conversations c
        JOIN workspaces w ON c.workspace_id = w.id
        JOIN agents a     ON c.agent_id    = a.id
        LEFT JOIN messages m_user
            ON m_user.conversation_id = c.id AND m_user.role = 'user'
        GROUP BY w.path, a.slug
        ORDER BY conversations DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Git / license classification
# ---------------------------------------------------------------------------

def gemini_hash_for_path(project_path: str) -> str:
    """Compute the Gemini CLI storage hash for a project path.

    Gemini CLI does not store session data under the project directory itself.
    Instead, it hashes the absolute project path with SHA-256 and stores
    sessions in ~/.gemini/tmp/{hash}/chats/. This means CASS indexes Gemini
    conversations under those hash-based workspace paths, not the real project
    paths that every other agent uses.

    To work with Gemini data in CASS, we need this hash in two places:
      1. During scan: to reverse-map .gemini/tmp/{hash} entries back to real
         project paths so conversation counts and agent lists are correct.
      2. During analyze/push: to expand bootblock paths into CASS queries
         that also match the Gemini hash workspace, so Gemini sessions are
         included alongside sessions from other agents.

    The hash algorithm matches Gemini CLI's getProjectHash() implementation:
      crypto.createHash('sha256').update(projectRoot).digest('hex')
    (see gemini-cli/packages/core/src/utils/paths.ts)
    """
    return hashlib.sha256(project_path.encode()).hexdigest()


def _is_gemini_hash_path(workspace_path: str) -> bool:
    """True if the path is a Gemini CLI hash directory (~/.gemini/tmp/{hash})."""
    return str(GEMINI_TMP_DIR) + "/" in workspace_path or \
           workspace_path.startswith(str(GEMINI_TMP_DIR))


def resolve_gemini_hashes(by_path: dict[str, dict]) -> dict[str, dict]:
    """Resolve Gemini CLI hash directories back to real project paths.

    Gemini CLI stores sessions in ~/.gemini/tmp/{sha256(project_root)}/chats/.
    This function reverses the hash by computing SHA-256 for all known
    non-Gemini workspace paths, then merges Gemini conversation data into
    the real workspace entries.
    """
    # Separate Gemini hash paths from real paths
    gemini_entries = {}  # hash -> aggregated data
    real_entries = {}    # path -> aggregated data

    for path, info in by_path.items():
        if _is_gemini_hash_path(path):
            # Extract the hash from the path (last component of .gemini/tmp/{hash})
            parts = path.split(str(GEMINI_TMP_DIR) + "/")
            if len(parts) == 2:
                hash_part = parts[1].split("/")[0]
                gemini_entries[hash_part] = info
        else:
            real_entries[path] = info

    if not gemini_entries:
        return by_path

    # Build reverse lookup: sha256(real_path) -> real_path
    hash_to_path = {}
    for real_path in real_entries:
        hash_to_path[gemini_hash_for_path(real_path)] = real_path

    # Also try hashing directories that exist on disk under .gemini/tmp
    # but aren't in CASS yet (gemini-only projects)
    for hash_dir in gemini_entries:
        if hash_dir not in hash_to_path:
            # Check if any common project directories hash to this
            # We can't reverse SHA-256, so unmatched hashes stay unresolved
            pass

    # Merge Gemini data into real workspace entries
    resolved = 0
    unresolved_entries = {}
    for gem_hash, gem_info in gemini_entries.items():
        real_path = hash_to_path.get(gem_hash)
        if real_path:
            # Merge into existing real entry
            entry = real_entries[real_path]
            entry["agents"].extend(gem_info["agents"])
            entry["total_conversations"] += gem_info["total_conversations"]
            entry["total_user_messages"] += gem_info["total_user_messages"]
            if gem_info["first_session"] and (not entry["first_session"] or gem_info["first_session"] < entry["first_session"]):
                entry["first_session"] = gem_info["first_session"]
            if gem_info["last_session"] and (not entry["last_session"] or gem_info["last_session"] > entry["last_session"]):
                entry["last_session"] = gem_info["last_session"]
            resolved += 1
        else:
            # Keep unresolved Gemini entries as-is (will be skipped later)
            orig_path = str(GEMINI_TMP_DIR / gem_hash)
            unresolved_entries[orig_path] = gem_info

    if resolved:
        print(f"  Resolved {resolved} Gemini hash dir(s) to real project paths")
    if unresolved_entries:
        print(f"  {len(unresolved_entries)} Gemini hash dir(s) could not be resolved")

    # Return merged result
    result = dict(real_entries)
    result.update(unresolved_entries)
    return result


def expand_with_gemini_hashes(paths: list[str]) -> list[str]:
    """Expand a list of real workspace paths to include Gemini equivalents.

    Gemini CLI stores sessions under ~/.gemini/tmp/ using either:
    - {sha256(path)}/  (older versions)
    - {basename}/      (0.31+)
    This appends both variants so CASS queries match either.
    """
    expanded = list(paths)
    for p in paths:
        expanded.append(str(GEMINI_TMP_DIR / gemini_hash_for_path(p)))
        expanded.append(str(GEMINI_TMP_DIR / Path(p).name))
    return list(dict.fromkeys(expanded))  # dedupe


def is_skippable(path: str) -> bool:
    """True if the workspace path matches known temp/transient patterns."""
    for pat in SKIP_PATTERNS:
        if re.search(pat, path):
            return True
    # Skip paths that are just the home directory or very short
    home = str(Path.home())
    if path == home or path == home + "/":
        return True
    return False


def git_remote_url(folder: str) -> str | None:
    """Return the git remote origin URL, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", folder, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Fall back to parsing .git/config directly (useful in minimal containers
        # where `git` may not be installed).
        try:
            git_path = Path(folder) / ".git"
            git_dir = None
            if git_path.is_dir():
                git_dir = git_path
            elif git_path.is_file():
                # Worktree/submodule style: .git is a file with "gitdir: /path"
                txt = git_path.read_text(errors="replace")
                m = re.search(r"^gitdir:\s*(.+?)\s*$", txt, flags=re.MULTILINE)
                if m:
                    ref = m.group(1).strip()
                    git_dir = (git_path.parent / ref).resolve()

            if not git_dir:
                return None

            cfg = git_dir / "config"
            if not cfg.exists():
                return None

            parser = ConfigParser(interpolation=None)
            parser.read(cfg)
            section = 'remote "origin"'
            if parser.has_section(section) and parser.has_option(section, "url"):
                url = parser.get(section, "url")
                return url.strip() if url else None
        except Exception:
            return None
    return None


def _extract_github_slug(remote_url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL."""
    if not remote_url:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote_url)
    return m.group(1) if m else None


def _gh_cli_available() -> bool:
    """Check if the GitHub CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


_GH_AVAILABLE: bool | None = None


def _check_gh_once() -> bool:
    """Check gh availability once and cache the result."""
    global _GH_AVAILABLE
    if _GH_AVAILABLE is None:
        _GH_AVAILABLE = _gh_cli_available()
        if not _GH_AVAILABLE:
            print("  Note: GitHub CLI (gh) not found or not authenticated.")
            print("  Install: brew install gh && gh auth login")
            print("  Without gh, all repos are classified as private (safe default).\n")
    return _GH_AVAILABLE


def classify_github_repo(remote_url: str) -> dict | None:
    """Classify a GitHub repo's visibility + license in a single `gh` call.

    GitHub uses the Licensee gem to match LICENSE file contents against known
    licenses and returns SPDX-compliant IDs.  This replaces both local regex
    license detection and the separate visibility check for GitHub repos.

    Returns dict with keys: is_public, license_key, license_name
    or None if not a GitHub repo or the `gh` CLI fails.
    """
    slug = _extract_github_slug(remote_url)
    if not slug:
        return None
    if not _check_gh_once():
        return None
    try:
        result = subprocess.run(
            ["gh", "repo", "view", slug, "--json", "isPrivate,licenseInfo"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            license_info = data.get("licenseInfo")
            license_key = None
            license_name = None
            if license_info:
                license_key = license_info.get("key")    # e.g. "mit", "apache-2.0", "other"
                license_name = license_info.get("name")  # e.g. "MIT License"
            return {
                "is_public": not data.get("isPrivate", True),
                "license_key": license_key,
                "license_name": license_name,
            }
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def detect_license_local(folder: str) -> str | None:
    """Fallback: detect license from local manifest files for non-GitHub repos.

    Only checks package manifests (pyproject.toml, package.json, etc.).
    For GitHub repos, use classify_github_repo() instead — it uses GitHub's
    own Licensee-based detection which is far more reliable.
    """
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return None
    for manifest, pattern in [
        ("pyproject.toml", r'license\s*=\s*["\']([^"\']+)'),
        ("package.json", r'"license"\s*:\s*"([^"]+)"'),
        ("Cargo.toml", r'license\s*=\s*"([^"]+)"'),
        ("pubspec.yaml", r'license:\s*(\S+)'),
    ]:
        mf = folder_path / manifest
        if mf.is_file():
            try:
                content = mf.read_text(errors="replace")[:4000]
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_scan(args):
    """Scan CASS index, classify workspaces, generate bootblock.txt."""
    db_path = find_cass_db()
    if not db_path:
        print("ERROR: Could not find CASS database.", file=sys.stderr)
        print("Install CASS and run `cass index --full` first.", file=sys.stderr)
        sys.exit(1)

    print(f"Reading CASS database: {db_path}")
    workspaces = get_workspaces(db_path)

    # Aggregate by workspace path
    by_path: dict[str, dict] = {}
    for w in workspaces:
        path = w["workspace"]
        if path not in by_path:
            by_path[path] = {
                "agents": [],
                "total_conversations": 0,
                "total_user_messages": 0,
                "first_session": w["first_session"],
                "last_session": w["last_session"],
            }
        entry = by_path[path]
        entry["agents"].append(w["agent"])
        entry["total_conversations"] += w["conversations"]
        entry["total_user_messages"] += w["user_messages"]
        if w["first_session"] and (not entry["first_session"] or w["first_session"] < entry["first_session"]):
            entry["first_session"] = w["first_session"]
        if w["last_session"] and (not entry["last_session"] or w["last_session"] > entry["last_session"]):
            entry["last_session"] = w["last_session"]

    # Resolve Gemini hash directories to real project paths
    by_path = resolve_gemini_hashes(by_path)

    # Classify each workspace
    included = []
    excluded = []
    skipped = 0

    total = len(by_path)
    print(f"Classifying {total} workspace paths...")

    for i, (path, info) in enumerate(sorted(by_path.items(), key=lambda x: -x[1]["total_conversations"])):
        if is_skippable(path):
            skipped += 1
            continue

        if not Path(path).is_dir():
            skipped += 1
            continue

        # Progress indicator for slow git/gh checks
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{total}] classifying...", end="\r")

        remote = git_remote_url(path)
        gh = classify_github_repo(remote) if remote else None

        if gh:
            # GitHub API: single call for visibility + license (Licensee + SPDX)
            public = gh["is_public"]
            license_key = gh["license_key"]
            license_name = gh["license_name"]
            # A real license key (not "other") means GitHub's Licensee matched
            # the LICENSE file to a known OSS license.
            oss = license_key is not None and license_key != "other"
            license_id = license_name or license_key
        else:
            # Non-GitHub repo: no reliable license detection available
            public = None
            license_id = detect_license_local(path)
            oss = False  # manifest-only license can't satisfy LICENSE file requirement

        entry = {
            "path": path,
            "remote": remote,
            "license": license_id,
            "public": public,
            "oss": oss,
            "agents": sorted(set(info["agents"])),
            "conversations": info["total_conversations"],
            "user_messages": info["total_user_messages"],
        }

        if public and oss:
            entry["reason"] = f"{license_id}, public"
            included.append(entry)
        else:
            reasons = []
            if gh:
                if not public:
                    reasons.append("private repo")
                if license_key is None:
                    reasons.append("no LICENSE file")
                elif license_key == "other":
                    reasons.append("unrecognized license in LICENSE file")
            else:
                if not remote:
                    reasons.append("no git remote")
                else:
                    reasons.append("not a GitHub repo")
                if license_id:
                    reasons.append(f"manifest license: {license_id}")
                else:
                    reasons.append("no license detected")
            entry["reason"] = ", ".join(reasons) if reasons else "unknown"
            excluded.append(entry)

    print(f"\nClassification complete:")
    print(f"  Auto-included (public/OSS): {len(included)}")
    print(f"  Excluded (private/no-license): {len(excluded)}")
    print(f"  Skipped (temp/transient): {skipped}")

    # Write bootblock.txt
    output_path = Path(args.output) if args.output else BOOTBLOCK_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("# SkillBench Boot Block — auto-generated from CASS index + git/license scan\n")
        f.write("# Edit this file: add/remove folders, then run `skillbench analyze`\n")
        f.write("#\n")

        if included:
            f.write("# AUTO-INCLUDED (public repo + recognized OSS license in LICENSE file):\n")
            for entry in included:
                agents = ",".join(entry["agents"])
                comment = f"# {entry['reason']}, {agents}, {entry['conversations']} convos"
                f.write(f"{entry['path']}  {comment}\n")
            f.write("\n")

        if excluded:
            f.write("# EXCLUDED (private/no-license/unrecognized-license — uncomment to include):\n")
            for entry in excluded:
                agents = ",".join(entry["agents"])
                comment = f"# {entry['reason']}, {agents}, {entry['conversations']} convos"
                f.write(f"# {entry['path']}  {comment}\n")
            f.write("\n")

        f.write("# MANUAL ADDITIONS (paste paths here):\n")

    print(f"\nWrote {output_path}")
    print(f"Review the file, then run: skillbench analyze")


def cmd_analyze(args):
    """Compute agentic engineering metrics from CASS data for allowed workspaces."""
    db_path = find_cass_db()
    if not db_path:
        print("ERROR: Could not find CASS database.", file=sys.stderr)
        sys.exit(1)

    # Parse bootblock.txt for allowed paths
    bootblock = Path(args.bootblock) if args.bootblock else BOOTBLOCK_FILE
    if not bootblock.exists():
        print(f"ERROR: {bootblock} not found. Run `skillbench scan` first.", file=sys.stderr)
        sys.exit(1)

    allowed_paths = []
    for line in bootblock.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comments
        path = line.split("  #")[0].strip()
        if path and Path(path).is_absolute():
            allowed_paths.append(path)

    if not allowed_paths:
        print("No paths enabled in bootblock.txt. Uncomment or add paths to analyze.")
        sys.exit(1)

    print(f"Analyzing {len(allowed_paths)} workspace(s) from {bootblock}...")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Build workspace ID mapping (include Gemini hash paths)
    query_paths = expand_with_gemini_hashes(allowed_paths)
    placeholders = ",".join("?" * len(query_paths))
    workspace_rows = conn.execute(
        f"SELECT id, path FROM workspaces WHERE path IN ({placeholders})",
        query_paths
    ).fetchall()
    ws_ids = [r["id"] for r in workspace_rows]
    ws_map = {r["id"]: r["path"] for r in workspace_rows}

    if not ws_ids:
        print("No matching workspaces found in CASS database for the given paths.")
        conn.close()
        sys.exit(1)

    ws_placeholders = ",".join("?" * len(ws_ids))

    # --- Tier 1: Usage Patterns ---
    conversations = conn.execute(f"""
        SELECT c.id, c.agent_id, c.workspace_id, c.started_at, c.ended_at,
               c.source_path, a.slug as agent
        FROM conversations c
        JOIN agents a ON c.agent_id = a.id
        WHERE c.workspace_id IN ({ws_placeholders})
        ORDER BY c.started_at
    """, ws_ids).fetchall()

    conv_ids = [c["id"] for c in conversations]
    total_convos = len(conversations)

    if total_convos == 0:
        print("No conversations found for the allowed workspaces.")
        conn.close()
        sys.exit(1)

    # Date range
    timestamps = [c["started_at"] for c in conversations if c["started_at"]]
    if timestamps:
        first_ts = min(timestamps)
        last_ts = max(timestamps)
        first_date = datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc)
        last_date = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
        span_days = max((last_date - first_date).days, 1)
        span_weeks = max(span_days / 7, 1)
    else:
        span_weeks = 1
        first_date = last_date = None

    # Sessions per week
    sessions_per_week = total_convos / span_weeks

    # Agent diversity
    agents_used = set(c["agent"] for c in conversations)

    # Active days
    active_days = set()
    for c in conversations:
        if c["started_at"]:
            day = datetime.fromtimestamp(c["started_at"] / 1000, tz=timezone.utc).date()
            active_days.add(day)
    active_days_per_week = len(active_days) / span_weeks

    # --- Tier 2: Prompting Sophistication ---
    # Get all user messages for these conversations
    conv_placeholders = ",".join("?" * len(conv_ids))
    user_messages = conn.execute(f"""
        SELECT conversation_id, LENGTH(content) as content_len, content
        FROM messages
        WHERE conversation_id IN ({conv_placeholders})
          AND role = 'user'
    """, conv_ids).fetchall()

    total_user_msgs = len(user_messages)
    avg_prompt_length = (sum(m["content_len"] for m in user_messages) / total_user_msgs
                         if total_user_msgs else 0)

    # Context provision rate: mentions of file paths, code blocks, line numbers
    context_indicators = re.compile(
        r'(/[\w./-]+\.\w+|```|line \d+|src/|lib/|test/|\.py|\.ts|\.js|\.go|\.rs|\.dart)',
        re.IGNORECASE
    )
    context_count = sum(1 for m in user_messages if context_indicators.search(m["content"] or ""))
    context_provision_rate = context_count / total_user_msgs if total_user_msgs else 0

    # Multi-step rate: sessions with >3 user turns
    msgs_per_conv = defaultdict(int)
    for m in user_messages:
        msgs_per_conv[m["conversation_id"]] += 1
    multi_step = sum(1 for count in msgs_per_conv.values() if count > 3)
    multi_step_rate = multi_step / total_convos if total_convos else 0

    # --- Tier 3: Iteration Efficiency ---
    # First attempt success: sessions with <=2 user messages
    first_attempt = sum(1 for count in msgs_per_conv.values() if count <= 2)
    first_attempt_success_rate = first_attempt / total_convos if total_convos else 0

    # Correction rate: user messages containing correction patterns
    correction_re = re.compile(
        r'\b(no[,.]|wrong|incorrect|instead|actually|not what|try again|that\'s not|fix |redo)\b',
        re.IGNORECASE
    )
    correction_count = sum(1 for m in user_messages if correction_re.search(m["content"] or ""))
    correction_rate = correction_count / total_user_msgs if total_user_msgs else 0

    # Average turns to completion
    avg_turns = sum(msgs_per_conv.values()) / len(msgs_per_conv) if msgs_per_conv else 0

    # Session duration
    durations = []
    for c in conversations:
        if c["started_at"] and c["ended_at"] and c["ended_at"] > c["started_at"]:
            dur_min = (c["ended_at"] - c["started_at"]) / 60000
            if dur_min < 480:  # cap at 8 hours
                durations.append(dur_min)
    avg_session_duration = sum(durations) / len(durations) if durations else 0

    conn.close()

    # --- Ladder Placement ---
    score = compute_ladder_score(
        sessions_per_week=sessions_per_week,
        avg_prompt_length=avg_prompt_length,
        context_provision_rate=context_provision_rate,
        first_attempt_success_rate=first_attempt_success_rate,
        correction_rate=correction_rate,
        multi_step_rate=multi_step_rate,
        avg_turns=avg_turns,
        agent_diversity=len(agents_used),
    )

    level, level_name = ladder_level(score)

    # --- Output ---
    print()
    print("=" * 60)
    print("  SkillBench — Agentic Engineering Profile")
    print("=" * 60)
    print()
    if first_date and last_date:
        print(f"  Date range: {first_date.date()} to {last_date.date()} ({span_days} days)")
    print(f"  Workspaces analyzed: {len(ws_ids)}")
    print(f"  Total conversations: {total_convos}")
    print(f"  Total user messages: {total_user_msgs}")
    print()

    print(f"  Level: {level} {level_name}  {'█' * (score // 5)}{'░' * (20 - score // 5)}  {score}/100")
    print()

    print("  ── Tier 1: Usage Patterns ──")
    print(f"  Sessions/week:       {sessions_per_week:.1f}")
    print(f"  Active days/week:    {active_days_per_week:.1f}")
    print(f"  Avg session duration: {avg_session_duration:.0f} min")
    print(f"  Agents used:         {', '.join(sorted(agents_used))}")
    print()

    print("  ── Tier 2: Prompting Sophistication ──")
    print(f"  Avg prompt length:    {avg_prompt_length:.0f} chars")
    print(f"  Context provision:    {context_provision_rate:.0%}")
    print(f"  Multi-step rate:      {multi_step_rate:.0%}")
    print()

    print("  ── Tier 3: Iteration Efficiency ──")
    print(f"  First-attempt success: {first_attempt_success_rate:.0%}")
    print(f"  Correction rate:       {correction_rate:.0%}")
    print(f"  Avg turns/session:     {avg_turns:.1f}")
    print()

    # Strengths and growth edges
    strengths, edges = identify_strengths_and_edges(
        sessions_per_week, avg_prompt_length, context_provision_rate,
        first_attempt_success_rate, correction_rate, multi_step_rate,
    )
    if strengths:
        print("  Strengths:")
        for s in strengths:
            print(f"    ✦ {s}")
    if edges:
        print("  Growth edges:")
        for e in edges:
            print(f"    ✧ {e}")
    print()

    # Level up tips
    print_level_up_guide(level, score, context_provision_rate,
                         first_attempt_success_rate, correction_rate, avg_prompt_length)

    # JSON output option
    if args.json:
        report = {
            "level": level,
            "level_name": level_name,
            "score": score,
            "date_range": {
                "start": str(first_date.date()) if first_date else None,
                "end": str(last_date.date()) if last_date else None,
            },
            "workspaces_analyzed": len(ws_ids),
            "tier1": {
                "total_conversations": total_convos,
                "sessions_per_week": round(sessions_per_week, 1),
                "active_days_per_week": round(active_days_per_week, 1),
                "avg_session_duration_min": round(avg_session_duration, 1),
                "agents_used": sorted(agents_used),
            },
            "tier2": {
                "avg_prompt_length": round(avg_prompt_length),
                "context_provision_rate": round(context_provision_rate, 3),
                "multi_step_rate": round(multi_step_rate, 3),
            },
            "tier3": {
                "first_attempt_success_rate": round(first_attempt_success_rate, 3),
                "correction_rate": round(correction_rate, 3),
                "avg_turns_per_session": round(avg_turns, 1),
            },
        }
        json_path = DIST_DIR / "skillbench_report.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2))
        print(f"  JSON report written to {json_path}")


def compute_ladder_score(
    sessions_per_week: float,
    avg_prompt_length: float,
    context_provision_rate: float,
    first_attempt_success_rate: float,
    correction_rate: float,
    multi_step_rate: float,
    avg_turns: float,
    agent_diversity: int,
) -> int:
    """Compute an overall agentic engineering score (0-100)."""
    score = 0.0

    # Usage frequency (0-20 pts)
    score += min(sessions_per_week / 15 * 20, 20)

    # Prompt sophistication (0-25 pts)
    # Longer prompts suggest more detail
    prompt_score = min(avg_prompt_length / 400 * 10, 10)
    # Context provision
    prompt_score += context_provision_rate * 10
    # Multi-step work
    prompt_score += multi_step_rate * 5
    score += min(prompt_score, 25)

    # Iteration efficiency (0-30 pts)
    score += first_attempt_success_rate * 15
    score += (1 - min(correction_rate, 0.5)) * 10  # lower correction = better
    # Sweet spot for turns: 2-5 is efficient
    if 2 <= avg_turns <= 5:
        score += 5
    elif avg_turns < 2:
        score += 3  # too few might mean trivial tasks
    else:
        score += max(0, 5 - (avg_turns - 5) * 0.5)

    # Agent diversity (0-10 pts)
    score += min(agent_diversity * 3, 10)

    # Consistency bonus (0-15 pts)
    # Multi-step + context + low correction = mastery signal
    mastery = (multi_step_rate * 0.3 +
               context_provision_rate * 0.4 +
               (1 - correction_rate) * 0.3)
    score += mastery * 15

    return min(int(score), 100)


def ladder_level(score: int) -> tuple[str, str]:
    """Map score to ladder level."""
    if score >= 85:
        return "L5", "Maestro"
    elif score >= 70:
        return "L4", "Engineer"
    elif score >= 50:
        return "L3", "Practitioner"
    elif score >= 30:
        return "L2", "Adopter"
    else:
        return "L1", "Dabbler"


def identify_strengths_and_edges(
    sessions_per_week, avg_prompt_length, context_provision_rate,
    first_attempt_success_rate, correction_rate, multi_step_rate,
):
    """Identify top strengths and growth areas."""
    strengths = []
    edges = []

    if sessions_per_week >= 8:
        strengths.append("High session frequency")
    elif sessions_per_week < 3:
        edges.append("Session frequency (try daily AI usage)")

    if avg_prompt_length >= 250:
        strengths.append("Detailed prompts")
    elif avg_prompt_length < 100:
        edges.append("Prompt detail (add more context to requests)")

    if context_provision_rate >= 0.5:
        strengths.append("Good context provision")
    elif context_provision_rate < 0.3:
        edges.append("Context provision (reference specific files/lines)")

    if first_attempt_success_rate >= 0.6:
        strengths.append("Good first-attempt success")
    elif first_attempt_success_rate < 0.4:
        edges.append("First-attempt success (front-load context)")

    if correction_rate < 0.1:
        strengths.append("Low correction rate")
    elif correction_rate > 0.25:
        edges.append("Correction rate (be more specific upfront)")

    if multi_step_rate >= 0.4:
        strengths.append("Multi-step workflow comfort")
    elif multi_step_rate < 0.2:
        edges.append("Multi-step tasks (try larger delegations)")

    return strengths[:3], edges[:3]


def print_level_up_guide(level, score, context_rate, first_attempt, correction, prompt_len):
    """Print personalized level-up suggestions."""
    next_levels = {"L1": "L2 Adopter", "L2": "L3 Practitioner",
                   "L3": "L4 Engineer", "L4": "L5 Maestro"}
    target = next_levels.get(level)
    if not target:
        print("  🎯 You're at the highest level! Focus on mentoring others.")
        return

    print(f"  ── To reach {target}: ──")

    tips = []
    if context_rate < 0.5:
        tips.append(
            f"  CONTEXT PROVISION (you: {context_rate:.0%})\n"
            f"    Try referencing specific files and line numbers:\n"
            f'    "In src/auth/middleware.ts, the JWT validation at line 42\n'
            f'     doesn\'t handle expired tokens. Add a refresh flow that..."'
        )
    if first_attempt < 0.6:
        tips.append(
            f"  FIRST-ATTEMPT SUCCESS (you: {first_attempt:.0%})\n"
            f"    Before prompting, ask yourself:\n"
            f'    "What does the agent need to know to get this right\n'
            f'     the first time?" Include constraints and examples.'
        )
    if correction > 0.2:
        tips.append(
            f"  CORRECTION RATE (you: {correction:.0%})\n"
            f"    Your follow-ups often add context that should have been\n"
            f"    in the original prompt. Front-load requirements."
        )
    if prompt_len < 200:
        tips.append(
            f"  PROMPT DETAIL (avg: {prompt_len:.0f} chars)\n"
            f"    Longer, more specific prompts reduce back-and-forth.\n"
            f"    Include: what, where, why, constraints, and examples."
        )

    for i, tip in enumerate(tips[:3], 1):
        print(f"\n  {i}. {tip}")
    print()


def cmd_gather(args):
    """Export session data for allowed workspaces to a local file."""
    db_path = find_cass_db()
    if not db_path:
        print("ERROR: Could not find CASS database.", file=sys.stderr)
        sys.exit(1)

    bootblock = Path(args.bootblock) if args.bootblock else BOOTBLOCK_FILE
    if not bootblock.exists():
        print(f"ERROR: {bootblock} not found. Run `skillbench scan` first.", file=sys.stderr)
        sys.exit(1)

    allowed_paths = []
    for line in bootblock.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = line.split("  #")[0].strip()
        if path and Path(path).is_absolute():
            allowed_paths.append(path)

    if not allowed_paths:
        print("No paths enabled in bootblock.txt.")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Include Gemini hash/basename paths so we also capture Gemini CLI sessions
    query_paths = expand_with_gemini_hashes(allowed_paths)
    placeholders = ",".join("?" * len(query_paths))
    workspace_rows = conn.execute(
        f"SELECT id, path FROM workspaces WHERE path IN ({placeholders})",
        query_paths
    ).fetchall()
    ws_ids = [r["id"] for r in workspace_rows]

    if not ws_ids:
        print("No matching workspaces found.")
        conn.close()
        sys.exit(1)

    ws_placeholders = ",".join("?" * len(ws_ids))

    # Build reverse map: Gemini hash workspace -> real project path
    gemini_to_real = {}
    for real_path in allowed_paths:
        gem_hash_path = str(GEMINI_TMP_DIR / gemini_hash_for_path(real_path))
        gemini_to_real[gem_hash_path] = real_path

    def resolve_workspace(ws_path: str) -> str:
        """Map Gemini hash workspace back to real project path."""
        if _is_gemini_hash_path(ws_path):
            return gemini_to_real.get(ws_path, ws_path)
        return ws_path

    # Build workspace -> git remote lookup (using resolved paths)
    ws_remotes = {}
    for r in workspace_rows:
        resolved = resolve_workspace(r["path"])
        if resolved not in ws_remotes:
            remote = git_remote_url(resolved) if Path(resolved).is_dir() else None
            ws_remotes[resolved] = remote

    # Normalize CASS message roles to a clean set: user, agent
    # CASS uses inconsistent roles (e.g. "gemini" instead of "agent",
    # "developer" for system prompts, "info"/"error" for metadata).
    ROLE_MAP = {
        "user": "user",
        "agent": "agent",
        "tool": "agent",        # tool responses are agent-side content
        "gemini": "agent",      # Gemini responses stored with role=gemini
        "developer": "user",    # system/developer prompts → user
    }
    # Roles to drop entirely (not meaningful for analysis)
    DROP_ROLES = {"info", "error"}

    # Export conversations with messages
    conversations = conn.execute(f"""
        SELECT c.id, c.external_id, c.started_at, c.ended_at,
               c.source_path, c.title, c.approx_tokens,
               a.slug as agent, w.path as workspace
        FROM conversations c
        JOIN agents a ON c.agent_id = a.id
        JOIN workspaces w ON c.workspace_id = w.id
        WHERE c.workspace_id IN ({ws_placeholders})
        ORDER BY c.started_at
    """, ws_ids).fetchall()

    full_mode = getattr(args, "full", False)
    raw_success = 0
    raw_fallback = 0

    export = []
    for conv in conversations:
        resolved_ws = resolve_workspace(conv["workspace"])
        source_path = conv["source_path"]

        # --full mode: read raw JSONL for full tool payloads
        if full_mode and source_path:
            raw_msgs = _parse_raw_session(source_path, conv["agent"])
            if raw_msgs is not None:
                raw_success += 1
                export.append({
                    "session_id": conv["external_id"],
                    "agent": conv["agent"],
                    "workspace": resolved_ws,
                    "git_remote": ws_remotes.get(resolved_ws),
                    "source_path": source_path,
                    "title": conv["title"],
                    "started_at": conv["started_at"],
                    "ended_at": conv["ended_at"],
                    "approx_tokens": conv["approx_tokens"],
                    "messages": raw_msgs,
                    "full_fidelity": True,
                })
                continue
            else:
                raw_fallback += 1
                # Fall through to CASS flat content

        # Default: read from CASS messages table (flat content)
        messages = conn.execute("""
            SELECT role, created_at, content
            FROM messages
            WHERE conversation_id = ?
            ORDER BY idx
        """, (conv["id"],)).fetchall()

        normalized_msgs = []
        for m in messages:
            if m["role"] in DROP_ROLES:
                continue
            normalized_msgs.append({
                "role": ROLE_MAP.get(m["role"], "agent"),
                "created_at": m["created_at"],
                "content": m["content"],
            })

        export.append({
            "session_id": conv["external_id"],
            "agent": conv["agent"],
            "workspace": resolved_ws,
            "git_remote": ws_remotes.get(resolved_ws),
            "source_path": source_path,
            "title": conv["title"],
            "started_at": conv["started_at"],
            "ended_at": conv["ended_at"],
            "approx_tokens": conv["approx_tokens"],
            "messages": normalized_msgs,
            "full_fidelity": False,
        })

    conn.close()

    # Write to file
    split = getattr(args, "split", "weekly")
    if args.output or split == "none":
        output_path = Path(args.output) if args.output else DIST_DIR / "skillbench_export.json"
        output_path.write_text(json.dumps(export, indent=2, default=str))
        output_paths = [output_path]
        total_msgs = sum(len(c["messages"]) for c in export)
        print(f"Exported {len(export)} conversations ({total_msgs} messages) to {output_path}")

    elif split == "weekly":
        by_week = defaultdict(list)
        for conv in export:
            ts = conv.get("started_at")
            if ts:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                week_label = f"{dt.isocalendar().year}_W{dt.isocalendar().week:02d}"
            else:
                week_label = "unknown"
            by_week[week_label].append(conv)
        output_paths = []
        for week_label, convs in sorted(by_week.items()):
            out = DIST_DIR / f"skillbench_export_{week_label}.json"
            out.write_text(json.dumps(convs, indent=2, default=str))
            total_msgs = sum(len(c["messages"]) for c in convs)
            print(f"  {week_label}: {len(convs)} conversations ({total_msgs} messages) → {out.name}")
            output_paths.append(out)
        output_path = output_paths[-1] if output_paths else DIST_DIR / "skillbench_export.json"

    elif split == "session":
        output_paths = []
        for conv in export:
            session_id = conv.get("session_id", "unknown")
            out = DIST_DIR / f"skillbench_export_{session_id}.json"
            out.write_text(json.dumps([conv], indent=2, default=str))
            output_paths.append(out)
        print(f"Exported {len(export)} sessions to {DIST_DIR}")
        output_path = output_paths[-1] if output_paths else DIST_DIR / "skillbench_export.json"

    if full_mode:
        full_count = sum(1 for c in export if c.get("full_fidelity"))
        flat_count = len(export) - full_count
        print(f"  Full-fidelity (raw JSONL): {full_count} sessions")
        if flat_count:
            print(f"  Fell back to CASS flat content: {flat_count} sessions")
        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"  Export size: {size_mb:.1f} MB")

    print()
    print("⚠  IMPORTANT: Sanitize before sharing!")
    print("   The export contains raw conversation data that may include secrets,")
    print("   API keys, PII, internal URLs, and other sensitive information.")
    if full_mode:
        print()
        print("   ⚠  FULL-FIDELITY MODE: The export includes complete code diffs,")
        print("   file contents, and command outputs. Sanitization is CRITICAL.")
    print()
    skill_path = "skills/sanitize-export/SKILL.md"
    print("   Ask your AI agent to run the sanitize-export skill:")
    print()
    print(f"     Read {skill_path} and follow its instructions to sanitize {output_path}")
    print()
    print("   The skill contains a comprehensive taxonomy of sensitive data categories")
    print("   (secrets, PII, infrastructure, analytics, org data, git, compliance, etc.)")
    print("   and will guide the agent through discovery, reporting, and redaction.")
    print()
    print("   After sanitization, run `skillbench push` to upload the clean data.")


def cmd_push(args):
    """Upload sanitized session data to SkillBench API (not yet implemented)."""
    sanitized = DIST_DIR / "skillbench_export_sanitized.json"
    raw_export = DIST_DIR / "skillbench_export.json"
    skill_path = "skills/sanitize-export/SKILL.md"

    if sanitized.exists():
        print(f"Found sanitized export: {sanitized}")
        print("Server push is not yet implemented — check back soon.")
        return

    if raw_export.exists():
        print(f"⚠  Found raw export ({raw_export}) but no sanitized version.")
        print(f"   You must sanitize before pushing.")
        print()
        print("   Ask your AI agent:")
        print()
        print(f"     Read {skill_path} and follow its instructions to sanitize {raw_export}")
        print()
        print("   The skill guides the agent through a comprehensive check of 10 categories")
        print("   (secrets, PII, infrastructure, file paths, analytics, org data, git info,")
        print("   communications, runtime/debug data, and compliance-sensitive data).")
        print()
        print("   After sanitization, run `skillbench push` again.")
    else:
        print("No export found. Run `skillbench gather` first.")
        print()
        print("Full workflow:")
        print("  1. skillbench scan       → classify workspaces")
        print("  2. skillbench gather     → export session data")
        print(f"  3. Sanitize: ask your AI agent to read {skill_path}")
        print("  4. skillbench push       → upload sanitized data")


# ---------------------------------------------------------------------------
# Direct metrics computation (no CASS dependency)
# ---------------------------------------------------------------------------

def _compute_metrics(conversations) -> dict | None:
    """Compute Tier 1-3 agentic engineering metrics from Conversation objects.

    Works directly with session_parser.Conversation objects — no CASS/SQL needed.
    Returns a dict matching the JSON report structure, or None if no data.
    """
    total_convos = len(conversations)
    if total_convos == 0:
        return None

    # Date range
    timestamps = []
    for c in conversations:
        if c.started_at:
            timestamps.append(c.started_at)
        if c.ended_at:
            timestamps.append(c.ended_at)

    if timestamps:
        first_ts = min(timestamps)
        last_ts = max(timestamps)
        first_date = datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc)
        last_date = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
        span_days = max((last_date - first_date).days, 1)
        span_weeks = max(span_days / 7, 1)
    else:
        span_weeks = span_days = 1
        first_date = last_date = None

    sessions_per_week = total_convos / span_weeks
    agents_used = set(c.agent for c in conversations)

    # Active days
    active_days = set()
    for c in conversations:
        if c.started_at:
            day = datetime.fromtimestamp(c.started_at / 1000, tz=timezone.utc).date()
            active_days.add(day)
    active_days_per_week = len(active_days) / span_weeks

    # Session durations
    durations = []
    for c in conversations:
        if c.started_at and c.ended_at and c.ended_at > c.started_at:
            dur_min = (c.ended_at - c.started_at) / 60000
            if dur_min < 480:  # cap at 8 hours
                durations.append(dur_min)
    avg_session_duration = sum(durations) / len(durations) if durations else 0

    # User messages
    user_messages = []
    msgs_per_conv = defaultdict(int)
    for c in conversations:
        for m in c.messages:
            if m.role == "user":
                user_messages.append(m)
                msgs_per_conv[c.session_id] += 1

    total_user_msgs = len(user_messages)
    avg_prompt_length = (
        sum(len(m.content) for m in user_messages) / total_user_msgs
        if total_user_msgs else 0
    )

    # Context provision rate
    context_indicators = re.compile(
        r'(/[\w./-]+\.\w+|```|line \d+|src/|lib/|test/|\.py|\.ts|\.js|\.go|\.rs|\.dart)',
        re.IGNORECASE
    )
    context_count = sum(1 for m in user_messages if context_indicators.search(m.content or ""))
    context_provision_rate = context_count / total_user_msgs if total_user_msgs else 0

    # Multi-step rate: sessions with >3 user turns
    multi_step = sum(1 for count in msgs_per_conv.values() if count > 3)
    multi_step_rate = multi_step / total_convos if total_convos else 0

    # First attempt success: sessions with <=2 user messages
    first_attempt = sum(1 for count in msgs_per_conv.values() if count <= 2)
    first_attempt_success_rate = first_attempt / total_convos if total_convos else 0

    # Correction rate
    correction_re = re.compile(
        r'\b(no[,.]|wrong|incorrect|instead|actually|not what|try again|that\'s not|fix |redo)\b',
        re.IGNORECASE
    )
    correction_count = sum(1 for m in user_messages if correction_re.search(m.content or ""))
    correction_rate = correction_count / total_user_msgs if total_user_msgs else 0

    # Average turns
    avg_turns = sum(msgs_per_conv.values()) / len(msgs_per_conv) if msgs_per_conv else 0

    # Ladder score
    score = compute_ladder_score(
        sessions_per_week=sessions_per_week,
        avg_prompt_length=avg_prompt_length,
        context_provision_rate=context_provision_rate,
        first_attempt_success_rate=first_attempt_success_rate,
        correction_rate=correction_rate,
        multi_step_rate=multi_step_rate,
        avg_turns=avg_turns,
        agent_diversity=len(agents_used),
    )
    level, level_name = ladder_level(score)

    return {
        "level": level,
        "level_name": level_name,
        "score": score,
        "date_range": {
            "start": str(first_date.date()) if first_date else None,
            "end": str(last_date.date()) if last_date else None,
            "span_days": span_days,
        },
        "tier1": {
            "total_conversations": total_convos,
            "total_user_messages": total_user_msgs,
            "sessions_per_week": round(sessions_per_week, 1),
            "active_days_per_week": round(active_days_per_week, 1),
            "avg_session_duration_min": round(avg_session_duration, 1),
            "agents_used": sorted(agents_used),
        },
        "tier2": {
            "avg_prompt_length": round(avg_prompt_length),
            "context_provision_rate": round(context_provision_rate, 3),
            "multi_step_rate": round(multi_step_rate, 3),
        },
        "tier3": {
            "first_attempt_success_rate": round(first_attempt_success_rate, 3),
            "correction_rate": round(correction_rate, 3),
            "avg_turns_per_session": round(avg_turns, 1),
        },
        "_raw": {
            "sessions_per_week": sessions_per_week,
            "avg_prompt_length": avg_prompt_length,
            "context_provision_rate": context_provision_rate,
            "first_attempt_success_rate": first_attempt_success_rate,
            "correction_rate": correction_rate,
            "multi_step_rate": multi_step_rate,
        },
    }


# ---------------------------------------------------------------------------
# Unified collect command (replaces scan → analyze → gather → sanitize)
# ---------------------------------------------------------------------------

def cmd_collect(args):
    """One-command pipeline: scan → classify → analyze → export → sanitize.

    Replaces the manual 4-step workflow with a single interactive command.
    No CASS dependency — reads sessions directly from local agent log files.
    """
    from session_parser import SessionScanner
    from sanitizer import Sanitizer

    print("=" * 60)
    print("  SkillBench Collect")
    print("=" * 60)

    # --- Step 1: Scan ---
    print("\n[1/5] Scanning for coding agent sessions...")
    scanner = SessionScanner()
    scanner.scan_all(verbose=True)
    resolved = scanner.resolve_gemini_hashes()
    if resolved:
        print(f"  Resolved {resolved} Gemini hash dir(s) to real project paths")

    if not scanner.conversations:
        print("\nNo sessions found.")
        print("Supported agents: Claude Code, Gemini CLI, Codex CLI")
        sys.exit(1)

    # --- Step 2: Classify ---
    summaries = scanner.get_workspace_summary()
    print(f"\n[2/5] Classifying {len(summaries)} workspaces...")

    included = []
    excluded = []
    skipped_count = 0

    for i, ws in enumerate(summaries):
        path = ws["workspace"]
        if is_skippable(path):
            skipped_count += 1
            continue
        if not Path(path).is_dir():
            skipped_count += 1
            continue

        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(summaries)}] classifying...", end="\r")

        remote = git_remote_url(path)
        gh = classify_github_repo(remote) if remote else None

        if gh:
            public = gh["is_public"]
            license_key = gh["license_key"]
            license_name = gh["license_name"]
            oss = license_key is not None and license_key != "other"
            license_id = license_name or license_key
        else:
            public = None
            license_id = detect_license_local(path)
            oss = False

        entry = {
            "path": path,
            "remote": remote,
            "license": license_id,
            "public": public,
            "oss": oss,
            "agents": ws["agents"],
            "conversations": ws["total_conversations"],
            "user_messages": ws["total_user_messages"],
        }

        if public and oss:
            entry["reason"] = f"{license_id}, public"
            included.append(entry)
        else:
            reasons = []
            if gh:
                if not public:
                    reasons.append("private")
                if license_key is None:
                    reasons.append("no LICENSE")
                elif license_key == "other":
                    reasons.append("unrecognized license")
            else:
                if not remote:
                    reasons.append("no git remote")
                else:
                    reasons.append("not GitHub")
            entry["reason"] = ", ".join(reasons) if reasons else "unknown"
            excluded.append(entry)

    # Show classification
    print(f"\n  Auto-included (public + OSS license): {len(included)}")
    for e in included:
        agents = ", ".join(e["agents"])
        print(f"    {e['path']}")
        print(f"      {e['conversations']} sessions, {e['reason']}, {agents}")

    print(f"  Excluded (private/no-license): {len(excluded)}")
    if len(excluded) <= 10:
        for e in excluded:
            print(f"    {e['path']}  ({e['reason']})")
    else:
        for e in excluded[:5]:
            print(f"    {e['path']}  ({e['reason']})")
        print(f"    ... and {len(excluded) - 5} more")

    print(f"  Skipped (temp/transient): {skipped_count}")

    include_excluded = getattr(args, "include_excluded", False)
    allowed_paths = [e["path"] for e in included]

    if not allowed_paths and include_excluded:
        # Explicit opt-in: proceed with excluded workspaces too.
        allowed_paths = [e["path"] for e in (included + excluded)]

    if not allowed_paths:
        print("\nNo public/OSS workspaces found to include.")
        if excluded and not getattr(args, "yes", False):
            print("\nYou have private/unlicensed workspaces with session data.")
            print("Data from these workspaces will be PII-scrubbed before export.")
            print("Only sessions (your prompts + AI responses) are collected — not source code.\n")
            print("Select workspaces to include:\n")
            for i, e in enumerate(excluded):
                agents = ", ".join(e["agents"]) if "agents" in e else "unknown"
                convs = e.get("conversations", "?")
                print(f"  [{i+1}] {e['path']}")
                print(f"      {convs} sessions, {agents} ({e['reason']})")
            print(f"\n  [a] Include all")
            print(f"  [n] Skip — exit without exporting\n")
            try:
                response = input("Enter numbers separated by commas, 'a' for all, or 'n' to skip: ").strip().lower()
                if response in ("n", "no", ""):
                    print("Aborted.")
                    sys.exit(0)
                elif response == "a":
                    allowed_paths = [e["path"] for e in excluded]
                    print(f"\n  Including all {len(allowed_paths)} workspaces.")
                else:
                    indices = [int(x.strip()) - 1 for x in response.split(",")]
                    allowed_paths = [excluded[i]["path"] for i in indices if 0 <= i < len(excluded)]
                    if not allowed_paths:
                        print("No valid selections. Aborted.")
                        sys.exit(0)
                    print(f"\n  Including {len(allowed_paths)} workspace(s).")
            except (KeyboardInterrupt, EOFError):
                print("\nAborted.")
                sys.exit(0)
            except (ValueError, IndexError):
                print("Invalid input. Aborted.")
                sys.exit(0)
        else:
            print("No workspaces available to collect from.")
            print("Or rerun: skillbench collect --include-excluded")
            sys.exit(1)

    # Interactive confirmation
    if not getattr(args, "yes", False):
        print()
        try:
            response = input("Continue with these workspaces? [Y/n] ")
            if response.strip().lower() in ("n", "no"):
                print("Aborted. Use `skillbench scan` to manually edit the workspace list.")
                sys.exit(0)
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

    # Write bootblock.txt for compatibility with scan/analyze/gather
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    with open(BOOTBLOCK_FILE, "w") as f:
        f.write("# SkillBench Boot Block — auto-generated by `skillbench collect`\n")
        f.write("# Edit this file to add/remove workspaces\n#\n")
        for e in included:
            agents = ",".join(e["agents"])
            f.write(f"{e['path']}  # {e['reason']}, {agents}, {e['conversations']} sessions\n")
        f.write("\n# EXCLUDED (uncomment to include):\n")
        for e in excluded:
            f.write(f"# {e['path']}  # {e['reason']}\n")

    # --- Step 3: Analyze ---
    print(f"\n[3/5] Computing agentic engineering metrics...")
    conversations = scanner.get_conversations_for_workspaces(allowed_paths)

    if not conversations:
        print("No conversations found for the included workspaces.")
        sys.exit(1)

    metrics = _compute_metrics(conversations)
    if not metrics:
        print("Could not compute metrics (no data).")
        sys.exit(1)

    # Print compact report
    t1 = metrics["tier1"]
    t2 = metrics["tier2"]
    t3 = metrics["tier3"]
    raw = metrics["_raw"]
    dr = metrics["date_range"]

    print()
    print("  " + "─" * 56)
    bar = "█" * (metrics["score"] // 5) + "░" * (20 - metrics["score"] // 5)
    print(f"  {metrics['level']} {metrics['level_name']}  {bar}  {metrics['score']}/100")
    print("  " + "─" * 56)

    if dr["start"] and dr["end"]:
        print(f"  {dr['start']} → {dr['end']} ({dr['span_days']} days)")
    print(f"  {t1['total_conversations']} sessions, {t1['total_user_messages']} user messages")
    print(f"  {t1['sessions_per_week']} sessions/week, {t1['active_days_per_week']} active days/week")
    print(f"  Agents: {', '.join(t1['agents_used'])}")
    print()
    print(f"  Prompts: avg {t2['avg_prompt_length']} chars, "
          f"context {t2['context_provision_rate']:.0%}, "
          f"multi-step {t2['multi_step_rate']:.0%}")
    print(f"  Efficiency: first-attempt {t3['first_attempt_success_rate']:.0%}, "
          f"correction {t3['correction_rate']:.0%}, "
          f"avg {t3['avg_turns_per_session']} turns")
    print()

    # Strengths and edges
    strengths, edges = identify_strengths_and_edges(
        raw["sessions_per_week"], raw["avg_prompt_length"],
        raw["context_provision_rate"], raw["first_attempt_success_rate"],
        raw["correction_rate"], raw["multi_step_rate"],
    )
    if strengths:
        print("  Strengths: " + " · ".join(strengths))
    if edges:
        print("  Growth edges: " + " · ".join(edges))
    print()

    # Write JSON report (without _raw key)
    report = {k: v for k, v in metrics.items() if k != "_raw"}
    report_path = DIST_DIR / "skillbench_report.json"
    report_path.write_text(json.dumps(report, indent=2))

    # --- Step 4: Export ---
    print(f"[4/5] Exporting {len(conversations)} sessions...")

    remote_cache = {}
    export_data = []
    for conv in conversations:
        ws = conv.workspace
        if ws not in remote_cache:
            remote_cache[ws] = git_remote_url(ws) if Path(ws).is_dir() else None
        d = conv.to_dict()
        d["git_remote"] = remote_cache[ws]
        export_data.append(d)

    # --- Step 5: Sanitize ---
    print(f"\n[5/5] Sanitizing...")
    sanitizer = Sanitizer()
    sanitized = sanitizer.sanitize_export(export_data)
    sanitizer.print_stats()

    # Write sanitized export
    split = getattr(args, "split", "weekly")

    if args.output or split == "none":
        output_path = Path(args.output) if args.output else DIST_DIR / "skillbench_export_sanitized.json"
        with open(output_path, "w") as f:
            json.dump(sanitized, f, indent=2, default=str)
        output_paths = [output_path]

    elif split == "weekly":
        by_week = defaultdict(list)
        for conv in sanitized:
            dt_str = conv.get("started_at")
            if dt_str:
                dt = datetime.fromisoformat(str(dt_str)) if isinstance(dt_str, str) else datetime.fromtimestamp(dt_str / 1000)
                week_label = f"{dt.isocalendar().year}_W{dt.isocalendar().week:02d}"
            else:
                week_label = "unknown"
            by_week[week_label].append(conv)
        output_paths = []
        for week_label, convs in sorted(by_week.items()):
            out = DIST_DIR / f"skillbench_export_sanitized_{week_label}.json"
            with open(out, "w") as f:
                json.dump(convs, f, indent=2, default=str)
            print(f"  {week_label}: {len(convs)} sessions → {out.name}")
            output_paths.append(out)
        output_path = output_paths[-1] if output_paths else DIST_DIR / "skillbench_export_sanitized.json"

    elif split == "session":
        output_paths = []
        for conv in sanitized:
            session_id = conv.get("session_id", "unknown")
            out = DIST_DIR / f"skillbench_export_sanitized_{session_id}.json"
            with open(out, "w") as f:
                json.dump([conv], f, indent=2, default=str)
            output_paths.append(out)
        output_path = output_paths[-1] if output_paths else DIST_DIR / "skillbench_export_sanitized.json"
        print(f"  {len(sanitized)} session files → {DIST_DIR}")


    total_msgs = sum(len(s.get("messages", [])) for s in sanitized)
    size_mb = sum(p.stat().st_size for p in output_paths) / (1024 * 1024)

    # --- Bonus: Generate dashboard ---
    dashboard_path = None
    try:
        from ce_engine import compute_dashboard_data, generate_html
        print(f"\nGenerating dashboard...")
        dashboard_data = compute_dashboard_data(sanitized, report)
        html = generate_html(dashboard_data)
        dashboard_path = DIST_DIR / "dashboard.html"
        dashboard_path.write_text(html)
        # Also write dashboard_data.json
        data_json_path = DIST_DIR / "dashboard_data.json"
        with open(data_json_path, "w") as f:
            json.dump(dashboard_data, f, indent=2, default=str)
        print(f"  Dashboard: {dashboard_path}")
    except Exception as e:
        print(f"  Dashboard generation skipped: {e}")

    # Count workspaces
    workspace_set = set()
    for s in sanitized:
        ws = s.get("workspace", "")
        if ws:
            workspace_set.add(ws)

    print(f"\n{'=' * 60}")
    print(f"  Collection complete!")
    print(f"  {len(sanitized)} sessions, {total_msgs} messages, {len(workspace_set)} workspace(s)")
    print(f"  Total size: {size_mb:.1f} MB")
    print(f"  Report:    {report_path}")
    if split == "none" or args.output:
        print(f"  Export:    {output_path}")
    else:
        print(f"  Exports ({len(output_paths)} files):")
        for p in output_paths:
            print(f"    {p.name}")
    if dashboard_path:
        print(f"  Dashboard: {dashboard_path}")
    print(f"{'=' * 60}")
    print()

    if args.upload_guide:
        print(f"  >> Send these files to the SkillBench team:")
        if split == "none" or args.output:
            print(f"     {output_path}")
        else:
            for p in output_paths:
                print(f"     {p}")
        print()
        print(f"  Upload to: https://drive.google.com/drive/folders/1SOrF1-VkCMoUxM4DDUZ2rD6rjlR7TFXP")
        print()
    print(f"  This file has been sanitized — API keys, emails, IPs, and home")
    print(f"  paths have been redacted. No private repo data is included unless")
    print(f"  you explicitly selected it above.")
    if dashboard_path:
        print(f"\n  Open {dashboard_path} in a browser to preview your results.")

def cmd_dashboard(args):
    """Generate a standalone HTML dashboard from export + report data."""
    from ce_engine import build_dashboard

    export_path = Path(args.export)
    report_path = None
    if args.report:
        report_path = Path(args.report)
    else:
        auto_report = export_path.parent / "skillbench_report.json"
        if auto_report.exists():
            report_path = auto_report
    output_path = Path(args.output) if args.output else DIST_DIR / "dashboard.html"
    commit_path = Path(args.commits) if args.commits else None

    if not export_path.exists():
        print(f"ERROR: Export file not found: {export_path}", file=sys.stderr)
        sys.exit(1)

    result = build_dashboard(
        export_path, report_path, output_path,
        commit_data_path=commit_path,
        verbose=True,
    )
    print(f"\nOpen {result} in a browser to view your dashboard.")


# ---------------------------------------------------------------------------
# Raw JSONL session parsing (bypasses CASS flattening)
# ---------------------------------------------------------------------------

# Message types in raw JSONL that carry conversation content
_CONTENT_TYPES = {"user", "assistant"}


def _resolve_persisted_output(text: str, source_path: str) -> str:
    """Replace <persisted-output> references with actual file contents."""
    if "<persisted-output>" not in text:
        return text

    import re as _re
    session_dir = Path(source_path).parent / Path(source_path).stem
    # Pattern: <persisted-output>\nOutput too large (XXX). Full output saved to: /path/to/file\n</persisted-output>
    # Or just check for the saved-to path and read it
    match = _re.search(r"Full output saved to: (.+?)[\n<]", text)
    if match:
        ref_path = Path(match.group(1).strip())
        if ref_path.exists():
            try:
                return ref_path.read_text(errors="replace")
            except Exception:
                pass

    # Also check tool-results/ directory in the session dir
    if session_dir.is_dir():
        tool_results_dir = session_dir / "tool-results"
        if tool_results_dir.is_dir():
            # Try to match by tool_use_id in the text
            id_match = _re.search(r"toolu_[A-Za-z0-9]+", text)
            if id_match:
                tool_file = tool_results_dir / f"{id_match.group(0)}.txt"
                if tool_file.exists():
                    try:
                        return tool_file.read_text(errors="replace")
                    except Exception:
                        pass

    return text  # Return original if can't resolve


def _parse_raw_claude(source_path: str) -> list[dict] | None:
    """Parse a Claude Code raw JSONL session file into structured messages.

    Returns a list of messages preserving full tool_use blocks, or None
    if the file can't be read. Each message has:
      - role: "user" or "agent"
      - created_at: ISO timestamp string
      - content: structured content (list of blocks or string)

    Content blocks can be:
      - {"type": "text", "text": "..."}
      - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
      - {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": bool}
      - {"type": "thinking", "thinking": "..."}
    """
    path = Path(source_path)
    if not path.exists():
        return None

    try:
        messages = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")
                if msg_type not in _CONTENT_TYPES:
                    continue

                msg = obj.get("message", {})
                role = msg.get("role", msg_type)
                timestamp = obj.get("timestamp", "")
                content = msg.get("content", "")

                # Normalize role
                if role == "user" or msg_type == "user":
                    role = "user"
                else:
                    role = "agent"

                # Process content blocks
                if isinstance(content, list):
                    processed = []
                    for block in content:
                        if not isinstance(block, dict):
                            processed.append(block)
                            continue

                        block_type = block.get("type", "")

                        if block_type == "text":
                            text = block.get("text", "")
                            text = _resolve_persisted_output(text, source_path)
                            processed.append({"type": "text", "text": text})

                        elif block_type == "tool_use":
                            processed.append({
                                "type": "tool_use",
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            })

                        elif block_type == "tool_result":
                            result_content = block.get("content", "")
                            # Resolve persisted outputs in tool results
                            if isinstance(result_content, str):
                                result_content = _resolve_persisted_output(
                                    result_content, source_path
                                )
                            processed.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("tool_use_id", ""),
                                "content": result_content,
                                "is_error": block.get("is_error", False),
                            })

                        elif block_type == "thinking":
                            processed.append({
                                "type": "thinking",
                                "thinking": block.get("thinking", ""),
                            })

                        else:
                            # Preserve unknown block types as-is
                            processed.append(block)

                    content = processed

                elif isinstance(content, str):
                    content = _resolve_persisted_output(content, source_path)

                messages.append({
                    "role": role,
                    "created_at": timestamp,
                    "content": content,
                })

        return messages if messages else None

    except Exception as e:
        print(f"  WARNING: Could not parse {source_path}: {e}")
        return None


def _parse_raw_codex(source_path: str) -> list[dict] | None:
    """Parse a Codex CLI raw JSONL session file."""
    path = Path(source_path)
    if not path.exists():
        return None
    try:
        messages = []
        pending_thinking = None
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                evt_type = obj.get("type", "")

                # agent_reasoning → thinking block (attach to next assistant)
                if evt_type == "event_msg":
                    payload = obj.get("payload", {})
                    if payload.get("type") == "agent_reasoning":
                        pending_thinking = payload.get("text", "")
                    continue

                if evt_type != "response_item":
                    continue

                payload = obj.get("payload", {})
                role = payload.get("role", "")
                if role == "assistant":
                    role = "agent"
                elif role != "user":
                    continue
                timestamp = obj.get("timestamp", "")
                raw_content = payload.get("content", [])

                blocks = []
                if pending_thinking and role == "agent":
                    blocks.append({"type": "thinking", "thinking": pending_thinking})
                    pending_thinking = None

                if isinstance(raw_content, list):
                    for block in raw_content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "input_text":
                            blocks.append({"type": "text", "text": block.get("text", "")})
                        elif bt == "text":
                            blocks.append({"type": "text", "text": block.get("text", "")})
                        elif bt == "tool_use":
                            blocks.append({
                                "type": "tool_use",
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            })
                        elif bt == "tool_result":
                            blocks.append({
                                "type": "tool_result",
                                "tool_use_id": block.get("tool_use_id", ""),
                                "content": block.get("content", ""),
                                "is_error": block.get("is_error", False),
                            })
                        else:
                            blocks.append(block)
                elif isinstance(raw_content, str):
                    blocks.append({"type": "text", "text": raw_content})

                content = blocks if blocks else raw_content
                messages.append({"role": role, "created_at": timestamp, "content": content})

        return messages if messages else None
    except Exception as e:
        print(f"  WARNING: Could not parse {source_path} (codex): {e}")
        return None


def _parse_raw_pi_agent(source_path: str) -> list[dict] | None:
    """Parse a Pi-Agent raw JSONL session file."""
    path = Path(source_path)
    if not path.exists():
        return None
    try:
        messages = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "message":
                    continue

                msg = obj.get("message", {})
                role = msg.get("role", "")
                timestamp = obj.get("timestamp", "")
                raw_content = msg.get("content", [])

                # toolResult role → tool_result block
                if role == "toolResult":
                    result = msg.get("result", raw_content)
                    content_text = result if isinstance(result, str) else str(result)
                    messages.append({
                        "role": "user",
                        "created_at": timestamp,
                        "content": [{
                            "type": "tool_result",
                            "tool_use_id": msg.get("toolCallId", msg.get("tool_use_id", "")),
                            "content": content_text,
                            "is_error": msg.get("isError", msg.get("is_error", False)),
                        }],
                    })
                    continue

                if role == "assistant":
                    role = "agent"
                elif role != "user":
                    continue

                blocks = []
                if isinstance(raw_content, list):
                    for block in raw_content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "text":
                            blocks.append({"type": "text", "text": block.get("text", "")})
                        elif bt == "thinking":
                            blocks.append({"type": "thinking", "thinking": block.get("thinking", block.get("text", ""))})
                        elif bt == "toolCall":
                            blocks.append({
                                "type": "tool_use",
                                "id": block.get("id", block.get("toolCallId", "")),
                                "name": block.get("name", ""),
                                "input": block.get("arguments", block.get("input", {})),
                            })
                        elif bt == "tool_use":
                            blocks.append({
                                "type": "tool_use",
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            })
                        else:
                            blocks.append(block)
                elif isinstance(raw_content, str):
                    blocks.append({"type": "text", "text": raw_content})

                content = blocks if blocks else (raw_content if isinstance(raw_content, str) else "")
                messages.append({"role": role, "created_at": timestamp, "content": content})

        return messages if messages else None
    except Exception as e:
        print(f"  WARNING: Could not parse {source_path} (pi_agent): {e}")
        return None


def _parse_raw_gemini(source_path: str) -> list[dict] | None:
    """Parse a Gemini CLI session JSON file.

    Gemini CLI stores chats as JSON with a ``messages`` array. Each item has
    a ``type`` field: ``"user"`` for prompts, ``"gemini"`` for model responses,
    and ``"info"``/``"error"``/``"warning"`` for system messages.

    Model responses (``type: "gemini"``) may additionally carry:
      - ``toolCalls``: list of tool invocations with args/results
      - ``thoughts``: list of thinking summaries
      - ``model``: model ID string (e.g. ``gemini-3-pro-preview``)
      - ``tokens``: token usage summary
    """
    path = Path(source_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(errors="replace"))
        raw_msgs = data.get("messages", []) if isinstance(data, dict) else data if isinstance(data, list) else []

        messages = []
        for item in raw_msgs:
            msg_type = item.get("type", item.get("role", ""))

            # Map type → normalized role
            if msg_type in ("gemini", "model", "assistant"):
                role = "agent"
            elif msg_type == "user":
                role = "user"
            else:
                # Skip info/error/warning system messages
                continue

            timestamp = item.get("timestamp", "")
            if not isinstance(timestamp, str):
                timestamp = ""

            # Extract text content
            raw_content = item.get("content", item.get("text", ""))
            if isinstance(raw_content, list):
                text = "\n".join(str(p) for p in raw_content)
            elif isinstance(raw_content, str):
                text = raw_content
            else:
                text = str(raw_content) if raw_content else ""

            thoughts = item.get("thoughts", [])
            tool_calls = item.get("toolCalls", [])

            # Simple message (text only, no tools/thoughts): keep as string
            if not thoughts and not tool_calls:
                content = text
            else:
                # Build structured content blocks
                blocks = []
                for thought in thoughts:
                    desc = thought.get("description", "") if isinstance(thought, dict) else str(thought)
                    if desc:
                        blocks.append({"type": "thinking", "thinking": desc})
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("name", ""),
                        "input": tc.get("args", {}),
                    })
                    result = tc.get("result")
                    if result is not None:
                        result_text = result if isinstance(result, str) else json.dumps(result, default=str)
                        blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tc.get("id", ""),
                            "content": result_text,
                            "is_error": tc.get("status", "") == "error",
                        })
                content = blocks if blocks else text
            messages.append({"role": role, "created_at": timestamp, "content": content})

        return messages if messages else None
    except Exception as e:
        print(f"  WARNING: Could not parse {source_path} (gemini): {e}")
        return None


# Parser registry — maps agent slug to raw session parser
_RAW_PARSERS = {
    "claude": _parse_raw_claude,
    "claude_code": _parse_raw_claude,
    "codex": _parse_raw_codex,
    "pi_agent": _parse_raw_pi_agent,
    "gemini": _parse_raw_gemini,
    "gemini_cli": _parse_raw_gemini,
}


def _parse_raw_session(source_path: str, agent_slug: str = "claude") -> list[dict] | None:
    """Dispatch to the appropriate raw session parser for the given agent.

    Returns structured messages or None if no parser exists / parsing fails.
    """
    parser = _RAW_PARSERS.get(agent_slug)
    if parser is None:
        return None
    try:
        return parser(source_path)
    except Exception as e:
        print(f"  WARNING: Could not parse {source_path} ({agent_slug}): {e}")
        return None


# ---------------------------------------------------------------------------
# GitHub data collection helpers
# ---------------------------------------------------------------------------

def _extract_repo_slug(git_remote_url: str) -> str | None:
    """Convert https://github.com/owner/repo.git → owner/repo"""
    try:
        parsed = urlparse(git_remote_url)
        if not parsed.hostname or "github.com" not in parsed.hostname:
            return None
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    except Exception:
        pass
    return None


def _gh_api(endpoint: str, params: dict = None) -> dict | list | None:
    """Call gh api and return parsed JSON."""
    cmd = ["gh", "api", endpoint, "--method", "GET"]
    if params:
        for k, v in params.items():
            cmd.extend(["-f", f"{k}={v}"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            if "404" in result.stderr or "Not Found" in result.stderr:
                return None
            if "409" in result.stderr:  # empty repo
                return None
            print(f"  WARNING: gh api error for {endpoint}: {result.stderr.strip()[:120]}")
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print(f"  WARNING: timeout for {endpoint}")
        return None
    except json.JSONDecodeError:
        return None


def _repos_from_export(export_path: Path) -> Counter:
    """Extract GitHub repo slugs and session counts from an export file."""
    with open(export_path) as f:
        sessions = json.load(f)
    counts = Counter()
    for s in sessions:
        r = s.get("git_remote")
        if r:
            slug = _extract_repo_slug(r)
            if slug:
                counts[slug] += 1
    return counts


def _infer_author_from_export(export_path: Path) -> str | None:
    """Try to infer the GitHub username from repo ownership in the export."""
    counts = _repos_from_export(export_path)
    # Most repos are owned by the user — find the most common owner
    owners = Counter()
    for slug in counts:
        owner = slug.split("/")[0]
        owners[owner] += counts[slug]
    if owners:
        return owners.most_common(1)[0][0]
    return None


# ---------------------------------------------------------------------------
# collect-commits command
# ---------------------------------------------------------------------------

def cmd_collect_commits(args):
    """Fetch commit history from GitHub for repos in the export."""
    export_path = Path(args.export)
    if not export_path.exists():
        print(f"Export not found: {export_path}")
        print("Run `skillbench gather` first, then sanitize the export.")
        return

    author = args.author
    if not author:
        author = _infer_author_from_export(export_path)
        if not author:
            print("Could not infer GitHub username from export.")
            print("Provide it explicitly: skillbench collect-commits --author USERNAME")
            return
        print(f"Inferred GitHub author: {author}")

    since = args.since or "2020-01-01T00:00:00Z"
    sleep_ms = 0.05  # 50ms between API calls

    # Check gh CLI
    try:
        subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10)
    except FileNotFoundError:
        print("ERROR: gh CLI not found. Install with: brew install gh")
        sys.exit(1)

    print(f"Loading export from {export_path}...")
    remote_counts = _repos_from_export(export_path)
    print(f"Found {len(remote_counts)} GitHub repos across {sum(remote_counts.values())} sessions\n")

    repos = {}
    repos_no_commits = []
    total_commits = 0
    total_api_calls = 0

    for slug, session_count in remote_counts.most_common():
        print(f"[{slug}] ({session_count} sessions)")

        # Fetch commits
        all_commits = []
        page = 1
        while True:
            print(f"    page {page}...", end=" ", flush=True)
            data = _gh_api(
                f"repos/{slug}/commits",
                {"author": author, "since": since, "per_page": "100", "page": str(page)},
            )
            time.sleep(sleep_ms)
            total_api_calls += 1

            if not data or not isinstance(data, list) or len(data) == 0:
                print("done")
                break

            for item in data:
                ci = item.get("commit", {})
                ai = ci.get("author", {})
                all_commits.append({
                    "sha": item.get("sha", "")[:12],
                    "date": ai.get("date", ""),
                    "message": ci.get("message", "").split("\n")[0][:120],
                })
            print(f"{len(data)} commits")
            if len(data) < 100:
                break
            page += 1

        if not all_commits:
            print(f"  → 0 commits by {author}")
            repos_no_commits.append(slug)
            repos[slug] = {"commits": [], "total_commits": 0, "sessions_count": session_count}
            continue

        # Fetch stats per commit
        print(f"  → {len(all_commits)} commits, fetching stats...")
        enriched = []
        for i, commit in enumerate(all_commits):
            if (i + 1) % 20 == 0 or i == 0:
                print(f"    stats {i+1}/{len(all_commits)}...", flush=True)
            data = _gh_api(f"repos/{slug}/commits/{commit['sha']}")
            time.sleep(sleep_ms)
            total_api_calls += 1
            stats = {}
            if data and "stats" in data:
                s = data["stats"]
                stats = {"additions": s.get("additions", 0), "deletions": s.get("deletions", 0),
                         "files_changed": len(data.get("files", []))}
            else:
                stats = {"additions": 0, "deletions": 0, "files_changed": 0}
            enriched.append({**commit, **stats})

        total_add = sum(c.get("additions", 0) for c in enriched)
        total_del = sum(c.get("deletions", 0) for c in enriched)
        print(f"  → {len(enriched)} commits, +{total_add}/-{total_del} lines\n")
        total_commits += len(enriched)

        repos[slug] = {"commits": enriched, "total_commits": len(enriched), "sessions_count": session_count}

    output = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "since": since,
        "repos": repos,
        "repos_no_commits": repos_no_commits,
        "total_commits": total_commits,
        "total_api_calls": total_api_calls,
    }

    output_path = Path(args.output) if args.output else DIST_DIR / "commit_data.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))

    size_kb = output_path.stat().st_size / 1024
    print(f"\n{'='*60}")
    print(f"Commit data written to {output_path} ({size_kb:.0f} KB)")
    print(f"  {total_commits} total commits across {len(repos) - len(repos_no_commits)} repos")
    print(f"  {len(repos_no_commits)} repos with 0 commits by {author}")
    print(f"  {total_api_calls} GitHub API calls made")


# ---------------------------------------------------------------------------
# collect-prs command
# ---------------------------------------------------------------------------

def cmd_collect_prs(args):
    """Fetch pull request history from GitHub for repos in the export."""
    export_path = Path(args.export)
    if not export_path.exists():
        print(f"Export not found: {export_path}")
        print("Run `skillbench gather` first, then sanitize the export.")
        return

    author = args.author
    if not author:
        author = _infer_author_from_export(export_path)
        if not author:
            print("Could not infer GitHub username from export.")
            print("Provide it explicitly: skillbench collect-prs --author USERNAME")
            return
        print(f"Inferred GitHub author: {author}")

    since = args.since or "2020-01-01T00:00:00Z"
    sleep_ms = 0.05

    # Check gh CLI
    try:
        subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10)
    except FileNotFoundError:
        print("ERROR: gh CLI not found. Install with: brew install gh")
        sys.exit(1)

    print(f"Loading export from {export_path}...")
    remote_counts = _repos_from_export(export_path)
    print(f"Found {len(remote_counts)} GitHub repos across {sum(remote_counts.values())} sessions\n")

    repos = {}
    repos_no_prs = []
    total_prs = 0
    total_api_calls = 0

    for slug, session_count in remote_counts.most_common():
        print(f"[{slug}] ({session_count} sessions)")

        all_prs = []
        page = 1
        while True:
            print(f"    page {page}...", end=" ", flush=True)
            data = _gh_api(
                f"repos/{slug}/pulls",
                {"state": "all", "per_page": "100", "page": str(page), "sort": "created", "direction": "desc"},
            )
            time.sleep(sleep_ms)
            total_api_calls += 1

            if not data or not isinstance(data, list) or len(data) == 0:
                print("done")
                break

            for pr in data:
                user = pr.get("user", {})
                if user.get("login", "").lower() != author.lower():
                    continue
                created = pr.get("created_at", "")
                if created < since:
                    continue
                all_prs.append({
                    "number": pr.get("number"),
                    "title": pr.get("title", "")[:150],
                    "state": pr.get("state", ""),
                    "created_at": created,
                    "updated_at": pr.get("updated_at", ""),
                    "merged_at": pr.get("merged_at"),
                    "closed_at": pr.get("closed_at"),
                    "draft": pr.get("draft", False),
                    "html_url": pr.get("html_url", ""),
                })

            count_on_page = len(data)
            author_count = sum(1 for pr in data if pr.get("user", {}).get("login", "").lower() == author.lower())
            print(f"{count_on_page} PRs ({author_count} by {author})")

            if count_on_page < 100:
                break
            page += 1

        if not all_prs:
            print(f"  → 0 PRs by {author}")
            repos_no_prs.append(slug)
            repos[slug] = {"pull_requests": [], "total_prs": 0, "sessions_count": session_count}
            continue

        # Fetch detailed stats per PR
        print(f"  → {len(all_prs)} PRs, fetching details...")
        for i, pr in enumerate(all_prs):
            if (i + 1) % 10 == 0 or i == 0:
                print(f"    details {i+1}/{len(all_prs)}...", flush=True)
            data = _gh_api(f"repos/{slug}/pulls/{pr['number']}")
            time.sleep(sleep_ms)
            total_api_calls += 1
            if data:
                pr["additions"] = data.get("additions", 0)
                pr["deletions"] = data.get("deletions", 0)
                pr["changed_files"] = data.get("changed_files", 0)
                pr["comments"] = data.get("comments", 0)
                pr["review_comments"] = data.get("review_comments", 0)
                pr["commits"] = data.get("commits", 0)
                pr["merged_at"] = data.get("merged_at")
                labels = data.get("labels", [])
                pr["labels"] = [l.get("name", "") for l in labels] if labels else []

        merged = sum(1 for p in all_prs if p.get("merged_at"))
        open_count = sum(1 for p in all_prs if p.get("state") == "open")
        closed = sum(1 for p in all_prs if p.get("state") == "closed" and not p.get("merged_at"))
        total_add = sum(p.get("additions", 0) for p in all_prs)
        total_del = sum(p.get("deletions", 0) for p in all_prs)
        print(f"  → {len(all_prs)} PRs ({merged} merged, {open_count} open, {closed} closed), +{total_add}/-{total_del} lines\n")
        total_prs += len(all_prs)

        repos[slug] = {
            "pull_requests": all_prs,
            "total_prs": len(all_prs),
            "merged": merged, "open": open_count, "closed_unmerged": closed,
            "sessions_count": session_count,
        }

    total_merged = sum(r.get("merged", 0) for r in repos.values())
    total_open = sum(r.get("open", 0) for r in repos.values())

    output = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "author": author,
        "since": since,
        "repos": repos,
        "repos_no_prs": repos_no_prs,
        "total_prs": total_prs,
        "total_merged": total_merged,
        "total_open": total_open,
        "total_api_calls": total_api_calls,
    }

    output_path = Path(args.output) if args.output else DIST_DIR / "pr_data.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))

    size_kb = output_path.stat().st_size / 1024
    print(f"\n{'='*60}")
    print(f"PR data written to {output_path} ({size_kb:.0f} KB)")
    print(f"  {total_prs} total PRs across {len(repos) - len(repos_no_prs)} repos")
    print(f"  {total_merged} merged, {total_open} open")
    print(f"  {len(repos_no_prs)} repos with 0 PRs by {author}")
    print(f"  {total_api_calls} GitHub API calls made")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="skillbench",
        description="SkillBench session-collector: agentic engineering metrics from CASS data",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # scan
    scan_p = sub.add_parser("scan", help="Scan CASS index and generate bootblock.txt")
    scan_p.add_argument("-o", "--output", help=f"Output file (default: {BOOTBLOCK_FILE})")

    # analyze
    analyze_p = sub.add_parser("analyze", help="Compute agentic engineering metrics")
    analyze_p.add_argument("-b", "--bootblock", help=f"Bootblock file (default: {BOOTBLOCK_FILE})")
    analyze_p.add_argument("--json", action="store_true", help="Also write JSON report")

    # gather
    gather_p = sub.add_parser("gather", help="Export session data for allowed workspaces")
    gather_p.add_argument("-b", "--bootblock", help=f"Bootblock file (default: {BOOTBLOCK_FILE})")
    gather_p.add_argument("-o", "--output", help=f"Output file (default: {DIST_DIR / 'skillbench_export.json'})")
    gather_p.add_argument("--full", action="store_true",
                          help="Full-fidelity export: read raw JSONL session files to preserve "
                               "complete tool_use payloads (code diffs, edit contents, command outputs). "
                               "Without this flag, uses CASS flat content (tool blocks stubbed).")

    # split export
    gather_p.add_argument("--split", choices=["session", "weekly", "none"],
                      default="weekly", help="How to split export files (default: weekly)")
    # collect (unified pipeline)
    collect_p = sub.add_parser("collect",
        help="One-command pipeline: scan → classify → analyze → export → sanitize")
    collect_p.add_argument("-o", "--output", help="Output file for sanitized export")
    collect_p.add_argument("-y", "--yes", action="store_true",
                           help="Skip interactive confirmation")
    collect_p.add_argument(
        "--include-excluded",
        action="store_true",
        help="Proceed even if no public/OSS repos were found (includes excluded workspaces too).",
    )
    # split export
    collect_p.add_argument("--split", choices=["session", "weekly", "none"],
                      default="weekly", help="How to split export files (default: weekly)")
    
    # upload guide
    collect_p.add_argument("--upload-guide", action="store_true",
                       help="Show upload instructions at the end")

    # dashboard
    dash_p = sub.add_parser("dashboard", help="Generate standalone HTML dashboard from export data")
    dash_p.add_argument("-e", "--export", default=str(DIST_DIR / "skillbench_export_sanitized.json"),
                        help="Path to sanitized export JSON")
    dash_p.add_argument("-r", "--report", help="Path to skillbench_report.json (auto-detected)")
    dash_p.add_argument("-c", "--commits", help="Path to commit_data.json (optional)")
    dash_p.add_argument("-o", "--output", help=f"Output HTML file (default: {DIST_DIR / 'dashboard.html'})")

    # push
    sub.add_parser("push", help="Upload sanitized session data to SkillBench API")

    # collect-commits
    cc_p = sub.add_parser("collect-commits", help="Fetch commit history from GitHub for repos in export")
    cc_p.add_argument("-e", "--export", default=str(DIST_DIR / "skillbench_export_sanitized.json"),
                      help="Path to export JSON file")
    cc_p.add_argument("-a", "--author", help="GitHub username (auto-detected from export if omitted)")
    cc_p.add_argument("-s", "--since", help="Fetch commits since this ISO date (default: 2020-01-01)")
    cc_p.add_argument("-o", "--output", help=f"Output file (default: {DIST_DIR / 'commit_data.json'})")

    # collect-prs
    cp_p = sub.add_parser("collect-prs", help="Fetch PR history from GitHub for repos in export")
    cp_p.add_argument("-e", "--export", default=str(DIST_DIR / "skillbench_export_sanitized.json"),
                      help="Path to export JSON file")
    cp_p.add_argument("-a", "--author", help="GitHub username (auto-detected from export if omitted)")
    cp_p.add_argument("-s", "--since", help="Fetch PRs since this ISO date (default: 2020-01-01)")
    cp_p.add_argument("-o", "--output", help=f"Output file (default: {DIST_DIR / 'pr_data.json'})")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "gather":
        cmd_gather(args)
    elif args.command == "collect":
        cmd_collect(args)
    elif args.command == "push":
        cmd_push(args)
    elif args.command == "collect-commits":
        cmd_collect_commits(args)
    elif args.command == "collect-prs":
        cmd_collect_prs(args)
    elif args.command == "dashboard":
        cmd_dashboard(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
