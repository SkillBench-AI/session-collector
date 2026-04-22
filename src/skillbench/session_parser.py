#!/usr/bin/env python3
from __future__ import annotations
"""Lightweight multi-agent session parser — replaces CASS dependency.

Reads session data directly from local agent log files:
  - Claude Code:  ~/.claude/projects/*/session-*.jsonl
  - Gemini CLI:   ~/.gemini/tmp/{sha256(path)}/chats/*.json
  - Codex CLI:    ~/.codex/sessions/*.jsonl

Normalizes into a common schema compatible with the existing skillbench.py
metrics pipeline. No Rust, no CASS, no cargo install — pure Python.
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Common schema
# ---------------------------------------------------------------------------

class Message:
    """A single message in a conversation."""
    __slots__ = ("role", "content", "timestamp")

    def __init__(self, role: str, content, timestamp: float | None = None):
        self.role = role          # "user", "assistant", "tool"
        self.content = content    # text or structured content blocks
        self.timestamp = timestamp  # epoch milliseconds (or None)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "created_at": (
                datetime.fromtimestamp(self.timestamp / 1000, tz=timezone.utc).isoformat()
                if self.timestamp else None
            ),
        }


class Conversation:
    """A single coding agent session/conversation."""
    __slots__ = (
        "session_id", "agent", "workspace", "git_remote",
        "started_at", "ended_at", "messages", "source_path",
    )

    def __init__(
        self,
        session_id: str,
        agent: str,
        workspace: str,
        git_remote: str | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        messages: list[Message] | None = None,
        source_path: str | None = None,
    ):
        self.session_id = session_id
        self.agent = agent            # "claude_code", "gemini", "codex", etc.
        self.workspace = workspace    # absolute path to project root
        self.git_remote = git_remote
        self.started_at = started_at  # epoch milliseconds
        self.ended_at = ended_at      # epoch milliseconds
        self.messages = messages or []
        self.source_path = source_path

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "workspace": self.workspace,
            "git_remote": self.git_remote,
            "source_path": self.source_path,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "messages": [m.to_dict() for m in self.messages],
        }


# ---------------------------------------------------------------------------
# Claude Code parser
# ---------------------------------------------------------------------------

def _decode_claude_project_dir(dirname: str) -> str:
    """Convert Claude Code's directory name back to a filesystem path.

    Claude Code encodes project paths by replacing '/' with '-' and
    storing sessions under ~/.claude/projects/{encoded}/.
    e.g. "-Users-mattbeane-knowledge-work" → "/Users/mattbeane/knowledge-work"
    """
    if not dirname.startswith("-"):
        return dirname
    # Replace leading dash with '/', then remaining dashes with '/'
    # But this is lossy — dashes in actual directory names become ambiguous.
    # We'll handle this by checking if the decoded path exists on disk.
    decoded = dirname.replace("-", "/")
    if Path(decoded).exists():
        return decoded
    # Try common variations: the directory name might have legitimate dashes
    # For now, return the basic decode — it works for most cases
    return decoded


def parse_claude_code_sessions(
    base_dir: Path | None = None,
) -> Iterator[Conversation]:
    """Parse Claude Code session files from ~/.claude/projects/.

    Each .jsonl file is one session. Lines are JSON objects with types:
    - "user": user messages (role=user in message)
    - "assistant": assistant responses (role=assistant in message)
    - "file-history-snapshot": file state snapshots (ignored)

    Yields Conversation objects.
    """
    if base_dir is None:
        base_dir = Path.home() / ".claude" / "projects"

    if not base_dir.exists():
        return

    for project_dir in base_dir.iterdir():
        if not project_dir.is_dir():
            continue

        workspace = _decode_claude_project_dir(project_dir.name)

        for session_file in project_dir.glob("*.jsonl"):
            # Skip agent-prefixed files (subagent sessions)
            if session_file.name.startswith("agent-"):
                continue

            try:
                conv = _parse_single_claude_session(session_file, workspace)
                if conv and conv.messages:
                    yield conv
            except Exception:
                # Skip malformed files silently
                continue


def _parse_single_claude_session(
    filepath: Path, workspace: str,
) -> Conversation | None:
    """Parse a single Claude Code JSONL session file."""
    messages = []
    session_id = filepath.stem  # UUID from filename
    timestamps = []
    cwd = None

    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            if entry_type not in ("user", "assistant"):
                continue

            # Skip meta messages
            if entry.get("isMeta"):
                continue

            msg_data = entry.get("message", {})
            role = msg_data.get("role")
            if not role:
                continue

            # Extract text content
            content = _extract_claude_content(msg_data.get("content"))
            if not content:
                continue

            # Timestamp (ISO-8601 → epoch ms)
            ts_str = entry.get("timestamp")
            ts_ms = None
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts_ms = int(dt.timestamp() * 1000)
                    timestamps.append(ts_ms)
                except (ValueError, TypeError):
                    pass

            # Track cwd for workspace resolution
            if not cwd and entry.get("cwd"):
                cwd = entry["cwd"]

            messages.append(Message(role=role, content=content, timestamp=ts_ms))

    if not messages:
        return None

    # Use cwd from messages if available, falling back to decoded dir name
    effective_workspace = cwd or workspace

    return Conversation(
        session_id=session_id,
        agent="claude_code",
        workspace=effective_workspace,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        messages=messages,
        source_path=str(filepath),
    )


def _extract_claude_content(content) -> str:
    """Extract text content from Claude Code message content field.

    Content can be:
    - A string (simple text)
    - A list of content blocks (text, tool_use, tool_result, thinking)
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    # Include tool results as context
                    result_content = block.get("content", "")
                    if isinstance(result_content, str) and result_content:
                        parts.append(f"[tool_result: {result_content[:500]}]")
                # Skip thinking blocks and tool_use blocks (they're assistant internals)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)

    return ""


