# webAgent

A FastAPI agent application with tool-calling support and Supabase persistence.

## Features

- **Chat endpoint** that maintains session‑specific message history.
- **WebSocket streaming** for real-time agent responses and tool call visibility.
- **Context‑aware system prompts** assembled from `public.context` rows (agent, user, skills, tools, tasks).
- **Tool-calling agent loop** using OpenRouter with DeepSeek V3.2 (or any OpenRouter model).
- **Supabase Postgres** for `sessions`, `messages`, and `context` (same schema as the Web Portal migrations).
- **Dev‑friendly** with a `/seed‑docs` endpoint to bootstrap test data.

## Project Structure

```
webagent/
├── app/
│   ├── main.py              # FastAPI app entrypoint
│   ├── api/
│   │   ├── chat.py          # POST /chat route
│   │   ├── agent.py         # WebSocket streaming endpoint
│   │   └── terminal.py      # Web terminal interface
│   ├── agent/
│   │   ├── loop.py          # Simple tool-calling agent loop
│   │   ├── streaming_loop.py # Streaming agent loop (events)
│   │   └── prompts.py       # System prompt assembly
│   ├── db/
│   │   └── supabase.py      # Supabase client + all DB operations
│   ├── tools/
│   │   ├── loader.py        # Dynamic tool loading
│   │   ├── registry.py      # Tool registry
│   │   └── tracker.py       # Tool execution tracking
│   ├── models/
│   │   └── schemas.py       # Pydantic request/response models
│   └── admin/
│       └── review.py        # Admin review routes
├── .env.example
├── requirements.txt
└── README.md
```

## Environment Variables

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_MODEL=deepseek/deepseek-v3.2
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
```

## Installation

1. Clone the repository and enter the directory.

2. Create a virtual environment and install dependencies:

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

3. Set up Supabase (shared with the Web Portal):

   - Create a new project at [supabase.com](https://supabase.com) or use an existing one.
   - Apply the single migration [`Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`](../Web%20Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql) (SQL Editor or `supabase db push` from `Web Portal`). See [`Web Portal/supabase/NEW_PROJECT_CHECKLIST.md`](../Web%20Portal/supabase/NEW_PROJECT_CHECKLIST.md) for env vars when pointing at a new project.
   - Copy your project URL and **service role** key from **Project Settings → API** into `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.

4. Run the application:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`. Interactive Swagger docs at `http://localhost:8000/docs`.

## Seeding Test Documents

Once the app is running, you can insert placeholder rows into `public.context` for a user UUID that already exists in `public.users` (e.g. your Supabase auth user id):

```bash
curl -X POST "http://localhost:8000/api/v1/seed-docs?user_id=YOUR_AUTH_USER_UUID"
```

This creates five rows with `context_type`: `agent`, `user`, `skills`, `tools`, and `tasks`.

## Using the Chat Endpoint

**POST `/api/v1/chat`**

Request body (minimal):

```json
{
  "user_id": "auth-user-uuid",
  "session_id": "session-uuid",
  "message": "What can you help me with?"
}
```

Optional fields (sent by the Web Portal): `documents` (array of `{ "doc_type", "title", "content" }`), `history` (array of `{ "role", "content" }`). When provided, the agent uses them instead of loading from the database for that turn.

Example curl:

```bash
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "session_id": "session_456",
    "message": "What can you help me with?"
  }'
```

Response (includes `response` for the Next.js client):

```json
{
  "reply": "...",
  "response": "...",
  "session_id": "session-uuid"
}
```

Each turn saves the user message and assistant reply to **`public.messages`** for the given `session_id` (session must exist in **`public.sessions`** and belong to `user_id`).

## Adding Custom Documents

You can insert rows via Supabase’s Table Editor into **`public.context`**. The chat endpoint loads rows whose **`context_type`** is one of:

- `agent`
- `user`
- `skills`
- `tools`
- `tasks`

(Optional: forward additional slices from the portal as `documents` with a `doc_type` of `memory` for prompt assembly in that request only.)

## Quick Test Interface

For immediate testing without building a frontend, use the included HTML interface:

1. Start the backend server:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

2. Open `test_interface.html` in your browser (double-click or `open test_interface.html`)

3. Configure the API URL (default: `http://localhost:8000/api/v1/chat`), User ID, and Session ID

4. Click "Seed Test Documents" to populate the database with sample context

5. Start chatting!

The interface will display your conversation and handle all API calls to the webAgent backend.

## Deployment

The application is ready to be deployed on any platform that supports Python (e.g., Railway, Render, Fly.io, Docker). Ensure environment variables are set in production.

## License

MIT