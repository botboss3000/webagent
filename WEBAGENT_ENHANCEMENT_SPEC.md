# webAgent Enhancement Specification

## Current System Analysis

### Database Schema (from `Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`)
1. **messages** table:
   - `id` (uuid), `user_id` (text), `session_id` (text)
   - `role` (text: 'user', 'assistant', 'system')
   - `content` (text), `created_at` (timestamptz)

2. **documents** table:
   - `id` (uuid), `user_id` (text), `doc_type` (text)
   - `title` (text), `content` (text), `tags` (text[]), `linked_doc_ids` (uuid[])
   - `created_at`, `updated_at` (timestamptz)

### API Endpoints
- `POST /api/v1/chat`: Process chat with tool-calling agent loop
- `POST /api/v1/seed-docs`: Insert placeholder documents (dev-only)

### Agent Architecture
- **Simple tool-calling agent loop** with OpenRouter (DeepSeek V3.2)
- **System prompt** assembled from context documents
- **Message history** loaded from database
- **Tool calling** via dynamic tool registry

### Context Documents
- `doc_type` values: 'agent', 'user_profile', 'skills', 'tools', 'jobs', 'memory', 'project'
- Loaded at each request, assembled into labeled markdown sections

---

## Enhancement Goals

Transform webAgent into a Hermes-like AI agent with:
1. **Durable memory** across sessions
2. **Self-building skillset** (procedural memory)
3. **Tool calling & execution**
4. **Session search & recall**
5. **Autonomous operation** (cron jobs, background tasks)

---

## Phase 1: Memory System

### 1.1 Memory Schema Enhancement

**Option A**: Extend existing `documents` table
- Add `memory_type` column: 'user_preference', 'environment', 'tool_quirk', 'convention'
- Add `priority` integer (1-10, default 5)
- Add `access_count` integer, `last_accessed` timestamptz
- Add `is_active` boolean (for soft deletion)

**Option B**: New `memories` table
```sql
CREATE TABLE memories (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL,
  memory_type text NOT NULL,  -- 'user', 'environment', 'tool', 'convention'
  content text NOT NULL,      -- Declarative fact
  priority integer DEFAULT 5,
  access_count integer DEFAULT 0,
  last_accessed timestamptz DEFAULT now(),
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
```

**Recommendation**: Option B (separate table) to avoid polluting documents table with memory-specific fields.

### 1.2 Memory CRUD API

**Endpoints**:
- `GET /api/v1/memories`: List memories with filtering
- `POST /api/v1/memories`: Create new memory
- `PATCH /api/v1/memories/{id}`: Update memory content or metadata
- `DELETE /api/v1/memories/{id}`: Soft delete (is_active = false)

**Memory Types**:
- `user`: User preferences, habits, corrections
- `environment`: OS, installed tools, project structure
- `tool`: API quirks, workflow conventions
- `convention`: Project-specific patterns

### 1.3 Memory Injection Logic

**Priority-based injection**:
1. High-priority memories (priority >= 8) always injected
2. Medium (5-7): Inject up to 10 most recently accessed
3. Low (1-4): Inject only if space permits

**Memory format in prompt**:
```
# [MEMORY]

## User Preferences
- User prefers concise responses
- User hates markdown formatting

## Environment
- Running on WSL (Windows Subsystem for Linux)
- Project uses pytest with xdist

## Tool Quirks
- Supabase .in_() query requires list of strings
```

### 1.4 Memory Management Rules

**When to save**:
- User corrects you: "Remember this", "Don't do that again"
- User shares preference: "I like...", "I prefer..."
- Discover environment fact: OS, tools, project structure
- Learn convention/API quirk

**What NOT to save**:
- Task progress, session outcomes, TODO state
- Raw data dumps, trivial/obvious info
- Temporary state

**Memory compaction**:
- Automatic merging of similar memories
- Deprecation based on last_accessed timestamp
- Priority adjustment based on usage frequency

---

## Phase 2: Skill System

### 2.1 Skill Schema

