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
export CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio # or http or sse
# Optional: mount the HTTP endpoint under a sub-path (default: /mcp).
# Set when reverse-proxied behind a prefix so trailing-slash redirects
# include it, e.g. /db/mcp when served at https://host/db/mcp.
# export CLICKHOUSE_MCP_HTTP_PATH=/db/mcp
```

### ClickHouse Query Cache Pilot (optional, off by default)

Repeated mutation-frequency and top-gene queries can use ClickHouse's query
cache:

```bash
export CBIOPORTAL_MCP_QUERY_CACHE_ENABLED=1   # only the exact value 1 enables it
export CBIOPORTAL_MCP_QUERY_CACHE_TTL=300     # seconds; default and maximum 3600
```

Only allowlisted queries are cached:
- the `get_study_guide` top-genes section (`study_guide.top_genes`). This is
  trusted server code and reads `genomic_event_derived` directly by design;
  the view allowlist below applies to model-written SQL only.
- `clickhouse_run_select_query` SQL whose every `FROM`/`JOIN` source is one of
  the standard frequency / top-gene parameterized views from
  `sql/4-mutation-frequency-views.sql` (called as the guides show, e.g.
  `FROM top_mutated_genes_in_study(study = '...', top_n = 5)`) or a
  parenthesized subquery built only from them, and that does not reference
  `system.*`. Queries that read those views through a `WITH` name, or use
  `ARRAY JOIN`, run uncached.

While the pilot is on, SQL that sets `use_query_cache` or any `query_cache*`
setting itself is rejected. Both variables are validated at startup: TTLs above
3600 are clamped, and non-positive or non-integer values stop the server.

Server requirements:
- ClickHouse 24.4 or later.
- The MCP user's settings profile must let it change the cache settings: use
  `readonly = 2` (recommended), or `readonly = 1` plus `CHANGEABLE_IN_READONLY`
  constraints on `use_query_cache`, `query_cache_ttl` and
  `query_cache_nondeterministic_function_handling`. That option also needs
  `access_control_improvements.settings_constraints_replace_previous = true`
  in the server config; without it the constraints are ignored and the pilot
  turns itself off. With a plain
  `readonly = 1`, ClickHouse refuses the settings; the server then logs one
  warning, counts `cbioportal_mcp.db_query.cache_disabled`, and runs uncached.
- Recommended constraints: `query_cache_ttl` max 3600 and
  `query_cache_share_between_users` readonly.

Cached queries carry `query_cache:on` on the `db_query.*` metrics, or
`query_cache:fallback` when ClickHouse refused the settings and an uncached
retry answered. Measure actual hits in `system.query_log`
(`query_cache_usage`, `ProfileEvents['QueryCacheHits']`). The cache is not
invalidated when the database behind the MCP changes; the deployment should
run `SYSTEM DROP QUERY CACHE` when it switches databases.

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
