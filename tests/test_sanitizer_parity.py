"""Parity tests for the deterministic sanitizer.

Ensures the session-collector's PATTERNS list scrubs the same secret corpus as
the SkillMeter Codex collector's TIER1_DETECTORS (scripts/sanitizer.js). The
corpus below is the canonical set of fake, non-functional secrets used as
Codex's own detector fixtures (test/sanitization.test.js FAKE). When node and
the Codex sanitizer are available, the same corpus is run through it live to
prove both surfaces redact identically rather than merely trusting a copy.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skillbench.sanitizer import Sanitizer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX_SANITIZER = (
    REPO_ROOT
    / "skillmeter-codex-marketplace"
    / "plugins"
    / "skillmeter"
    / "scripts"
    / "sanitizer.js"
)

# Fake, non-functional secrets — mirror of Codex FAKE (sanitization.test.js).
# None are real credentials; they exist only to exercise the detectors.
FAKE_CORPUS = {
    "githubClassic": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "githubPat": "github_pat_11ABCDE0000aBcDeFgHiJ_KLMNOPqrstuvWXYZ0123456789abcdef",
    "openai": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
    "anthropic": "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "google": "AIza" + "Sy0123456789abcdefghijklmnopqrstuvw",  # AIza + 35 chars
    "aws": "AKIAIOSFODNN7EXAMPLE",  # AKIA + 16 chars (canonical fake)
    "awsTemp": "ASIAIOSFODNN7EXAMPLE",  # ASIA temporary/STS credential prefix
    # Split literal so repo secret-scanning doesn't flag this fake fixture;
    # the runtime value is identical to Codex's slack fixture.
    "slack": "xox" + "b-1234567890-ABCDEFGHIJKLMNOP",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
}

PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEpAIBAAKCAQEA1234567890abcdef\n"
    "QEFAAOCAQ8AMIIBCgKCAQEA\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize("label", sorted(FAKE_CORPUS))
def test_session_collector_redacts_each_tier1_fixture(label):
    """Every seeded Tier-1 secret is removed and a placeholder left behind."""
    secret = FAKE_CORPUS[label]
    sanitizer = Sanitizer(redact_home=False)
    out = sanitizer.sanitize_text(f"prefix {secret} suffix")

    assert secret not in out, f"{label}: raw secret survived sanitization"
    assert "[REDACTED" in out, f"{label}: expected a redaction placeholder"
    assert sanitizer.stats, f"{label}: expected a recorded redaction"


def test_new_detectors_redacted_in_nested_message_content():
    """JWT / Google key / AWS temp key seeded in message content are redacted."""
    sanitizer = Sanitizer(redact_home=False)
    session = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"jwt is {FAKE_CORPUS['jwt']}"},
                    {"type": "text", "text": f"google {FAKE_CORPUS['google']}"},
                    {
                        "type": "tool_result",
                        "content": [{"text": f"temp creds {FAKE_CORPUS['awsTemp']}"}],
                    },
                ],
            }
        ],
    }

    sanitized = sanitizer.sanitize_export([session])[0]
    blob = json.dumps(sanitized)

    for label in ("jwt", "google", "awsTemp"):
        assert FAKE_CORPUS[label] not in blob, f"{label}: leaked into output"
    assert "[REDACTED_JWT]" in blob
    assert "[REDACTED_GOOGLE_KEY]" in blob
    assert "[REDACTED_AWS_KEY]" in blob


def test_no_original_secret_leaks_into_stats_or_output():
    """No original secret value is ever written to the output or the stats."""
    sanitizer = Sanitizer(redact_home=False)
    all_secrets = list(FAKE_CORPUS.values()) + [PRIVATE_KEY]
    text = "\n".join(f"line {i}: {s}" for i, s in enumerate(all_secrets))

    out = sanitizer.sanitize_text(text)
    stats_blob = json.dumps(sanitizer.stats)

    for secret in all_secrets:
        assert secret not in out, "secret leaked into sanitized output"
        # stats hold detector-name -> count only, never the matched value.
        assert secret not in stats_blob, "secret leaked into stats"


def _codex_redact(corpus: dict[str, str]) -> dict[str, dict]:
    """Run each corpus string through the live Codex sanitizer via node."""
    script = f"""
const s = require({json.dumps(str(CODEX_SANITIZER))});
const corpus = {json.dumps(corpus)};
const out = {{}};
for (const [k, v] of Object.entries(corpus)) {{
  const r = s.redactString("prefix " + v + " suffix");
  out[k] = {{ leaked: r.value.includes(v), redacted: r.value.includes(s.SECRET_PLACEHOLDER) }};
}}
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"codex sanitizer failed: {result.stderr}"
    return json.loads(result.stdout)


def test_parity_same_corpus_redacted_by_codex_and_session_collector():
    """The same fixture corpus is redacted by BOTH the Codex collector and here.

    Skips when node or the Codex source is unavailable (e.g. python-only CI),
    but the pure-Python assertions above still guarantee coverage.
    """
    if shutil.which("node") is None or not CODEX_SANITIZER.exists():
        pytest.skip("node or Codex sanitizer.js unavailable")

    codex = _codex_redact(FAKE_CORPUS)

    for label, secret in FAKE_CORPUS.items():
        # Codex side.
        assert not codex[label]["leaked"], f"{label}: leaked through Codex"
        assert codex[label]["redacted"], f"{label}: not redacted by Codex"

        # Session-collector side, same input.
        sanitizer = Sanitizer(redact_home=False)
        out = sanitizer.sanitize_text(f"prefix {secret} suffix")
        assert secret not in out, f"{label}: leaked through session-collector"
        assert "[REDACTED" in out, f"{label}: not redacted by session-collector"
