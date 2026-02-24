# Protodashboard Spec: Chris Sells (Customer 0, B2C)

## Context

Chris Sells runs the Gastown Discord. He tried our `skillbench.html` and found nothing actionable. In contrast, Claude Code's `/insights` command gave him:
- Where he sits on an informal "agentic engineering ladder"
- Concrete next-level behaviors with example prompts
- A sense of progression

He wants **CC Insights x10**: same thing, but powered by cross-user data, giving him (a) a formalized ladder position, (b) examples from users ahead of him, and (c) eventually mentor connections.

**Goal**: Ship a protodashboard daily until Chris is delighted, then launch on the Gastown Discord.

**Code freeze**: March 6, 2026.

---

## The Data Gap (and the Opportunity)

### What we have now (skillmeter pipeline)
- VS Code extension captures keystroke-level `text.change` events
- Author classification (human/agent/system/clipboard)
- AI/human character ratio per device per time window
- Feeds through ClickHouse → skillmeter-analysis → JSON

### What Chris has (prior session data)
- Claude Code sessions at `~/.claude/projects/*/session-*.jsonl`
- Possibly Cursor, Copilot, or other agent history
- **This is conversation-level data**: prompts, responses, tool calls, code snippets, timestamps

### The key insight
These are **complementary data layers**, not competing ones:

| Layer | Source | Granularity | What it tells you |
|-------|--------|-------------|-------------------|
| Keystroke | skillmeter extension | Character-level | How much AI wrote vs. you |
| Session | CASS extraction | Conversation-level | How effectively you direct AI |

Chris's feedback makes it clear: **the session layer is where the actionable insights live.** The keystroke layer tells you _how much_ AI help you get. The session layer tells you _how well_ you use AI — which is what people actually want to improve.

---

## Architecture: CASS as Ingestion Layer

### Why CASS (coding_agent_session_search)