**New `skills` table**:
```sql
CREATE TABLE skills (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL,
  name text NOT NULL,          -- Lowercase, hyphenated (e.g., 'code-review')
  category text,               -- 'devops', 'data-science', 'mlops'
  trigger_conditions text[],   -- Keywords/phrases that trigger this skill
  content text NOT NULL,       -- SKILL.md format (YAML frontmatter + markdown)
  usage_count integer DEFAULT 0,
  last_used timestamptz DEFAULT now(),
  success_rate float DEFAULT 1.0,  -- Track success/failure
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  is_active boolean DEFAULT true
);
```

**SKILL.md Format**:
```yaml
---
name: code-review
description: Review pull requests with security and quality gates
category: github
trigger:
  - "review this PR"
  - "check the diff"
  - "code review"
toolsets: ['terminal', 'file', 'web']
---
## Steps
1. Fetch PR diff using `gh pr diff`
2. Run security scan with bandit/semgrep
3. Check for common pitfalls (SQL injection, XSS)
4. Verify tests pass
5. Leave inline comments

## Pitfalls
- Don't approve without security scan
- Watch for hardcoded secrets
- Check migration rollback safety
```

### 2.2 Skill CRUD API

**Endpoints**:
- `GET /api/v1/skills`: List skills with filtering
- `POST /api/v1/skills`: Create new skill
- `PATCH /api/v1/skills/{id}`: Update skill
- `DELETE /api/v1/skills/{id}`: Soft delete
- `POST /api/v1/skills/{id}/execute`: Execute skill (returns plan)

### 2.3 Skill Auto-Detection

**When to create skill**:
- Task completes with 5+ tool calls
- Iterative debugging resolves complex issue
- User-corrected approach works
- Non-trivial workflow discovered

**Skill creation flow**:
1. After task completion, analyze transcript
2. Extract steps, commands, pitfalls
3. Offer to save as skill: "Would you like me to save this as a reusable skill?"
4. If yes, create SKILL.md with proper formatting

### 2.4 Skill Loading & Execution

**Before each agent response**:
1. Scan user message for trigger keywords
2. Load matching skills from database
3. Inject skill content into system prompt
4. Agent follows skill instructions

**Skill execution modes**:
1. **Advisory**: Skill content injected as context
2. **Automatic**: Agent automatically executes skill steps
3. **Hybrid**: Agent uses skill as guide but can adapt

---

## Phase 3: Tool System

### 3.1 Tool Registry

**Tool categories**:
- `terminal`: Shell commands (with safety confirmation)
- `file`: Read/write/search files
- `web`: Web search, browser automation
- `database`: Supabase queries
- `code`: Code execution, debugging
- `delegation`: Spawn subagents

**Tool definition format**:
```python
class Tool:
    name: str
    description: str
    parameters: dict
    function: callable
    dangerous: bool = False
    requires_confirmation: bool = False
```

### 3.2 Agent Loop Enhancement

**Enhanced loop structure**:
```
User Input → LLM → Tool Call? → Execute Tool → LLM → ...
                  ↓ (no)
              Final Response
```

**Routing decisions**:
- Use tool if user requests specific action
- Use tool if skill requires it
- Otherwise, respond directly

### 3.3 Safety & Confirmation

**Dangerous operations** require explicit user confirmation:
- File deletion
- System commands (rm, mv, etc.)
- Database writes
- API calls with side effects

**Confirmation flow**:
1. Agent proposes action: "I need to delete file X. Confirm?"
2. User confirms via API response field
3. Tool executes

### 3.4 Tool Documentation

**Auto-generated tool descriptions** injected into system prompt:
```
# [TOOLS]

## terminal
Execute shell commands. Dangerous commands require confirmation.

## file
Read, write, search files. Paths are relative to project root.

## web_search
Search the web via DuckDuckGo. Use for current information.
```

---

## Phase 4: Session Search & Recall

### 4.1 Search Schema

**Enhanced `messages` table**:
- Add `embedding` vector(384) for semantic search
- Add `summary` text (LLM-generated summary of message)

**New `session_summaries` table**:
```sql
CREATE TABLE session_summaries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL,
  session_id text NOT NULL,
  summary text NOT NULL,
  tags text[] DEFAULT '{}',
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);
```