# ---------------------------------------------------------------------------
# Gemini CLI parser
# ---------------------------------------------------------------------------

def gemini_hash_for_path(project_path: str) -> str:
    """Compute the Gemini CLI storage hash for a project path.

    Matches Gemini CLI's getProjectHash():
      crypto.createHash('sha256').update(projectRoot).digest('hex')
    """
    return hashlib.sha256(project_path.encode()).hexdigest()


def parse_gemini_sessions(
    base_dir: Path | None = None,
) -> Iterator[Conversation]:
    """Parse Gemini CLI session files from ~/.gemini/tmp/{hash}/chats/.

    Gemini stores sessions in hash-based directories where the hash
    is SHA-256 of the absolute project path.

    Yields Conversation objects.
    """
    if base_dir is None:
        base_dir = Path.home() / ".gemini" / "tmp"

    if not base_dir.exists():
        return

    for hash_dir in base_dir.iterdir():
        if not hash_dir.is_dir():
            continue

        chats_dir = hash_dir / "chats"
        if not chats_dir.exists():
            continue

        # We can't reverse the hash to get the workspace path directly.
        # Store the hash path as workspace — will be resolved later.
        workspace = str(hash_dir)

        for chat_file in chats_dir.glob("*.json"):
            try:
                conv = _parse_single_gemini_session(chat_file, workspace)
                if conv and conv.messages:
                    yield conv
            except Exception:
                continue


def _parse_single_gemini_session(
    filepath: Path, workspace: str,
) -> Conversation | None:
    """Parse a single Gemini CLI chat JSON file."""
    with open(filepath, "r", errors="replace") as f:
        data = json.load(f)

    # Gemini chat files can have various structures
    # Common: list of messages or object with messages array
    if isinstance(data, list):
        raw_messages = data
    elif isinstance(data, dict):
        raw_messages = data.get("messages", data.get("history", []))
    else:
        return None

    messages = []
    timestamps = []

    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "").lower()
        if role not in ("user", "model", "assistant"):
            continue

        # Normalize role
        if role == "model":
            role = "assistant"

        content = ""
        parts = msg.get("parts", [])
        if isinstance(parts, list):
            text_parts = []
            for part in parts:
                if isinstance(part, dict) and "text" in part:
                    text_parts.append(part["text"])
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "\n".join(text_parts)
        elif isinstance(msg.get("content"), str):
            content = msg["content"]

        if not content:
            continue

        # Try to extract timestamp
        ts_ms = None
        ts_val = msg.get("timestamp") or msg.get("create_time")
        if ts_val:
            if isinstance(ts_val, (int, float)):
                ts_ms = int(ts_val * 1000) if ts_val < 1e12 else int(ts_val)
                timestamps.append(ts_ms)
            elif isinstance(ts_val, str):
                try:
                    dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                    ts_ms = int(dt.timestamp() * 1000)
                    timestamps.append(ts_ms)
                except (ValueError, TypeError):
                    pass

        messages.append(Message(role=role, content=content, timestamp=ts_ms))

    if not messages:
        return None

    return Conversation(
        session_id=filepath.stem,
        agent="gemini",
        workspace=workspace,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        messages=messages,
        source_path=str(filepath),
    )


