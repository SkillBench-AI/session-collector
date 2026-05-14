"""Persistent user configuration for the skillbench CLI.

Stored as JSON at ``~/.skillbench/config.json`` (overridable via
``SKILLBENCH_CONFIG``). Kept JSON instead of YAML to avoid an extra
dependency — the README still presents the contents in YAML-style for
human consumption, but the on-disk format is a stable JSON object.

Only a small, explicit set of keys is supported so we can validate values
and produce useful error messages instead of silently accepting typos.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".skillbench" / "config.json"


# Keys are flattened with dot-notation in the CLI surface, e.g.
# ``codex.allowed_orgs``. The structure on disk mirrors this nesting so a
# user can read/edit the JSON directly without learning the CLI vocabulary.
SUPPORTED_KEYS: dict[str, dict[str, Any]] = {
    "codex.allowed_orgs": {
        "type": "list[str]",
        "description": "Default --allowed-orgs for codex commands.",
    },
    "codex.export_path": {
        "type": "str",
        "description": "Default sanitized export path written by `codex collect`.",
    },
    "codex.interval": {
        "type": "int",
        "description": "Default poll interval (seconds) for `codex watch`.",
    },
    "codex.db": {
        "type": "str",
        "description": "Override path to the daemon SQLite database.",
    },
    "codex.base_dir": {
        "type": "str",
        "description": "Override Codex session root directory.",
    },
}


def config_path() -> Path:
    """Return the active config path, honoring ``SKILLBENCH_CONFIG``."""
    override = os.environ.get("SKILLBENCH_CONFIG")
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


@dataclass
class Config:
    data: dict[str, Any]
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        target = path or config_path()
        if target.exists():
            try:
                payload = json.loads(target.read_text() or "{}")
            except json.JSONDecodeError:
                payload = {}
        else:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return cls(data=payload, path=target)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[parts[-1]] = value

    def unset(self, key: str) -> bool:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            return False
        del node[parts[-1]]
        return True

    def flat(self) -> dict[str, Any]:
        out: dict[str, Any] = {}

        def walk(prefix: str, node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(f"{prefix}.{k}" if prefix else k, v)
            else:
                out[prefix] = node

        walk("", self.data)
        return out


def parse_value(key: str, raw_values: list[str]) -> Any:
    """Coerce CLI ``raw_values`` into the right type for ``key``."""
    spec = SUPPORTED_KEYS.get(key)
    if spec is None:
        raise ValueError(
            f"Unknown config key '{key}'. "
            f"Known keys: {', '.join(sorted(SUPPORTED_KEYS))}"
        )

    kind = spec["type"]
    if kind == "list[str]":
        # Accept either repeated args or a single comma-separated string.
        flat: list[str] = []
        for raw in raw_values:
            for piece in str(raw).split(","):
                piece = piece.strip()
                if piece:
                    flat.append(piece)
        return flat

    if not raw_values:
        raise ValueError(f"config set {key} requires a value")
    raw = raw_values[0]
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} expects an integer, got {raw!r}") from exc
    return str(raw)


def known_keys_help() -> str:
    """Render a human-readable listing of supported config keys."""
    lines = ["Supported keys:"]
    for key in sorted(SUPPORTED_KEYS):
        spec = SUPPORTED_KEYS[key]
        lines.append(f"  {key:<22}  {spec['description']}")
    return "\n".join(lines)
