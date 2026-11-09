# cBioPortal MCP Server

> **WARNING ⚠️: This is still under construction**

A wrapper around the [mcp-clickhouse server](https://github.com/ClickHouse/mcp-clickhouse) adding a [cBioPortal-specific system prompt](https://github.com/cBioPortal/cbioportal-mcp/blob/main/src/cbioportal_mcp/resources/system-prompt.md).

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
export CLICKHOUSE_DATABASE=your-cbioportal-database  # see "Preparing the database" below
export CLICKHOUSE_SECURE=true  # or false for insecure connections
export CBIOPORTAL_MCP_STUDY_FILTERING=off  # required; no default
# Auth rollout switches: required in HTTP/SSE mode (no defaults).
export CBIOPORTAL_MCP_AUTH_ENFORCEMENT=off   # off | enforce
export CBIOPORTAL_MCP_AUTH_TEST_USERS=      # comma-separated emails; may be empty
export CBIOPORTAL_MCP_PUBLIC_DB_CHECK=off    # off | enforce
export CLICKHOUSE_MCP_SERVER_TRANSPORT=http # default; or sse
# Local stdio only: set BOTH of these explicitly.
# export CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio
# export CBIOPORTAL_MCP_LOCAL_DEV=1
# Optional: mount the HTTP endpoint under a sub-path (default: /mcp).
# Set when reverse-proxied behind a prefix so trailing-slash redirects
# include it, e.g. /db/mcp when served at https://host/db/mcp.
# export CLICKHOUSE_MCP_HTTP_PATH=/db/mcp
# Optional: how long table lists, column descriptions, and generated study
# guides are cached in-process (default: 3600; 0 disables the cache).
# export CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS=3600
# Optional: most ClickHouse queries one server process runs at once, across
# concurrent tool calls (default: 4). Sets mcp-clickhouse's query pool
# (CLICKHOUSE_MCP_MAX_WORKERS, which it overrides); may also be set in the .env
# mcp-clickhouse loads. A query waiting for a free slot counts against
# CLICKHOUSE_MCP_QUERY_TIMEOUT, so a saturated pool yields a timeout error
# rather than a hung request. If mcp_clickhouse is imported before
# cbioportal_mcp with a different pool size, the server refuses to start.
# export CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=4
```

Study filtering accepts only `on` or `off` (case-insensitive, surrounding whitespace
is ignored). Missing, blank, and other values refuse startup. `off` preserves
current behavior and is the permanent setting for the public deployment. `on`
is for private deployments only and serves study data only when both
`CBIOPORTAL_MCP_AUTH_ENFORCEMENT` and `CBIOPORTAL_MCP_PUBLIC_DB_CHECK` are
`enforce`, `CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES` is set, and the
database-side startup check passes; see
[docs/study-filtering-rollout.md](docs/study-filtering-rollout.md). These checks
apply to alternate launchers too. Failed filtering checks keep the server running
in degraded mode and block all public/private study tools and resource reads with
MCP errors before handlers or caches. Login, schema tools and OncoTree remain
available under configured login rules. Checks retry only on restart. After
configuration changes or rollback, inspect `GET /health` or the startup log:
health returns `study_data_available` and a stable `reason` (`checks_pending`,
`rollout_not_enforced`, `filtering_check_failed`, `ready`, `filtering_off`). HTTP
503 (`checks_pending`) is observable on hosts that skip the lifespan. Normal
uvicorn startup binds only after the lifespan checks finish, so probes during
checks get connection refused. HTTP 200 means the server is serving, including
degraded mode to keep Kubernetes routing login/schema requests. Check the boolean
for study availability. General ClickHouse permission and login configuration
errors still refuse startup.
HTTP is the default transport; stdio requires exactly `CBIOPORTAL_MCP_LOCAL_DEV=1`.

The auth rollout switches are parsed once at startup and refuse startup when missing
or invalid, naming the variable. `CBIOPORTAL_MCP_AUTH_ENFORCEMENT` and
`CBIOPORTAL_MCP_PUBLIC_DB_CHECK` accept `off` or `enforce`.
`CBIOPORTAL_MCP_AUTH_TEST_USERS` must be set but may be empty; entries are trimmed,
lower-cased and de-duplicated, empty entries are dropped, and an entry that is not a
plausible email refuses startup. It is ignored (with a warning) when enforcement is
`enforce`. `CBIOPORTAL_MCP_STUDY_FILTERING=on` requires both switches at `enforce`.
Only local stdio (`CBIOPORTAL_MCP_LOCAL_DEV=1`) defaults unset switches to
`off` / empty / `off`. The startup log shows each value and the tester count, never
tester emails. These switches are temporary and expire on 2027-06-30 (see
`cbioportal_mcp.rollout.EXPIRES`). Authentication enforcement is described below.
The public-database check (P10) is not built yet: `CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce`
is validated, logged and required for study filtering `on`, but currently has no
other effect. In particular, nothing yet refuses a filtering-`off` server pointed at
a database with private studies.

### Required login (P2)

`CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce` requires verified login on every
HTTP/SSE request, including tool/resource/prompt discovery and reads. With `off`,
only emails in `CBIOPORTAL_MCP_AUTH_TEST_USERS` receive the full verification
checks. Other callers retain the existing login behavior: Google-configured
servers require a valid bearer or bridge secret; without Google, anonymous
requests remain allowed. Presented invalid credentials always fail, in either
mode. A valid bridge secret admits non-testers in `off` without a verified flag,
but no verified identity is assigned until the full checks pass. A claimed tester
email adds checks even if its credentials fail. Decisions log only the route and admission booleans.
Legacy `AUTH_REQUIRED` / `ALLOW_UNAUTHENTICATED` settings have no effect.

Two login routes can coexist on the same endpoint:

- **Google OAuth connectors:** set all three `CBIOPORTAL_MCP_GOOGLE_CLIENT_ID`,
  `CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET`, and `CBIOPORTAL_MCP_GOOGLE_BASE_URL`.
  Register `<base-url>/auth/callback` in the Google OAuth client, enable
  `openid email profile` scopes, and publish the consent configuration or add
  your testers there. Verification must be positively reported by Google's
  userinfo/tokeninfo response. Partial Google configuration refuses startup,
  including when a bridge is configured.
- **LibreChat bridge:** configure a non-empty `CBIOPORTAL_MCP_BRIDGE_SECRET`
  in both services using secret storage. LibreChat must send `x-user-email`,
  `x-user-id`, `x-cbioportal-mcp-bridge-secret`, and `x-user-email-verified`
  on every request. The verified flag must be `true` (case-insensitive), derived
  from trusted account verification rather than a user-editable field. Secret
  comparison is timing-safe; unexpanded `${...}` / `{{...}}` placeholders fail.
  Configure the ingress/proxy to strip client-supplied bridge headers before
  LibreChat injects its own values. Use TLS between services.

Enforcement requires at least one configured route. A presented invalid
credential cannot fall back to the other route. Bridge headers are stripped
before handlers and telemetry; only the adapter's verified identity is trusted.
OAuth client/token storage should use `REDIS_URL` with persistent Redis; otherwise
registrations are stored on local disk and are lost on pod replacement.

FastMCP remains locked at 2.13.3, matching the frozen Docker build. A small
adapter mounts the existing Google provider's OAuth/discovery endpoints and
shares admission with the bridge; it avoids a FastMCP 3 upgrade (and conflicts
with the current mcp-clickhouse dependency). Local stdio still requires
`CBIOPORTAL_MCP_LOCAL_DEV=1`, carries no user identity, and must satisfy startup
configuration checks even with enforcement enabled.

### Datadog Tool Metrics

The server emits one OpenTelemetry span per MCP tool call and can also emit
DogStatsD metrics for dashboard-level aggregates:

| Metric | Type | Purpose |
|---|---|---|
| `cbioportal_mcp.tool.calls` | counter | Tool-call volume by `tool`, `success`, `client_kind`, and `client_name` |
| `cbioportal_mcp.tool.duration_ms` | distribution | Tool latency, including p50/p95/p99 by tool |
| `cbioportal_mcp.tool.errors` | counter | Tool-call failures by tool/client |

DogStatsD metrics are enabled by default when `DD_AGENT_HOST` or
`DD_DOGSTATSD_HOST` is configured:

```bash
export DD_AGENT_HOST=<datadog-agent-host>
# Optional overrides:
export DD_DOGSTATSD_HOST=<dogstatsd-host>
export DD_DOGSTATSD_PORT=8125
export DD_SERVICE=cbioportal-mcp
export DD_ENV=prod
export CBIOPORTAL_MCP_DD_METRICS_ENABLED=true
export CBIOPORTAL_MCP_DD_METRIC_PREFIX=cbioportal_mcp
```

Set `CBIOPORTAL_MCP_DD_METRICS_ENABLED=false` to disable DogStatsD metrics.
The checked-in dashboard definition at
[`datadog/cbioagent-tool-metrics-dashboard.json`](datadog/cbioagent-tool-metrics-dashboard.json)
can be imported into Datadog or used as the source for updating the existing
cBioAgent dashboard.

## Preparing the database

**We strongly recommend pointing the MCP at a *separate* ClickHouse database, not your production cBioPortal database directly.** Two reasons:

1. **LLM-friendly fixes are destructive.** The agent works much better against a schema that's been cleaned up (misleading columns dropped, column comments added, OncoTree fields denormalized, named cohorts materialized). Applying those changes to your production database would interfere with the cBioPortal application.
2. **Isolation.** A separate database with a read-only user (`SELECT`-only) means agent traffic — including pathological queries — can't degrade production performance or accidentally expose data your portal users shouldn't see.

The recommended pattern is a periodic clone job: copy your production cBioPortal database into a separate ClickHouse database, then apply the SQL files in [`sql/`](sql/) — these add column comments, drop misleading columns, denormalize OncoTree, and materialize the `cancer_study_query_preferences` table the agent uses for cohort lookups. Point the MCP at this cloned-and-prepped database. See [`sql/README.md`](sql/README.md) for the full schema-prep contract and how to add deployment-specific preferences.

To apply the SQL files manually (e.g. for ad-hoc testing), use the helper script:

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_DATABASE=your-prepped-db
export CLICKHOUSE_ADMIN_USER=...  CLICKHOUSE_ADMIN_PASSWORD=...
./scripts/apply_sql.sh
```

Note the deliberately separate `CLICKHOUSE_ADMIN_*` env vars — admin credentials with DDL rights are kept out of the MCP server's runtime environment (which only ever needs `SELECT`).

For an end-to-end reference deployment (Kubernetes CronJob that handles the clone + SQL apply + atomic pointer-flip), see the cBioPortal team's daily clone CronJob in [knowledgesystems-k8s-deployment](https://github.com/knowledgesystems/knowledgesystems-k8s-deployment).

## Development

### Inspecting the Server with MCP Inspector

To connect to the MCP server and see requests and replies, use MCP Inspector.
You can run it with:
```bash
CBIOPORTAL_MCP_STUDY_FILTERING=off CBIOPORTAL_MCP_LOCAL_DEV=1 \
  CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio fastmcp dev inspector src/cbioportal_mcp/server.py
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
docker run -p 8000:8000 -e CBIOPORTAL_MCP_STUDY_FILTERING=off \
  -e CBIOPORTAL_MCP_AUTH_ENFORCEMENT=off -e CBIOPORTAL_MCP_AUTH_TEST_USERS= \
  -e CBIOPORTAL_MCP_PUBLIC_DB_CHECK=off cbioportal-mcp
```

## License

Apache License 2.0 - see [LICENSE](LICENSE) for details.

## Related Projects

- [cBioPortal](https://github.com/cBioPortal/cbioportal) - The main cBioPortal platform
- [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) - ClickHouse MCP server