# ---------------------------------------------------------------------------
# Codex CLI parser (OpenAI Codex / ChatGPT CLI)
# ---------------------------------------------------------------------------

def _parse_timestamp_ms(value) -> int | None:
    """Normalize various timestamp formats to epoch milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000) if value < 1e12 else int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return None
    return None


def _normalize_codex_content_blocks(content, *, thinking: str | None = None) -> list[dict]:
    """Normalize Codex content into Claude-style structured blocks."""
    blocks: list[dict] = []

    if thinking:
        blocks.append({"type": "thinking", "thinking": thinking})

    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
        return blocks

    if not isinstance(content, list):
        return blocks

    for block in content:
        if isinstance(block, str):
            if block:
                blocks.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue

        block_type = block.get("type", "")
        if block_type in ("input_text", "text", "output_text"):
            blocks.append({"type": "text", "text": block.get("text", "")})
        elif block_type == "tool_use":
            blocks.append({
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
        elif block_type == "tool_result":
            blocks.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": block.get("content", ""),
                "is_error": block.get("is_error", False),
            })
        else:
            blocks.append(block)

    return blocks


def _extract_apply_patch_path(patch_text: str) -> str | None:
    """Best-effort path extraction from an apply_patch payload."""
    if not patch_text:
        return None
    match = re.search(r"^\*\*\* (?:Add|Update) File: (.+)$", patch_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _normalize_codex_tool_input(name: str, raw_input):
    """Normalize tool-call inputs from live Codex event records."""
    if isinstance(raw_input, dict):
        return raw_input

    if isinstance(raw_input, str):
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        if name == "apply_patch":
            path = _extract_apply_patch_path(raw_input)
            result = {"patch": raw_input}
            if path:
                result["path"] = path
            return result

        return {"input": raw_input}

    return {}


def _build_codex_tool_result(content, *, is_error: bool = False, metadata: dict | None = None) -> dict:
    """Build a normalized tool_result block."""
    block = {
        "type": "tool_result",
        "content": content,
        "is_error": is_error,
    }
    if metadata:
        block["metadata"] = metadata
    return block

def parse_codex_sessions(
    base_dir: Path | None = None,
) -> Iterator[Conversation]:
    """Parse Codex CLI session files.

    Codex CLI stores sessions as JSONL in ~/.codex/ or similar. The primary
    on-disk format is a stream of envelope events such as ``session_meta``,
    ``response_item``, and ``event_msg``.

    Yields Conversation objects.
    """
    # Check common locations
    candidates = [
        Path.home() / ".codex",
        Path.home() / ".codex-cli",
        Path.home() / ".openai-codex",
    ]
    if base_dir:
        candidates = [base_dir]

    for candidate in candidates:
        if not candidate.exists():
            continue

        # Walk the directory looking for session files
        for filepath in candidate.rglob("*.jsonl"):
            try:
                conv = _parse_codex_jsonl(filepath)
                if conv and conv.messages:
                    yield conv
            except Exception:
                continue

        for filepath in candidate.rglob("*.json"):
            try:
                conv = _parse_codex_json(filepath)
                if conv and conv.messages:
                    yield conv
            except Exception:
                continue


def _parse_codex_jsonl(filepath: Path) -> Conversation | None:
    """Parse a Codex JSONL session file."""
    messages = []
    timestamps = []
    session_id = filepath.stem
    workspace = None
    git_remote = None
    pending_thinking = None
    pending_calls: dict[str, dict] = {}

    with open(filepath, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            evt_type = entry.get("type")
            if evt_type == "session_meta":
                payload = entry.get("payload", {})
                meta_session_id = payload.get("id")
                meta_workspace = payload.get("cwd")
                meta_git = payload.get("git", {})
                if meta_session_id:
                    session_id = str(meta_session_id)
                if meta_workspace:
                    workspace = str(meta_workspace)
                if isinstance(meta_git, dict) and meta_git.get("repository_url"):
                    git_remote = str(meta_git["repository_url"])
                continue

            if evt_type == "event_msg":
                payload = entry.get("payload", {})
                payload_type = payload.get("type")
                if payload_type == "agent_reasoning":
                    pending_thinking = payload.get("text", "")
                    continue

                call_id = payload.get("call_id")
                pending = pending_calls.get(call_id) if call_id else None
                if pending is None:
                    continue

                if payload_type == "exec_command_end":
                    result_block = _build_codex_tool_result(
                        payload.get("aggregated_output")
                        or payload.get("formatted_output")
                        or payload.get("stderr")
                        or payload.get("stdout")
                        or "",
                        is_error=(payload.get("exit_code", 0) != 0),
                        metadata={
                            "command": payload.get("command"),
                            "cwd": payload.get("cwd"),
                            "exit_code": payload.get("exit_code"),
                        },
                    )
                    pending["result"] = result_block
                elif payload_type == "patch_apply_end":
                    result_block = _build_codex_tool_result(
                        payload.get("stdout") or payload.get("stderr") or "",
                        is_error=not payload.get("success", False),
                        metadata={
                            "changes": payload.get("changes"),
                            "stderr": payload.get("stderr"),
                            "success": payload.get("success"),
                        },
                    )
                    pending["result"] = result_block
                continue

            if evt_type == "response_item":
                payload = entry.get("payload", {})
                payload_type = payload.get("type")

                if payload_type == "function_call":
                    call_id = payload.get("call_id", "")
                    tool_name = payload.get("name", "")
                    tool_input = _normalize_codex_tool_input(
                        tool_name,
                        payload.get("arguments", {}),
                    )
                    block = {
                        "type": "tool_use",
                        "id": call_id,
                        "name": tool_name,
                        "input": tool_input,
                    }
                    ts_ms = _parse_timestamp_ms(entry.get("timestamp"))
                    if ts_ms is not None:
                        timestamps.append(ts_ms)
                    msg = Message(role="assistant", content=[block], timestamp=ts_ms)
                    messages.append(msg)
                    if call_id:
                        pending_calls[call_id] = {"message": msg, "result": None}
                    continue

                if payload.get("type") == "custom_tool_call":
                    call_id = payload.get("call_id", "")
                    tool_name = payload.get("name", "")
                    tool_input = _normalize_codex_tool_input(
                        tool_name,
                        payload.get("input", {}),
                    )
                    block = {
                        "type": "tool_use",
                        "id": call_id,
                        "name": tool_name,
                        "input": tool_input,
                    }
                    ts_ms = _parse_timestamp_ms(entry.get("timestamp"))
                    if ts_ms is not None:
                        timestamps.append(ts_ms)
                    msg = Message(role="assistant", content=[block], timestamp=ts_ms)
                    messages.append(msg)
                    if call_id:
                        pending_calls[call_id] = {"message": msg, "result": None}
                    continue

                if payload_type == "function_call_output":
                    call_id = payload.get("call_id", "")
                    pending = pending_calls.get(call_id)
                    if pending is not None and pending["result"] is None:
                        pending["result"] = _build_codex_tool_result(
                            payload.get("output", ""),
                            is_error=False,
                        )
                    continue

                if payload_type == "custom_tool_call_output":
                    call_id = payload.get("call_id", "")
                    pending = pending_calls.get(call_id)
                    if pending is not None and pending["result"] is None:
                        pending["result"] = _build_codex_tool_result(
                            payload.get("output", ""),
                            is_error=False,
                        )
                    continue

                if payload_type == "reasoning":
                    summary = payload.get("summary")
                    if isinstance(summary, list) and summary:
                        pending_thinking = "\n".join(str(item) for item in summary if item)
                    continue

                role = payload.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = _normalize_codex_content_blocks(
                    payload.get("content", []),
                    thinking=pending_thinking if role == "assistant" else None,
                )
                if role == "assistant":
                    pending_thinking = None
                if not content:
                    continue

                ts_ms = _parse_timestamp_ms(
                    entry.get("timestamp") or payload.get("created_at")
                )
                if ts_ms is not None:
                    timestamps.append(ts_ms)

                messages.append(Message(role=role, content=content, timestamp=ts_ms))
                continue

            # Fallback for older/simple JSONL shapes.
            role = entry.get("role") or entry.get("message", {}).get("role")
            if role not in ("user", "assistant"):
                continue
            content = _normalize_codex_content_blocks(
                entry.get("content") or entry.get("message", {}).get("content", "")
            )
            if not content:
                continue

            ts_ms = _parse_timestamp_ms(entry.get("timestamp") or entry.get("created_at"))
            if ts_ms is not None:
                timestamps.append(ts_ms)

            messages.append(Message(role=role, content=content, timestamp=ts_ms))

    for pending in pending_calls.values():
        if pending["result"] is not None:
            pending["message"].content.append({
                "tool_use_id": pending["message"].content[0].get("id", ""),
                **pending["result"],
            })

    if not messages:
        return None

    return Conversation(
        session_id=session_id,
        agent="codex",
        workspace=workspace or str(filepath.parent),
        git_remote=git_remote,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        messages=messages,
        source_path=str(filepath),
    )


def _parse_codex_json(filepath: Path) -> Conversation | None:
    """Parse a Codex JSON session file."""
    with open(filepath, "r", errors="replace") as f:
        data = json.load(f)

    if isinstance(data, list):
        raw_messages = data
    elif isinstance(data, dict):
        raw_messages = data.get("messages", data.get("history", []))
    else:
        return None

    messages = []
    timestamps = []

    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = _normalize_codex_content_blocks(msg.get("content", ""))
        if role not in ("user", "assistant") or not content:
            continue

        ts_ms = _parse_timestamp_ms(msg.get("timestamp") or msg.get("created_at"))
        if ts_ms is not None:
            timestamps.append(ts_ms)

        messages.append(Message(role=role, content=content, timestamp=ts_ms))

    if not messages:
        return None

    workspace = str(filepath.parent)
    return Conversation(
        session_id=filepath.stem,
        agent="codex",
        workspace=workspace,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        messages=messages,
        source_path=str(filepath),
    )


# ---------------------------------------------------------------------------
# Unified scanner
# ---------------------------------------------------------------------------

class SessionScanner:
    """Scan local filesystem for coding agent sessions across multiple tools.

    Replaces CASS as the data source. No external dependencies.
    """

    def __init__(self):
        self._conversations: list[Conversation] = []
        self._by_workspace: dict[str, list[Conversation]] = defaultdict(list)

    def scan_all(self, *, verbose: bool = False) -> "SessionScanner":
        """Scan all supported agent session stores."""
        agents_found = defaultdict(int)

        if verbose:
            print("Scanning for coding agent sessions...")

        # Claude Code
        for conv in parse_claude_code_sessions():
            self._add(conv)
            agents_found["claude_code"] += 1

        if verbose and agents_found["claude_code"]:
            print(f"  Claude Code: {agents_found['claude_code']} sessions")

        # Gemini CLI
        for conv in parse_gemini_sessions():
            self._add(conv)
            agents_found["gemini"] += 1

        if verbose and agents_found["gemini"]:
            print(f"  Gemini CLI: {agents_found['gemini']} sessions")

        # Codex CLI
        for conv in parse_codex_sessions():
            self._add(conv)
            agents_found["codex"] += 1

        if verbose and agents_found["codex"]:
            print(f"  Codex CLI: {agents_found['codex']} sessions")

        total = sum(agents_found.values())
        if verbose:
            print(f"  Total: {total} sessions across {len(agents_found)} agent(s)")
            print(f"  Workspaces: {len(self._by_workspace)}")

        return self

    def _add(self, conv: Conversation):
        self._conversations.append(conv)
        self._by_workspace[conv.workspace].append(conv)

    @property
    def conversations(self) -> list[Conversation]:
        return self._conversations

    @property
    def workspaces(self) -> dict[str, list[Conversation]]:
        return dict(self._by_workspace)

    def get_workspace_summary(self) -> list[dict]:
        """Return workspace summaries compatible with skillbench.py's get_workspaces().

        Returns list of dicts with keys matching CASS output:
        - workspace: path string
        - agents: list of agent slugs
        - total_conversations: int
        - total_user_messages: int
        - first_session: epoch ms
        - last_session: epoch ms
        """
        summaries = []
        for ws_path, convs in sorted(
            self._by_workspace.items(),
            key=lambda x: -len(x[1]),
        ):
            agents = sorted(set(c.agent for c in convs))
            timestamps = [
                ts for c in convs
                for ts in [c.started_at, c.ended_at]
                if ts is not None
            ]
            user_msg_count = sum(
                1 for c in convs for m in c.messages if m.role == "user"
            )

            summaries.append({
                "workspace": ws_path,
                "git_remote": next((c.git_remote for c in convs if c.git_remote), None),
                "agents": agents,
                "total_conversations": len(convs),
                "total_user_messages": user_msg_count,
                "first_session": min(timestamps) if timestamps else None,
                "last_session": max(timestamps) if timestamps else None,
            })

        return summaries

    def resolve_gemini_hashes(self, known_paths: list[str] | None = None):
        """Resolve Gemini hash workspaces to real project paths.

        Uses known workspace paths (from Claude Code, Codex, etc.) to
        reverse-map Gemini's SHA-256 hash directories.
        """
        if known_paths is None:
            # Use all non-Gemini workspace paths as candidates
            gemini_tmp = str(Path.home() / ".gemini" / "tmp")
            known_paths = [
                ws for ws in self._by_workspace
                if gemini_tmp not in ws
            ]

        # Build reverse lookup: sha256(path) → real_path
        hash_to_path = {}
        for real_path in known_paths:
            h = gemini_hash_for_path(real_path)
            hash_to_path[h] = real_path

        # Resolve Gemini workspaces
        gemini_tmp = str(Path.home() / ".gemini" / "tmp")
        resolved = 0
        to_remap = []

        for ws_path in list(self._by_workspace.keys()):
            if gemini_tmp not in ws_path:
                continue
            # Extract hash from path
            parts = ws_path.split(gemini_tmp + "/")
            if len(parts) != 2:
                continue
            hash_part = parts[1].split("/")[0]

            real_path = hash_to_path.get(hash_part)
            if real_path:
                to_remap.append((ws_path, real_path))
                resolved += 1

        # Apply remapping
        for old_ws, new_ws in to_remap:
            convs = self._by_workspace.pop(old_ws)
            for c in convs:
                c.workspace = new_ws
            self._by_workspace[new_ws].extend(convs)

        return resolved

    def get_conversations_for_workspaces(
        self, workspace_paths: list[str],
    ) -> list[Conversation]:
        """Return all conversations for the given workspace paths.

        Also includes Gemini hash equivalents automatically.
        """
        result = []
        gemini_tmp = str(Path.home() / ".gemini" / "tmp")

        # Build expanded set of paths (include Gemini hashes)
        expanded = set(workspace_paths)
        for p in workspace_paths:
            gemini_path = os.path.join(gemini_tmp, gemini_hash_for_path(p))
            expanded.add(gemini_path)

        for ws_path, convs in self._by_workspace.items():
            if ws_path in expanded:
                result.extend(convs)

        return result

    def export_sessions(
        self, workspace_paths: list[str],
    ) -> list[dict]:
        """Export sessions for given workspaces as dicts (for JSON serialization)."""
        convs = self.get_conversations_for_workspaces(workspace_paths)
        return [c.to_dict() for c in convs]


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def scan_sessions(*, verbose: bool = False) -> SessionScanner:
    """One-liner to scan all local agent sessions and resolve Gemini hashes."""
    scanner = SessionScanner()
    scanner.scan_all(verbose=verbose)
    scanner.resolve_gemini_hashes()
    return scanner


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    scanner = scan_sessions(verbose=True)

    summaries = scanner.get_workspace_summary()
    print(f"\n{'='*60}")
    print(f"  {len(summaries)} workspaces found")
    print(f"{'='*60}\n")

    for ws in summaries[:20]:
        agents = ", ".join(ws["agents"])
        print(f"  {ws['workspace']}")
        print(f"    {ws['total_conversations']} sessions, "
              f"{ws['total_user_messages']} user msgs, "
              f"agents: {agents}")
