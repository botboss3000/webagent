#!/bin/bash
# Quick start script for webAgent
# Run this after database migration

echo "========================================="
echo "webAgent - Quick Start"
echo "========================================="

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "❌ Virtual environment not found"
    echo "Run: uv venv --python 3.12"
    echo "Then: uv pip install -r requirements.txt"
    exit 1
fi

# Check environment file
if [ ! -f ".env" ]; then
    echo "❌ .env file not found"
    echo "Copy .env.example to .env and add your credentials"
    exit 1
fi

# Start server in background
echo "Starting FastAPI server on port 8000..."
echo "Server will run in background. Logs: server.log"
echo "To stop: pkill -f 'uvicorn app.main:app'"

# Create log file
LOG_FILE="server_$(date +%Y%m%d_%H%M%S).log"
echo "Server starting at $(date)" > "$LOG_FILE"

# Start server
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >> "$LOG_FILE" 2>&1 &
SERVER_PID=$!

echo "Server started with PID: $SERVER_PID"
echo "Logs: $LOG_FILE"
echo ""

# Wait for server to start
echo "Waiting for server to start..."
sleep 3

# Check if server is running
if curl -s http://localhost:8000/health > /dev/null; then
    echo "✅ Server is running!"
    echo ""
    
    # Seed documents
    echo "Seeding test documents..."
    SEED_RESPONSE=$(curl -s -X POST "http://localhost:8000/api/v1/seed-docs?user_id=webagent_user")
    
    if echo "$SEED_RESPONSE" | grep -q "Inserted"; then
        echo "✅ Test documents seeded successfully"
    else
        echo "⚠️  Seed response: $SEED_RESPONSE"
        echo "   (This might be OK if documents already exist)"
    fi
    
    echo ""
    echo "========================================="
    echo "✅ webAgent is ready!"
    echo ""
    echo "Test endpoints:"
    echo "  Health:    curl http://localhost:8000/health"
    echo "  Chat test: curl -X POST http://localhost:8000/api/v1/chat \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -d '{\"user_id\":\"test\",\"session_id\":\"test\",\"message\":\"Hello!\"}'"
    echo ""
    echo "Web interface:"
    echo "  Open test_interface.html in your browser"
    echo "  Click 'Seed Test Documents' then start chatting"
    echo ""
    echo "Connect your web app to:"
    echo "  http://localhost:8000/api/v1/chat"
    echo ""
    echo "API documentation:"
    echo "  http://localhost:8000/docs"
    echo ""
    echo "To stop server:"
    echo "  pkill -f 'uvicorn app.main:app'"
    echo "  or kill $SERVER_PID"
    echo ""
    echo "========================================="
    
    # Write PID to file for later cleanup
    echo $SERVER_PID > .server.pid
    echo "Server PID saved to .server.pid"
    
else
    echo "❌ Server failed to start"
    echo "Check the log file: $LOG_FILE"
    kill $SERVER_PID 2>/dev/null
    exit 1
fi