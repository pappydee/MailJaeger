# Multi-stage Dockerfile for MailJaeger

# Stage 1: Builder
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies directly in runtime
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify uvicorn is available (fail-fast)
RUN python -c "import uvicorn; print(uvicorn.__version__)"

# Copy application code
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY cli.py .

# Create necessary directories with restrictive permissions
RUN mkdir -p /app/data /app/data/logs /app/data/search_index /app/data/attachments && \
    chmod 700 /app/data

# Set Python path
ENV PYTHONPATH=/app

# Default server configuration (can be overridden via environment variables)
ENV SERVER_PORT=8000

# Expose port (default localhost binding is in config)
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Run application using Python module syntax with configurable host/port
# Default to localhost (127.0.0.1) if SERVER_HOST is not set for safety
CMD python -m uvicorn src.main:app --host ${SERVER_HOST:-127.0.0.1} --port ${SERVER_PORT}