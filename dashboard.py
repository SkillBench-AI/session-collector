#!/usr/bin/env python3
"""Dashboard generation module for SkillBench.

Takes a sanitized session export + report and produces:
  1. dashboard_data.json — compact preprocessed metrics (~50KB)
  2. dashboard.html — standalone HTML file with embedded data + Chart.js

Reuses the preprocessing logic from csells-2026-02-24/build_dashboard.py
but generalized for any user.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMPLATE_PATH = Path(__file__).parent / "dashboard_template.html"

# Correction indicators in follow-up messages
CORRECTION_KEYWORDS = [
    "no,", "no ", "wrong", "instead", "actually,", "not what",
    "try again", "redo", "that's not", "don't ", "shouldn't",
    "incorrect", "fix ", "revert"
]

# Context provision indicators
CONTEXT_INDICATORS = [
    "/", ".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".dart",
    ".java", ".rb", ".cpp", ".c", ".h", ".css", ".html", ".json",
    ".yaml", ".yml", ".toml", ".md", ".sh", ".sql",
    "```", "line ", "src/", "lib/", "test/", "spec/",
    "function ", "class ", "def ", "import ", "from ",
]

# L5 thresholds for radar chart normalization


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(content) -> str:
    """Normalize message content to string (handles list-of-blocks format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content) if content else ""


def has_context(content: str) -> bool:
    """Check if a message provides code/file context."""
    content_lower = content.lower()
    matches = sum(1 for ind in CONTEXT_INDICATORS if ind.lower() in content_lower)
    return matches >= 2


def has_correction(content: str) -> bool:
    """Check if a follow-up message is a correction/redirect."""
    content_lower = content.lower()
    if len(content) > 2000:
        return False
    return any(kw in content_lower for kw in CORRECTION_KEYWORDS)


def extract_repo_slug(git_remote_url: str) -> str:
    """Convert https://github.com/owner/repo.git → owner/repo"""
    path = git_remote_url.split("github.com/")[-1]
    if path.endswith(".git"):
        path = path[:-4]
    return path.split("/")[0] + "/" + path.split("/")[1] if "/" in path else path