The [Dicklesworthstone/coding_agent_session_search](https://github.com/Dicklesworthstone/coding_agent_session_search) repo is a Rust tool that:
- Reads Claude Code sessions from `~/.claude/projects/*/session-*.jsonl`
- Supports 11+ agent providers (Cursor, Copilot, Gemini, Aider, etc.)
- Normalizes everything into a unified schema: `Conversation → Message → Snippet`
- Outputs JSON via `--robot` mode for machine consumption
- Runs fully local, no network calls
- Sub-60ms search, SQLite + Tantivy indexing

### Proposed data flow

```
Chris's machine                              SkillBench
──────────────────────────────               ──────────────────────────
CASS index (all agents)
    │
    ▼
┌──────────────────────┐
│  Boot Block Tool     │
│                      │
│  1. Scan CASS index  │
│     (all projects)   │
│                      │
│  2. For each project │
│     folder, check:   │
│     - git remote     │
│       (public/priv)  │
│     - LICENSE file   │
│       (MIT/Apache =  │
│        auto-include) │
│     - No license or  │
│       proprietary =  │
│       exclude        │
│                      │
│  3. Generate:        │
│     bootblock.txt    │
│     (editable list   │
│      of folders)     │
│                      │
│  4. User reviews,    │
│     adds/removes     │
│     folders          │
│                      │
│  5. Push selected    │
│     session data     │
└──────────┬───────────┘
           │ HTTPS POST (raw sessions
           │ for selected folders only)
           ▼
    ┌─────────────┐    ┌──────────────────┐
    │  ClickHouse  │───▶│ session-analysis  │
    │  (new table) │    │  (Python)         │
    └─────────────┘    └────────┬─────────┘
                                │
                                ▼
                       ┌────────────────┐
                       │ protodashboard  │
                       │ (frontend-web)  │
                       └────────────────┘
                                │
                                ▼
                       ┌────────────────┐
                       │ Email insights  │
                       │ on each upload  │
                       └────────────────┘
```

### Boot block tool: `bootblock.txt` format

```
# SkillBench Boot Block — auto-generated from CASS index + git/license scan
# Edit this file: add/remove folders, then run `skillbench push`
#
# AUTO-INCLUDED (public repo + OSS license detected):
/Users/csells/src/open-project-1          # MIT, github.com/csells/open-project-1
/Users/csells/src/open-project-2          # Apache-2.0, github.com/csells/open-project-2

# EXCLUDED (no license or proprietary — uncomment to include):
# /Users/csells/src/client-work-1         # no LICENSE file
# /Users/csells/src/private-project       # proprietary license
# /Users/csells/src/day-job-stuff         # private repo, no OSS license

# MANUAL ADDITIONS (paste paths here):
```

### Upload loop

- Each `skillbench push` sends raw sessions for the listed folders
- Server tracks `last_upload_timestamp` per user
- Subsequent pushes can be incremental (only new sessions since last upload)
- Each upload triggers an insights email summarizing what changed since last time
- Email acts as a behavioral nudge — encourages repeat uploads

### Alternative: skip ClickHouse for v0

For speed-to-Chris, we could skip ClickHouse entirely for the protodashboard and process the uploaded JSON directly:

```
skillbench push → our server (Python analysis) → email + static HTML report
```

This gets us to a shippable artifact in days, not weeks. Wire it into the real pipeline later.

---

## Session-Level Metrics (the "Agentic Engineering Ladder")

Derived from CASS conversation data, these metrics define where someone sits:

### Tier 1: Usage Patterns (table stakes)
| Metric | What it measures | Data source |
|--------|-----------------|-------------|
| `sessions_per_week` | How often they use AI agents | Conversation timestamps |
| `avg_session_duration` | How long sessions run | First/last message timestamps |
| `agent_diversity` | How many different agents used | `agent` field |
| `active_days_per_week` | Consistency of usage | Date aggregation |

### Tier 2: Prompting Sophistication
| Metric | What it measures | Data source |
|--------|-----------------|-------------|
| `avg_prompt_length` | Detail level in requests | User message character count |
| `context_provision_rate` | How often they provide file/code context | Mentions of files, paths, code blocks in user messages |
| `specificity_score` | Vague ("fix this") vs. precise ("refactor X to use pattern Y") | NLP classification on user messages |
| `multi_step_rate` | % of sessions with >3 user turns | Message count per conversation |

### Tier 3: Iteration Efficiency
| Metric | What it measures | Data source |
|--------|-----------------|-------------|
| `first_attempt_success_rate` | How often the first response works | Sessions with only 1-2 user messages |
| `correction_rate` | How often they redirect the agent | "no", "wrong", "instead" patterns in follow-ups |
| `avg_turns_to_completion` | Efficiency of task completion | Messages per session |
| `tool_diversity_per_session` | Range of tools invoked | Tool message types |

### Tier 4: Agentic Engineering Mastery
| Metric | What it measures | Data source |
|--------|-----------------|-------------|
| `delegation_complexity` | Complexity of tasks delegated | Code snippet count, file diversity, language diversity per session |
| `multi_file_rate` | % of sessions touching 3+ files | Unique file paths in snippets |
| `architecture_level_work` | Refactoring, design, cross-cutting changes | NLP + heuristics on prompts |
| `recovery_skill` | How well they handle agent errors | Pattern: error → user correction → success |

### Ladder Levels (strawman — refine with data)

| Level | Name | Profile |
|-------|------|---------|
| L1 | **Dabbler** | <3 sessions/week, short prompts, single-file tasks, high correction rate |
| L2 | **Adopter** | Daily usage, moderate prompts, some multi-file work, learning to provide context |
| L3 | **Practitioner** | Daily, specific prompts with context, multi-file workflows, moderate first-attempt success |
| L4 | **Engineer** | High delegation complexity, architecture-level tasks, low correction rate, diverse tool usage |
| L5 | **Maestro** | Orchestrates multi-agent workflows, novel prompt patterns, mentors others (future: detected via community data) |

---

## Protodashboard v0: What Chris Sees

### Page 1: Your Agentic Profile

```
┌─────────────────────────────────────────────────────┐
│  Chris Sells — Agentic Engineering Profile          │
│                                                     │
│  Level: L3 Practitioner  ████████████░░░░  72/100   │
│  (Top 35% of Gastown community)                     │
│                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │ Sessions/wk │  │ Avg Prompt  │  │ First-try   │ │
│  │    12.4     │  │   287 chars │  │   Success   │ │
│  │  ▲ +3 vs    │  │  ▲ +45 vs   │  │    64%      │ │
│  │   last mo   │  │   last mo   │  │  ▲ +8% vs   │ │
│  │             │  │             │  │   last mo   │ │
│  └─────────────┘  └─────────────┘  └─────────────┘ │
│                                                     │
│  Strengths                 Growth Edges             │
│  ✦ High session frequency  ✧ Context provision      │
│  ✦ Multi-file comfort      ✧ First-attempt success  │
│  ✦ Tool diversity          ✧ Delegation complexity   │
└─────────────────────────────────────────────────────┘
```

### Page 2: Level Up Guide

```
┌─────────────────────────────────────────────────────┐
│  To reach L4 Engineer, focus on:                    │
│                                                     │
│  1. CONTEXT PROVISION (you: 45% → L4 avg: 78%)     │
│     Your prompts often lack file references.        │
│     Try: "In src/auth/middleware.ts, the JWT        │
│     validation at line 42 doesn't handle expired    │
│     tokens. Add a refresh flow that..."             │
│                                                     │
│     Example from an L4 user:                        │
│     "Refactor the payment processing in             │
│     src/payments/ to use the Strategy pattern.      │
│     Currently checkout.ts has 4 nested if/else      │
│     blocks for different payment providers..."      │
│                                                     │
│  2. DELEGATION COMPLEXITY (you: 2.1 → L4 avg: 4.3) │
│     You tend to break work into small single-file   │
│     tasks. Try combining related changes:           │
│     "Add user authentication: create the User       │
│     model, auth middleware, login/register           │
│     endpoints, and update the route config..."      │
│                                                     │
│  3. FIRST-ATTEMPT SUCCESS (you: 64% → L4 avg: 79%) │
│     Your corrections often add context that should  │
│     have been in the original prompt. Before        │
│     prompting, ask: "What does the agent need to    │
│     know to get this right the first time?"         │
└─────────────────────────────────────────────────────┘
```

### Page 3: Your Trends (weekly)

```
┌─────────────────────────────────────────────────────┐
│  Agentic Engineering Score — Last 8 Weeks           │
│                                                     │
│  80 ┤                                    ╭──● 72    │
│  70 ┤                          ╭────╮╭──╯           │
│  60 ┤              ╭──────────╯    ╰╯               │
│  50 ┤    ╭────────╯                                 │
│  40 ┤───╯                                           │
│     └──┬──┬──┬──┬──┬──┬──┬──┬──                     │
│       w1 w2 w3 w4 w5 w6 w7 w8                       │
│                                                     │
│  Biggest jump: Week 4 → 5 (+12 pts)                 │
│  What changed: Started providing file paths in      │
│  prompts, reduced correction rate by 15%            │
└─────────────────────────────────────────────────────┘
```

---

## Implementation Plan (Daily Ship Cadence)

### Phase 0: Boot block tool + first upload (Days 1-2)
- [ ] Chris already has or installs CASS (`cargo install cass`), runs `cass index --full`
- [ ] We build `skillbench-bootblock` CLI (Python, single file, pip-installable)
  - Scans CASS index for all project folders
  - For each folder: checks `git remote -v` (public/private), scans for LICENSE/COPYING files, classifies license type (MIT/Apache = OSS, other/missing = excluded)
  - Generates `bootblock.txt` with auto-included OSS projects and commented-out private ones
- [ ] Chris reviews/edits `bootblock.txt` (~2 min of work per his estimate)
- [ ] Chris runs `skillbench push` → uploads raw sessions for selected folders to our endpoint
- [ ] Server confirms receipt, stores with `last_upload_timestamp`
- **Ship to Chris**: confirmation email with raw stats (N sessions, date range, agents used, folder count)
- **Chris's effort**: ~2 minutes

### Phase 1: Session analyzer (Days 3-5)
- [ ] New Python module: `session_analysis/` in skillmeter-analysis (or standalone)
- [ ] Parse CASS JSON → compute Tier 1 + Tier 2 metrics
- [ ] Generate static HTML report (fork the CC Insights pattern — Chris already likes that format)
- **Ship to Chris**: first HTML report with his usage patterns + prompting stats

### Phase 2: Ladder placement (Days 6-8)
- [ ] Implement Tier 3 + Tier 4 metrics
- [ ] Define ladder thresholds (start with hardcoded percentiles from Chris's data + our team's data)
- [ ] Add "Level Up Guide" section with concrete examples pulled from his best sessions
- **Ship to Chris**: full report with ladder level + personalized improvement suggestions

### Phase 3: Protodashboard (Days 9-12)
- [ ] Build in frontend-web (Next.js) or as standalone React page
- [ ] Interactive charts (weekly trends, metric breakdowns)
- [ ] Wire to backend API endpoint that serves session analysis results
- **Ship to Chris**: live dashboard URL he can bookmark and check

### Phase 4: Multi-user + comparison (Post code freeze, March 6+)
- [ ] Anonymized percentile comparisons ("you vs. community")
- [ ] Example prompts from higher-level users (with permission)
- [ ] Mentor matching prototype
- **Ship to Chris**: the full "CC Insights x10" experience

### Ongoing: Upload loop
- Chris runs `skillbench push` whenever he wants fresh insights
- Incremental upload (only new sessions since `last_upload_timestamp`)
- Each upload triggers insights email highlighting changes since last time
- Email content evolves as our analysis gets richer across phases

---

## Key Decisions Needed

### 1. Where does Chris's data live?

**Resolved by Chris's boot block design.** Data lives on his machine until he pushes it. The boot block tool auto-classifies projects by git visibility + license type, generates an editable allowlist, and he pushes when ready. Repeat uploads are incremental. No auto-sync needed — the email-on-upload loop incentivizes regular pushes.

Ask Chris to also install the skillmeter VS Code extension so we start collecting keystroke data in parallel — the two layers combine over time for richer analysis.

### 2. Static HTML vs. live dashboard?
**Recommendation**: Static HTML for Phase 1-2 (ship in days), migrate to frontend-web dashboard for Phase 3. Chris already responded well to the HTML format from CC Insights and Ducky's reports.

### 3. How do we get cross-user data for the ladder?
- Our own team's CASS exports (Matt, Homin, Ducky, EunYoung) → immediate N=5
- Gastown Discord beta → N=50-100 within weeks of launch
- **Recommendation**: Seed the ladder with our team data, openly label it as "early calibration" to Chris

### 4. What about the skillmeter extension?
The extension captures keystroke-level data that CASS doesn't. For the protodashboard, CASS session data is sufficient and more actionable. But we should ask Chris to install the extension too — over time the two data layers combine for richer analysis (e.g., "you delegated this task to AI but then manually rewrote 60% of the output → your prompts may need more specificity").

---

## Privacy & Consent

- CASS data contains full conversation transcripts including code snippets
- Chris is voluntarily sharing — get explicit written consent anyway
- For cross-user features: anonymize by default, opt-in for name visibility
- Mentor matching: strictly opt-in on both sides
- The Gastown Discord launch needs a clear data policy

---

## Success Criteria (Chris Delight Checklist)

- [ ] Chris says "this is better than /insights" for at least one dimension
- [ ] Chris can articulate his ladder level and what to work on next
- [ ] Chris checks the dashboard at least weekly without prompting
- [ ] Chris voluntarily shares it with someone on the Gastown Discord
- [ ] Chris gives us a testimonial we can use for launch

---

## Compatibility with Existing Skillmeter Pipeline

### What already exists
- **skillmeter extension**: VS Code extension capturing keystroke-level `text.change` events
- **Author classification**: 23 heuristic rules (timing/size patterns) → human/agent/system/undo/redo/clipboard
- **Core metric**: `ai_human_ratio` (agent chars vs. human chars per device)
- **User model**: GitHub OAuth → JWT, `tenants → users → devices` in PostgreSQL
- **Storage**: ClickHouse `skillmeter.otel_logs` (OTLP log format)
- **Analysis**: `skillmeter-analysis` Python module queries ClickHouse, computes metrics per device

### Integration points

| Concern | Session pipeline needs | Existing skillmeter has | Resolution |
|---------|----------------------|------------------------|------------|
| **User identity** | `skillbench push` must tie to a user | GitHub OAuth → `users` table | Reuse same GitHub OAuth flow. Same user record, new data type. |
| **Storage** | Conversations (prompts, responses, tool calls) | OTLP keystroke events | New ClickHouse table `skillmeter.sessions` — different schema, same cluster. |
| **Metric namespace** | `session.*` metrics (prompting sophistication, etc.) | `keystroke.*` metrics (ai_human_ratio) | Metric registry with explicit `layer` tag. Dashboard renders both, clearly separated. |
| **Privacy model** | Raw conversation text (user-selected, opt-in) | Client-side hashed, zero raw content | Separate consent flows. Session data is explicitly user-curated via boot block. Different data handling policy. |
| **Dashboard** | Session-level insights (ladder, level-up guide) | Keystroke-level charts (ai_human_ratio over time) | Same frontend-web app, separate tabs/sections. Cross-reference over time. |
| **Analysis code** | New session analysis module | `author_classification.py`, `ai_human_ratio.py` | New module in skillmeter-analysis: `session_analysis/`. Parallel to existing, not replacing. |

### What this does NOT change
- Existing skillmeter extension behavior: untouched
- Existing ClickHouse tables: untouched
- Existing metrics: untouched
- Existing dashboard views: untouched
- Auth flow: reused, not modified

### Future cross-layer analysis (post code freeze)
When a user has both keystroke AND session data, we can correlate:
- "You delegated this task but manually rewrote 60% of the output → your prompts may need more specificity"
- "Your ai_human_ratio jumped 20% this week AND your prompt specificity improved → the two are connected"

---

## Risk: What If CASS Data Isn't Rich Enough?

CASS gives us conversation transcripts but the schema normalizes somewhat aggressively. If the `content` field lacks tool call details or code diffs, we may need to:
1. Parse Claude Code's raw JSONL directly (skip CASS, go to `~/.claude/projects/`)
2. Build our own lighter extractor that preserves tool metadata
3. Ask Jeff (Dicklesworthstone) to expose richer fields

**Mitigation**: In Phase 0, do a data quality audit on Chris's export before committing to CASS as the ingestion layer. If the data is too lossy, pivot to direct JSONL parsing — the format is well-documented and simple.
