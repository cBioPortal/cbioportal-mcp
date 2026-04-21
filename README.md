# cBioPortal MCP Server

> **WARNING ⚠️: This is still under construction**

A wrapper around the [mcp-clickhouse server](https://github.com/ClickHouse/mcp-clickhouse) adding a [cBioPortal-specific system prompt](https://github.com/cBioPortal/cbioportal-mcp/blob/main/src/cbioportal_mcp/prompts/cbioportal_prompt.py).

## Installation

```bash
# Navigate to the project directory
cd cbioportal-mcp

# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
# venv\Scripts\activate

# Upgrade pip
pip install --upgrade pip

# Install the package in development mode
pip install -e .

# Or install with development dependencies
pip install -e "."
```

## Configuration

Set the same environment variables used by mcp-clickhouse:

```bash
export CLICKHOUSE_HOST=your-clickhouse-host
export CLICKHOUSE_PORT=9000
export CLICKHOUSE_USER=your-username
export CLICKHOUSE_PASSWORD=your-password
export CLICKHOUSE_DATABASE=your-cbioportal-database  # e.g., cgds_public_2025_06_24
export CLICKHOUSE_SECURE=true  # or false for insecure connections
export CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio # or http or sse
```

## Authentication

Authentication is optional and only applies to HTTP and SSE transports. It is disabled by default. The stdio transport does not require authentication since it communicates over local standard input/output.

Three authentication modes are available:

| Mode | Use Case |
|------|----------|
| `none` | Default. No authentication required. |
| `static` | Simple token-based auth for development or internal use. |
| `jwt` | JSON Web Token validation for production deployments. |

### Quick Start with Static Tokens

Set the following environment variables to enable static token authentication:

```bash
export MCP_AUTH_MODE=static
export MCP_AUTH_TOKENS='{"token-abc123": "user1", "token-def456": "user2"}'
```

Clients include the token in the `Authorization` header:

```
Authorization: Bearer token-abc123
```

### JWT Authentication

#### JWKS Method (Recommended for Production)

Use a JWKS (JSON Web Key Set) endpoint to validate tokens signed with asymmetric keys (e.g., RS256). This is the recommended approach for production deployments because keys can be rotated without restarting the server.

```bash
export MCP_AUTH_MODE=jwt
export MCP_AUTH_JWKS_URI=https://your-idp.example.com/.well-known/jwks.json
export MCP_AUTH_JWT_ISSUER=https://your-idp.example.com/
export MCP_AUTH_JWT_AUDIENCE=cbioportal-mcp
export MCP_AUTH_JWT_ALGORITHM=RS256
```

#### Symmetric Key Method (HMAC)

Use a shared secret to validate tokens signed with HMAC (e.g., HS256). Suitable for simpler setups where a JWKS endpoint is not available.

```bash
export MCP_AUTH_MODE=jwt
export MCP_AUTH_JWT_SECRET=your-shared-secret-key
export MCP_AUTH_JWT_ISSUER=https://your-idp.example.com/
export MCP_AUTH_JWT_AUDIENCE=cbioportal-mcp
export MCP_AUTH_JWT_ALGORITHM=HS256
```

### Docker Deployment with Authentication

```bash
docker build -t cbioportal-mcp -f docker/Dockerfile .

docker run -i -p 8000:8000 \
  -e CLICKHOUSE_HOST=your-clickhouse-host \
  -e CLICKHOUSE_PORT=9000 \
  -e CLICKHOUSE_USER=your-username \
  -e CLICKHOUSE_PASSWORD=your-password \
  -e CLICKHOUSE_DATABASE=your-cbioportal-database \
  -e CLICKHOUSE_SECURE=true \
  -e CLICKHOUSE_MCP_SERVER_TRANSPORT=http \
  -e MCP_AUTH_MODE=jwt \
  -e MCP_AUTH_JWKS_URI=https://your-idp.example.com/.well-known/jwks.json \
  -e MCP_AUTH_JWT_ISSUER=https://your-idp.example.com/ \
  -e MCP_AUTH_JWT_AUDIENCE=cbioportal-mcp \
  cbioportal-mcp
```

### Environment Variables Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_AUTH_MODE` | Auth mode: `none`, `static`, `jwt` | `none` |
| `MCP_AUTH_TOKENS` | JSON map of static tokens to user identifiers | - |
| `MCP_AUTH_JWKS_URI` | JWKS endpoint URL for asymmetric key validation | - |
| `MCP_AUTH_JWT_ISSUER` | Expected JWT issuer (`iss` claim) | - |
| `MCP_AUTH_JWT_AUDIENCE` | Expected JWT audience (`aud` claim) | - |
| `MCP_AUTH_JWT_SECRET` | Symmetric key for HMAC-based JWT validation | - |
| `MCP_AUTH_JWT_ALGORITHM` | JWT signing algorithm | `RS256` (JWKS) / `HS256` (symmetric) |

### Upgrading from Unauthenticated to Authenticated

Existing deployments run without authentication and are **not affected** by this change. Authentication is disabled by default (`MCP_AUTH_MODE=none`). No configuration changes are required to maintain current behavior.

When you are ready to enable authentication, follow these steps:

1. **Choose an auth mode.** Static tokens are the simplest starting point. JWT with JWKS is recommended for production.

2. **Set the required environment variables** on your server (or in your Docker/docker-compose configuration). For example, to start with static tokens:
   ```bash
   export MCP_AUTH_MODE=static
   export MCP_AUTH_TOKENS='{"my-token": {"client_id": "app1", "scopes": ["read"]}}'
   ```

3. **Switch to an HTTP-based transport** if you haven't already. Authentication only applies to `http` and `sse` transports. The `stdio` transport inherits security from the local process and does not use MCP-level auth.
   ```bash
   export CLICKHOUSE_MCP_SERVER_TRANSPORT=http
   ```

4. **Update your MCP clients** to include the `Authorization: Bearer <token>` header in requests.

5. **Restart the server.** You should see `Authentication enabled (mode: static)` in the startup logs. Requests without a valid token will be rejected.

6. **When ready for production**, migrate from static tokens to JWT:
   ```bash
   export MCP_AUTH_MODE=jwt
   export MCP_AUTH_JWKS_URI=https://your-idp.example.com/.well-known/jwks.json
   export MCP_AUTH_JWT_ISSUER=https://your-idp.example.com/
   export MCP_AUTH_JWT_AUDIENCE=cbioportal-mcp
   ```

If anything goes wrong, set `MCP_AUTH_MODE=none` (or unset it entirely) to immediately disable authentication and restore the previous behavior.

### Security Best Practices

- **Do not commit secrets.** Never store tokens, JWT secrets, or passwords in version control. Use environment variables or a secrets manager.
- **Prefer JWT with JWKS for production.** Asymmetric key validation via a JWKS endpoint allows key rotation without restarting the server.
- **Use an HTTPS reverse proxy.** The MCP server itself does not terminate TLS. Place it behind a reverse proxy (e.g., nginx, Caddy) that handles HTTPS in production.
- **Rotate tokens regularly.** If using static tokens, rotate them periodically. With JWKS, rely on your identity provider's key rotation policy.

## Development

### Inspecting the Server with MCP Inspector

To connect to the MCP server and see requests and replies, use MCP Inspector.
You can run it with:
```bash
fastmcp dev inspector src/cbioportal_mcp/server.py
```

### Running the Server
```bash
# For development
python -m cbioportal_mcp.server

# Or using the installed script
cbioportal-mcp
```

### Running in Docker
```bash
# Build the image
docker build -t cbioportal-mcp -f docker/Dockerfile .
docker run -i -p 8000:8000 cbioportal-mcp
```

## License

MIT License - see LICENSE file for details.

## Related Projects

- [cBioPortal](https://github.com/cBioPortal/cbioportal) - The main cBioPortal platform
- [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) - ClickHouse MCP server
