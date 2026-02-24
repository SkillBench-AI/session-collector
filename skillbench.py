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
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CASS_DB_DEFAULT = Path.home() / "Library" / "Application Support" / \
    "com.coding-agent-search.coding-agent-search" / "agent_search.db"

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
    if CASS_DB_DEFAULT.exists():
        return CASS_DB_DEFAULT
    # Try XDG data dir on Linux
    xdg = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    linux_path = Path(xdg) / "coding-agent-search" / "agent_search.db"
    if linux_path.exists():
        return linux_path
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
    """Expand a list of real workspace paths to include Gemini hash equivalents.

    When querying CASS, Gemini sessions are stored under
    ~/.gemini/tmp/{sha256(path)}/ — not the real project path. This function
    computes the hash for each path and appends the Gemini hash path so that
    CASS queries return both direct and Gemini workspace entries.
    """
    expanded = list(paths)
    for p in paths:
        expanded.append(str(GEMINI_TMP_DIR / gemini_hash_for_path(p)))
    return expanded


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
        pass
    return None


def _extract_github_slug(remote_url: str) -> str | None:
    """Extract owner/repo from a GitHub remote URL."""
    if not remote_url:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote_url)
    return m.group(1) if m else None


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


def cmd_push(args):
    """Export session data for allowed workspaces (stub for server push)."""
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

    # Include Gemini hash paths so we also capture Gemini CLI sessions
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

    # Normalize CASS message roles to a clean set: user, agent, tool
    # CASS uses inconsistent roles (e.g. "gemini" instead of "agent",
    # "developer" for system prompts, "info"/"error" for metadata).
    ROLE_MAP = {
        "user": "user",
        "agent": "agent",
        "tool": "tool",
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

    export = []
    for conv in conversations:
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

        resolved_ws = resolve_workspace(conv["workspace"])
        export.append({
            "session_id": conv["external_id"],
            "agent": conv["agent"],
            "workspace": resolved_ws,
            "git_remote": ws_remotes.get(resolved_ws),
            "source_path": conv["source_path"],
            "title": conv["title"],
            "started_at": conv["started_at"],
            "ended_at": conv["ended_at"],
            "approx_tokens": conv["approx_tokens"],
            "messages": normalized_msgs,
        })

    conn.close()

    # Write to file
    output_path = Path(args.output) if args.output else DIST_DIR / "skillbench_export.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export, indent=2, default=str))

    total_msgs = sum(len(c["messages"]) for c in export)
    print(f"Exported {len(export)} conversations ({total_msgs} messages) to {output_path}")
    print()
    print("Server push not yet implemented — this file can be manually shared.")
    print("Future: `skillbench push` will upload to SkillBench API.")


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

    # push
    push_p = sub.add_parser("push", help="Export session data for allowed workspaces")
    push_p.add_argument("-b", "--bootblock", help=f"Bootblock file (default: {BOOTBLOCK_FILE})")
    push_p.add_argument("-o", "--output", help=f"Output file (default: {DIST_DIR / 'skillbench_export.json'})")

    args = parser.parse_args()

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "push":
        cmd_push(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
