"""End-to-end gather tests — CASS DB build/load, --full vs flat, unified schema.

Builds a mock CASS agent_search.db with 4 agents (claude, codex, pi_agent,
gemini), runs gather with and without --full, then compares the two exports
to verify full-fidelity output, role unification, and tool block preservation.
"""

import json
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import skillbench

FIXTURES = Path(__file__).parent / "fixtures"

# All 4 agents, each with a fixture that contains tool_use blocks
AGENTS = {
    "claude":   "claude/basic_session.jsonl",
    "codex":    "codex/basic_session.jsonl",
    "pi_agent": "pi_agent/basic_session.jsonl",
    "gemini":   "gemini/v031_chat_session.json",
}


# ---------------------------------------------------------------------------
# CASS DB builder
# ---------------------------------------------------------------------------

def _build_cass_db(db_path, conversations, workspace_path):
    """Build a minimal CASS agent_search.db matching the real schema."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '13')")

    conn.execute("""CREATE TABLE sources (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, host_label TEXT,
        machine_id TEXT, platform TEXT, config_json TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
    conn.execute("INSERT INTO sources VALUES ('local','local',NULL,NULL,NULL,NULL,1700000000,1700000000)")

    conn.execute("""CREATE TABLE agents (
        id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        version TEXT, kind TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")

    conn.execute("""CREATE TABLE workspaces (
        id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, display_name TEXT)""")
    conn.execute("INSERT INTO workspaces (id, path) VALUES (1, ?)", (workspace_path,))

    conn.execute("""CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        agent_id INTEGER NOT NULL REFERENCES agents(id),
        workspace_id INTEGER REFERENCES workspaces(id),
        external_id TEXT, title TEXT, source_path TEXT NOT NULL,
        started_at INTEGER, ended_at INTEGER, approx_tokens INTEGER,
        metadata_json TEXT,
        source_id TEXT NOT NULL DEFAULT 'local' REFERENCES sources(id),
        origin_host TEXT, metadata_bin BLOB,
        total_input_tokens INTEGER, total_output_tokens INTEGER,
        total_cache_read_tokens INTEGER, total_cache_creation_tokens INTEGER,
        grand_total_tokens INTEGER, estimated_cost_usd REAL,
        primary_model TEXT, api_call_count INTEGER, tool_call_count INTEGER,
        user_message_count INTEGER, assistant_message_count INTEGER,
        UNIQUE(source_id, agent_id, external_id))""")

    conn.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        idx INTEGER NOT NULL, role TEXT NOT NULL, author TEXT,
        created_at INTEGER, content TEXT NOT NULL,
        extra_json TEXT, extra_bin BLOB,
        UNIQUE(conversation_id, idx))""")

    agent_ids = {}
    agent_n = 0
    conv_id = 0
    for conv in conversations:
        slug = conv["agent_slug"]
        if slug not in agent_ids:
            agent_n += 1
            conn.execute(
                "INSERT INTO agents (id,slug,name,kind,created_at,updated_at) "
                "VALUES (?,?,?,'cli',1700000000,1700000000)",
                (agent_n, slug, slug.replace("_", " ").title()))
            agent_ids[slug] = agent_n

        conv_id += 1
        conn.execute(
            "INSERT INTO conversations "
            "(id,agent_id,workspace_id,external_id,title,source_path,"
            " started_at,ended_at,approx_tokens) "
            "VALUES (?,?,1,?,?,?,1700000000000,1700003600000,5000)",
            (conv_id, agent_ids[slug], conv["external_id"],
             conv["title"], conv["source_path"]))

        # CASS flat messages (fallback when --full is off or parsing fails)
        conn.execute(
            "INSERT INTO messages (conversation_id,idx,role,created_at,content) "
            "VALUES (?,0,'user',1700000000000,?)",
            (conv_id, f"[CASS flat] {slug} user msg"))
        conn.execute(
            "INSERT INTO messages (conversation_id,idx,role,created_at,content) "
            "VALUES (?,1,'agent',1700000001000,?)",
            (conv_id, f"[CASS flat] {slug} agent msg"))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path):
    """CASS DB + bootblock + output paths for 4 agents."""
    workspace = str(tmp_path / "projects" / "repo")
    Path(workspace).mkdir(parents=True)

    conversations = []
    for slug, rel in AGENTS.items():
        conversations.append({
            "agent_slug": slug,
            "external_id": f"e2e-{slug}",
            "source_path": str(FIXTURES / rel),
            "title": f"Test {slug}",
        })

    db = tmp_path / "agent_search.db"
    _build_cass_db(db, conversations, workspace)

    bootblock = tmp_path / "bootblock.txt"
    bootblock.write_text(f"{workspace}\n")

    return {
        "db": db,
        "bootblock": bootblock,
        "workspace": workspace,
        "tmp": tmp_path,
    }


