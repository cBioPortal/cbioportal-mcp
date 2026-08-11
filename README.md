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

Optional hardening for header-based auth:

```bash
export CBIOPORTAL_MCP_AUTH_PROXY_SECRET=shared-secret-known-only-to-the-proxy
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
as a query setting on every SELECT. Row policies should then filter each
study-scoped table, including indirect tables such as raw `mutation` rows where
the study is derived through `sample_id`, `genetic_profile_id`, or another
provenance path - and tables that are *multiple* joins removed from a study
column (e.g. `clinical_event_data`, which only has `clinical_event_id`, itself
resolved through `clinical_event.patient_id` -> `patient.cancer_study_identifier`).
This is the recommended defense-in-depth mode for restricted deployments; the
app-level guard still rejects explicit requests for denied study IDs and blocks
attempts to set the internal allowlist setting in user SQL.

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
reference data, e.g. `gene`/`oncotree`, with its own `USING 1` policy).

For a runnable local Keycloak + ClickHouse + MCP authz stack demonstrating
this pattern - including the direct, one-join, and two-join cases - see
[`docker/local-e2e/`](docker/local-e2e/). `tests/test_clickhouse_init_sql_coverage.py`
guards that mock schema against drift: every table must have its own row
policy and a classification in `study_access.py` as either study-scoped
(`PROTECTED_QUERY_MARKERS`) or study-agnostic
(`STUDY_AGNOSTIC_REFERENCE_TABLES`).

Before enabling `CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true` against a
real database - and periodically after, since an unrelated cBioPortal schema
migration can add a table nobody's scoped yet - run
[`scripts/verify_row_policy_coverage.py`](scripts/verify_row_policy_coverage.py),
which performs the same check live against `system.tables` /
`system.row_policies` instead of against the mock SQL file. It takes
**admin** credentials (`CLICKHOUSE_ADMIN_USER`/`CLICKHOUSE_ADMIN_PASSWORD`,
the same split [`scripts/apply_sql.sh`](scripts/apply_sql.sh) uses), not the
MCP's runtime SELECT-only user: reading `system.row_policies` requires a
grant that is *not* scoped to the querying user's own database access, so
granting it to the runtime role would leak policy metadata for every
database on the cluster through the `clickhouse_run_select_query`
arbitrary-SQL tool. The admin identity only needs `SHOW TABLES` on the
target database (metadata, not row data) plus `SELECT ON system.row_policies`.

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
