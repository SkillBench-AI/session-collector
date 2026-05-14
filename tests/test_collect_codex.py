import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import skillbench  # noqa: E402


def test_cmd_collect_exports_structured_codex_session(tmp_path):
    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex" / "sessions"
    codex_dir.mkdir(parents=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    session_file = codex_dir / "structured.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2025-09-30T15:42:34.559Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "collect-codex-session",
                            "cwd": str(workspace),
                            "cli_version": "0.42.0",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2025-09-30T15:42:36.190Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Build a smoke implementation.",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2025-09-30T15:42:50.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_reasoning",
                            "text": "Need to write the file first.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2025-09-30T15:43:00.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "I'll add the file and run a quick check.",
                                },
                                {
                                    "type": "tool_use",
                                    "id": "call_write01",
                                    "name": "write_file",
                                    "input": {
                                        "path": str(workspace / "smoke.py"),
                                        "content": "print('smoke')",
                                    },
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call_write01",
                                    "content": "File written successfully",
                                },
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2025-09-30T15:43:05.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Great, thanks."}],
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    output_path = tmp_path / "codex-export.json"
    dist_dir = tmp_path / "dist"
    bootblock = dist_dir / "bootblock.txt"

    with patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False), patch.object(
        skillbench,
        "git_remote_url",
        return_value="git@github.com:skillbench-ai/codex-smoke.git",
    ), patch.object(
        skillbench,
        "classify_github_repo",
        return_value={"is_public": False, "license_key": None, "license_name": None},
    ), patch.object(
        skillbench,
        "is_skippable",
        return_value=False,
    ), patch.object(
        skillbench,
        "DIST_DIR",
        dist_dir,
    ), patch.object(
        skillbench,
        "BOOTBLOCK_FILE",
        bootblock,
    ):
        args = Namespace(
            output=str(output_path),
            yes=True,
            include_excluded=True,
            allowed_orgs=["skillbench-ai"],
            split="none",
            write_report=False,
            upload_guide=False,
        )
        skillbench.cmd_collect(args)

    export = json.loads(output_path.read_text())
    assert len(export) == 1

    session = export[0]
    assert session["session_id"] == "collect-codex-session"
    assert session["agent"] == "codex"
    assert session["full_fidelity"] is True
    assert session["workspace"] == str(workspace)
    assert session["git_remote"] == "git@github.com:skillbench-ai/codex-smoke.git"
    assert session["source_path"] == "~/.codex/sessions/structured.jsonl"

    assert [msg["role"] for msg in session["messages"]] == ["user", "assistant", "user"]
    assert session["messages"][0]["content"] == [
        {"type": "text", "text": "Build a smoke implementation."}
    ]
    assert session["messages"][1]["content"] == [
        {"type": "thinking", "thinking": "Need to write the file first."},
        {"type": "text", "text": "I'll add the file and run a quick check."},
        {
            "type": "tool_use",
            "id": "call_write01",
            "name": "write_file",
            "input": {
                "path": str(workspace / "smoke.py"),
                "content": "print('smoke')",
            },
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_write01",
            "content": "File written successfully",
            "is_error": False,
        },
    ]
    assert session["messages"][2]["content"] == [
        {"type": "text", "text": "Great, thanks."}
    ]


def test_cmd_collect_exports_live_codex_tool_events_without_git_repo(tmp_path):
    fake_home = tmp_path / "home"
    codex_dir = fake_home / ".codex" / "sessions"
    codex_dir.mkdir(parents=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    session_file = codex_dir / "live.jsonl"
    session_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:20.019Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "collect-live-codex",
                            "cwd": str(workspace),
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
                            "content": [{"type": "output_text", "text": "Running checks first."}],
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
                            "arguments": json.dumps({"cmd": "pytest -q", "cwd": str(workspace)}),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:23.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "exec_command_end",
                            "call_id": "call_exec",
                            "command": ["bash", "-lc", "pytest -q"],
                            "cwd": str(workspace),
                            "exit_code": 0,
                            "aggregated_output": "3 passed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:24.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call_patch",
                            "name": "apply_patch",
                            "input": (
                                "*** Begin Patch\n"
                                f"*** Update File: {workspace / 'app.py'}\n"
                                "@@\n"
                                "-print('old')\n"
                                "+print('new')\n"
                                "*** End Patch\n"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-21T13:33:25.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "patch_apply_end",
                            "call_id": "call_patch",
                            "success": True,
                            "stdout": "Applied patch",
                            "stderr": "",
                            "changes": {"updated": [str(workspace / "app.py")]},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    output_path = tmp_path / "codex-live-export.json"
    dist_dir = tmp_path / "dist"
    bootblock = dist_dir / "bootblock.txt"

    with patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False), patch.object(
        skillbench,
        "git_remote_url",
        return_value=None,
    ), patch.object(
        skillbench,
        "classify_github_repo",
        return_value={"is_public": False, "license_key": None, "license_name": None},
    ), patch.object(
        skillbench,
        "is_skippable",
        return_value=False,
    ), patch.object(
        skillbench,
        "DIST_DIR",
        dist_dir,
    ), patch.object(
        skillbench,
        "BOOTBLOCK_FILE",
        bootblock,
    ):
        args = Namespace(
            output=str(output_path),
            yes=True,
            include_excluded=True,
            allowed_orgs=["skillbench-ai"],
            split="none",
            write_report=False,
            upload_guide=False,
        )
        skillbench.cmd_collect(args)

    export = json.loads(output_path.read_text())
    assert len(export) == 1

    session = export[0]
    assert session["session_id"] == "collect-live-codex"
    assert session["full_fidelity"] is True
    assert session["git_remote"] == "git@github.com:skillbench-ai/live-codex.git"
    assert [msg["role"] for msg in session["messages"]] == ["assistant", "assistant", "assistant"]
    assert session["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "call_exec",
        "name": "exec_command",
        "input": {"cmd": "pytest -q", "cwd": str(workspace)},
    }
    assert session["messages"][1]["content"][1]["type"] == "tool_result"
    assert session["messages"][1]["content"][1]["tool_use_id"] == "call_exec"
    assert session["messages"][1]["content"][1]["content"] == "3 passed"
    assert session["messages"][2]["content"][0]["name"] == "apply_patch"
    assert session["messages"][2]["content"][0]["input"]["path"] == str(workspace / "app.py")
