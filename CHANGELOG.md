# Changelog

## Multi-Agent Raw Parsers

Added `gather --full` support for 3 additional agents beyond Claude Code:

| Agent | Format | Key Data |
|-------|--------|----------|
| Gemini CLI | JSON (`chats/session-*.json`) | toolCalls, thoughts, model info |
| Codex CLI | JSONL (`response_item` events) | thinking summaries, output_text |
| Pi-Agent | JSONL (message events) | toolCall/toolResult blocks |

### Schema Unification

All parsers now output unified message format:
- `role`: `"user"` or `"agent"` (was `"assistant"` in raw parsers)
- `created_at`: ISO-8601 timestamp (was `"timestamp"` in raw parsers)
- `content`: string or structured block list

### Removed Parsers

Removed parsers for agents without CASS connectors:
- `_parse_raw_aider()`
- `_parse_raw_cline()`
- `_parse_raw_chatgpt()`
- `_parse_raw_cursor()`

### CASS Integration

- `_RAW_PARSERS` dispatcher maps CASS agent slugs (`claude_code`, `gemini_cli`) to parsers
- `gather --full` tries raw parser first, falls back to CASS flat on failure
- Gemini `"type": "gemini"` responses now correctly recognized (was silently dropped)

### gather --full vs gather (no flag)

| Mode | Source | Content | Structured blocks |
|------|--------|---------|-------------------|
| `gather` | CASS DB messages table | Flat text | No |
| `gather --full` | Raw session files | Full fidelity | Yes (tool_use, tool_result, thinking) |
