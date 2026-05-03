# webAgent: Testing Current Implementation

> **Schema note:** The app uses the Web Portal Postgres schema (`sessions`, `messages`, `context`, …). Some steps below still mention legacy `documents` / `user_id` on messages; treat those as outdated unless you are on an old database.

## Prerequisites

1. **Python 3.11+** (3.12 recommended)
2. **Supabase account** (free tier: https://supabase.com)
3. **Anthropic API key** (Claude Sonnet/Opus)
4. **Git** (optional)

## Step 1: Environment Setup

### 1.1 Create Virtual Environment
```bash
cd /home/alex_r/projects/webagent
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 1.2 Install Dependencies
```bash
pip install -r requirements.txt
```

**Verify installation**:
```bash
python verify.py
```
Should show all imports successful (except missing env vars).

### 1.3 Configure Environment Variables
Copy the example file and edit:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here
SUPABASE_URL=your_supabase_project_url
SUPABASE_SERVICE_ROLE_KEY=your_supabase_service_role_key
CLAUDE_MODEL=claude-sonnet-4-20250514
```

**Where to find these values**:

1. **Anthropic API Key**: https://console.anthropic.com/
2. **Supabase URL & Service Role Key**:
   - Create new project at https://supabase.com
   - Go to **Project Settings → API**
   - Copy "Project URL" (SUPABASE_URL)
   - Copy "service_role" secret (SUPABASE_SERVICE_ROLE_KEY)

## Step 2: Database Setup

### 2.1 Create Supabase Tables
1. In Supabase dashboard, go to **SQL Editor**
2. Create a new query
3. Run the SQL in `Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`
4. Run the query

**Verify tables created**:
- Go to **Table Editor**
- You should see `messages` and `documents` tables

### 2.2 (Optional) Enable Row Level Security (RLS)
For production, enable RLS on both tables:
```sql
-- Enable RLS on messages
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- Create policy (example: users can only see their own messages)
CREATE POLICY "Users can view own messages" ON messages
  FOR SELECT USING (auth.uid()::text = user_id);

CREATE POLICY "Users can insert own messages" ON messages
  FOR INSERT WITH CHECK (auth.uid()::text = user_id);

-- Repeat for documents table
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
-- ... add policies
```

## Step 3: Run the Application

### 3.1 Start the FastAPI Server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Expected output**:
```
INFO:     Will watch for changes in these directories: [...]
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### 3.2 Verify API is Running
Open browser or use curl:
```bash
curl http://localhost:8000/
```
Should return: `{"message":"Welcome to webAgent API","docs":"/docs"}`

**Interactive docs**: http://localhost:8000/docs

## Step 4: Seed Test Data

### 4.1 Insert Placeholder Documents
```bash
curl -X POST "http://localhost:8000/api/v1/seed-docs?user_id=user_123"
```

**Expected response**:
```json
{
  "message": "Inserted 5 placeholder documents for user user_123",
  "document_ids": ["...", "..."]
}
```

### 4.2 Verify Documents in Supabase
Go to Supabase Table Editor → `documents` table
Should see 5 rows with doc_types: agent, user_profile, skills, tools, jobs

## Step 5: Test Chat Endpoint

### 5.1 Basic Chat Test
```bash
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "session_id": "session_456",
    "message": "What can you help me with?"
  }'
```

**Expected response**:
```json
{
  "reply": "I'm webAgent, an AI assistant specialized in managing and coordinating other AI agents...",
  "session_id": "session_456"
}
```

### 5.2 Verify Message History
Check Supabase `messages` table:
- Should have 2 new rows (user message + assistant reply)
- `user_id`: "user_123", `session_id`: "session_456"

### 5.3 Continue Conversation
```bash
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "session_id": "session_456",
    "message": "Tell me more about your skills"
  }'
```

**Expected**: Response references skills from context documents.

## Step 6: Test Context Documents

### 6.1 Add Custom Document
Via Supabase Table Editor, insert a new document:
```sql
INSERT INTO documents (user_id, doc_type, title, content)
VALUES (
  'user_123',
  'memory',
  'User Preference',
  'The user prefers responses in bullet points, not paragraphs.'
);
```

### 6.2 Test with New Context
```bash
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "session_id": "session_456",
    "message": "What is my preference for responses?"
  }'
```

**Expected**: Response should mention bullet points preference.

## Step 7: Error Handling Tests

### 7.1 Missing Environment Variables
```bash
# Temporarily rename .env
mv .env .env.bak
# Restart server - should fail with clear error
```

### 7.2 Invalid API Key
```bash
# Edit .env with wrong key
ANTHROPIC_API_KEY=invalid_key
# Chat should return 500 error
```

### 7.3 Missing User Documents
```bash
# Test with non-existent user_id
curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_999",
    "session_id": "session_999",
    "message": "Hello"
  }'
```

**Expected**: Response still works (just no context documents).

## Step 8: Performance Testing

### 8.1 Response Time
```bash
time curl -X POST "http://localhost:8000/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "session_id": "session_456",
    "message": "Test"
  }'
```

**Target**: < 5 seconds for first response (includes LLM call).

### 8.2 Concurrent Requests
```bash
# Install apache bench if needed
sudo apt-get install apache2-utils

# Run 10 concurrent requests
ab -n 10 -c 3 -p test.json -T application/json http://localhost:8000/api/v1/chat
```

Create `test.json`:
```json
{
  "user_id": "user_123",
  "session_id": "session_456",
  "message": "Test"
}
```

## Step 9: Docker Test (Optional)

### 9.1 Build Docker Image
```bash
docker build -t webagent .
```

### 9.2 Run Container
```bash
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=your_key \
  -e SUPABASE_URL=your_url \
  -e SUPABASE_SERVICE_ROLE_KEY=your_key \
  -e CLAUDE_MODEL=claude-sonnet-4-20250514 \
  webagent
```

## Common Issues & Solutions

### Issue 1: "ModuleNotFoundError" for a package
**Solution**: Run `pip install -r requirements.txt` to install all dependencies.

### Issue 2: "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set"
**Solution**: Ensure `.env` file exists in project root with correct values.

### Issue 3: "Connection refused" to Supabase
**Solution**: Check Supabase URL format: `https://[project-ref].supabase.co`

### Issue 4: Anthropic API rate limits
**Solution**: Use Claude Sonnet (cheaper, higher limits) or add rate limiting.

### Issue 5: Slow responses (>10s)
**Solution**: 
- Check network latency to Anthropic
- Consider caching frequent queries
- Use async/await properly

## Success Criteria

1. ✅ Server starts without errors
2. ✅ `/api/v1/seed-docs` creates 5 documents
3. ✅ `/api/v1/chat` returns coherent responses
4. ✅ Messages saved to Supabase
5. ✅ Context documents influence responses
6. ✅ Error handling works (missing env vars, etc.)

## Next Steps After Testing

1. **Review enhancement spec** (`WEBAGENT_ENHANCEMENT_SPEC.md`)
2. **Provide feedback** on architecture decisions
3. **Prioritize features** for implementation
4. **Begin Phase 1** (Memory System)

## Troubleshooting Logs

Check application logs:
```bash
# Terminal where uvicorn is running shows detailed logs
# Or check Supabase logs in dashboard
```

Enable debug logging:
```python
# In app/main.py
logging.basicConfig(level=logging.DEBUG, ...)
```

---
*Testing completed: $(date)*