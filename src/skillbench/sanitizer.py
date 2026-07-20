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


# Sanitization policy version. Bumped when the detector set or redaction
# behavior changes so downstream consumers can reason about which rules a
# record was scrubbed under. Kept in lockstep with the Codex/Claude collectors'
# POLICY_VERSION ("1.0.0") so a version string means the same thing across
# every client surface.
POLICY_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------

# Each pattern: (name, regex, replacement)
# Order matters — more specific patterns should come before generic ones.

# Each entry: (name, compiled_regex, replacement, value).
#   value == "whole"  -> the whole match is the secret; replace it entirely.
#   value == <int N>  -> capture group N holds the secret; the surrounding
#                        structure (field name, scheme word) is preserved and
#                        only group N is replaced.
# This mirrors the Codex/Claude detector model so the placeholder allow-list
# can inspect exactly the credential (not the surrounding structure).
PATTERNS: list[tuple[str, re.Pattern, str, object]] = [
    # === API Keys & Tokens ===
    (
        # AWS access key id — long-term (AKIA) + temporary/STS (ASIA) + the
        # other documented id prefixes.
        "aws_key",
        re.compile(r"\bA(?:KIA|SIA|IDA|GPA|ROA|NPA|NVA)[A-Z0-9]{16}\b", re.ASCII),
        "[REDACTED_AWS_KEY]",
        "whole",
    ),
    (
        # JSON Web Token (header.payload.signature, base64url segments).
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
        "[REDACTED_JWT]",
        "whole",
    ),
    (
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        "[REDACTED_GOOGLE_KEY]",
        "whole",
    ),
    (
        "google_oauth_client",
        re.compile(r"\b[0-9]+-[0-9A-Za-z_]{32}\.apps\.googleusercontent\.com\b"),
        "[REDACTED_GOOGLE_OAUTH]",
        "whole",
    ),
    (
        "aws_secret",
        re.compile(r"(?:aws_secret_access_key|AWS_SECRET)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
        "[REDACTED_AWS_SECRET]",
        1,
    ),
    (
        # Classic/OAuth/user/server/refresh tokens (ghp_/gho_/ghu_/ghs_/ghr_)
        # plus fine-grained PATs.
        "github_token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
        "[REDACTED_GITHUB_TOKEN]",
        "whole",
    ),
    (
        "gitlab_pat",
        re.compile(r"\bglpat-[0-9A-Za-z_-]{20}\b"),
        "[REDACTED_GITLAB_TOKEN]",
        "whole",
    ),
    (
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
        "[REDACTED_ANTHROPIC_KEY]",
        "whole",
    ),
    (
        # OpenAI style keys (sk-, sk-proj-). sk-ant- is handled above.
        "openai_key",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
        "[REDACTED_OPENAI_KEY]",
        "whole",
    ),
    (
        "stripe_key",
        re.compile(r"\b(?:sk|rk)_(?:test|live|prod)_[0-9A-Za-z]{10,99}\b"),
        "[REDACTED_STRIPE_KEY]",
        "whole",
    ),
    (
        "twilio_key",
        re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
        "[REDACTED_TWILIO_KEY]",
        "whole",
    ),
    (
        "sendgrid_key",
        re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
        "[REDACTED_SENDGRID_KEY]",
        "whole",
    ),
    (
        "mailgun_key",
        re.compile(r"\bkey-[0-9a-zA-Z]{32}\b"),
        "[REDACTED_MAILGUN_KEY]",
        "whole",
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
        "[REDACTED_SLACK_TOKEN]",
        "whole",
    ),
    (
        "slack_webhook",
        re.compile(r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,60}"),
        "[REDACTED_SLACK_WEBHOOK]",
        "whole",
    ),
    (
        "npm_token",
        re.compile(r"\bnpm_[0-9A-Za-z]{36}\b"),
        "[REDACTED_NPM_TOKEN]",
        "whole",
    ),
    (
        "pypi_token",
        re.compile(r"\bpypi-AgEIcHlwaS[A-Za-z0-9_-]{50,}\b"),
        "[REDACTED_PYPI_TOKEN]",
        "whole",
    ),
    (
        "digitalocean_token",
        re.compile(r"\bdo[oprv]_v1_[a-f0-9]{64}\b"),
        "[REDACTED_DO_TOKEN]",
        "whole",
    ),
    (
        "hashicorp_vault_token",
        re.compile(r"\bhv[bs]\.[A-Za-z0-9_-]{90,}\b"),
        "[REDACTED_VAULT_TOKEN]",
        "whole",
    ),
    (
        "generic_api_key",
        re.compile(r"(?:api[_-]?key|apikey|api[_-]?token|access[_-]?token|auth[_-]?token|secret[_-]?key)\s*[=:]\s*['\"]?([A-Za-z0-9_\-./+=]{16,})['\"]?", re.IGNORECASE),
        "[REDACTED_API_KEY]",
        1,
    ),

    # === Bearer / Authorization headers ===
    (
        "bearer_token",
        re.compile(r"\b(Bearer\s+)([A-Za-z0-9_\-./+=]{20,})", re.IGNORECASE),
        "[REDACTED_BEARER_TOKEN]",
        2,
    ),
    (
        "authorization_header",
        re.compile(r"((?:Proxy-)?Authorization:\s*)([^\n]{20,})", re.IGNORECASE),
        "[REDACTED_AUTH_HEADER]",
        2,
    ),

    # === SSH keys ===
    (
        "ssh_private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
        "[REDACTED_PRIVATE_KEY]",
        "whole",
    ),

    # === Connection strings ===
    (
        "db_connection_string",
        re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"]+", re.IGNORECASE),
        "[REDACTED_DB_URL]",
        "whole",
    ),
    (
        # Any URL with embedded basic-auth credentials (user:pass@host). Placed
        # before the email pass so the whole credential URL is redacted first.
        "basic_auth_url",
        re.compile(r"\bhttps?://[^\s:/@]+:[^\s:/@]+@[^\s'\"]+"),
        "[REDACTED_BASIC_AUTH_URL]",
        "whole",
    ),

    # === Email addresses ===
    (
        "email",
        re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
        "[REDACTED_EMAIL]",
        "whole",
    ),

    # === Private IP addresses ===
    (
        "private_ip",
        re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"),
        "[REDACTED_IP]",
        "whole",
    ),

    # === Passwords in URLs/configs ===
    (
        "password_in_url",
        re.compile(r"((?:password|passwd|pwd)\s*[=:]\s*)['\"]?([^\s'\"]{8,})['\"]?", re.IGNORECASE),
        "[REDACTED_PASSWORD]",
        2,
    ),

    # === .env file values ===
    (
        "env_secret",
        re.compile(r"^((?:SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL|AUTH)[A-Z_]*)\s*=\s*(.+)$", re.MULTILINE | re.IGNORECASE),
        "[REDACTED]",
        2,
    ),
]


