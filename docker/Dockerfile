# Use an official Python base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the source code into the image
COPY . /app

# Install dependencies
RUN pip install --upgrade pip && pip install .

# Set environment variables expected at runtime (user should override as needed)
ENV CLICKHOUSE_HOST=localhost \
    CLICKHOUSE_PORT=9000 \
    CLICKHOUSE_USER=user \
    CLICKHOUSE_PASSWORD=password \
    CLICKHOUSE_DATABASE=database \
    CLICKHOUSE_SECURE=true \
    CLICKHOUSE_VERIFY=true \
    CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio \
    CLICKHOUSE_MCP_BIND_HOST=127.0.0.1 \
    CLICKHOUSE_MCP_BIND_PORT=8000

# Expose port (optional — based on what the MCP server binds to; change if needed)
EXPOSE 8000

# Default command
CMD ["cbioportal-mcp"]