def compute_report_from_sessions(sessions: list[dict]) -> dict:
    """Generate a skillbench_report-compatible dict directly from export sessions.

    This avoids requiring a separate report file — computes Tier 1-3 metrics
    from the raw session dicts (same format as the sanitized export JSON).
    """
    total = len(sessions)
    if total == 0:
        return {"level": 0, "level_name": "No Data", "score": 0,
                "tier1": {}, "tier2": {}, "tier3": {}}

    timestamps = []
    active_days = set()
    agents_used = set()
    durations = []
    user_msg_lengths = []
    context_provisions = 0
    total_user_msgs = 0
    multi_step = 0
    first_attempt = 0
    correction_count = 0
    msgs_per_session = []

    correction_re = re.compile(
        r'\b(no[,.]|wrong|incorrect|instead|actually|not what|try again|that\'s not|fix |redo)\b',
        re.IGNORECASE
    )

    for s in sessions:
        started = s.get("started_at")
        ended = s.get("ended_at")
        agent = s.get("agent", "unknown")
        messages = s.get("messages", [])
        agents_used.add(agent)

        if started:
            timestamps.append(started)
            dt = datetime.fromtimestamp(started / 1000, tz=timezone.utc)
            active_days.add(dt.date())
        if ended:
            timestamps.append(ended)

        if started and ended and ended > started:
            dur = (ended - started) / 60000
            if 0 < dur < 480:
                durations.append(dur)

        user_msgs = [m for m in messages if m.get("role") == "user"]
        n_user = len(user_msgs)
        msgs_per_session.append(n_user)
        total_user_msgs += n_user

        for m in user_msgs:
            content = _text(m.get("content", ""))
            user_msg_lengths.append(len(content))
            if has_context(content):
                context_provisions += 1
            if correction_re.search(content):
                correction_count += 1

        if n_user > 3:
            multi_step += 1
        if n_user <= 2:
            first_attempt += 1

    if timestamps:
        first_date = datetime.fromtimestamp(min(timestamps) / 1000, tz=timezone.utc)
        last_date = datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
        span_days = max((last_date - first_date).days, 1)
        span_weeks = max(span_days / 7, 1)
    else:
        first_date = last_date = None
        span_days = span_weeks = 1

    spw = total / span_weeks
    adpw = len(active_days) / span_weeks
    avg_dur = sum(durations) / len(durations) if durations else 0
    avg_pl = sum(user_msg_lengths) / len(user_msg_lengths) if user_msg_lengths else 0
    cpr = context_provisions / total_user_msgs if total_user_msgs else 0
    msr = multi_step / total if total else 0
    fasr = first_attempt / total if total else 0
    cr = correction_count / total_user_msgs if total_user_msgs else 0
    avg_turns = sum(msgs_per_session) / len(msgs_per_session) if msgs_per_session else 0

    # Ladder scoring (simplified — matches skillbench.py thresholds)
    # L1(0-20), L2(21-40), L3(41-60), L4(61-80), L5(81-100)
    def _score_component(value, thresholds):
        """Score 0-100 based on value vs threshold breakpoints."""
        # thresholds = [(cutoff, max_score), ...]
        for cutoff, max_pts in thresholds:
            if value <= cutoff:
                return min(max_pts, round(value / cutoff * max_pts))
        return max_pts

    score = 0
    # Tier 1 (30 pts): sessions/wk + active days/wk
    score += min(15, round(spw / 350 * 15))
    score += min(15, round(adpw / 5.0 * 15))
    # Tier 2 (40 pts): prompt length + context + multi-step
    score += min(15, round(avg_pl / 2000 * 15))
    score += min(15, round(cpr / 0.60 * 15))
    score += min(10, round(msr / 0.20 * 10))
    # Tier 3 (30 pts): first attempt + correction (inverted)
    score += min(15, round(fasr / 0.95 * 15))
    score += min(15, round(max(0, 1 - cr / 0.20) * 15))

    score = min(100, score)
    if score >= 81: level, level_name = 5, "L5 Maestro"
    elif score >= 61: level, level_name = 4, "L4 Engineer"
    elif score >= 41: level, level_name = 3, "L3 Practitioner"
    elif score >= 21: level, level_name = 2, "L2 Explorer"
    else: level, level_name = 1, "L1 Novice"

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
            "total_conversations": total,
            "total_user_messages": total_user_msgs,
            "sessions_per_week": round(spw, 1),
            "active_days_per_week": round(adpw, 1),
            "avg_session_duration_min": round(avg_dur, 1),
            "agents_used": sorted(agents_used),
        },
        "tier2": {
            "avg_prompt_length": round(avg_pl),
            "context_provision_rate": round(cpr, 3),
            "multi_step_rate": round(msr, 3),
        },
        "tier3": {
            "first_attempt_success_rate": round(fasr, 3),
            "correction_rate": round(cr, 3),
            "avg_turns_per_session": round(avg_turns, 1),
        },
    }


# ---------------------------------------------------------------------------
# Dashboard data computation
# ---------------------------------------------------------------------------

