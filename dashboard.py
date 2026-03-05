#!/usr/bin/env python3
"""Dashboard generation module for SkillBench.

Takes a sanitized session export + report and produces:
  1. dashboard_data.json — compact preprocessed metrics (~50KB)
  2. dashboard.html — standalone HTML file with embedded data + Chart.js

Reuses the preprocessing logic from csells-2026-02-24/build_dashboard.py
but generalized for any user.
"""

import json
import math
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


# ---------------------------------------------------------------------------
# Cognitive Efficiency (CE) — internal computation (IP — not exposed to users)
# ---------------------------------------------------------------------------

# Phase classification keywords for tool_use/tool_result analysis
_PHASE_PATTERNS = {
    "understand": re.compile(
        r'\b(Read|Glob|Grep|search|find|grep|cat|head|ls|rg)\b', re.IGNORECASE),
    "plan": re.compile(
        r'\b(plan|design|architect|outline|TODO|todo_write|EnterPlanMode)\b', re.IGNORECASE),
    "implement": re.compile(
        r'\b(Edit|Write|NotebookEdit|mkdir|cp|mv|create|add|implement)\b', re.IGNORECASE),
    "validate": re.compile(
        r'\b(test|pytest|jest|npm test|go test|flutter test|lint|check|build|compile|tsc|mypy)\b',
        re.IGNORECASE),
    "repair": re.compile(
        r'\b(fix|debug|revert|undo|rollback|patch|hotfix|workaround)\b', re.IGNORECASE),
    "polish": re.compile(
        r'\b(refactor|rename|cleanup|format|prettier|eslint --fix|doc|README|comment)\b',
        re.IGNORECASE),
}

# Inactivity gap for episode chunking (milliseconds)
_EPISODE_GAP_MS = 20 * 60 * 1000  # 20 minutes


def _linear_regression_slope(x_values, y_values):
    """Simple OLS slope. No numpy needed."""
    n = len(x_values)
    if n < 2:
        return 0.0
    x_mean = sum(x_values) / n
    y_mean = sum(y_values) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    den = sum((x - x_mean) ** 2 for x in x_values)
    return num / den if den != 0 else 0.0


def _z_scores(values):
    """Compute z-scores for a list of values."""
    n = len(values)
    if n < 2:
        return [0.0] * n
    mean_val = sum(values) / n
    var = sum((v - mean_val) ** 2 for v in values) / (n - 1)
    std = var ** 0.5
    if std < 1e-10:
        return [0.0] * n
    return [(v - mean_val) / std for v in values]


def _extract_file_anchors(text: str) -> set[str]:
    """Extract file path anchors from message text for task switching detection."""
    # Match common source file patterns
    pattern = re.compile(
        r'(?:^|[\s`\'"(])(/[\w./-]+\.\w+|[\w./-]+\.(?:py|ts|tsx|js|jsx|rs|go|dart|java|rb|cpp|c|h|css|html|json|yaml|yml|toml|md|sh|sql))\b'
    )
    paths = set()
    for match in pattern.finditer(text):
        path = match.group(1).strip()
        # Normalize to top-level module (first 2 path segments)
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            paths.add(parts[0] + "/" + parts[1])
        else:
            paths.add(parts[0])
    return paths


def _classify_phases(text: str) -> set[str]:
    """Classify a message into work phases based on content."""
    phases = set()
    for phase, pattern in _PHASE_PATTERNS.items():
        if pattern.search(text):
            phases.add(phase)
    return phases or {"understand"}  # default to understand if no match


def _extract_tool_errors(messages: list[dict]) -> tuple[int, int]:
    """Count tool_use invocations and tool_result errors from raw messages.

    Returns (total_tool_calls, error_count).
    """
    tool_calls = 0
    errors = 0
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "tool_use":
                tool_calls += 1
            elif btype == "tool_result":
                if block.get("is_error", False):
                    errors += 1
    return tool_calls, errors


def _chunk_into_episodes(messages: list[dict]) -> list[list[dict]]:
    """Split a session's messages into active work episodes.

    Breaks on inactivity gaps > _EPISODE_GAP_MS. Each episode is a list
    of messages. Returns at least one episode.
    """
    if not messages:
        return []

    episodes = []
    current_episode = [messages[0]]
    prev_ts = _msg_timestamp_ms(messages[0])

    for msg in messages[1:]:
        ts = _msg_timestamp_ms(msg)
        if prev_ts and ts and (ts - prev_ts) > _EPISODE_GAP_MS:
            episodes.append(current_episode)
            current_episode = [msg]
        else:
            current_episode.append(msg)
        if ts:
            prev_ts = ts

    if current_episode:
        episodes.append(current_episode)

    return episodes


