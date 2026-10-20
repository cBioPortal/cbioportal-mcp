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

### Study-Level Access Control

Public deployments default to unrestricted study access:

```bash
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=public
```

Internal deployments can enable per-request study authorization:

```bash
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted
export CBIOPORTAL_MCP_AUTH_REQUIRED=true
export CBIOPORTAL_MCP_AUTH_PROXY_SECRET=shared-secret-known-only-to-the-proxy
```

The MCP server expects authentication to be handled by a trusted reverse proxy
or gateway backed by Keycloak. The proxy should validate the user session or
token, strip spoofable inbound identity headers, and inject trusted headers such
as:

```text
x-user-id: <keycloak-sub-or-username>
x-user-email: <email>
x-forwarded-groups: /group/a,/group/b
x-cbioportal-allowed-studies: study_a,study_b
```

The server reads identity only from the configured header names (defaults
above); if your proxy uses others, set them explicitly, e.g. for
oauth2-proxy:

```bash
export CBIOPORTAL_MCP_AUTH_USER_ID_HEADERS=x-auth-request-user
export CBIOPORTAL_MCP_AUTH_EMAIL_HEADERS=x-auth-request-email
export CBIOPORTAL_MCP_AUTH_GROUPS_HEADERS=x-auth-request-groups
```

The proxy must drop every one of these headers - plus the allowed-studies
header and the proxy-secret header - from inbound client requests before
injecting its own values; otherwise a client can add its own groups header.
`authz_header_names()` in `study_access.py` lists the full set, and the
reference proxy (`local_keycloak_proxy.py`) strips exactly that.

If your proxy can inject allowed studies directly, no ACL file is required. If
you prefer to manage access in the MCP deployment, configure a JSON ACL:

```bash
export CBIOPORTAL_MCP_STUDY_ACL_FILE=/etc/cbioportal-mcp/study-acl.json
```

```json
{
  "users": {
    "alice@example.org": ["brca_msk_2024"],
    "keycloak-subject-id": ["study_a", "study_b"]
  },
  "groups": {
    "/cbioportal/admins": ["*"],
    "/research/brca": ["brca_msk_2024"]
  },
  "default": []
}
```

`CBIOPORTAL_MCP_AUTH_PROXY_SECRET` (set above) is **required** in restricted
mode, not optional: it is the only thing that lets the server verify that
`x-user-id` / `x-forwarded-groups` / `x-cbioportal-allowed-studies` actually
came from your trusted proxy rather than a direct caller who can reach this
process on the network. The server refuses to start in restricted mode
without it (`validate_study_access_startup_config`, called from `main()`)
rather than silently trusting unverified headers. Only the header name
carrying the secret is configurable:

```bash
# optional; default is x-cbioportal-mcp-proxy-secret
export CBIOPORTAL_MCP_AUTH_PROXY_SECRET_HEADER=x-cbioportal-mcp-proxy-secret
```

In restricted mode, `list_studies`, study guide access, and arbitrary ClickHouse
queries are checked against the current user's allowed studies. Arbitrary SQL is
intentionally conservative: study-scoped queries must include literal
`cancer_study_identifier` filters, or parameterized view arguments such as
`study='...'` / `studies=['...']`, so the server can verify the requested study
IDs before ClickHouse executes the query.

For stronger isolation, enable ClickHouse row-policy enforcement:

```bash
export CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true
# optional; ClickHouse custom settings generally need an allowed prefix.
export CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING=SQL_cbiomcp_allowed_studies
```

In this mode, the MCP server passes the resolved study allowlist to ClickHouse
as a query setting on every SELECT, and ClickHouse row policies filter every
study-scoped table by following its real foreign-key path back to
`cancer_study.cancer_study_identifier`. The production policies live in
[`sql/study-authz/0-row-policies.sql`](sql/study-authz/0-row-policies.sql) and
are **opt-in** (restricted deployments only):

```bash
APPLY_STUDY_AUTHZ_SQL=true ./scripts/apply_sql.sh   # see sql/README.md
# then, once per runtime user:
#   GRANT cbioportal_mcp_study_restricted TO <mcp_user>;
#   SET DEFAULT ROLE cbioportal_mcp_study_restricted TO <mcp_user>;
```