def _generate_insights(report: dict, agent_metrics: list[dict],
                       time_series: list[dict]) -> list[dict]:
    """Generate narrative insight cards from data patterns.

    Each insight is a dict with 'title', 'body', and optional 'color'
    (one of: '', 'green', 'amber', 'blue', 'pink').
    """
    insights = []
    t1 = report.get("tier1", {})
    t2 = report.get("tier2", {})
    t3 = report.get("tier3", {})

    # --- Cross-agent usage patterns (if multi-agent) ---
    if len(agent_metrics) > 1:
        # Find the agent with longest sessions vs shortest
        by_duration = sorted(agent_metrics, key=lambda a: a["avg_duration_min"], reverse=True)
        longest = by_duration[0]
        shortest = by_duration[-1]

        # Find the agent with most turns vs fewest
        by_turns = sorted(agent_metrics, key=lambda a: a["avg_turns"], reverse=True)
        most_iterative = by_turns[0]
        most_oneshot = by_turns[-1]

        # Find the agent with longest prompts
        by_prompt = sorted(agent_metrics, key=lambda a: a["avg_prompt_length"], reverse=True)
        longest_prompts = by_prompt[0]

        # Build cross-agent narrative
        parts = []
        agent_names = [a["name"].replace("_", " ") for a in agent_metrics]
        parts.append(
            f"You use {len(agent_metrics)} different agents: "
            f"<strong>{', '.join(agent_names)}</strong>. "
            f"Each gets used in a distinctly different way."
        )

        if most_iterative["avg_turns"] > 3 * most_oneshot["avg_turns"] and most_iterative["avg_turns"] > 5:
            parts.append(
                f"<strong>{most_iterative['name'].replace('_', ' ')}</strong> "
                f"sessions average {most_iterative['avg_turns']:.0f} turns "
                f"({most_iterative['avg_duration_min']:.0f} min) — "
                f"you're having extended back-and-forth conversations with it. "
                f"Meanwhile <strong>{most_oneshot['name'].replace('_', ' ')}</strong> "
                f"averages just {most_oneshot['avg_turns']:.1f} turns — "
                f"more of a single-shot pattern."
            )

        if longest_prompts["avg_prompt_length"] > 3 * shortest["avg_prompt_length"]:
            parts.append(
                f"Prompt length varies dramatically: "
                f"<strong>{longest_prompts['name'].replace('_', ' ')}</strong> "
                f"gets {longest_prompts['avg_prompt_length']:,} chars on average, "
                f"while <strong>{shortest['name'].replace('_', ' ')}</strong> "
                f"gets {shortest['avg_prompt_length']:,}. "
                f"Suggests different roles — "
                f"{'context-dumping vs. conversational' if longest_prompts['avg_prompt_length'] > 5000 else 'detailed vs. quick'}."
            )

        # Correction rate comparison
        by_correction = sorted(agent_metrics, key=lambda a: a.get("correction_rate", 0), reverse=True)
        if by_correction[0].get("correction_rate", 0) > 0.15:
            highest_friction = by_correction[0]
            parts.append(
                f"<strong>{highest_friction['name'].replace('_', ' ')}</strong> "
                f"has a {highest_friction['correction_rate']:.0%} correction rate — "
                f"notably higher friction than other agents."
            )

        insights.append({
            "title": "Cross-Agent Usage Patterns",
            "body": " ".join(parts),
            "color": "blue",
        })

    # --- Volume & rhythm ---
    total = t1.get("total_conversations", 0)
    spw = t1.get("sessions_per_week", 0)
    adpw = t1.get("active_days_per_week", 0)
    span = report.get("date_range", {}).get("span_days", 1)

    volume_parts = []
    volume_parts.append(
        f"<strong>{total:,} sessions</strong> over {span} days "
        f"({spw:.0f}/week, {adpw:.1f} active days/week)."
    )
    avg_dur = t1.get("avg_session_duration_min", 0)
    if avg_dur > 0:
        volume_parts.append(
            f"Average session lasts <strong>{avg_dur:.1f} minutes</strong>."
        )
    avg_turns = t3.get("avg_turns_per_session", 0)
    if avg_turns > 0:
        volume_parts.append(
            f"Average <strong>{avg_turns:.1f} turns</strong> per session."
        )

    insights.append({
        "title": "Usage Volume",
        "body": " ".join(volume_parts),
        "color": "",
    })

    # --- Trend observation (is usage growing, shrinking, stable?) ---
    if len(time_series) >= 4:
        half = len(time_series) // 2
        first_half_avg = sum(w["sessions"] for w in time_series[:half]) / half
        second_half_avg = sum(w["sessions"] for w in time_series[half:]) / (len(time_series) - half)
        if first_half_avg > 0:
            change_pct = (second_half_avg - first_half_avg) / first_half_avg * 100
            if abs(change_pct) > 20:
                direction = "increased" if change_pct > 0 else "decreased"
                insights.append({
                    "title": "Usage Trend",
                    "body": f"Session volume {direction} roughly "
                            f"<strong>{abs(change_pct):.0f}%</strong> "
                            f"from the first half of the period to the second half "
                            f"({first_half_avg:.0f}/wk → {second_half_avg:.0f}/wk).",
                    "color": "green" if change_pct > 0 else "amber",
                })

    # --- Prompting style ---
    avg_pl = t2.get("avg_prompt_length", 0)
    msr = t2.get("multi_step_rate", 0)
    if avg_pl > 0:
        style_parts = []
        if avg_pl > 2000:
            style_parts.append(
                f"Prompts average <strong>{avg_pl:,} chars</strong> — "
                f"you tend to front-load context."
            )
        elif avg_pl < 200:
            style_parts.append(
                f"Prompts average just <strong>{avg_pl:,} chars</strong> — "
                f"short, conversational interactions."
            )
        else:
            style_parts.append(
                f"Prompts average <strong>{avg_pl:,} chars</strong>."
            )

        if msr > 0.5:
            style_parts.append(
                f"<strong>{msr:.0%}</strong> of sessions are multi-step (>3 turns) — "
                f"you frequently iterate with the agent."
            )
        elif msr < 0.1 and msr > 0:
            style_parts.append(
                f"Only <strong>{msr:.1%}</strong> of sessions go beyond 3 turns — "
                f"most interactions are quick and self-contained."
            )

        if style_parts:
            insights.append({
                "title": "Prompting Style",
                "body": " ".join(style_parts),
                "color": "pink",
            })

    return insights