def _msg_timestamp_ms(msg: dict) -> int | None:
    """Extract timestamp in ms from a message's created_at field."""
    created = msg.get("created_at")
    if not created:
        return None
    if isinstance(created, (int, float)):
        return int(created)
    # ISO 8601 string
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return None


def _compute_session_ce(session: dict) -> dict | None:
    """Compute CE sub-components for a single session.

    Returns dict with all component values, or None if session doesn't
    meet minimums. Components:
      Original 4: IF, PE, VER, CQ
      New signals: TF (tool friction), TS (task switching), PS (phase spread),
                   n_episodes, active_duration_min
    """
    messages = session.get("messages", [])
    user_msgs = []
    agent_msgs = []
    all_text_by_turn = []  # (role, text) for switching detection

    for msg in messages:
        content = _text(msg.get("content", ""))
        role = msg.get("role", "")
        # Skip environment context preambles
        if role == "user" and content.strip().startswith("<environment_context"):
            continue
        if role == "user" and content.strip():
            user_msgs.append(content)
            all_text_by_turn.append(("user", content))
        elif role in ("agent", "assistant") and content.strip():
            agent_msgs.append(content)
            all_text_by_turn.append(("agent", content))

    if len(user_msgs) < 1 or len(agent_msgs) < 1:
        return None

    # IF: Interaction Friction (EL proxy)
    correction_count = sum(
        1 for msg in user_msgs[1:] if has_correction(msg)
    )
    friction = correction_count / len(user_msgs)

    # PE: Prompt Escalation (EL persistence proxy)
    lengths = [len(msg) for msg in user_msgs]
    if len(lengths) >= 3:
        slope = _linear_regression_slope(list(range(len(lengths))), lengths)
        mean_len = sum(lengths) / len(lengths)
        escalation = slope / (mean_len + 1)
    else:
        escalation = 0.0

    # VER: Value Extraction Ratio (performance proxy)
    total_agent = sum(len(msg) for msg in agent_msgs)
    total_user = sum(len(msg) for msg in user_msgs)
    extraction = total_agent / (total_user + 1)

    # CQ: Convergence Quality (resolution proxy)
    if correction_count == 0:
        convergence = 1.0
    else:
        last_corr_idx = 0
        for i, msg in enumerate(user_msgs[1:], start=1):
            if has_correction(msg):
                last_corr_idx = i
        convergence = 1.0 - (last_corr_idx / max(len(user_msgs) - 1, 1))

    # TF: Tool Friction — error rate from tool_use/tool_result pairs
    tool_calls, tool_errors = _extract_tool_errors(messages)
    tool_friction = tool_errors / max(tool_calls, 1)

    # TS: Task Switching — anchor set changes between consecutive user turns
    switch_count = 0
    prev_anchors = set()
    for text in user_msgs:
        anchors = _extract_file_anchors(text)
        if prev_anchors and anchors and not anchors & prev_anchors:
            # Complete anchor disjunction = context switch
            switch_count += 1
        if anchors:
            prev_anchors = anchors
    task_switching = switch_count / max(len(user_msgs) - 1, 1)

    # PS: Phase Spread — entropy of work phases across the session
    all_phases = Counter()
    for _, text in all_text_by_turn:
        for phase in _classify_phases(text):
            all_phases[phase] += 1
    total_phase_tags = sum(all_phases.values())
    if total_phase_tags > 0 and len(all_phases) > 1:
        # Shannon entropy, normalized to [0, 1]
        max_entropy = math.log(len(_PHASE_PATTERNS))  # max possible phases
        entropy = -sum(
            (c / total_phase_tags) * math.log(c / total_phase_tags)
            for c in all_phases.values() if c > 0
        )
        phase_spread = entropy / max_entropy if max_entropy > 0 else 0.0
    else:
        phase_spread = 0.0

    # Episode chunking metrics
    episodes = _chunk_into_episodes(messages)
    n_episodes = len(episodes)

    # Active duration (sum of episode durations, excluding gaps)
    active_ms = 0
    for ep in episodes:
        first_ts = _msg_timestamp_ms(ep[0])
        last_ts = _msg_timestamp_ms(ep[-1])
        if first_ts and last_ts and last_ts > first_ts:
            active_ms += (last_ts - first_ts)
    active_duration_min = active_ms / 60000

    return {
        "friction": friction,
        "escalation": escalation,
        "extraction": extraction,
        "convergence": convergence,
        "tool_friction": tool_friction,
        "task_switching": task_switching,
        "phase_spread": phase_spread,
        "n_episodes": n_episodes,
        "active_duration_min": active_duration_min,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
    }