# Obvious non-secret stand-ins. A matched credential that is exactly one of
# these (case-insensitive), or an all-mask string like "xxxxxxxx"/"********",
# is left in place so doc/example/fixture text isn't needlessly redacted.
# Mirrors the Codex/Claude placeholder allow-list.
STOPWORDS = frozenset({
    "example", "examples", "dummy", "test", "tests", "testing", "test-token",
    "testtoken", "placeholder", "redacted", "changeme", "your-token",
    "your-api-key", "your_api_key", "your-secret", "yourkey", "xxx", "xxxx",
    "xxxxxxxx", "none", "null", "nil", "undefined", "true", "false", "sample",
    "secret", "token", "password", "apikey", "api-key", "api_key", "default",
    "root", "admin", "user", "username", "foo", "bar", "baz", "abc", "abc123",
    "123456", "process", "env", "string", "number", "boolean", "value",
})

_MASK_RE = re.compile(r"^[x*•]+$", re.IGNORECASE)


def _is_placeholder(value: str) -> bool:
    """True when a matched credential is an obvious non-secret stand-in."""
    if not value:
        return True
    trimmed = value.strip().lower()
    if not trimmed:
        return True
    if trimmed in STOPWORDS:
        return True
    if _MASK_RE.match(trimmed):
        return True
    return False


# Object-key names that force Tier-1 redaction of their string value even when
# the value doesn't match a detector (mirrors Codex/Claude scrubDeep).
SECRET_KEY_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"api[_-]?key", r"token", r"password", r"passwd", r"secret",
        r"credentials?", r"auth", r"bearer", r"access[_-]?key",
    )
]


def _is_secret_key(key: str) -> bool:
    return any(p.search(key) for p in SECRET_KEY_PATTERNS)