def compute_dashboard_data(sessions: list[dict], report: dict,
                           commit_data: dict | None = None) -> dict:
    """Crunch session export into compact dashboard-ready JSON.

    Args:
        sessions: List of session dicts from the sanitized export.
        report: The skillbench_report.json dict.
        commit_data: Optional commit_data.json dict.

    Returns:
        Dict ready for JSON serialization and embedding in the dashboard HTML.
    """
    weekly = defaultdict(lambda: {
        "sessions": 0,
        "active_days": set(),
        "total_duration_min": 0.0,
        "user_messages": 0,
        "user_msg_lengths": [],
        "context_provisions": 0,
        "multi_step_sessions": 0,
        "first_attempt_successes": 0,
        "corrections": 0,
        "total_turns": 0,
        "agents": Counter(),
    })

    workspace_stats = Counter()
    agent_stats = Counter()
    workspace_agents = defaultdict(Counter)
    hour_counts = Counter()
    dow_counts = Counter()
    heatmap = defaultdict(int)
    session_durations = []
    prompt_lengths = []

    # Per-agent metric accumulators
    agent_metrics_acc = defaultdict(lambda: {
        "sessions": 0,
        "active_days": set(),
        "active_weeks": set(),
        "total_duration_min": 0.0,
        "user_messages": 0,
        "user_msg_lengths": [],
        "context_provisions": 0,
        "multi_step_sessions": 0,
        "first_attempt_successes": 0,
        "corrections": 0,
        "total_turns": 0,
    })

    for session in sessions:
        started_at = session.get("started_at")
        ended_at = session.get("ended_at")
        agent = session.get("agent", "unknown")
        workspace = session.get("workspace", "unknown")
        messages = session.get("messages", [])

        if not started_at:
            continue

        dt = datetime.fromtimestamp(started_at / 1000, tz=timezone.utc)
        week_key = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
        day_key = dt.strftime("%Y-%m-%d")

        w = weekly[week_key]
        w["sessions"] += 1
        w["active_days"].add(day_key)
        w["agents"][agent] += 1

        hour_counts[dt.hour] += 1
        dow_counts[dt.weekday()] += 1
        heatmap[(dt.weekday(), dt.hour)] += 1

        if started_at and ended_at:
            duration_min = (ended_at - started_at) / 60000
            if 0 < duration_min < 480:
                w["total_duration_min"] += duration_min
                session_durations.append(duration_min)

        user_messages = [m for m in messages if m.get("role") == "user"]
        user_turn_count = len(user_messages)
        w["total_turns"] += user_turn_count

        for msg in user_messages:
            content = _text(msg.get("content", ""))
            w["user_messages"] += 1
            msg_len = len(content)
            w["user_msg_lengths"].append(msg_len)
            prompt_lengths.append(msg_len)

            if has_context(content):
                w["context_provisions"] += 1

        if user_turn_count > 3:
            w["multi_step_sessions"] += 1
        if user_turn_count <= 2:
            w["first_attempt_successes"] += 1

        for msg in user_messages[1:]:
            if has_correction(_text(msg.get("content", ""))):
                w["corrections"] += 1
                break

        # Per-agent accumulation
        a = agent_metrics_acc[agent]
        a["sessions"] += 1
        a["active_days"].add(day_key)
        a["active_weeks"].add(week_key)
        if started_at and ended_at:
            dur = (ended_at - started_at) / 60000
            if 0 < dur < 480:
                a["total_duration_min"] += dur
        a["user_messages"] += len(user_messages)
        for msg in user_messages:
            mc = _text(msg.get("content", ""))
            a["user_msg_lengths"].append(len(mc))
            if has_context(mc):
                a["context_provisions"] += 1
        a["total_turns"] += user_turn_count
        if user_turn_count > 3:
            a["multi_step_sessions"] += 1
        if user_turn_count <= 2:
            a["first_attempt_successes"] += 1
        for msg in user_messages[1:]:
            if has_correction(_text(msg.get("content", ""))):
                a["corrections"] += 1
                break

        ws_short = workspace.replace("~/", "")
        workspace_stats[ws_short] += 1
        agent_stats[agent] += 1
        workspace_agents[ws_short][agent] += 1

    # Weekly time series
    sorted_weeks = sorted(weekly.keys())
    time_series = []
    for wk in sorted_weeks:
        w = weekly[wk]
        n_sessions = w["sessions"]
        n_user_msgs = w["user_messages"]
        lengths = w["user_msg_lengths"]

        time_series.append({
            "week": wk,
            "sessions": n_sessions,
            "active_days": len(w["active_days"]),
            "avg_duration_min": round(w["total_duration_min"] / n_sessions, 1) if n_sessions else 0,
            "avg_prompt_length": round(sum(lengths) / len(lengths)) if lengths else 0,
            "context_provision_rate": round(w["context_provisions"] / n_user_msgs, 3) if n_user_msgs else 0,
            "multi_step_rate": round(w["multi_step_sessions"] / n_sessions, 3) if n_sessions else 0,
            "first_attempt_success_rate": round(w["first_attempt_successes"] / n_sessions, 3) if n_sessions else 0,
            "correction_rate": round(w["corrections"] / n_sessions, 3) if n_sessions else 0,
            "avg_turns": round(w["total_turns"] / n_sessions, 1) if n_sessions else 0,
            "agents": dict(w["agents"]),
        })

    # Top workspaces
    top_workspaces = []
    for ws, count in workspace_stats.most_common(15):
        agents = dict(workspace_agents[ws])
        top_workspaces.append({"name": ws, "sessions": count, "agents": agents})

    # Agent breakdown
    agent_breakdown = [
        {"name": a, "sessions": c}
        for a, c in agent_stats.most_common()
    ]

    # Per-agent metric summaries (only for agents with ≥5 sessions)
    agent_metrics = []
    for agent_name, acc in sorted(agent_metrics_acc.items(),
                                   key=lambda x: x[1]["sessions"], reverse=True):
        n = acc["sessions"]
        if n < 5:
            continue
        n_weeks = len(acc["active_weeks"]) or 1
        n_msgs = acc["user_messages"] or 1
        lengths = acc["user_msg_lengths"]
        agent_metrics.append({
            "name": agent_name,
            "sessions": n,
            "active_days": len(acc["active_days"]),
            "weeks_active": n_weeks,
            "sessions_per_week": round(n / n_weeks, 1),
            "active_days_per_week": round(len(acc["active_days"]) / n_weeks, 1),
            "avg_duration_min": round(acc["total_duration_min"] / n, 1) if n else 0,
            "avg_prompt_length": round(sum(lengths) / len(lengths)) if lengths else 0,
            "context_provision_rate": round(acc["context_provisions"] / n_msgs, 3),
            "multi_step_rate": round(acc["multi_step_sessions"] / n, 3),
            "first_attempt_success_rate": round(acc["first_attempt_successes"] / n, 3),
            "correction_rate": round(acc["corrections"] / n, 3),
            "avg_turns": round(acc["total_turns"] / n, 1),
        })

    # Heatmap (7 x 24)
    heatmap_data = []
    for dow in range(7):
        for hour in range(24):
            count = heatmap.get((dow, hour), 0)
            if count > 0:
                heatmap_data.append({"dow": dow, "hour": hour, "count": count})

    # Duration distribution
    duration_buckets = Counter()
    for d in session_durations:
        if d < 1: duration_buckets["<1 min"] += 1
        elif d < 3: duration_buckets["1-3 min"] += 1
        elif d < 5: duration_buckets["3-5 min"] += 1
        elif d < 10: duration_buckets["5-10 min"] += 1
        elif d < 30: duration_buckets["10-30 min"] += 1
        elif d < 60: duration_buckets["30-60 min"] += 1
        else: duration_buckets["60+ min"] += 1
    bucket_order = ["<1 min", "1-3 min", "3-5 min", "5-10 min", "10-30 min", "30-60 min", "60+ min"]
    duration_dist = [{"bucket": b, "count": duration_buckets.get(b, 0)} for b in bucket_order]

    # Prompt length distribution
    prompt_buckets = Counter()
    for pl in prompt_lengths:
        if pl < 50: prompt_buckets["<50"] += 1
        elif pl < 200: prompt_buckets["50-200"] += 1
        elif pl < 500: prompt_buckets["200-500"] += 1
        elif pl < 1000: prompt_buckets["500-1K"] += 1
        elif pl < 2000: prompt_buckets["1K-2K"] += 1
        elif pl < 5000: prompt_buckets["2K-5K"] += 1
        else: prompt_buckets["5K+"] += 1
    pl_order = ["<50", "50-200", "200-500", "500-1K", "1K-2K", "2K-5K", "5K+"]
    prompt_dist = [{"bucket": b, "count": prompt_buckets.get(b, 0)} for b in pl_order]

    # Narrative insights (auto-generated from data patterns)
    insights = _generate_insights(report, agent_metrics, time_series)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report": report,
        "time_series": time_series,
        "top_workspaces": top_workspaces,
        "agent_breakdown": agent_breakdown,
        "agent_metrics": agent_metrics,
        "insights": insights,
        "heatmap": heatmap_data,
        "duration_distribution": duration_dist,
        "prompt_length_distribution": prompt_dist,
        "total_sessions": len(sessions),
    }

    # Commit data (optional)
    if commit_data:
        commit_metrics = _compute_commit_metrics(sessions, commit_data)
        if commit_metrics:
            result.update(commit_metrics)

    return result


