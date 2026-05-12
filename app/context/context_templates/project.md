---
context_type: project
title: WebAgent Project Context
tags: [project, webagent, codebase]
---

## Project structure

```
webAgent/
├── app/
│   ├── api/              # FastAPI routers (chat, uploads, admin)
│   ├── agent/            # Agent loop, prompts, session history
│   ├── db/               # Storage backends (local SQLite, Supabase)
│   ├── tools/            # Tool loading, core tools, browser
│   ├── admin/            # Optional admin tools (source mgmt, settings)
│   ├── visualizer/       # AutoAgent p5.js creative coding
│   └── context/          # NEW: .md file context seeder
├── context/
│   └── context_templates/ # Markdown files → context documents
├── ui/                   # Single-page browser UI
├── uploads/              # User-uploaded files
└── visuals/              # AutoAgent rendered sketches
```

## Architecture

- **FastAPI** server with dual storage (local SQLite or Supabase)
- **Agent loop** streams LLM responses with tool-calling (OpenRouter)
- **Per-user sessions** — each user messages persists across browser refreshes
- **Context** is loaded from `context_documents` DB table per agent
- **Memory** is a hybrid FTS5 + vector search brain

## Key endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/chat` | Buffered chat (returns full reply) |
| `POST /api/v1/chat/stream` | SSE streaming chat |
| `POST /api/v1/upload` | File upload |
| `GET /index.html` | Web UI |