### 4.2 Search API

**Endpoints**:
- `GET /api/v1/search/sessions`: Search session summaries
- `GET /api/v1/search/messages`: Semantic search in messages
- `GET /api/v1/search/context`: Search across all context (documents, memories, skills)

**Search types**:
- **Keyword**: Simple text search
- **Semantic**: Vector similarity search
- **Hybrid**: Combine both with RRF

### 4.3 Recall Logic

**When to search**:
- User says "we did this before", "last time", "as I mentioned"
- User references familiar topic not in current context
- Agent needs similar past solutions

**Search injection**:
- Top 3 relevant past sessions injected as context
- Format: "[PAST SESSION: <date>] <summary>"

---

## Phase 5: Autonomous Operations

### 5.1 Cron Job System

**New `cron_jobs` table**:
```sql
CREATE TABLE cron_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL,
  name text NOT NULL,
  schedule text NOT NULL,  -- '30m', 'every 2h', '0 9 * * *'
  prompt text NOT NULL,
  skills text[],          -- Skill IDs to load
  enabled boolean DEFAULT true,
  last_run timestamptz,
  next_run timestamptz,
  created_at timestamptz DEFAULT now()
);
```

### 5.2 Background Tasks

**Task queue** for long-running operations:
- Web scraping
- Data processing
- Model training
- Report generation

**Notification system**:
- Webhook callbacks
- Email notifications
- Database status updates

### 5.3 Health Monitoring

**Agent self-monitoring**:
- Tool failure tracking
- LLM cost/usage metrics
- Error rate monitoring
- Performance benchmarks

---

## Implementation Roadmap

### Week 1: Foundation
1. Set up enhanced database schema
2. Implement memory CRUD API
3. Add memory injection to system prompt
4. Test with current agent

### Week 2: Skills & Tools
1. Implement skills table and CRUD
2. Add tool registry with basic tools (terminal, file)
3. Enhance agent loop for tool calling
4. Implement skill auto-detection

### Week 3: Search & Recall
1. Add vector embeddings to messages
2. Implement session search API
3. Add recall injection logic
4. Test cross-session memory

### Week 4: Autonomous Features
1. Implement cron job system
2. Add background task queue
3. Build health monitoring
4. End-to-end testing

### Week 5: Polish & Optimization
1. Performance optimization
2. Error handling improvement
3. Documentation
4. Deployment preparation

---

## Database Migration Strategy

**Sequential migrations**:
1. `002_add_memories_table.sql`
2. `003_add_skills_table.sql`
3. `004_add_tool_registry.sql`
4. `005_add_vector_embeddings.sql`
5. `006_add_cron_jobs.sql`

**Backward compatibility**:
- Keep existing API endpoints working
- Gradual migration of data
- Feature flags for new functionality

---

## Testing Strategy

### Unit Tests
- Memory CRUD operations
- Skill matching logic
- Tool execution safety
- Search relevance

### Integration Tests
- Full agent workflow with tools
- Skill auto-creation flow
- Cross-session memory recall
- Cron job scheduling

### Load Tests
- Concurrent chat sessions
- Memory injection performance
- Vector search latency
- Tool execution concurrency

---

## Success Metrics

1. **Memory effectiveness**: Reduction in user repetitions
2. **Skill reuse**: Frequency of skill auto-loading
3. **Tool accuracy**: Success rate of tool executions
4. **Recall relevance**: Search result precision
5. **Autonomy**: Percentage of tasks completed without user intervention

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Tool safety issues | Confirmation prompts, sandboxed execution |
| Memory pollution | Regular compaction, user review |
| Skill overfitting | Success rate tracking, skill versioning |
| Performance degradation | Caching, lazy loading, query optimization |
| LLM cost escalation | Usage limits, cost monitoring |

---

## Next Steps

1. **Test current implementation**: Set up Supabase, run app, verify basic chat works
2. **Review spec**: Provide feedback on architecture decisions
3. **Prioritize phases**: Decide which features to implement first
4. **Begin implementation**: Start with Phase 1 (Memory System)

---
*Last updated: $(date)*