def _compute_commit_metrics(sessions, commit_data):
    """Correlate sessions with commits (optional enrichment)."""
    if not commit_data or "repos" not in commit_data:
        return None

    repos = commit_data["repos"]
    session_dates_by_repo = defaultdict(set)

    for s in sessions:
        remote = s.get("git_remote")
        started = s.get("started_at")
        if not remote or not started:
            continue
        slug = extract_repo_slug(remote)
        dt = datetime.fromtimestamp(started / 1000, tz=timezone.utc)
        session_dates_by_repo[slug].add(dt.strftime("%Y-%m-%d"))

    weekly_commits = defaultdict(lambda: {
        "commits": 0, "additions": 0, "deletions": 0,
        "files_changed": 0, "commit_days": set(), "repos": set(),
    })

    for slug, repo_info in repos.items():
        for commit in repo_info.get("commits", []):
            date_str = commit.get("date", "")
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            wk = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
            w = weekly_commits[wk]
            w["commits"] += 1
            w["additions"] += commit.get("additions", 0)
            w["deletions"] += commit.get("deletions", 0)
            w["files_changed"] += commit.get("files_changed", 0)
            w["commit_days"].add(dt.strftime("%Y-%m-%d"))
            w["repos"].add(slug)

    all_weeks = set(weekly_commits.keys())
    for s in sessions:
        started = s.get("started_at")
        if started:
            dt = datetime.fromtimestamp(started / 1000, tz=timezone.utc)
            all_weeks.add(f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}")

    commit_time_series = []
    for wk in sorted(all_weeks):
        w = weekly_commits.get(wk)
        if w:
            commit_time_series.append({
                "week": wk, "commits": w["commits"],
                "additions": w["additions"], "deletions": w["deletions"],
                "files_changed": w["files_changed"],
                "active_commit_days": len(w["commit_days"]),
                "repos_committed": sorted(w["repos"]),
            })
        else:
            commit_time_series.append({
                "week": wk, "commits": 0, "additions": 0, "deletions": 0,
                "files_changed": 0, "active_commit_days": 0, "repos_committed": [],
            })

    repo_productivity = []
    for slug, repo_info in repos.items():
        commits = repo_info.get("commits", [])
        n_commits = len(commits)
        n_sessions = repo_info.get("sessions_count", 0)
        total_add = sum(c.get("additions", 0) for c in commits)
        total_del = sum(c.get("deletions", 0) for c in commits)
        commit_days = set()
        for c in commits:
            d = c.get("date", "")
            if d:
                try:
                    commit_days.add(datetime.fromisoformat(
                        d.replace("Z", "+00:00")).strftime("%Y-%m-%d"))
                except (ValueError, TypeError):
                    pass
        session_days = session_dates_by_repo.get(slug, set())
        overlapping = session_days & commit_days
        repo_productivity.append({
            "repo": slug, "commits": n_commits, "sessions": n_sessions,
            "additions": total_add, "deletions": total_del,
            "session_days": len(session_days), "commit_days": len(commit_days),
            "overlapping_days": len(overlapping),
        })
    repo_productivity.sort(key=lambda r: r["commits"], reverse=True)

    total_commits = sum(r["commits"] for r in repo_productivity)
    total_add = sum(r["additions"] for r in repo_productivity)
    total_del = sum(r["deletions"] for r in repo_productivity)
    repos_with_commits = sum(1 for r in repo_productivity if r["commits"] > 0)
    total_sessions = sum(r["sessions"] for r in repo_productivity)
    n_weeks = len([w for w in commit_time_series if w["commits"] > 0])

    commit_summary = {
        "total_commits": total_commits,
        "total_additions": total_add,
        "total_deletions": total_del,
        "repos_with_commits": repos_with_commits,
        "repos_total": len(repo_productivity),
        "commits_per_week": round(total_commits / max(n_weeks, 1), 1),
        "lines_per_commit": round((total_add + total_del) / max(total_commits, 1)),
        "sessions_per_commit": round(total_sessions / max(total_commits, 1), 1),
    }

    return {
        "commit_time_series": commit_time_series,
        "repo_productivity": repo_productivity,
        "commit_summary": commit_summary,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(dashboard_data: dict, template_path: Path | None = None) -> str:
    """Generate a standalone dashboard HTML file with embedded data.

    Reads the template, replaces __DASHBOARD_DATA__ with the JSON blob,
    and returns the complete HTML string.
    """
    if template_path is None:
        template_path = TEMPLATE_PATH

    template = template_path.read_text()
    data_json = json.dumps(dashboard_data, default=str)

    # Replace the placeholder with actual data
    html = template.replace("__DASHBOARD_DATA__", data_json)
    return html


def build_dashboard(
    export_path: str | Path,
    report_path: str | Path | None = None,
    output_path: str | Path | None = None,
    commit_data_path: str | Path | None = None,
    *,
    verbose: bool = True,
) -> Path:
    """Full pipeline: read export + report → compute data → generate HTML.

    Args:
        export_path: Path to skillbench_export_sanitized.json
        report_path: Path to skillbench_report.json (auto-computed if None)
        output_path: Where to write the HTML (default: dist/dashboard.html)
        commit_data_path: Optional path to commit_data.json
        verbose: Print progress

    Returns:
        Path to the generated dashboard HTML file.
    """
    export_path = Path(export_path)

    if output_path is None:
        output_path = export_path.parent / "dashboard.html"
    output_path = Path(output_path)

    if verbose:
        print(f"Loading export: {export_path}")
    with open(export_path) as f:
        sessions = json.load(f)
    if verbose:
        print(f"  {len(sessions)} sessions")

    if report_path and Path(report_path).exists():
        if verbose:
            print(f"Loading report: {report_path}")
        with open(report_path) as f:
            report = json.load(f)
    else:
        if verbose:
            print("Computing report from sessions...")
        report = compute_report_from_sessions(sessions)
        if verbose:
            print(f"  {report['level_name']} ({report['score']}/100)")

    commit_data = None
    if commit_data_path and Path(commit_data_path).exists():
        if verbose:
            print(f"Loading commit data: {commit_data_path}")
        with open(commit_data_path) as f:
            commit_data = json.load(f)

    if verbose:
        print("Computing dashboard data...")
    data = compute_dashboard_data(sessions, report, commit_data)

    if verbose:
        print("Generating HTML...")
    html = generate_html(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)

    # Also write the dashboard_data.json alongside
    data_path = output_path.with_name("dashboard_data.json")
    with open(data_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    if verbose:
        html_kb = output_path.stat().st_size / 1024
        data_kb = data_path.stat().st_size / 1024
        print(f"  Dashboard: {output_path} ({html_kb:.0f} KB)")
        print(f"  Data:      {data_path} ({data_kb:.0f} KB)")
        print(f"  {len(data['time_series'])} weeks, {len(data['agent_breakdown'])} agents")

    return output_path
