import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skillbench.sanitizer import POLICY_VERSION, Sanitizer  # noqa: E402


# --- Shared cross-surface secret-fixture parity corpus ---------------------
# Loads the vendored copy of skillbench-docs/eval/secret-corpus/corpus.json and
# asserts every fixture is redacted. Tier-1 misses fail CI (blocks merge), so
# Tier-1 recall can't silently diverge from the Codex / Claude sanitizers.
# See SANITIZATION_EPIC.md Task 5.2.
_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "secret-corpus.json").read_text()
)


def _build_corpus_value(parts: list[str]) -> str:
    def expand(p: str) -> str:
        if p[:2] == "HI" and p[2:].isdigit():
            return _CORPUS["hi"][: int(p[2:])]
        if p[:2] == "HX" and p[2:].isdigit():
            return _CORPUS["hex"][: int(p[2:])]
        return p

    return "".join(expand(p) for p in parts)


def test_shared_corpus_version_and_size_guard():
    """Guard against silently shrinking the shared corpus to make tests pass."""
    assert _CORPUS["version"] == "1", "corpus version changed — re-sync all repo copies"
    tier1 = [f for f in _CORPUS["fixtures"] if f["tier"] == "tier1"]
    assert len(tier1) >= 24, f"expected >= 24 Tier-1 fixtures, got {len(tier1)}"
    assert any(f["tier"] == "tier2" for f in _CORPUS["fixtures"])


@pytest.mark.parametrize(
    "fixture",
    _CORPUS["fixtures"],
    ids=[f"{f['tier']}-{f['id']}" for f in _CORPUS["fixtures"]],
)
def test_shared_corpus_fixture_is_redacted(fixture):
    """Every shared-corpus fixture must be redacted by the session collector."""
    secret = _build_corpus_value(fixture["parts"])
    sanitizer = Sanitizer(redact_home=False)
    out = sanitizer.sanitize_text(f"prefix {secret} suffix")

    assert secret not in out, f"{fixture['id']}: raw secret survived sanitization"
    assert sanitizer.stats, f"{fixture['id']}: expected a redaction event"


def test_sanitizer_recursively_redacts_nested_tool_blocks():
    sanitizer = Sanitizer()
    home = str(Path.home())
    session = {
        "workspace": f"{home}/project",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "input": {
                            "path": f"{home}/project/.env",
                            "content": "OPENAI_API_KEY=sk-12345678901234567890",
                        },
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": [
                            {
                                "text": f"Saved file for john@example.com at {home}/project/.env"
                            }
                        ],
                        "is_error": False,
                    },
                ],
            }
        ],
    }

    sanitized = sanitizer.sanitize_export([session])[0]
    blocks = sanitized["messages"][0]["content"]

    assert sanitized["workspace"] == "~/project"
    assert blocks[0]["input"]["path"] == "~/project/.env"
    assert "OPENAI_API_KEY=" in blocks[0]["input"]["content"]
    assert "[REDACTED" in blocks[0]["input"]["content"]
    assert blocks[1]["content"][0]["text"] == "Saved file for [REDACTED_EMAIL] at ~/project/.env"


def test_sanitize_session_redacts_unexpected_nested_fields():
    """Secrets in arbitrary / nested / future fields are redacted, not just the
    known workspace/title/source_path/messages fields."""
    sanitizer = Sanitizer()
    session = {
        # An unexpected top-level field that no field-by-field scrubber knew about.
        "environment": {
            "vars": [
                {"name": "GITHUB_TOKEN", "value": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
            ],
            "note": "reach me at deploy@example.com",
        },
        "metadata": {"custom": {"deep": {"aws": "AKIAIOSFODNN7EXAMPLE"}}},
    }

    sanitized = sanitizer.sanitize_session(session)

    assert sanitized["environment"]["vars"][0]["value"] == "[REDACTED_GITHUB_TOKEN]"
    assert sanitized["environment"]["note"] == "reach me at [REDACTED_EMAIL]"
    assert sanitized["metadata"]["custom"]["deep"]["aws"] == "[REDACTED_AWS_KEY]"
    # Field name (a dict key) is structural and left intact.
    assert "GITHUB_TOKEN" == session["environment"]["vars"][0]["name"]


def test_sanitize_session_preserves_known_field_behavior():
    """Existing workspace/title/source_path scrubbing is unchanged."""
    sanitizer = Sanitizer()
    home = str(Path.home())
    session = {
        "workspace": f"{home}/proj",
        "title": "notes for jane@example.com",
        "source_path": f"{home}/proj/session.json",
        "git_remote": "git@github.com:skillbench-ai/repo.git",
        "count": 7,  # non-string scalar passes through untouched
    }

    sanitized = sanitizer.sanitize_session(session)

    assert sanitized["workspace"] == "~/proj"
    assert sanitized["title"] == "notes for [REDACTED_EMAIL]"
    assert sanitized["source_path"] == "~/proj/session.json"
    # git_remote is a structural field kept verbatim for org attribution.
    assert sanitized["git_remote"] == "git@github.com:skillbench-ai/repo.git"
    assert sanitized["count"] == 7


def test_home_path_normalized_to_tilde_not_hashed():
    """Per Open Decision #7, home paths are rewritten to a readable ``~`` and
    NOT HMAC-hashed like the Claude/Codex collectors — the export stays
    human-reviewable. Only the home prefix is rewritten; the rest is kept."""
    home = str(Path.home())
    sanitizer = Sanitizer()

    out = sanitizer.sanitize_text(f"opened {home}/project/src/app.py")

    assert out == "opened ~/project/src/app.py"
    # Divergence guard: no opaque hash token, the readable tail survives.
    assert home not in out
    assert "project/src/app.py" in out
    assert sanitizer.stats.get("home_dir") == 1


def test_home_redaction_can_be_disabled():
    """redact_home=False leaves paths untouched (used by content-only callers)."""
    home = str(Path.home())
    sanitizer = Sanitizer(redact_home=False)
    text = f"{home}/project/app.py"
    assert sanitizer.sanitize_text(text) == text


def test_sanitize_session_attaches_per_record_redaction_metadata():
    """A redacted record carries policy_version + counts by type, no values."""
    sanitizer = Sanitizer(redact_home=False)
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    session = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": f"tok {secret}"}]},
            {"role": "user", "content": [{"type": "text", "text": "email me@example.com"}]},
        ],
    }

    sanitized = sanitizer.sanitize_session(session)
    meta = sanitized["_sanitization"]

    assert meta["policy_version"] == POLICY_VERSION
    # Counts are keyed by detector type; two distinct detectors fired once each.
    assert meta["counts"] == {"github_token": 1, "email": 1}
    assert meta["total"] == 2
    # No original secret value anywhere in the metadata.
    assert secret not in json.dumps(meta)
    assert "me@example.com" not in json.dumps(meta)


def test_sanitize_session_omits_metadata_when_nothing_redacted():
    """Clean records get no _sanitization key (mirrors Codex tier1||tier2 gate)."""
    sanitizer = Sanitizer(redact_home=False)
    sanitized = sanitizer.sanitize_session({"title": "just a plain title", "n": 1})
    assert "_sanitization" not in sanitized


def test_per_record_counts_are_independent_across_sessions():
    """Per-record counts reflect only that record, not the running total."""
    sanitizer = Sanitizer(redact_home=False)
    s1 = {"title": "a@example.com"}
    s2 = {"title": "b@example.com and c@example.com"}

    out = sanitizer.sanitize_export([s1, s2])

    assert out[0]["_sanitization"]["counts"] == {"email": 1}
    assert out[1]["_sanitization"]["counts"] == {"email": 2}
