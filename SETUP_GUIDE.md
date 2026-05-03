# webAgent - Complete Setup Guide

## What I've Done For You

1. **Updated the agent to use OpenRouter** instead of Anthropic
   - Modified `app/agent/graph.py` to use `ChatOpenAI` with OpenRouter's API endpoint
   - Added your OpenRouter API key to `.env` file
   - Set model to `deepseek/deepseek-v3.2`

2. **Updated configuration files:**
   - `.env.example` - Updated with OpenRouter variables
   - `.env` - Created with your actual API key
   - `requirements.txt` - Updated with agent loop dependencies

3. **Added testing tools:**
   - `test_interface.html` - Web interface for testing without coding
   - `test_openrouter.py` - Direct OpenRouter API test
   - `integration_test.py` - Full system integration test

4. **Updated documentation** in README.md

## How to Get Your Web App to Use This Python Agent

### Step 1: Set Up the Backend (Python Agent)

```bash
cd webagent

# Install dependencies
pip install -r requirements.txt

# Start the FastAPI server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Your API will be available at `http://localhost:8000`

### Step 2: Set Up Supabase (Database)

1. Go to [supabase.com](https://supabase.com) and create a free project
2. In the SQL Editor, run `Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`
3. Copy your `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from Project Settings → API
4. Add them to your `.env` file

### Step 3: Test the Agent

```bash
# Seed test documents
curl -X POST "http://localhost:8000/api/v1/seed-docs?user_id=test_user"

# Test chat endpoint
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test_user",
    "session_id": "session_123",
    "message": "Hello!"
  }'
```

### Step 4: Connect Your Web App

Your web app can now communicate with the webAgent API:

**JavaScript Example:**
```javascript
async function chatWithBot(userId, sessionId, message) {
  const response = await fetch('http://localhost:8000/api/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user_id: userId,
      session_id: sessionId,
      message: message
    })
  });
  return await response.json();
}

// Usage
chatWithBot('user_123', 'session_456', 'Hello, webAgent!')
  .then(data => console.log('Bot:', data.reply));
```

**React Example:**
```jsx
function ChatComponent() {
  const [messages, setMessages] = useState([]);
  
  const sendMessage = async (text) => {
    const response = await fetch('http://localhost:8000/api/v1/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: 'user_123',
        session_id: 'session_456',
        message: text
      })
    });
    const data = await response.json();
    setMessages(prev => [...prev, 
      { role: 'user', content: text },
      { role: 'assistant', content: data.reply }
    ]);
  };
  
  return <div>{/* Your UI */}</div>;
}
```

## API Key Storage

Your OpenRouter API key is securely stored in:
- **`.env` file** - Local development (never committed to Git)
- **Environment variables** - Production deployment

**Never expose your API key in frontend code!** The key stays in the backend.

## Customizing the Agent

### Change Model:
Edit `.env`:
```bash
OPENROUTER_MODEL=openai/gpt-4o-mini  # Or any OpenRouter model
```

### Adjust Parameters:
Edit `app/agent/streaming_loop.py` (or `app/agent/loop.py`):
```python
model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
temperature=0.0,
max_tokens=4096,
```

### Add More Context:
Insert rows into Supabase **`public.context`** with **`context_type`**:
- `agent`, `user`, `skills`, `tools`, `tasks` (Web Portal schema; see `Web Portal/supabase/migrations/`)

## Quick Start

1. **Install:** `pip install -r requirements.txt`
2. **Configure:** Update `.env` with Supabase credentials
3. **Run:** `uvicorn app.main:app --reload`
4. **Test:** Open `test_interface.html` in browser
5. **Integrate:** Connect your web app to `http://localhost:8000/api/v1/chat`

## Deployment

Deploy to:
- **Railway**: `railway up`
- **Render**: Connect GitHub repo
- **Docker**: `docker build -t webagent .`

Set the same environment variables in your hosting platform.

## Files Created/Modified

- `.env` - Your OpenRouter API key
- `.env.example` - Updated template
- `app/agent/loop.py` - Agent loop implementation
- `app/agent/streaming_loop.py` - Streaming agent events
- `requirements.txt` - Updated dependencies
- `README.md` - Updated instructions
- `test_interface.html` - Web test interface
- `test_openrouter.py` - Direct API test
- `test_config.py` - Configuration test
- `integration_test.py` - Full system test

Your webAgent agent is now ready to power your web applications! 🚀