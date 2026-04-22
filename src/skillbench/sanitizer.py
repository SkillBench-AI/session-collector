#!/usr/bin/env python3
from __future__ import annotations
"""Deterministic pattern-based sanitizer for session data.

Replaces the AI-driven sanitization skill with a fast, reproducible
regex-based sanitizer. No Claude Code dependency — runs standalone.

Handles:
  - API keys and tokens (AWS, GitHub, Anthropic, OpenAI, etc.)
  - Email addresses
  - IP addresses (private ranges)
  - Home directory paths (replaces with ~)
  - SSH keys and certificates
  - Connection strings / database URLs
  - Bearer tokens in headers
  - Base64-encoded secrets (common patterns)
"""

import json
import re
import sys
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------

# Each pattern: (name, regex, replacement)
# Order matters — more specific patterns should come before generic ones.

PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # === API Keys & Tokens ===
    (
        "aws_key",
        re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),
        "[REDACTED_AWS_KEY]",
    ),
    (
        "aws_secret",
        re.compile(r"(?:aws_secret_access_key|AWS_SECRET)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
        "[REDACTED_AWS_SECRET]",
    ),
    (
        "github_token",
        re.compile(r"(ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|ghu_[A-Za-z0-9]{36}|ghs_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        "anthropic_key",
        re.compile(r"sk-ant-[A-Za-z0-9_-]{90,}"),
        "[REDACTED_ANTHROPIC_KEY]",
    ),
    (
        "openai_key",
        re.compile(r"sk-[A-Za-z0-9]{20,}(?:-[A-Za-z0-9]+)*"),
        "[REDACTED_OPENAI_KEY]",
    ),
    (
        "slack_token",
        re.compile(r"xox[bporas]-[A-Za-z0-9-]{10,}"),
        "[REDACTED_SLACK_TOKEN]",
    ),
    (
        "stripe_key",
        re.compile(r"[sr]k_(test|live)_[A-Za-z0-9]{20,}"),
        "[REDACTED_STRIPE_KEY]",
    ),
    (
        "npm_token",
        re.compile(r"npm_[A-Za-z0-9]{36}"),
        "[REDACTED_NPM_TOKEN]",
    ),
    (
        "generic_api_key",
        re.compile(r"(?:api[_-]?key|apikey|api[_-]?token|access[_-]?token|auth[_-]?token|secret[_-]?key)\s*[=:]\s*['\"]?([A-Za-z0-9_\-./+=]{16,})['\"]?", re.IGNORECASE),
        "[REDACTED_API_KEY]",
    ),

    # === Bearer / Authorization headers ===
    (
        "bearer_token",
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-./+=]{20,}", re.IGNORECASE),
        r"\1[REDACTED_BEARER_TOKEN]",
    ),
    (
        "authorization_header",
        re.compile(r"(Authorization:\s*)[^\n]{20,}", re.IGNORECASE),
        r"\1[REDACTED_AUTH_HEADER]",
    ),

    # === SSH keys ===
    (
        "ssh_private_key",
        re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
    ),

    # === Connection strings ===
    (
        "db_connection_string",
        re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"]+", re.IGNORECASE),
        "[REDACTED_DB_URL]",
    ),

    # === Email addresses ===
    (
        "email",
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
        "[REDACTED_EMAIL]",
    ),

    # === Private IP addresses ===
    (
        "private_ip",
        re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"),
        "[REDACTED_IP]",
    ),

    # === Passwords in URLs/configs ===
    (
        "password_in_url",
        re.compile(r"((?:password|passwd|pwd)\s*[=:]\s*)['\"]?[^\s'\"]{8,}['\"]?", re.IGNORECASE),
        r"\1[REDACTED_PASSWORD]",
    ),

    # === .env file values ===
    (
        "env_secret",
        re.compile(r"^((?:SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL|AUTH)[A-Z_]*)\s*=\s*(.+)$", re.MULTILINE | re.IGNORECASE),
        r"\1=[REDACTED]",
    ),
]


# Home directory pattern — built dynamically per user
def _home_dir_pattern() -> tuple[str, re.Pattern, str]:
    home = str(Path.home())
    return (
        "home_dir",
        re.compile(re.escape(home)),
        "~",
    )


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------