def _gather(env, full: bool) -> list[dict]:
    """Run cmd_gather, return parsed export list."""
    out = env["tmp"] / f"out_{'full' if full else 'flat'}.json"
    args = Namespace(bootblock=str(env["bootblock"]), output=str(out), full=full)
    with patch.object(skillbench, "find_cass_db", return_value=env["db"]), \
         patch.object(skillbench, "git_remote_url", return_value=None):
        skillbench.cmd_gather(args)
    return json.loads(out.read_text())


# ---------------------------------------------------------------------------
# Tests — CASS DB load
# ---------------------------------------------------------------------------

class TestCASSLoad:
    """Verify the mock CASS DB is built and loaded correctly."""

    def test_db_has_4_agents(self, env):
        conn = sqlite3.connect(str(env["db"]))
        slugs = {r[0] for r in conn.execute("SELECT slug FROM agents").fetchall()}
        conn.close()
        assert slugs == set(AGENTS.keys())

    def test_db_has_4_conversations(self, env):
        conn = sqlite3.connect(str(env["db"]))
        n = conn.execute("SELECT count(*) FROM conversations").fetchone()[0]
        conn.close()
        assert n == 4

    def test_db_has_flat_messages(self, env):
        conn = sqlite3.connect(str(env["db"]))
        n = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        conn.close()
        # 2 flat messages per conversation × 4
        assert n == 8

    def test_gather_flat_loads_all_4(self, env):
        export = _gather(env, full=False)
        assert len(export) == 4
        assert {c["agent"] for c in export} == set(AGENTS.keys())

    def test_gather_full_loads_all_4(self, env):
        export = _gather(env, full=True)
        assert len(export) == 4
        assert {c["agent"] for c in export} == set(AGENTS.keys())


# ---------------------------------------------------------------------------
# Tests — --full vs flat comparison
# ---------------------------------------------------------------------------

class TestFullVsFlat:
    """Run gather with and without --full on the same DB, compare outputs."""

    def test_flat_never_full_fidelity(self, env):
        for conv in _gather(env, full=False):
            assert conv["full_fidelity"] is False

    def test_full_always_full_fidelity(self, env):
        for conv in _gather(env, full=True):
            assert conv["full_fidelity"] is True, (
                f"{conv['agent']} fell back to flat")

    def test_flat_has_cass_marker(self, env):
        """Without --full, messages come from CASS flat content."""
        for conv in _gather(env, full=False):
            texts = [m["content"] for m in conv["messages"] if isinstance(m["content"], str)]
            assert any("[CASS flat]" in t for t in texts), (
                f"{conv['agent']} missing CASS flat marker")

    def test_full_has_no_cass_marker(self, env):
        """With --full, raw parser is used — no CASS flat leakage."""
        for conv in _gather(env, full=True):
            for msg in conv["messages"]:
                if isinstance(msg["content"], str):
                    assert "[CASS flat]" not in msg["content"], (
                        f"{conv['agent']} leaked CASS flat content in full mode")

    def test_full_has_more_messages_than_flat(self, env):
        """Raw parsing yields richer messages than the 2 CASS stubs."""
        flat = {c["agent"]: len(c["messages"]) for c in _gather(env, full=False)}
        full = {c["agent"]: len(c["messages"]) for c in _gather(env, full=True)}
        for slug in AGENTS:
            assert full[slug] >= flat[slug], (
                f"{slug}: full={full[slug]} < flat={flat[slug]}")

    def test_full_has_structured_blocks(self, env):
        """At least one message per agent has list content (structured blocks)."""
        for conv in _gather(env, full=True):
            has_list = any(isinstance(m["content"], list) for m in conv["messages"])
            assert has_list, (
                f"{conv['agent']} has no structured block content in full mode")

    def test_flat_has_only_string_content(self, env):
        """CASS flat messages are always plain strings."""
        for conv in _gather(env, full=False):
            for msg in conv["messages"]:
                assert isinstance(msg["content"], str), (
                    f"{conv['agent']} flat msg has non-string content")


