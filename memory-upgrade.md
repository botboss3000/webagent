# Memory System Upgrade Plan

Living doc for enhancing webAgent's memory/brain system. Based on comparison against gbrain (Garry Tan) and OB1/Open Brain (Nate B. Jones).

See: [gbrain](https://github.com/garrytan/gbrain) · [OB1](https://github.com/NateBJones-Projects/OB1) · [`app/db/interface.py`](app/db/interface.py) · [`app/db/local.py`](app/db/local.py)

---

## Current Architecture (baseline)

- **Backend:** SQLite (local) or Supabase (cloud, memory methods mostly stubs)
- **Tables:** `memories`, `memory_chunks`, `memory_links`, `memory_timeline`
- **Retrieval:** FTS5 keyword search only — no embeddings, no vector search
- **Knowledge graph:** Typed edges via `memory_links`, BFS traversal in Python (SQLite lacks recursive CTEs)
- **Pipeline:** `memory_search` called pre-chat → results injected as `[BRAIN CONTEXT]` in system prompt
- **Internal tools:** `memory_search`, `memory_save` (omitted from model transcript)

---

## Priority 1 — Vector Embeddings + Hybrid Search

**Design Decision (2026-05-08):**

| Choice | Selected | Why |
|--------|----------|-----|
| Embed source | **OpenRouter embed API** (text-embedding-3-small) | No local ML model, no new deps, uses existing OpenRouter client. Cheaper than LLM rewrite (~$0.11/mo vs ~$10/mo at 1k users) |
| Search engine | **In-memory numpy cosine similarity** | Brute-force fine for <10k pages. No SQLite extension needed. Zero new deps (numpy already in env) |
| Memory gate | **Regex skip before memory_search** | Skip trivial messages (greetings, commands, affirmations). Saves latency + cost on ~40% of messages |

### Skip gate

Before `memory_search` runs, check message against regex patterns. If matched → skip entirely (no embed, no FTS, no DB call). Brain context = None.

Patterns:
- Greetings: "hi", "hello", "hey", "good morning", etc.
- Affirmations: "ok", "thanks", "got it", "cool", "sure"
- Tool commands: "check my email", "list my files", "show my messages"
- Permissions: "yes", "go ahead", "keep going", "continue", "next"
- Short follow-ups: "and?", "more", "elaborate"

### Embed + search flow (non-skipped messages)

```
User sends message
       │
  ┌────┴────┐
  │ Gate    │→ matched → brain_context = None (no memory call)
  └────┬────┘
       │ unmatched
       v
  ┌────────────────┐
  │ FTS5 search    │  (existing, stays)
  │ (SQLite MATCH) │
  └───────┬────────┘
          │
  ┌────────────────┐
  │ OpenRouter     │  (parallel with FTS5)
  │ embed API      │  text-embedding-3-small, 1536-dim
  │ ~100-300ms     │  ~$0.00001/query
  └───────┬────────┘
          │
  ┌────────────────┐
  │ Numpy cosine   │  dot product against stored embeddings
  │ ~5ms @ 10k     │  loaded from memory_chunks table
  └───────┬────────┘
          │
  ┌──────────────────────┐
  │ RRF merge            │  Reciprocal Rank Fusion: FTS5 rank + vector distance
  │ Return top 5 pages   │  formatted as [BRAIN CONTEXT] section
  └──────────────────────┘
```

### Schema changes

```sql
ALTER TABLE memory_chunks ADD COLUMN embedding BLOB;  -- F32 bytes, 1536-dim = 6144 bytes per chunk
```

### Implementation steps

1. **Skip gate** — Add regex check in `app/api/chat.py` before `memory_search_start` emit. 15 lines.
2. **Embed on write** — In `memory_upsert`, after saving compiled_truth, call OpenRouter embed API, store result as BLOB in `memory_chunks.embedding`.
3. **Vector search method** — Add `_vector_search(query_embedding, limit)` to `LocalBackend`. Loads all embeddings from chunks → numpy matrix → cosine sim → top-k.
4. **Hybrid search** — Rewrite `memory_search` to run FTS5 + vector in parallel, merge via RRF.
5. **Backfill** — One-off script `scripts/backfill_embeddings.py`: iterate existing memory chunks, embed, update.

### Cost at 1k users (12k non-skipped searches/day)

| Item | Cost | Notes |
|------|------|-------|
| Embed API | ~$0.11/mo | text-embedding-3-small at $0.02/1M tokens |
| Backfill one-time | ~$0.004 | For existing pages |
| Latency added | +100-300ms | Per non-skipped message (vs 1-30s LLM call) |
| New deps | None | Uses existing openai client + numpy |

### Status
- [x] Skip gate
- [x] Embed on write
- [x] Vector search method
- [x] Hybrid RRF merge
- [x] Backfill script

---

## Priority 2 — Auto-Linking on Write

**gbrain's killer feature: every write auto-extracts entity refs and creates typed links. Zero LLM calls.**

### Scope
- On every `memory_upsert`, scan compiled_truth + timeline for known entity references (people, companies, concepts)
- Auto-create `memory_links` entries (typed edges: `mentions`, `related_to`, `works_at`, `invested_in`, `founded`)
- Deterministic matching (regex/pattern) — NOT LLM — keeps it fast and cheap
- OR use agent to extract links after write (slower, richer)

### Implementation steps
1. Build entity index from existing memory slugs + titles
2. Add `resolve_entity_references(text) -> List[str]` utility
3. Wire into `memory_upsert` post-commit hook
4. Add automatic `related_to` linking between pages that share entity mentions
5. Backfill links for existing pages

### Status
- [ ] Not started

---

## Priority 3 — Enrichment Tiers

**gbrain auto-escalates pages from stub → enriched based on mention frequency. Brain self-prioritizes.**

### Scope
- Add `mention_count` and `enrichment_tier` to `memories` table
- Tiers:
  - **Tier 3** (stub): 1 mention — just slug + title + first mention context
  - **Tier 2** (enriched): 3+ mentions across sources — web/social enrichment auto-triggered
  - **Tier 1** (full): 8+ or after a meeting — full pipeline with research
- Track mention count by incrementing on every `memory_search` hit or every `memory_save` that references the slug

### Implementation steps
1. Add columns + migration
2. Increment mentions on reference
3. Build enrichment dispatch function (Tier 2 → enrich, Tier 1 → full)
4. Wire into post-chat background task

### Status
- [ ] Not started

---

## Priority 4 — Background Maintenance

**Prevents "brain rot": stale pages, orphans, dead links, citation rot.**

### Scope
- Periodic maintenance task (cron or post-chat bg)
- Checks:
  - **Stale pages:** pages not touched in N days → flag for review
  - **Orphan pages:** pages with no incoming links → flag or merge
  - **Broken citations:** links to slugs that no longer exist
  - **Citation format audit:** missing or malformed citations
  - **Duplicate pages:** near-duplicate detection (cosine sim on embeddings, once Priority 1 done)

### Implementation steps
1. Build `maintenance_check() -> List[Finding]` in `StorageBackend`
2. Render findings as a brain page or report
3. Schedule via cron or post-chat background task
4. Add auto-fix for simple cases (dead links → remove, orphans → tag)

### Status
- [ ] Not started

---

## Priority 5 — MCP Server

**Expose memory as standard protocol so other AI tools (Claude Code, Cursor, ChatGPT) can query webAgent's brain.**

### Scope
- Build MCP stdio server exposing tools:
  - `memory_search(query, limit)` — hybrid search
  - `memory_get(slug)` — full page
  - `memory_upsert(slug, type, title, truth, timeline)` — write
  - `memory_graph_query(node_slug, link_type, direction, depth)` — graph traversal
  - `memory_search_sessions(query, limit)` — session search
- Register in MCP client config (Claude Code, Cursor, etc.)

### Implementation steps
1. Add MCP Python SDK dependency (or implement stdio protocol manually)
2. Create `app/mcp_server.py`
3. Wire to existing `StorageBackend` methods
4. Document client setup

### Status
- [ ] Not started

---

## Priority 6 — Evaluations

**Without evals, can't tell if a change improves or degrades retrieval.**

### Scope
- Capture real query + result pairs from production
- Replay against code changes, compute:
  - Mean Jaccard@k between captured and current results
  - Top-1 stability
  - Latency Δ
- Modeled on gbrain's BrainBench + LongMemEval

### Implementation steps
1. Add `eval_candidates` table (query, captured slugs, timestamp, user_id)
2. Opt-in capture mode (off by default)
3. `memory eval replay` — runs current search against captured queries, computes metrics
4. CI gate: fail if recall drops below threshold

### Status
- [ ] Not started

---

## Stretch / Future

### Overnight Dream Cycle
- gbrain consolidates conversation transcripts into reflections, patterns, and insights overnight
- Requires: session summary → pattern extraction → brain page synthesis
- Overkill for most use cases, but powerful for long-term users

### Code Indexing
- gbrain indexes repos as call graphs (code-callers, code-callees, code-refs)
- WebAgent could index its own source or user repos
- Would need language parsers

### Remote OAuth Access
- gbrain serves HTTP with OAuth 2.1 — any AI client connects
- Useful if webAgent's brain should be a shared infrastructure layer

### Minion Job System
- gbrain's deterministic background job queue (Postgres-native, 753ms vs 10s+ for sub-agents)
- WebAgent already has async background tasks, but no durable job queue

---

## Implementation Notes

### Schema migration pattern
Add columns via `ALTER TABLE` with IF NOT EXISTS guards in `app/db/local.py` init. For new tables, add to schema init block. Migration version tracked in a `_schema_version` pragma or separate table.

### Backfill strategy
One-off scripts in `scripts/` dir. Idempotent (check before write). Dry-run flag.

### Testing approach
- Unit: mock `StorageBackend`, test each new method in isolation
- Integration: temp SQLite file, run CRUD + search, verify results
- Eval: captured query/result pairs — regression test suite

### Latency budget

| Step | Time (non-skipped) |
|------|--------------------|
| Regex gate | <1ms |
| FTS5 search | <1ms |
| OpenRouter embed API | +100-300ms |
| Numpy cosine | ~5ms (10k chunks) |
| RRF merge | <1ms |
| **Total added** | **~105-305ms** |
| LLM API call (bottleneck) | 1-30s |
| **User-visible delay** | **~1-2% increase** |

---

## Progress Log

| Date | Change | Status |
|------|--------|--------|
| 2026-05-08 | File created | Initial |
| 2026-05-08 | Priority 1 redesigned: skip gate + OpenRouter embed API + numpy cosine. No local ML. | Updated |
| 2026-05-08 | P1 implemented: `app/agent/embed.py`, `app/db/local.py` (vector search + hybrid merge + embed on write), `app/api/chat.py` (skip gate), `scripts/backfill_embeddings.py` | Done |
| 2026-05-08 | Embed client updated to use same provider config as chat (LLM_API_KEY/LLM_BASE_URL env vars, set by web UI). No separate .env key. | Done |
| 2026-05-08 | DB will be reset — no backfill needed. New pages auto-embed on write. | Noted |

**Issues / discoveries / design decisions appended below as we work through each priority.**

### 2026-05-08 — Multi-model routing (future) + current embed provider note

**Embedding requires a provider that supports OpenAI-compatible `/embeddings` endpoint.** OpenRouter: yes (text-embedding-3-small, 1536-dim). DeepInfra: their embed models are different dimensions (e.g. BAAI/bge-base-en-v1.5 is 768-dim, incompatible with hardcoded EMBED_DIM=1536). Currently hardcoded to `text-embedding-3-small`. If embed API call fails, vector search silently falls back to FTS5-only (already implemented in `_vector_search`).

The web UI Settings modal currently selects ONE model for everything (chat). Future advancement: separate model slots for different tasks:

| Slot | Current | Future |
|------|---------|--------|
| Chat model | LLM_MODEL | LLM_MODEL |
| Embed model | hardcoded `text-embedding-3-small` | EMBED_MODEL (UI-selectable) |
| Cheap rewrite model | — | REWRITE_MODEL (for P2 auto-linking, P3 enrichment) |
| Gatekeeper model | — | GATE_MODEL (for guardrails/validation) |

All share the same provider (`LLM_BASE_URL`) and API key (`LLM_API_KEY`) — just different model names on the same provider. Embed endpoint must be supported by the chosen provider (OpenRouter: yes, DeepInfra: check). If embed fails, vector search silently falls back to FTS5-only.

### 2026-05-08 — DB reset

Database will be reset before testing P1. No need to run the `scripts/backfill_embeddings.py` migration. All new memory pages auto-embed on write via `memory_upsert()`. The backfill script remains as a utility for future use if the DB is NOT reset.

---

## Design Decisions

### 2026-05-08 — Priority 1 embed source

- **Chosen:** OpenRouter embed API (text-embedding-3-small)
- **Rejected:** Local ML model (fastembed/sentence-transformers) — adds 80MB RAM, PyTorch dep, cold start lag on Cloud Run
- **Rejected:** LLM query rewrite — cheaper and better quality to use embeddings directly ($0.11/mo vs ~$10/mo at scale)
- **Rejected:** sqlite-vec extension — adds DLL dependency, overkill for <10k pages. Numpy brute-force fine.
- **Rationale:** Zero new deps beyond what's already in the env. Uses existing OpenRouter API key and client. No server-side ML runtime. Same cost profile whether hosted on bare metal, Docker, or Cloud Run.

### 2026-05-08 — Memory skip gate

- **Chosen:** Regex-based pre-check in chat.py before memory_search call
- **Rationale:** ~40% of messages are trivial (greetings, affirmations, commands). No reason to burn latency or $ on them. The gate is deterministic, zero cost, zero maintenance.
- **Not chosen:** ML classifier for skip — overkill for a yes/no decision on obvious patterns
