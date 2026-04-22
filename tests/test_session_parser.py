import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skillbench.session_parser import (  # noqa: E402
    _parse_codex_jsonl,
    parse_codex_sessions,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_codex_jsonl_reads_real_event_envelope():
    filepath = FIXTURES / "codex" / "basic_session.jsonl"

    conv = _parse_codex_jsonl(filepath)

    assert conv is not None
    assert conv.agent == "codex"
    assert conv.session_id == "test-session-id"
    assert conv.workspace == "/home/user/soldier-project"
    assert conv.started_at == 1759246956190
    assert conv.ended_at == 1759246980000
    assert conv.source_path == str(filepath)
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[0].content == [
        {
            "type": "text",
            "text": "Help me implement matrix completion with CMA-ES optimization",
        }
    ]
    assert conv.messages[1].content[0] == {
        "type": "text",
        "text": "I'll implement matrix completion using CMA-ES. Let me create the solver.",
    }
    assert conv.messages[1].content[1]["type"] == "tool_use"
    assert conv.messages[1].content[1]["name"] == "write_file"


def test_parse_codex_jsonl_preserves_tool_results_and_uses_session_meta(tmp_path):
    session_file = tmp_path / "session.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"codex-session","cwd":"/tmp/project"}}',
                '{"timestamp":"2025-10-01T10:00:00Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"text","text":"Running tests"},{"type":"tool_result","tool_use_id":"call_1","content":"ok"},{"type":"tool_use","id":"call_1","name":"run_tests","input":{"cmd":"pytest"}}]}}',
            ]
        )
        + "\n"
    )

    conv = _parse_codex_jsonl(session_file)

    assert conv is not None
    assert conv.session_id == "codex-session"
    assert conv.workspace == "/tmp/project"
    assert len(conv.messages) == 1
    assert conv.messages[0].role == "assistant"
    assert conv.messages[0].content == [
        {"type": "text", "text": "Running tests"},
        {"type": "tool_result", "tool_use_id": "call_1", "content": "ok", "is_error": False},
        {"type": "tool_use", "id": "call_1", "name": "run_tests", "input": {"cmd": "pytest"}},
    ]


def test_parse_codex_jsonl_attaches_reasoning_to_next_assistant_message(tmp_path):
    session_file = tmp_path / "thinking.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"codex-thinking","cwd":"/tmp/project"}}',
                '{"type":"event_msg","payload":{"type":"agent_reasoning","text":"Need to inspect the file first"}}',
                '{"timestamp":"2025-10-01T10:00:00Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"text","text":"I will open the file now."}]}}',
            ]
        )
        + "\n"
    )

    conv = _parse_codex_jsonl(session_file)

    assert conv is not None
    assert conv.messages[0].content == [
        {"type": "thinking", "thinking": "Need to inspect the file first"},
        {"type": "text", "text": "I will open the file now."},
    ]


def test_parse_codex_jsonl_maps_live_tool_events_and_git_remote(tmp_path):
    session_file = tmp_path / "real-shape.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:20.019Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-live",
                            "cwd": "/tmp/project",
                            "git": {
                                "repository_url": "git@github.com:skillbench-ai/live-codex.git"
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:21.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Running the command."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:22.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call_exec",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "pytest -q", "cwd": "/tmp/project"}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:23.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_exec",
                            "output": "fallback output",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:24.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "exec_command_end",
                            "call_id": "call_exec",
                            "command": ["bash", "-lc", "pytest -q"],
                            "cwd": "/tmp/project",
                            "exit_code": 0,
                            "aggregated_output": "2 passed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:25.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call_patch",
                            "name": "apply_patch",
                            "input": "*** Begin Patch\n*** Update File: /tmp/project/app.py\n@@\n-print('old')\n+print('new')\n*** End Patch\n",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:26.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "patch_apply_end",
                            "call_id": "call_patch",
                            "success": True,
                            "stdout": "Applied patch",
                            "stderr": "",
                            "changes": {"updated": ["/tmp/project/app.py"]},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    conv = _parse_codex_jsonl(session_file)

    assert conv is not None
    assert conv.session_id == "codex-live"
    assert conv.workspace == "/tmp/project"
    assert conv.git_remote == "git@github.com:skillbench-ai/live-codex.git"
    assert [m.role for m in conv.messages] == ["assistant", "assistant", "assistant"]
    assert conv.messages[0].content == [{"type": "text", "text": "Running the command."}]
    assert conv.messages[1].content == [
        {
            "type": "tool_use",
            "id": "call_exec",
            "name": "exec_command",
            "input": {"cmd": "pytest -q", "cwd": "/tmp/project"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_exec",
            "content": "2 passed",
            "is_error": False,
            "metadata": {
                "command": ["bash", "-lc", "pytest -q"],
                "cwd": "/tmp/project",
                "exit_code": 0,
            },
        },
    ]
    assert conv.messages[2].content == [
        {
            "type": "tool_use",
            "id": "call_patch",
            "name": "apply_patch",
            "input": {
                "patch": "*** Begin Patch\n*** Update File: /tmp/project/app.py\n@@\n-print('old')\n+print('new')\n*** End Patch\n",
                "path": "/tmp/project/app.py",
            },
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_patch",
            "content": "Applied patch",
            "is_error": False,
            "metadata": {
                "changes": {"updated": ["/tmp/project/app.py"]},
                "stderr": "",
                "success": True,
            },
        },
    ]


def test_parse_codex_sessions_scans_recursive_codex_directories(tmp_path):
    sessions_dir = tmp_path / ".codex" / "sessions" / "2025" / "10"
    sessions_dir.mkdir(parents=True)
    fixture_file = FIXTURES / "codex" / "basic_session.jsonl"
    target_file = sessions_dir / "captured.jsonl"
    target_file.write_text(fixture_file.read_text())

    conversations = list(parse_codex_sessions(tmp_path / ".codex"))

    assert len(conversations) == 1
    assert conversations[0].session_id == "test-session-id"
    assert conversations[0].workspace == "/home/user/soldier-project"