# ---------------------------------------------------------------------------
# Tests — unified schema (role + created_at + content)
# ---------------------------------------------------------------------------

class TestUnifiedSchema:
    """Verify all parsers produce {role, created_at, content} with correct values."""

    def test_full_keys_are_role_created_at_content(self, env):
        for conv in _gather(env, full=True):
            for msg in conv["messages"]:
                assert set(msg.keys()) == {"role", "created_at", "content"}, (
                    f"{conv['agent']}: unexpected keys {set(msg.keys())}")

    def test_flat_keys_are_role_created_at_content(self, env):
        for conv in _gather(env, full=False):
            for msg in conv["messages"]:
                assert set(msg.keys()) == {"role", "created_at", "content"}, (
                    f"{conv['agent']}: unexpected keys {set(msg.keys())}")

    def test_full_roles_are_user_or_agent(self, env):
        for conv in _gather(env, full=True):
            for msg in conv["messages"]:
                assert msg["role"] in ("user", "agent"), (
                    f"{conv['agent']}: bad role '{msg['role']}'")

    def test_flat_roles_are_user_or_agent(self, env):
        for conv in _gather(env, full=False):
            for msg in conv["messages"]:
                assert msg["role"] in ("user", "agent"), (
                    f"{conv['agent']}: bad role '{msg['role']}'")

    def test_every_agent_has_user_role(self, env):
        for conv in _gather(env, full=True):
            roles = {m["role"] for m in conv["messages"]}
            assert "user" in roles, f"{conv['agent']}: no user messages"

    def test_every_agent_has_agent_role(self, env):
        for conv in _gather(env, full=True):
            roles = {m["role"] for m in conv["messages"]}
            assert "agent" in roles, f"{conv['agent']}: no agent messages"

    def test_no_assistant_role_anywhere(self, env):
        """The old 'assistant' role must not appear."""
        for conv in _gather(env, full=True):
            for msg in conv["messages"]:
                assert msg["role"] != "assistant", (
                    f"{conv['agent']}: found deprecated 'assistant' role")

    def test_no_timestamp_key_anywhere(self, env):
        """The old 'timestamp' key must not appear (replaced by created_at)."""
        for conv in _gather(env, full=True):
            for msg in conv["messages"]:
                assert "timestamp" not in msg, (
                    f"{conv['agent']}: found deprecated 'timestamp' key")

    def test_created_at_is_string(self, env):
        for conv in _gather(env, full=True):
            for msg in conv["messages"]:
                assert isinstance(msg["created_at"], str), (
                    f"{conv['agent']}: created_at not a string")


# ---------------------------------------------------------------------------
# Tests — tool blocks in full mode
# ---------------------------------------------------------------------------

