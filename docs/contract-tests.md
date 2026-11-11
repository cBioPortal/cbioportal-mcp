# MCP contract tier

A merge to `main` publishes `cbioportal/mcp:latest`, and Keel rolls it straight
into the chat MCP pod that chat.cbioportal.org's LibreChat calls. The contract
tier tests **that image** before it is published, the way LibreChat uses it.
It needs no secrets and no shared database, so it also runs on fork PRs.

## What runs

`.github/workflows/mcp-contract.yml` (on every PR to `main`/`beta`, on pushes to
them, and called by `docker_ci.yml`) runs `scripts/contract/run.sh`, which:

1. builds the image from `docker/Dockerfile`;
2. starts a throwaway `clickhouse/clickhouse-server` container, loads the
   vendored cBioPortal schema (`tests/contract/fixtures/cbioportal_schema.sql`)
   and a tiny deterministic dataset (`tests/contract/fixtures/seed.py`: four
   studies, ~140 samples);
3. applies every `sql/*.sql` and `sql/portal-specific/*/*.sql` with
   `scripts/apply_sql.sh`, as the daily clone job does;
4. creates a read-only `app_user` with only `GRANT SELECT ON <db>.*`, like prod;
5. runs the image with prod's chat env (`CLICKHOUSE_MCP_SERVER_TRANSPORT=http`,
   `CLICKHOUSE_MCP_HTTP_PATH=/db/mcp`, `CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=4`,
   the `CBIOPORTAL_MCP_*=off` flags) as `app_user`;
6. runs `tests/contract/` against it.

The tests check that:

| Check | Incident it would have caught |
|-------|-------------------------------|
| The container is still running 30s after start | startup `sys.exit(2)` from `verify_query_pool` / `ensure_db_permissions` |
| `initialize` + `tools/list` work at exactly `/db/mcp` with LibreChat's `x-user-*` / bridge-secret headers, and no response is a 3xx | FastMCP trailing-slash 307 → 406 → 404 loop |
| `/db/mcp/` answers with one 307 to exactly `/db/mcp` (FastMCP today; a 404 would also pass), never a redirect anywhere else | same |
| Tool names and input schemas equal `tests/contract/fixtures/tools_snapshot.json` | a renamed tool or changed argument silently breaking chat agents |
| Every tool answers one valid call with no `error_message`, with the expected keys of the default (compact) result format | a tool that only fails against a real server |
| Every view defined in `sql/` returns rows when queried as `app_user` through `clickhouse_run_select_query` | views switched to caller privileges failing for `app_user` |
| `resources/list` lists the guides and each loads | guides missing from the image |

## Publishing gate

`docker_ci.yml` calls `unit-tests.yml` and `mcp-contract.yml` (both
`workflow_call`) for the commit it is about to publish; its `build` job
`needs` both, so nothing is pushed or tagged (`:latest`, `:beta`, or a
`workflow_dispatch` ref) unless they pass.

## Running it locally

Needs Docker, `uv`, and `python3`.

```bash
scripts/contract/run.sh                      # build, start, test, tear down
CONTRACT_SKIP_BUILD=1 scripts/contract/run.sh # reuse cbioportal-mcp:contract
CONTRACT_KEEP=1 scripts/contract/run.sh -k views   # leave containers up to debug
scripts/contract/down.sh                     # remove them afterwards
```

With the containers up, `.contract.env` holds the URL and container names, so
you can rerun just the tests with
`set -a; . ./.contract.env; set +a; uv run --frozen --extra dev pytest tests/contract`.
Without `CBIOPORTAL_MCP_CONTRACT_URL` set, `tests/contract` is skipped, so a
plain `uv run pytest` stays hermetic.

## Updating the tools snapshot

When a tool is added, removed, or its arguments change on purpose, the
snapshot test fails and prints the diff. Regenerate and commit:

```bash
UPDATE_SNAPSHOT=1 scripts/contract/run.sh -k snapshot
git add tests/contract/fixtures/tools_snapshot.json
```

A new tool also needs one entry in `TOOL_CALLS` in
`tests/contract/test_contract.py` (valid fixture arguments and the result keys
it must return); `test_every_tool_has_a_contract_call` fails until it has one.
A new `sql/` view with a new parameter name needs a value in `VIEW_PARAMS`.

## Fixture data

`seed.py` writes rows straight into the base and `*_derived` tables (it does
not run cBioPortal's `populate_derived_tables.sql`). Keep it small; add rows
only when a tool or view needs them to return something. When cBioPortal bumps
its schema, re-download `schema.sql` at a new commit into
`cbioportal_schema.sql` and update the SHA in its header.