def _compute_ce_metrics(sessions: list[dict]) -> dict:
    """Compute Cognitive Efficiency metrics across all sessions.

    Returns opaque dict for dashboard consumption — no formula details exposed.
    Uses 7 internal components (original 4 + tool friction, task switching,
    phase spread) and episode chunking for cleaner time estimates.
    """
    raw_components = []  # list of component dicts
    session_meta = []    # (started_at, agent)

    # Dedup tracking
    seen = set()

    for s in sessions:
        started = s.get("started_at")
        if not started:
            continue

        # Deduplicate sessions
        sid = s.get("session_id", "")
        if sid in seen:
            continue
        seen.add(sid)

        result = _compute_session_ce(s)
        if result is None:
            continue

        raw_components.append(result)
        session_meta.append((started, s.get("agent", "unknown")))

    n = len(raw_components)
    if n < 10:
        return {"available": False, "reason": "insufficient_sessions",
                "scored": n, "minimum": 10}

    # Z-score each component
    frictions = [c["friction"] for c in raw_components]
    escalations = [max(c["escalation"], 0) for c in raw_components]
    extractions = [c["extraction"] for c in raw_components]
    convergences = [c["convergence"] for c in raw_components]
    tool_frictions = [c["tool_friction"] for c in raw_components]
    task_switchings = [c["task_switching"] for c in raw_components]
    phase_spreads = [c["phase_spread"] for c in raw_components]

    z_f = _z_scores(frictions)
    z_e = _z_scores(escalations)
    z_x = _z_scores(extractions)
    z_c = _z_scores(convergences)
    z_tf = _z_scores(tool_frictions)
    z_ts = _z_scores(task_switchings)
    z_ps = _z_scores(phase_spreads)

    # Composite CE per session (formula is internal IP)
    # Positive: convergence quality, value extraction
    # Negative: friction, escalation, tool friction, task switching, phase spread
    # Task switching weighted 1.5x — paper's strongest finding
    ce_scores = [
        z_c[i] + z_x[i] - z_f[i] - z_e[i] - z_tf[i] - 1.5 * z_ts[i] - 0.5 * z_ps[i]
        for i in range(n)
    ]

    # Intrinsic load proxy (for difficulty-adjusted learning)
    # Phase spread + episode count approximate task complexity
    il_scores = [z_ps[i] for i in range(n)]

    # Performance proxy (positive components only)
    p_scores = [z_c[i] + z_x[i] for i in range(n)]

    # Weekly aggregation — output only opaque ce_score
    weekly_buckets = defaultdict(list)
    weekly_p_buckets = defaultdict(list)
    weekly_il_buckets = defaultdict(list)
    for i in range(n):
        started = session_meta[i][0]
        dt = datetime.fromtimestamp(started / 1000, tz=timezone.utc)
        week = f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"
        weekly_buckets[week].append(ce_scores[i])
        weekly_p_buckets[week].append(p_scores[i])
        weekly_il_buckets[week].append(il_scores[i])

    weekly_ce = []
    for week in sorted(weekly_buckets.keys()):
        vals = weekly_buckets[week]
        weekly_ce.append({
            "week": week,
            "ce_score": round(sum(vals) / len(vals), 3),
            "n_sessions": len(vals),
        })

    # Learning rate (schema formation signal) — raw CE slope
    learning_rate = None
    if len(weekly_ce) >= 3:
        indices = list(range(len(weekly_ce)))
        values = [w["ce_score"] for w in weekly_ce]
        learning_rate = round(_linear_regression_slope(indices, values), 4)

    # Difficulty-adjusted learning (residual performance after controlling for IL)
    # Fit P ~ alpha + beta * IL, then take slope of residuals over time
    schema_index = None
    if len(weekly_ce) >= 3:
        sorted_weeks = sorted(weekly_p_buckets.keys())
        weekly_p_means = [sum(weekly_p_buckets[w]) / len(weekly_p_buckets[w])
                          for w in sorted_weeks]
        weekly_il_means = [sum(weekly_il_buckets[w]) / len(weekly_il_buckets[w])
                           for w in sorted_weeks]

        # Regress P on IL
        beta_il = _linear_regression_slope(weekly_il_means, weekly_p_means)
        alpha_il = (sum(weekly_p_means) / len(weekly_p_means) -
                    beta_il * sum(weekly_il_means) / len(weekly_il_means))

        # Residuals = P - predicted_P
        residuals = [
            weekly_p_means[i] - (alpha_il + beta_il * weekly_il_means[i])
            for i in range(len(sorted_weeks))
        ]

        # Slope of residuals over time = schema formation
        schema_index = round(
            _linear_regression_slope(list(range(len(residuals))), residuals), 4
        )

    # Per-agent CE (opaque means only)
    agent_buckets = defaultdict(list)
    for i in range(n):
        agent_buckets[session_meta[i][1]].append(ce_scores[i])

    agent_ce = {}
    for agent, ces in sorted(agent_buckets.items()):
        if len(ces) >= 5:
            agent_ce[agent] = {
                "mean_ce": round(sum(ces) / len(ces), 3),
                "n_sessions": len(ces),
            }

    # --- Behavioral diagnostics (plain-language, no formula internals) ---

    # Time-of-day CE
    tod_buckets = defaultdict(list)
    for i in range(n):
        started = session_meta[i][0]
        dt = datetime.fromtimestamp(started / 1000, tz=timezone.utc)
        hour = dt.hour
        if 6 <= hour < 12:
            period = "morning"
        elif 12 <= hour < 17:
            period = "afternoon"
        elif 17 <= hour < 22:
            period = "evening"
        else:
            period = "night"
        tod_buckets[period].append(ce_scores[i])

    time_of_day_ce = {}
    for period in ["morning", "afternoon", "evening", "night"]:
        scores = tod_buckets.get(period, [])
        if len(scores) >= 5:
            time_of_day_ce[period] = {
                "mean_ce": round(sum(scores) / len(scores), 3),
                "n_sessions": len(scores),
            }

    # Correction rate (plain descriptive stat)
    avg_correction_rate = round(sum(frictions) / n, 3)

    # Prompt growth tendency
    pos_escalation = sum(1 for c in raw_components if c["escalation"] > 0.05)
    prompt_trend = ("expanding" if pos_escalation > 0.6 * n else
                    "contracting" if pos_escalation < 0.3 * n else "stable")

    # Output leverage (agent chars out / user chars in)
    avg_leverage = round(sum(extractions) / n, 1)

    # Tool error stats
    total_tool_calls = sum(c["tool_calls"] for c in raw_components)
    total_tool_errors = sum(c["tool_errors"] for c in raw_components)
    tool_error_rate = round(total_tool_errors / max(total_tool_calls, 1), 3)

    # Task switching rate
    avg_task_switching = round(sum(task_switchings) / n, 3)

    # Phase spread stats
    avg_phase_spread = round(sum(phase_spreads) / n, 3)

    # Episode stats
    total_episodes = sum(c["n_episodes"] for c in raw_components)
    avg_episodes_per_session = round(total_episodes / n, 1)
    active_durations = [c["active_duration_min"] for c in raw_components
                        if c["active_duration_min"] > 0]
    avg_active_duration = (round(sum(active_durations) / len(active_durations), 1)
                           if active_durations else 0)

    # Recent trend vs. prior baseline
    recent_trend = None
    if len(weekly_ce) >= 4:
        recent = weekly_ce[-3:]
        prior = weekly_ce[:-3]
        recent_avg = sum(w["ce_score"] for w in recent) / len(recent)
        prior_avg = sum(w["ce_score"] for w in prior) / len(prior)
        if recent_avg > prior_avg + 0.3:
            recent_trend = "improving"
        elif recent_avg < prior_avg - 0.3:
            recent_trend = "declining"
        else:
            recent_trend = "stable"

    diagnostics = {
        "correction_rate": avg_correction_rate,
        "prompt_trend": prompt_trend,
        "avg_output_ratio": avg_leverage,
        "time_of_day_ce": time_of_day_ce,
        "recent_trend": recent_trend,
        "tool_error_rate": tool_error_rate,
        "total_tool_calls": total_tool_calls,
        "total_tool_errors": total_tool_errors,
        "avg_task_switching": avg_task_switching,
        "avg_phase_spread": avg_phase_spread,
        "avg_episodes_per_session": avg_episodes_per_session,
        "avg_active_duration_min": avg_active_duration,
    }

    # --- Actionable recommendations ---
    recommendations = _generate_ce_recommendations(
        diagnostics, agent_ce, learning_rate, schema_index
    )

    return {
        "available": True,
        "weekly_ce": weekly_ce,
        "learning_rate": learning_rate,
        "schema_index": schema_index,
        "agent_ce": agent_ce,
        "total_scored_sessions": n,
        "total_sessions": len(sessions),
        "diagnostics": diagnostics,
        "recommendations": recommendations,
    }


