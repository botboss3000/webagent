---
context_type: tools
title: Tool Reference
tags: [tools, reference]
---

## Always-available core tools

| Tool | What it does |
|------|-------------|
| `web_search` | Search the web via DuckDuckGo |
| `browser_action` | Full browser automation (click, type, navigate, screenshot) |
| `memory` | Store/retrieve/search the knowledge brain |
| `db_query` | Query database tables |
| `get_time` / `get_date` | Current time and date |
| `get_weather` | Weather for a location |
| `calculate` | Evaluate math expressions |
| `read_attachment` | Read uploaded file contents |
| `http_request` | Make custom HTTP requests |

## Discovery tools (use these first)

- `list_tools` — see all available tools
- `search_tools` — find tools by keyword
- `get_tool_definition` — see a tool's full JSON schema parameters

## Admin tools (if enabled)

- `read_source` — read any file on the server
- `write_source` — create or overwrite files (requires confirmation)
- `edit_source` — edit files in-place (requires confirmation)
- `delete_source` — delete files (requires confirmation)
- `run_command` — execute shell commands (requires confirmation)
- `restart_server` — restart the webAgent process (requires confirmation)
