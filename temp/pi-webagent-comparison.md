# Pi Agent vs webAgent — Functional Comparison

## Core Purpose

| Dimension | pi | webAgent |
|-----------|-----|----------|
| **What it is** | Terminal-native coding harness (TypeScript/Node.js) | General-purpose chatbot web service (Python/FastAPI) |
| **Interface** | TUI in terminal (or RPC/JSON for process integration) | Browser UI (HTML/CSS/JS) + REST API + WebSocket |
| **Primary use** | Software development — read, edit, test cycle | General chat, web search, memory, custom tool calling |
| **Persistence** | JSONL session files (tree-structured with branching) | SQLite (local) or Supabase (cloud) |

## Tool Comparison

| Capability | pi | webAgent |
|------------|-----|----------|
| Read files | ✅ `read` — built-in, always available | ✅ `read_source` — admin tool, privileged |
| Write files | ✅ `write` — built-in, always available | ✅ `write_source` — admin tool, requires user confirmation |
| Edit (text replacement) | ✅ `edit` — built-in, always available | ✅ `edit_source` — admin tool, requires user confirmation |
| Delete files | ❌ Not a built-in tool (security by design) | ✅ `delete_source` — admin tool, requires user confirmation |
| Run shell commands | ✅ `bash` — built-in, always available | ✅ `run_command` — admin tool, requires user confirmation |
| Web search | ❌ Via extension or skill | ✅ `web_search` — core tool |
| Weather / time / calculator | ❌ Not built-in | ✅ core tools |
| HTTP requests | ❌ Via extension | ✅ `http_request` — core tool |
| Memory (persistent pages) | ❌ Via extension | ✅ `memory` — core tool |
| DB queries | ❌ Not built-in | ✅ `db_query` — core tool |
| grep / find / ls | ✅ Built-in variants | ❌ Uses bash for file ops |
| User confirmation on mutations | ❌ No popups by default (add via extension) | ✅ Required for all admin mutating tools |

## How the "Harness" Differs

### 1. Confirmation Gates Break the Coding Loop

- **pi**: Tools (`write`, `edit`, `bash`) execute immediately. A 10-file refactor flows uninterrupted.
- **webAgent**: Every mutating admin tool (`write_source`, `edit_source`, `delete_source`, `run_command`, `restart_server`) requires a UI confirmation popup **before each call**. Ten file edits = ten interruptions. This is intentional — the admin tools are described as "privileged debug tools, NOT available in normal user operation."

### 2. Admin Tools Are Optional / Second-Class

- **pi**: The four core tools (`read`, `write`, `edit`, `bash`) are its identity. They ship enabled, no import guards.
- **webAgent**: Admin tools live in `app/admin/source_tools.py` behind a `try/except ImportError` guard. Delete the directory, they vanish. Also guarded by a **deny-list** blocking `.env`, `.bash_history`, `.ssh/*`, and dangerous commands.

### 3. Session Management

- **pi**: Sessions are JSONL files with a **tree structure**. You can:
  - `/tree` — navigate to any past point and branch off
  - `/fork` — spawn a new session from a past entry
  - `/clone` — duplicate the active branch
  - Auto-compaction when approaching context limits (lossy but keeps session alive)
  - Manual compaction via `/compact`
  - `/branch` — switch between branches in-place
- **webAgent**: Sessions are linear rows in a database. History reloads from `interactions` table each turn. No branching, no forking, no compaction. Long coding sessions hit context limits with no recovery.

### 4. Terminal-Native vs Browser-Native

- **pi**: Runs anywhere you have a terminal — SSH, tmux, CI/CD pipelines, headless servers. No HTTP server to start, no browser needed.
- **webAgent**: Requires a running FastAPI server, a browser, and a network connection. Cannot be used over SSH for remote development.

### 5. Default Tool Philosophy

- **pi**: Small set of powerful coding tools. Everything else is **progressive disclosure** — extensions, skills, prompt templates load on-demand or install via npm.
- **webAgent**: **Bootstrap + on-demand discovery.** Small set of hardcoded core tools plus a `list_tools` / `search_tools` / `get_tool_definition` discovery cycle. Users can write custom tools stored in the DB.

### 6. Extensibility Model

- **pi**: TypeScript extensions with deep event hooks — `tool_call` (block/modify), `before_agent_start` (inject system prompt), `context` (modify LLM messages), `tool_result` (transform results), `input` (intercept user input). Full TUI component API.
- **webAgent**: Python tools stored in DB tables. Admin endpoints for settings, provider config, communications plugins. No event interception system.

## Pi Agent's Default System Prompt

This is the built-in system prompt from `dist/core/system-prompt.js` (no custom `SYSTEM.md` override present):