class TestToolBlocks:
    """Verify tool_use and tool_result blocks are preserved in full mode."""

    def _get(self, export, slug):
        return next(c for c in export if c["agent"] == slug)

    def test_claude_has_tool_use(self, env):
        conv = self._get(_gather(env, full=True), "claude")
        blocks = self._all_blocks(conv)
        assert any(b.get("type") == "tool_use" for b in blocks)

    def test_codex_has_tool_use(self, env):
        conv = self._get(_gather(env, full=True), "codex")
        blocks = self._all_blocks(conv)
        assert any(b.get("type") == "tool_use" for b in blocks)

    def test_pi_agent_has_tool_use(self, env):
        conv = self._get(_gather(env, full=True), "pi_agent")
        blocks = self._all_blocks(conv)
        assert any(b.get("type") == "tool_use" for b in blocks)

    def test_gemini_has_tool_use(self, env):
        conv = self._get(_gather(env, full=True), "gemini")
        blocks = self._all_blocks(conv)
        assert any(b.get("type") == "tool_use" for b in blocks)

    def test_tool_use_has_required_fields(self, env):
        """Every tool_use block has id, name, input."""
        for conv in _gather(env, full=True):
            for b in self._all_blocks(conv):
                if b.get("type") == "tool_use":
                    for key in ("id", "name", "input"):
                        assert key in b, (
                            f"{conv['agent']}: tool_use missing '{key}'")

    def test_tool_result_has_required_fields(self, env):
        """Every tool_result block has tool_use_id, content, is_error."""
        for conv in _gather(env, full=True):
            for b in self._all_blocks(conv):
                if b.get("type") == "tool_result":
                    for key in ("tool_use_id", "content", "is_error"):
                        assert key in b, (
                            f"{conv['agent']}: tool_result missing '{key}'")
                    assert isinstance(b["is_error"], bool)

    def test_thinking_blocks_valid(self, env):
        """Every thinking block has a 'thinking' string."""
        for conv in _gather(env, full=True):
            for b in self._all_blocks(conv):
                if b.get("type") == "thinking":
                    assert "thinking" in b and isinstance(b["thinking"], str), (
                        f"{conv['agent']}: bad thinking block")

    @staticmethod
    def _all_blocks(conv):
        """Collect all dict blocks from structured content across messages."""
        blocks = []
        for msg in conv["messages"]:
            if isinstance(msg["content"], list):
                blocks.extend(b for b in msg["content"] if isinstance(b, dict))
        return blocks


# ---------------------------------------------------------------------------
# Tests — export structure
# ---------------------------------------------------------------------------

class TestExportStructure:

    def test_required_top_level_keys(self, env):
        required = {
            "session_id", "agent", "workspace", "git_remote", "source_path",
            "title", "started_at", "ended_at", "approx_tokens",
            "messages", "full_fidelity",
        }
        for conv in _gather(env, full=True):
            missing = required - set(conv.keys())
            assert not missing, f"{conv['agent']} missing keys: {missing}"

    def test_session_ids_match(self, env):
        ids = {c["session_id"] for c in _gather(env, full=True)}
        for slug in AGENTS:
            assert f"e2e-{slug}" in ids

    def test_workspace_paths_match(self, env):
        for conv in _gather(env, full=True):
            assert conv["workspace"] == env["workspace"]

    def test_fallback_on_missing_source(self, env):
        """Unknown agent falls back to CASS flat (full_fidelity=False)."""
        workspace = str(env["tmp"] / "ws2")
        Path(workspace).mkdir()
        db2 = env["tmp"] / "db2.db"
        _build_cass_db(db2, [{
            "agent_slug": "copilot",
            "external_id": "copilot-001",
            "source_path": str(FIXTURES / "claude" / "basic_session.jsonl"),
            "title": "Copilot test",
        }], workspace)
        bb2 = env["tmp"] / "bb2.txt"
        bb2.write_text(f"{workspace}\n")
        out2 = env["tmp"] / "out_fallback.json"
        args = Namespace(bootblock=str(bb2), output=str(out2), full=True)
        with patch.object(skillbench, "find_cass_db", return_value=db2), \
             patch.object(skillbench, "git_remote_url", return_value=None):
            skillbench.cmd_gather(args)
        export = json.loads(out2.read_text())
        assert len(export) == 1
        assert export[0]["full_fidelity"] is False
        assert export[0]["agent"] == "copilot"
