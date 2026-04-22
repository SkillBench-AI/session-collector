import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from skillbench.sanitizer import Sanitizer  # noqa: E402


def test_sanitizer_recursively_redacts_nested_tool_blocks():
    sanitizer = Sanitizer()
    session = {
        "workspace": "/Users/john/project",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "write_file",
                        "input": {
                            "path": "/Users/john/project/.env",
                            "content": "OPENAI_API_KEY=sk-12345678901234567890",
                        },
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": [
                            {
                                "text": "Saved file for john@example.com at /Users/john/project/.env"
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
