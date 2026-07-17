import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skillbench.sanitizer import Sanitizer  # noqa: E402


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