They cover the cBioPortal ClickHouse schema (3.1.0 and the legacy schema
cloned deployments use): direct `cancer_study_identifier` tables
(`clinical_data_derived`, `genomic_event_derived`, the other `*_derived`
tables, `cancer_study_query_preferences`), `cancer_study_id` tables
(`patient`, `genetic_profile`, `sample_list`, `copy_number_seg`, ...),
tables reached through `patient` (`sample`, `clinical_event`,
`clinical_patient`) or `sample` (`clinical_sample`), every per-profile table
through `genetic_profile` (`mutation`, `genetic_alteration`,
`structural_variant`, `sample_cna_event`, ...), multi-hop tables
(`clinical_event_data` -> `clinical_event` -> `patient`), and the shared
`mutation_event` / `cna_event` dimensions (visible only when observed in an
allowed study). The classification is in
[`row_policy_catalog.py`](src/cbioportal_mcp/authentication/row_policy_catalog.py).
Views (`sql/4`-`sql/7`, including parameterized views) get a `USING 1`
pass-through policy and inherit filtering from their underlying tables,
because they run as the invoker; a `SQL SECURITY DEFINER` view, a
materialized view or a dictionary would bypass that, and the coverage check
below rejects them.
This is the recommended defense-in-depth mode for restricted deployments; the
app-level guard still rejects explicit requests for denied study IDs and
rejects any query mentioning an internal `SQL_cbiomcp_*` setting (tokenized,
so quoting, escapes and comments don't hide it). ClickHouse enforces the same
thing independently: the server sends `readonly=1` with every row-policy
query, which makes ClickHouse reject any `SETTINGS`/`SET` inside the query
while still applying the allowlist sent with the request. This only holds if
the runtime user's settings profile leaves `readonly` at its default (0) and
does not mark the setting `CHANGEABLE_IN_READONLY`; at startup the server
checks both directions (`verify_allowlist_lockdown`) and refuses to start
otherwise. Profiles with `readonly=1` or `readonly=2` are therefore
unsupported for row-policy mode. Running restricted
mode without it logs a startup warning: the app-level guard is a heuristic
pattern check on arbitrary SELECT queries, not a real SQL parser, so it's the
weaker of the two supported modes.

Per-table grants (no wildcard) would mechanically close the "forgotten
table" gap on their own - ClickHouse just denies an ungranted table
outright, which is fail-closed too. Two things rule that out here,
independent of whether it would work: the MCP server's own startup
permission check requires the literal wildcard-scoped
`GRANT SELECT ON db.* TO <restricted-role>` (per-table grants don't satisfy
that check even when every existing table is individually granted), and
`SHOW TABLES` / schema discovery is grant-scoped in ClickHouse, so per-table
grants would make an unscoped table invisible to `clickhouse_list_tables()`,
not just inaccessible. Keeping the wildcard grant for those two reasons
means it says yes to every table, forgotten or not, by construction - there
is no room left in the grant layer to say no to one specific table. Instead,
pair it with a database-wide **default-deny row policy**, which is the only
layer left that can say no:

```sql
CREATE ROW POLICY cbioportal_mcp_default_deny
ON <db>.* USING 0 TO <restricted-role>;
```

This auto-covers tables created after the policy, the same way the wildcard
grant does. ClickHouse combines multiple `PERMISSIVE` row policies on one
table with `OR`, so a table with no policy of its own is stuck at `0`
(nothing visible), while a table with its own policy gets
`0 OR <real condition>` = `<real condition>`. A newly added table is
therefore unreadable by default until someone adds a policy deciding how it
maps back to a study (or explicitly allowlists it as study-agnostic
reference data, e.g. `gene`/`type_of_cancer`, with its own `USING 1` policy).
The default-deny also matches views, which is why every view needs its own
pass-through policy.

For a runnable local Keycloak + ClickHouse + MCP authz stack see
[`docker/local-e2e/`](docker/local-e2e/). Its ClickHouse fixture mirrors the
production linkage (no study column on `sample`/`patient`) and has the repo's
real `sql/*.sql` plus the production row-policy file applied on top, and the
verifier runs unfiltered `SELECT *` as restricted users against every
study-scoped table and several views. `tests/test_row_policy_sql.py` keeps the
policy file, the catalog, the views defined in `sql/` and the fixture in sync
without needing ClickHouse.

Before enabling `CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true` against a
real database - and periodically after, since an unrelated cBioPortal schema
migration can add a table nobody's scoped yet - run
[`scripts/verify_row_policy_coverage.py`](scripts/verify_row_policy_coverage.py)
against the live `system.tables` / `system.row_policies`. It fails if the
default-deny is missing, if a study-scoped table has only the default-deny
(fully denied by accident) or an unfiltered policy, if a reference table or
view has no policy, on `SQL SECURITY DEFINER` views, materialized views and
dictionaries, and on unclassified tables. It takes
**admin** credentials (`CLICKHOUSE_ADMIN_USER`/`CLICKHOUSE_ADMIN_PASSWORD`,
the same split [`scripts/apply_sql.sh`](scripts/apply_sql.sh) uses), not the
MCP's runtime SELECT-only user: reading `system.row_policies` requires a
grant that is *not* scoped to the querying user's own database access, so
granting it to the runtime role would leak policy metadata for every
database on the cluster through the `clickhouse_run_select_query`
arbitrary-SQL tool. The admin identity only needs `SHOW TABLES` /
`SHOW DICTIONARIES` on the target database (metadata, not row data) plus
`SELECT ON system.row_policies` and `system.dictionaries`.

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
