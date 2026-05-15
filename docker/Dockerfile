# Use an official Python base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# uv (instead of pip) so the build respects uv.lock
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Deps-only layer — cache stays warm unless pyproject/uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Source layer — invalidates on any source/SQL change. The install below
# is a fast project-only step now that deps are already in .venv.
COPY . /app
RUN uv sync --frozen --no-dev

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

CMD ["uv", "run", "cbioportal-mcp"]
