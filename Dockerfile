# Use Python 3.10 slim image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Install system dependencies (git for GitHub integration, gcc for some pip packages)
RUN apt-get update && apt-get install -y \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install WebSocket support package
RUN pip install --no-cache-dir wsproto

# Cloud Run sets PORT (default 8080)
EXPOSE 8080

# Shell form so $PORT is expanded
# --ws wsproto: WebSocket protocol support
# --timeout-keep-alive 300: keep WebSocket connections alive for 5 min
# --proxy-headers: trust X-Forwarded-* headers from Cloud Run LB
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --ws wsproto --timeout-keep-alive 300 --proxy-headers"]