class Sanitizer:
    """Deterministic pattern-based sanitizer for session export data."""

    def __init__(self, *, redact_home: bool = True, extra_patterns: list | None = None):
        self.patterns = list(PATTERNS)
        if redact_home:
            self.patterns.append(_home_dir_pattern())
        if extra_patterns:
            self.patterns.extend(extra_patterns)
        self.stats: dict[str, int] = {}

    def sanitize_text(self, text: str) -> str:
        """Apply all redaction patterns to a text string."""
        if not text:
            return text

        for name, pattern, replacement in self.patterns:
            new_text = pattern.sub(replacement, text)
            if new_text != text:
                self.stats[name] = self.stats.get(name, 0) + 1
                text = new_text

        return text

    def sanitize_message(self, message: dict) -> dict:
        """Sanitize a single message dict (in-place compatible but returns new dict)."""
        result = dict(message)
        if "content" in result:
            result["content"] = self._sanitize_value(result["content"])
        return result

    def _sanitize_content_block(self, block) -> dict | str:
        """Sanitize a content block (text, tool_result, etc.)."""
        return self._sanitize_value(block)

    def _sanitize_value(self, value):
        """Recursively sanitize nested strings inside dict/list structures."""
        if isinstance(value, str):
            return self.sanitize_text(value)
        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._sanitize_value(item) for key, item in value.items()}
        return value

    def sanitize_session(self, session: dict) -> dict:
        """Sanitize a full session dict."""
        result = dict(session)

        # Sanitize messages
        if "messages" in result:
            result["messages"] = [
                self.sanitize_message(m) for m in result["messages"]
            ]

        # Sanitize workspace path
        if "workspace" in result and isinstance(result["workspace"], str):
            result["workspace"] = self.sanitize_text(result["workspace"])

        # Sanitize title
        if "title" in result and isinstance(result["title"], str):
            result["title"] = self.sanitize_text(result["title"])

        # Sanitize source_path
        if "source_path" in result and isinstance(result["source_path"], str):
            result["source_path"] = self.sanitize_text(result["source_path"])

        return result

    def sanitize_export(self, sessions: list[dict]) -> list[dict]:
        """Sanitize a full export (list of session dicts)."""
        self.stats = {}
        return [self.sanitize_session(s) for s in sessions]

    def print_stats(self):
        """Print redaction statistics."""
        if not self.stats:
            print("  No sensitive patterns found.")
            return

        total = sum(self.stats.values())
        print(f"  Redacted {total} sensitive pattern(s):")
        for name, count in sorted(self.stats.items(), key=lambda x: -x[1]):
            print(f"    {name}: {count}")


def sanitize_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
    *,
    redact_home: bool = True,
    verbose: bool = True,
) -> Path:
    """Sanitize a JSON export file and write the result.

    Args:
        input_path: Path to the raw export JSON file (list of sessions)
        output_path: Path for sanitized output. Defaults to
                     input_path with '_sanitized' suffix.
        redact_home: Replace home directory paths with ~
        verbose: Print progress and stats

    Returns:
        Path to the sanitized output file.
    """
    input_path = Path(input_path)
    if output_path is None:
        stem = input_path.stem
        if stem.endswith("_sanitized"):
            output_path = input_path  # already named correctly
        else:
            output_path = input_path.with_stem(f"{stem}_sanitized")
    output_path = Path(output_path)

    if verbose:
        print(f"Reading {input_path}...")

    with open(input_path, "r") as f:
        sessions = json.load(f)

    if verbose:
        print(f"  {len(sessions)} sessions loaded.")
        print("Sanitizing...")

    sanitizer = Sanitizer(redact_home=redact_home)
    sanitized = sanitizer.sanitize_export(sessions)

    if verbose:
        sanitizer.print_stats()

    if verbose:
        print(f"Writing {output_path}...")

    with open(output_path, "w") as f:
        json.dump(sanitized, f, indent=2, default=str)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    if verbose:
        print(f"  Done. Output: {size_mb:.1f} MB")

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python sanitizer.py <input.json> [output.json]")
        print("  Sanitizes a SkillBench session export file.")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    sanitize_file(input_file, output_file)