```
You are an expert coding assistant operating inside pi, a coding agent harness.
You help users by reading files, executing commands, editing code, and writing
new files.

Available tools:
- read: Read file contents
- bash: Execute bash commands (ls, grep, find, etc.)
- edit: Make precise file edits with exact text replacement
- write: Create or overwrite files

In addition to the tools above, you may have access to other custom tools
depending on the project.

Guidelines:
- Use bash for file operations like ls, rg, find
- Be concise in your responses
- Show file paths clearly when working with files

Pi documentation (read only when the user asks about pi itself, its SDK,
extensions, themes, skills, or TUI):
- Main documentation: ...
- Additional docs: ...
- Examples: ...
- When asked about: extensions, themes, skills, prompt templates, TUI
  components, keybindings, SDK, custom providers, adding models, pi packages
- When working on pi topics, read the docs and examples, and follow .md
  cross-references before implementing

[project context files appended here if any]
[skills section appended here if read tool available]

Current date: YYYY-MM-DD
Current working directory: /path/to/project
```

### Variants (built dynamically)

The system prompt changes based on which tools are active:

- **Default tools** (`read`, `bash`, `edit`, `write`): The guidelines include file exploration instructions (prefer `grep`/`find`/`ls` over bash for file ops).
- **Read-only mode** (`--tools read,grep,find,ls`): Only read-capable tools appear. No write/edit/bash.
- **Custom tools** via extensions: Extra entries in the `Available tools:` list.
- **Skills**: Only injected when the `read` tool is available (so the agent can load skill content on-demand).
- **Context files** (`AGENTS.md` / `CLAUDE.md`): Appended as `# Project Context` section.
- **Append system prompt** (`--append-system-prompt` or `APPEND_SYSTEM.md`): Appended after the default prompt before context files.

## webAgent's System Prompt Structure

Built dynamically from database content (from `app/agent/prompts.py`):

```
# [AGENT DIRECTIVE]           -- non-editable system prompt from agents table

# [AGENT IDENTITY]            -- context_documents where context_type="agent"

# [USER]                      -- context_documents where context_type="user"

# [OPTIMIZER INSTRUCTIONS]    -- optimizer config (if enabled)

# [SKILLS]                    -- behavioral guidance docs

# [TOOLS]                     -- tool descriptions

# [TASKS]                     -- task definitions

# [MEMORY]                    -- persistent memory pages

# [PROJECT]                   -- project context

# [JOBS]                      -- job definitions

[critical rule]               -- confirmation requirement for destructive tools

# [BOOTSTRAP TOOLS]           -- core tool list + discovery instructions

# [BRAIN CONTEXT]             -- hybrid search results injected each turn

# [FEEDBACK]                  -- optimizer feedback prompt (if enabled)

# [USER ATTACHMENTS]          -- attached file descriptions
```

Key difference: webAgent's system prompt is highly **database-driven and dynamic** — context documents, tool definitions, memory, and brain search results are all loaded from the DB per-turn. Pi's is **statically defined** with progressive disclosure of skills and context files.


 ### What Would Be ~Identical

 - Quality of code written for a single-file task
 - Reasoning about a bug or architecture
 - Tool call accuracy (both call OpenAI/Anthropic tool APIs the same way)

 ### Where pi Still Pulls Ahead (even without confirmation gates)

 1. Context compaction → longer sessions survive.

 webAgent reloads the full interactions table into context every turn. After 50-100 turns of coding, you hit context limits hard —
 and there's no recovery. The model starts forgetting the project structure, the bug context, past decisions.

 Pi auto-compacts (summarizes older turns) and lets you /compact manually. It can do 200+ turn sessions on the same problem.

 2. Tree branching → safe exploration.

 When webAgent goes down a wrong path, there's no undo. The conversation is linear — you can't jump back and fork. Pi's /tree and
 /fork let you try a risky refactor, hit a dead end, jump back to the last good state, and keep going. This changes how you use the
 tool — you take more risks because you can recover.

 3. Progressive disclosure keeps the prompt lean.

 webAgent dumps ALL context documents, tool descriptions, memory, and brain search results into the system prompt every turn. That's
 a lot of noise tokens competing with the actual code context.

 Pi keeps skill descriptions in context (a few lines each) and loads full instructions only when the model calls read on the
 SKILL.md. Less noise per turn.

 4. Extension hooks change what's possible.

 This is the biggest intangible. Pi's event system lets extensions:
 - Inject system prompt content mid-turn (before_agent_start)
 - Block/modify tool calls (tool_call)
 - Transform tool results before the LLM sees them (tool_result)
 - Intercept user input before it reaches the LLM (input)

 webAgent's customization model is DB-stored Python tools — no lifecycle hooks. You can't build a "review my code before committing"
 gate, or an "auto-fix lint errors" middleware, or a "save session to git" workflow. Pi you can.

 5. The system prompt is tuned for coding.

 This is subtle but real. Pi's default prompt says "You are an expert coding assistant operating inside pi, a coding agent harness."
  It has specific guidelines about file operations, brevity, showing file paths. webAgent's prompt is built from DB context
 templates — generic by default, and only as good as the template content.