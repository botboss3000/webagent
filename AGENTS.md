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

- Copy `.env.example` → `.env` and set `OPENROUTER_API_KEY` for LLM functionality.
- Without the API key, the server starts and all non-LLM endpoints work (health, upload, DB viewer, UI serving). The chat endpoint will return a 401 error from OpenRouter.
- The app auto-creates `app/db/local.db` (SQLite) on first request — no manual migration needed in local mode.

### Gotchas

- The chat API requires a session row to exist in the `sessions` table before messages can be sent. The WebSocket agent (`/api/v1/agent/ws`) auto-creates sessions, but the HTTP chat endpoint (`POST /api/v1/chat`) does not — you must insert a session first or use the UI which handles this.
- The `--reload` flag in uvicorn watches the `app/` directory. If you install new packages, the server hot-reloads automatically but won't pick up new top-level imports until restarted.
- `playwright install` is needed only if testing the browser automation tool; it is not required for general development.