# Home directory pattern — built dynamically per user.
#
# Home paths are normalized to a literal ``~`` (only the home prefix is
# rewritten; the rest of the path stays readable). This intentionally diverges
# from the Claude/Codex collectors, which HMAC-hash path fields with a shared
# per-device salt to make the same path correlatable across surfaces.
#
# The divergence is deliberate (see SANITIZATION_EPIC.md, Open Decision #7,
# 2026-07): the session collector is a review-before-share tool — users inspect
# the sanitized export and choose what to share (docs/privacy.md) — so a
# human-readable ``~/project/...`` path is a feature, whereas an opaque HMAC
# token would make the export unreviewable. Cross-surface correlation is
# device-local only and low-value here, and this tool has no salt provisioning.
def _home_dir_pattern() -> tuple[str, re.Pattern, str, object]:
    home = str(Path.home())
    return (
        "home_dir",
        re.compile(re.escape(home)),
        "~",
        "whole",
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
        """Apply all redaction patterns to a text string.

        Each match's credential (the whole match, or the designated capture
        group) is checked against the placeholder allow-list; obvious
        stand-ins (``example``, ``xxxxxxxx``, …) are left in place. For
        group-based detectors the surrounding structure (field name / scheme
        word) is preserved and only the credential is replaced.
        """
        if not text:
            return text

        for name, pattern, replacement, value in self.patterns:

            def _repl(m, name=name, replacement=replacement, value=value):
                full = m.group(0)
                candidate = full if value == "whole" else (m.group(value) or "")
                if _is_placeholder(candidate):
                    return full
                self.stats[name] = self.stats.get(name, 0) + 1
                if value == "whole":
                    return replacement
                start = m.start(value) - m.start(0)
                end = m.end(value) - m.start(0)
                return full[:start] + replacement + full[end:]

            text = pattern.sub(_repl, text)

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

    def _sanitize_value(self, value, key: str | None = None):
        """Recursively sanitize nested strings inside dict/list structures.

        When a string leaf sits under a secret-labelled key (``api_key``,
        ``token``, ``password``, …) it is force-redacted even if it matches no
        detector — unless it's an obvious placeholder — mirroring the
        Codex/Claude ``scrubDeep`` key-name rule. Dict keys are structural and
        never scanned; the parent key propagates through lists.
        """
        if isinstance(value, str):
            if key is not None and _is_secret_key(key) and not _is_placeholder(value):
                self.stats["secret_key"] = self.stats.get("secret_key", 0) + 1
                return "[REDACTED_SECRET]"
            return self.sanitize_text(value)
        if isinstance(value, list):
            return [self._sanitize_value(item, key) for item in value]
        if isinstance(value, dict):
            return {k: self._sanitize_value(item, k) for k, item in value.items()}
        return value

    # Top-level fields kept verbatim rather than content-sanitized. These are
    # structural, non-secret metadata that downstream analysis depends on:
    # git_remote drives org-scope attribution (see daemon.py) and its SCP-form
    # value (git@host:org/repo) would otherwise be mangled by the email
    # detector. Kept deliberately tiny — anything not listed is fully walked.
    STRUCTURAL_FIELDS: frozenset[str] = frozenset({"git_remote"})

    def sanitize_session(self, session: dict) -> dict:
        """Sanitize a full session dict.

        Walks the entire session recursively and sanitizes every string leaf,
        rather than scrubbing a fixed set of known fields. This mirrors the
        Codex ``redactDeep`` / Claude ``scrubDeep`` boundary so that any
        other, nested, or future session field leaves the machine sanitized —
        not just ``messages``/``workspace``/``title``/``source_path``. Those
        known fields remain covered because they are string leaves reached by
        the same walk. A tiny allow-list of structural top-level fields
        (``STRUCTURAL_FIELDS``) is passed through untouched.

        When any redaction occurs, a compact ``_sanitization`` summary is
        attached to the record (policy version + counts by detector type,
        never original values), mirroring the Codex per-record ``_sanitization``
        meta stamped on uploaded events.
        """
        if not isinstance(session, dict):
            return self._sanitize_value(session)

        before = dict(self.stats)
        result = {
            key: value if key in self.STRUCTURAL_FIELDS else self._sanitize_value(value, key)
            for key, value in session.items()
        }

        counts = {
            name: self.stats[name] - before.get(name, 0)
            for name in self.stats
            if self.stats[name] - before.get(name, 0) > 0
        }
        if counts:
            result["_sanitization"] = {
                "policy_version": POLICY_VERSION,
                "counts": counts,
                "total": sum(counts.values()),
            }
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