def _generate_ce_recommendations(diagnostics: dict, agent_ce: dict,
                                  learning_rate: float | None,
                                  schema_index: float | None = None) -> list[dict]:
    """Generate concrete, actionable recommendations from CE diagnostics.

    Each recommendation is {title, body, priority, category}.
    Categories: prompting, tooling, workflow, timing, config.
    """
    recs = []
    cr = diagnostics["correction_rate"]

    # --- Task switching (paper's strongest finding) ---
    ts = diagnostics.get("avg_task_switching", 0)
    if ts > 0.25:
        recs.append({
            "title": "Reduce mid-session context switching",
            "body": (
                f"About {ts:.0%} of your turn transitions involve a complete "
                "context switch (jumping to unrelated files/modules). Research shows "
                "this is the single strongest predictor of quality loss. Try: "
                "finish the current loop (implement \u2192 test \u2192 verify) before "
                "switching focus. If a tangent comes up, log it and come back later. "
                "Consider starting a new session for new tasks rather than "
                "pivoting mid-conversation."
            ),
            "priority": "high",
            "category": "workflow",
        })
    elif ts > 0.10:
        recs.append({
            "title": "Watch for context switches",
            "body": (
                f"Your context-switch rate ({ts:.0%}) is moderate. When you notice "
                "yourself jumping to a different part of the codebase mid-session, "
                "pause and ask: is the current task done? If not, park the new "
                "idea and come back to it."
            ),
            "priority": "medium",
            "category": "workflow",
        })

    # --- Tool error rate ---
    ter = diagnostics.get("tool_error_rate", 0)
    total_tc = diagnostics.get("total_tool_calls", 0)
    total_te = diagnostics.get("total_tool_errors", 0)
    if ter > 0.15 and total_tc > 20:
        recs.append({
            "title": "High tool error rate \u2014 debug churn detected",
            "body": (
                f"{total_te:,} of {total_tc:,} tool calls ({ter:.0%}) resulted in errors. "
                "This suggests repeated failed attempts \u2014 builds that don't compile, "
                "tests that keep failing, or file operations that miss their target. "
                "When you hit 2 consecutive tool errors, stop and diagnose the root "
                "cause before retrying. Ask the agent to explain what went wrong "
                "before trying the next fix."
            ),
            "priority": "high",
            "category": "workflow",
        })
    elif ter > 0.08 and total_tc > 20:
        recs.append({
            "title": "Moderate tool friction",
            "body": (
                f"About {ter:.0%} of tool operations encounter errors. Some friction "
                "is normal, but watch for retry loops \u2014 they burn time and attention "
                "without producing output."
            ),
            "priority": "medium",
            "category": "tooling",
        })

    # --- Phase spread (doing too many things at once) ---
    ps = diagnostics.get("avg_phase_spread", 0)
    if ps > 0.7:
        recs.append({
            "title": "Tighten your work loops",
            "body": (
                "Your sessions mix many work phases (reading, planning, implementing, "
                "testing, debugging, polishing) in parallel. This increases cognitive "
                "load. Try enforcing a sequence: inspect \u2192 plan \u2192 implement \u2192 verify. "
                "Complete each phase before moving to the next. Save polish/refactor "
                "for a dedicated pass."
            ),
            "priority": "medium",
            "category": "workflow",
        })

    # --- Correction rate ---
    if cr > 0.25:
        recs.append({
            "title": "Reduce correction cycles",
            "body": (
                f"You're redirecting the agent in roughly {cr:.0%} of sessions. "
                "That's a lot of back-and-forth that doesn't produce output. "
                "Consider adding your project conventions, preferred patterns, and "
                "common pitfalls to your CLAUDE.md (or equivalent system prompt). "
                "Front-loading this context means the agent gets it right the first time."
            ),
            "priority": "high",
            "category": "prompting",
        })
    elif cr > 0.15:
        recs.append({
            "title": "Tune your initial context",
            "body": (
                f"Your correction rate ({cr:.0%}) is moderate \u2014 some redirection is "
                "normal, but there's room to reduce it. Review which corrections recur: "
                "if they're about code style, naming, or conventions, encode those in "
                "your CLAUDE.md. If they're about misunderstood requirements, try "
                "including acceptance criteria in your initial prompt."
            ),
            "priority": "medium",
            "category": "prompting",
        })

    # --- Prompt escalation ---
    if diagnostics["prompt_trend"] == "expanding":
        recs.append({
            "title": "Break complex tasks into steps",
            "body": (
                "Your prompts tend to grow longer within sessions \u2014 each message "
                "adds more context as the agent struggles with complexity. Try "
                "decomposing multi-part requests into focused, sequential prompts. "
                "Let the agent confirm each step before moving on. Smaller scope "
                "per prompt = fewer misunderstandings."
            ),
            "priority": "medium",
            "category": "workflow",
        })

    # --- Output leverage ---
    olr = diagnostics["avg_output_ratio"]
    if olr < 3.0:
        recs.append({
            "title": "Delegate more to the agent",
            "body": (
                f"Your input-to-output ratio is {olr:.1f}:1 "
                "(agent output per unit of your input). You may be over-specifying. "
                "Try giving higher-level instructions and letting the agent fill in "
                "implementation details. Use follow-ups for refinement rather than "
                "front-loading every detail."
            ),
            "priority": "medium",
            "category": "prompting",
        })
    elif olr > 20.0:
        recs.append({
            "title": "High leverage \u2014 spot-check output quality",
            "body": (
                f"You're extracting {olr:.0f}\u00d7 more output "
                "than input, which is efficient \u2014 but high-leverage sessions can "
                "produce output you haven't fully reviewed. Spot-check agent output "
                "for correctness, especially on unfamiliar code or when the agent "
                "is generating large blocks."
            ),
            "priority": "low",
            "category": "workflow",
        })

    # --- Time-of-day ---
    tod = diagnostics.get("time_of_day_ce", {})
    if len(tod) >= 2:
        best_period = max(tod.items(), key=lambda x: x[1]["mean_ce"])
        worst_period = min(tod.items(), key=lambda x: x[1]["mean_ce"])
        gap = best_period[1]["mean_ce"] - worst_period[1]["mean_ce"]
        if gap > 0.5:
            bp_name = best_period[0]
            bp_ce = best_period[1]["mean_ce"]
            bp_n = best_period[1]["n_sessions"]
            wp_name = worst_period[0]
            wp_ce = worst_period[1]["mean_ce"]
            wp_n = worst_period[1]["n_sessions"]
            recs.append({
                "title": f"Your peak: {bp_name} sessions (UTC)",
                "body": (
                    f"Your interaction efficiency is highest in the {bp_name} "
                    f"(CE: {bp_ce:+.2f}, {bp_n} sessions) and lowest in the "
                    f"{wp_name} (CE: {wp_ce:+.2f}, {wp_n} sessions). "
                    "Consider scheduling your most demanding agent-assisted tasks "
                    f"during {bp_name} hours, and saving routine/simple tasks "
                    f"for the {wp_name}."
                ),
                "priority": "medium",
                "category": "timing",
            })

    # --- Agent selection ---
    if len(agent_ce) >= 2:
        sorted_agents = sorted(agent_ce.items(),
                                key=lambda x: x[1]["mean_ce"], reverse=True)
        best = sorted_agents[0]
        worst = sorted_agents[-1]
        gap = best[1]["mean_ce"] - worst[1]["mean_ce"]
        if gap > 1.0:
            best_name = best[0].replace("_", " ")
            worst_name = worst[0].replace("_", " ")
            recs.append({
                "title": f"Prefer {best_name} for complex work",
                "body": (
                    f"Your efficiency with {best_name} is "
                    f"substantially higher than with {worst_name} "
                    f"(CE gap: {gap:.1f}\u03c3). When both agents can handle a task, "
                    f"default to {best_name}. For tasks that require "
                    f"{worst_name}, invest more in initial prompting \u2014 "
                    "detailed context, explicit constraints, and examples."
                ),
                "priority": "high",
                "category": "tooling",
            })

    # --- Recent dip ---
    if diagnostics.get("recent_trend") == "declining":
        recs.append({
            "title": "Address the recent efficiency dip",
            "body": (
                "Your CE has declined over the last few weeks relative to your "
                "earlier baseline. Common causes: new codebase/domain, switching "
                "tools, or increasing task ambiguity. Review whether your CLAUDE.md "
                "or system prompt still matches your current workflows. If you "
                "recently changed projects or tools, expect a ramp-up period \u2014 "
                "update your agent configuration to reflect the new context."
            ),
            "priority": "medium",
            "category": "workflow",
        })

    # --- Schema formation signal ---
    if (schema_index is not None and schema_index < -0.1
            and learning_rate is not None and learning_rate < -0.05):
        recs.append({
            "title": "Schema formation is stalling",
            "body": (
                "After adjusting for task difficulty, your performance isn't improving "
                "over time. In CLT terms: working memory is being spent compensating "
                "for coordination overhead rather than building stable mental models. "
                "Signs to watch for: re-reading the same files repeatedly, asking the "
                "agent to re-explain architecture, not improving 'time to green' "
                "in repos you visit often. Focus on internalizing the codebase \u2014 "
                "write schema notes after each subtask to crystallize what you learned."
            ),
            "priority": "high",
            "category": "workflow",
        })
    elif learning_rate is not None and learning_rate > 0.1:
        recs.append({
            "title": "Keep doing what you\u2019re doing",
            "body": (
                "Your efficiency is improving over time \u2014 your mental models for "
                "agent interaction are getting sharper. Document your most effective "
                "prompting patterns so they survive context switches. Consider "
                "sharing what works with your team."
            ),
            "priority": "low",
            "category": "workflow",
        })

    # --- CLAUDE.md template (grounded in paper findings) ---
    config_signals = sum([
        cr > 0.15,
        ts > 0.15,
        diagnostics.get("prompt_trend") == "expanding",
        ter > 0.10,
    ])
    if config_signals >= 2:
        recs.append({
            "title": "Add CE guardrails to your agent config",
            "body": (
                "Multiple signals suggest your agent interactions could benefit from "
                "explicit guardrails. Consider adding this to your CLAUDE.md or "
                "system prompt:\n\n"
                "## Focus Discipline\n"
                "- Maintain ONE active objective at a time.\n"
                "- If you notice a valuable tangent, add it to a Parking Lot "
                "instead of switching.\n"
                "- Before switching files/modules, ask: is the current task done?\n\n"
                "## Structured Disclosure\n"
                "- Default format: 1) Current objective, 2) Next 1-3 actions, "
                "3) Why this is the right move.\n"
                "- Do NOT introduce new subtasks unless asked.\n\n"
                "## Close Loops\n"
                "- Define 'done' for each subtask (tests pass, lint clean).\n"
                "- Do not start a new subtask until the current one has a clear "
                "closure signal.\n\n"
                "## Tool-Error Hygiene\n"
                "- After a tool error: propose ONE root cause + ONE fix.\n"
                "- After 2 consecutive failures, stop and ask a clarifying question."
            ),
            "priority": "medium",
            "category": "config",
        })

    return recs





