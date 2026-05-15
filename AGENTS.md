# AGENTS.md

## Cursor Cloud specific instructions

### Overview

webAgent is a FastAPI backend with a vanilla JS UI. It uses SQLite (local mode) by default — no external DB or Docker required for development.

### Running the dev server

```bash
.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Key URLs: `/health`, `/index.html` (UI), `/docs` (Swagger), `/test` (minimal tester).

### Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

### Environment

- Copy `.env.example` → `.env` (no secrets required in the file — defaults work for local dev).
- The LLM provider and API key are configured **through the app UI** (Settings panel) or via `POST /admin/settings/provider`. The key is persisted in `provider.json` at the project root and applied at runtime — no environment variable needed.
- The app auto-creates `app/db/local.db` (SQLite) on first request — no manual migration needed in local mode.

### Gotchas

- The chat API requires a session row to exist in the `sessions` table before messages can be sent. The WebSocket agent (`/api/v1/agent/ws`) auto-creates sessions, but the HTTP chat endpoint (`POST /api/v1/chat`) does not — you must insert a session first or use the UI which handles this.
- The `--reload` flag in uvicorn watches the `app/` directory. If you install new packages, the server hot-reloads automatically but won't pick up new top-level imports until restarted.
- `playwright install` is needed only if testing the browser automation tool; it is not required for general development.