# ---------------------------------------------------------------------------
# Report computation
# ---------------------------------------------------------------------------

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
                       time_series: list[dict],
                       ce_metrics: dict | None = None) -> list[dict]:
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

    # --- Cognitive Efficiency trajectory (if available) ---
    if ce_metrics and ce_metrics.get("available"):
        scored = ce_metrics.get("total_scored_sessions", 0)
        total = ce_metrics.get("total_sessions", 0)
        lr = ce_metrics.get("learning_rate")
        si = ce_metrics.get("schema_index")
        diag = ce_metrics.get("diagnostics", {})
        cr = diag.get("correction_rate", 0)
        pt = diag.get("prompt_trend", "stable")
        olr = diag.get("avg_output_ratio", 0)
        ter = diag.get("tool_error_rate", 0)
        ts_rate = diag.get("avg_task_switching", 0)
        ps = diag.get("avg_phase_spread", 0)

        # --- Headline card: what's the trajectory and what does it mean? ---
        ce_parts = []

        if lr is not None:
            if lr > 0.05:
                ce_parts.append(
                    f"Your CE trajectory is <strong>trending up</strong> "
                    f"({lr:+.3f}/week across {scored:,} scored sessions). "
                    f"In practical terms: you're getting more done per interaction "
                    f"cycle \u2014 fewer corrections, better-targeted prompts, and more "
                    f"agent output per unit of your effort. This is consistent with "
                    f"<strong>schema formation</strong> \u2014 you're building efficient "
                    f"mental models for how to work with these agents."
                )
                ce_color = "green"
            elif lr < -0.05:
                ce_parts.append(
                    f"Your CE trajectory is <strong>declining</strong> "
                    f"({lr:+.3f}/week across {scored:,} scored sessions). "
                    f"In practical terms: recent sessions took more effort to get "
                    f"to the same result \u2014 more corrections, longer prompt chains, "
                    f"or less output per interaction. This doesn't necessarily mean "
                    f"your work quality dropped. Common causes: tackling harder "
                    f"problems, switching domains, adopting new tools, or working "
                    f"during less-focused hours."
                )
                ce_color = "amber"
            else:
                ce_parts.append(
                    f"Your CE trajectory is <strong>stable</strong> "
                    f"({lr:+.3f}/week across {scored:,} scored sessions). "
                    f"Your interaction efficiency is consistent \u2014 you've found "
                    f"a working rhythm with your agents."
                )
                ce_color = "blue"
        else:
            ce_color = "blue"
            ce_parts.append(
                f"CE was computed for {scored:,} sessions but there aren't "
                f"enough weekly data points to establish a trajectory yet."
            )

        # Difficulty-adjusted learning (schema index)
        if si is not None and lr is not None:
            if si > 0.05 and lr < -0.05:
                ce_parts.append(
                    "However, after adjusting for task difficulty, your "
                    "<strong>schema formation is actually positive</strong> "
                    f"(+{si:.3f}/week). The CE decline likely reflects harder "
                    "problems, not declining skill."
                )
                ce_color = "blue"  # upgrade from amber
            elif si < -0.1 and lr < -0.05:
                ce_parts.append(
                    "After adjusting for task difficulty, performance is still "
                    f"declining (schema index: {si:+.3f}/week). This suggests "
                    "coordination overhead is consuming resources that could "
                    "support learning."
                )

        # Add behavioral color
        behavior_parts = []
        if ts_rate > 0.15:
            behavior_parts.append(
                f"context switching is elevated ({ts_rate:.0%} of turns)"
            )
        if ter > 0.10:
            total_tc = diag.get("total_tool_calls", 0)
            behavior_parts.append(
                f"tool error rate is {ter:.0%} across {total_tc:,} calls"
            )
        if cr > 0.20:
            behavior_parts.append(
                f"correction rate is {cr:.0%}"
            )
        if pt == "expanding":
            behavior_parts.append(
                "prompts tend to escalate within sessions"
            )
        if ps > 0.6:
            behavior_parts.append(
                "sessions mix many work phases simultaneously"
            )
        if olr < 3.0:
            behavior_parts.append(
                f"output leverage is low ({olr:.1f}\u00d7)"
            )
        elif olr > 15.0:
            behavior_parts.append(
                f"output leverage is high ({olr:.0f}\u00d7)"
            )

        if behavior_parts:
            ce_parts.append(
                "Key behavioral signals: " +
                "; ".join(behavior_parts) + ". "
                "See the Cognitive Efficiency section below for specific "
                "recommendations."
            )

        # Episode stats
        avg_eps = diag.get("avg_episodes_per_session", 0)
        avg_active = diag.get("avg_active_duration_min", 0)
        if avg_eps > 1.5 and avg_active > 0:
            ce_parts.append(
                f"Sessions average {avg_eps:.1f} active work episodes "
                f"({avg_active:.0f} min active time per session)."
            )

        # Per-agent CE comparison
        agent_ce = ce_metrics.get("agent_ce", {})
        if len(agent_ce) > 1:
            sorted_agents = sorted(agent_ce.items(),
                                    key=lambda x: x[1]["mean_ce"], reverse=True)
            best = sorted_agents[0]
            worst = sorted_agents[-1]
            gap = best[1]["mean_ce"] - worst[1]["mean_ce"]
            best_name = best[0].replace("_", " ")
            worst_name = worst[0].replace("_", " ")
            best_ce = best[1]["mean_ce"]
            worst_ce = worst[1]["mean_ce"]
            if gap > 1.0:
                ce_parts.append(
                    f"Agent-wise, you're dramatically more efficient with "
                    f"<strong>{best_name}</strong> "
                    f"(CE: {best_ce:+.2f}) than "
                    f"<strong>{worst_name}</strong> "
                    f"(CE: {worst_ce:+.2f}) \u2014 "
                    f"a {gap:.1f}\u03c3 gap."
                )
            else:
                ce_parts.append(
                    f"CE is similar across agents ("
                    + ", ".join(
                        f"{a.replace('_', ' ')}: {d['mean_ce']:+.2f}"
                        for a, d in sorted_agents
                    ) + ")."
                )

        insights.append({
            "title": "Cognitive Efficiency",
            "body": " ".join(ce_parts),
            "color": ce_color,
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

    # Cognitive Efficiency (computed before insights so we can reference it)
    ce_metrics = _compute_ce_metrics(sessions)

    # Narrative insights (auto-generated from data patterns)
    insights = _generate_insights(report, agent_metrics, time_series,
                                   ce_metrics=ce_metrics)

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
        "ce": ce_metrics,
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
