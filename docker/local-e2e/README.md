# Local Keycloak Study Authz E2E

This stack validates the restricted-study access flow end to end:

1. Keycloak issues a bearer token for a local user.
2. A local dev auth proxy validates the token via Keycloak userinfo.
3. The proxy strips spoofable identity headers and injects trusted headers.
4. cBioPortal MCP maps Keycloak groups to allowed studies through `study-acl.json`.
5. cBioPortal MCP passes the resolved allowlist to ClickHouse as
   `SQL_cbiomcp_allowed_studies`.
6. ClickHouse runs a production-shaped fixture (`clickhouse-init.sql`):
   cBioPortal's real table and column names, with study linkage only through
   real foreign keys - `sample.patient_id` -> `patient.cancer_study_id` ->
   `cancer_study`, per-profile tables through `genetic_profile_id`,
   `clinical_event_data` through `clinical_event` -> `patient`, and so on.
   `init-apply-repo-sql.sh` then applies the repo's real `sql/*.sql` (the same
   files the daily clone job runs, including the `sql/4`/`sql/5`/`sql/7` views)
   and the production row policies from `sql/study-authz/0-row-policies.sql`,
   so the e2e exercises exactly what a restricted deployment would apply.
7. A database-wide default-deny row policy (`ON cbioportal_authz_e2e.*
   USING 0`) backstops every table, including ones added later. ClickHouse
   combines multiple PERMISSIVE row policies on a table with OR, so a table
   with no policy of its own is stuck at `0` (nothing visible) even though it
   inherits the database-wide `GRANT SELECT` the server's startup check
   requires; a table with its own policy gets `0 OR <real condition>`. The
   default-deny also matches *views*, which is why each view gets a
   `USING 1` pass-through policy; its rows are then filtered by the
   underlying tables' policies (views run as the invoker). Verified against
   ClickHouse 24.12. `tests/test_row_policy_sql.py` fails CI if the policy
   file, `row_policy_catalog.py`, the views in `sql/` or this fixture drift
   apart.
8. `scripts/verify_row_policy_coverage.py` checks coverage *live*, against a
   running ClickHouse's `system.tables` / `system.row_policies` (missing or
   unfiltered policies, study-scoped tables or views left fully denied,
   `SQL SECURITY DEFINER` views, materialized views, dictionaries,
   unclassified tables) - this is what a real deployment
   runs before enabling `CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true`,
   and periodically after, to catch drift (e.g. a table added by an
   unrelated cBioPortal schema migration with no row policy). It uses
   separate ADMIN credentials (`local_e2e_admin` here, `CLICKHOUSE_ADMIN_USER`
   in production - the same split `scripts/apply_sql.sh` uses), not the
   MCP's runtime `mcp_authz` user: reading `system.row_policies` requires a
   grant that is *not* scoped to the querying user's own database access
   (once granted, it exposes policy metadata for every database on the
   cluster), so granting it to the runtime role would leak that metadata
   through the `clickhouse_run_select_query` arbitrary-SQL tool. Verified
   this admin identity needs only `GRANT SHOW TABLES, SHOW DICTIONARIES ON
   <db>.*` (metadata, no row data) plus `GRANT SELECT ON system.row_policies`
   and `system.dictionaries` - nothing else.

This proxy is for local development only. Production should use a hardened
ingress/auth proxy or gateway backed by Keycloak.

## Run

From the repo root:

```bash
docker compose -f docker/local-e2e/docker-compose.yml up --build
```

To reset the seeded ClickHouse/Keycloak state:

```bash
docker compose -f docker/local-e2e/docker-compose.yml down -v
```

In another shell, run the verifier:

```bash
uv run python scripts/local_e2e_keycloak_authz.py
```

It checks, through the MCP's `clickhouse_run_select_query` tool, that an
unfiltered `SELECT *` on every study-scoped fixture table returns exactly
alice's (`study_alpha`) or bob's (`study_beta`) rows and both for
`admin-user`; that reference tables stay readable and cBioPortal's auth
tables stay denied; that a filter the app-level guard can't read
(`concat('study_', 'beta')`) is still enforced; and that the `sql/4`
coverage view and the parameterized `sql/5` / `sql/7` views inherit the
filtering. Expected output:

```text
Row policies OK on 44 study-scoped tables.
Local Keycloak authz e2e passed.
Alice access: {'user_id': '...', 'user_email': 'alice@example.org', ...}
Bob access: {'user_id': '...', 'user_email': 'bob@example.org', ...}
```

## Test Plan

Use this plan to verify the full Keycloak -> auth proxy -> MCP -> ClickHouse
authorization path after changing auth, ACL, or row-policy logic.

1. Reset any previously seeded local state when ClickHouse init SQL or the
   Keycloak realm changes:

   ```bash
   docker compose -f docker/local-e2e/docker-compose.yml down -v
   ```

2. Start the full stack from the repo root:

   ```bash
   docker compose -f docker/local-e2e/docker-compose.yml up --build -d
   docker compose -f docker/local-e2e/docker-compose.yml ps
   ```

   Expected services:

   - `clickhouse` healthy on `127.0.0.1:8123`
   - `keycloak` healthy on `127.0.0.1:18080`
   - `mcp` listening on `127.0.0.1:8000`
   - `auth-proxy` listening on `127.0.0.1:8001`

3. Confirm the local Keycloak realm was imported from
   `docker/local-e2e/keycloak-realm.json`. The realm defines:

   - password-grant client `mcp-local`
   - token-introspection client `mcp-local-proxy`
   - users `alice`, `bob`, and `admin-user`
   - groups `/research/alpha`, `/research/beta`, and `/cbioportal/admins`

4. Confirm the local auth proxy is configured to introspect Keycloak tokens and
   forward trusted headers to MCP:

   ```yaml
   LOCAL_AUTH_PROXY_KEYCLOAK_INTROSPECTION_URL: http://host.docker.internal:18080/realms/cbioportal-mcp-local/protocol/openid-connect/token/introspect
   LOCAL_AUTH_PROXY_UPSTREAM_MCP_URL: http://mcp:8000/mcp
   LOCAL_AUTH_PROXY_SECRET: local-proxy-secret
   ```

   The MCP service must use the matching trusted-proxy secret:

   ```yaml
   CBIOPORTAL_MCP_AUTH_PROXY_SECRET: local-proxy-secret
   ```

5. Run the verifier through the protected proxy endpoint:

   ```bash
   uv run python scripts/local_e2e_keycloak_authz.py
   ```

   The verifier obtains Keycloak tokens for Alice and Bob, calls MCP through
   `http://127.0.0.1:8001/mcp/`, and checks:

   - Alice resolves to `study_alpha`
   - Bob resolves to `study_beta`
   - `list_studies` only returns Alice's allowed study for Alice
   - Alice can query `clinical_data_derived` for `study_alpha`
   - Alice is denied when explicitly querying `study_beta`
   - an unfiltered `SELECT *` on every study-scoped table in the fixture
     (raw `mutation`, `sample`, `structural_variant`, `sample_cna_event`,
     `clinical_event_data`, the `*_derived` tables, ...) returns exactly the
     caller's study's rows, and both studies' rows for `admin-user`
   - reference tables (`gene`) stay readable; `users` / `authorities` stay
     denied even for `admin-user`
   - `concat('study_', 'beta')` filters, which the app-level guard can't
     read, are still enforced by ClickHouse
   - the `sql/4` coverage view and the parameterized `sql/5`
     (`gene_pair_coexpression`) and `sql/7` (`treatment_counts_in_study`)
     views only return the caller's data

6. Expected success output:

   ```text
   Row policies OK on 44 study-scoped tables.
   Local Keycloak authz e2e passed.
   Alice access: {'user_id': '...', 'user_email': 'alice@example.org', ...}
   Bob access: {'user_id': '...', 'user_email': 'bob@example.org', ...}
   ```

7. If an assertion fails with `UNKNOWN_TABLE`, or the fixture looks stale,
   the ClickHouse volume was initialized from an older `clickhouse-init.sql`.
   Run `docker compose -f docker/local-e2e/docker-compose.yml down -v`, start
   the stack again, and rerun the verifier. ClickHouse init takes about a
   minute (`sql/2` runs ~900 mutations); the healthcheck waits for it.

8. Run the full test suite, including the SQL-file drift guard:

   ```bash
   uv run pytest -q
   ```

   `tests/test_row_policy_sql.py` fails if `sql/study-authz/0-row-policies.sql`
   is missing the wildcard grant or default-deny, if a catalog table or view
   has no (or the wrong kind of) policy, if a policy subquery is not
   qualified with `__CLICKHOUSE_DATABASE__`, if a view in `sql/` or a table in
   `clickhouse-init.sql` is unclassified in `row_policy_catalog.py`, or if the
   fixture gives `sample`/`patient` a study column production doesn't have.

9. To manually confirm the fail-closed default for a table nobody has
   scoped yet (this is what step 7's default-deny wildcard row policy is
   for), with the stack still running from step 2:

   ```bash
   # Add a table after the stack (and its row policies) already exist,
   # with no table-specific policy of its own.
   docker exec local-e2e-clickhouse-1 clickhouse-client -q "
     CREATE TABLE cbioportal_authz_e2e.surprise_new_table (secret String) ENGINE=Memory;
     INSERT INTO cbioportal_authz_e2e.surprise_new_table VALUES ('leaked-if-fail-open');
   "

   # Query it as the restricted MCP user, with Alice's allowlist setting.
   docker exec local-e2e-clickhouse-1 clickhouse-client --user mcp_authz --password mcp_authz_pw -q "
     SELECT * FROM cbioportal_authz_e2e.surprise_new_table
     SETTINGS SQL_cbiomcp_allowed_studies = 'study_alpha'
   "
   # Expect zero rows printed (not an error, not the secret value) - the
   # default-deny policy on db.* auto-covers this table even though it was
   # created after that policy and after the stack started.

   docker exec local-e2e-clickhouse-1 clickhouse-client -q "DROP TABLE cbioportal_authz_e2e.surprise_new_table"
   ```

   Why the row policy is doing this job instead of the grant: per-table
   grants (no wildcard) *would* mechanically close this same gap on their
   own - ClickHouse just refuses to query an ungranted table outright
   (`ACCESS_DENIED`), which is fail-closed too. But two other things rule
   per-table grants out here, independent of whether they'd work:
   1. The server's own startup check (`ensure_db_permissions`) does
      `CHECK GRANT SELECT ON db.*`, which checks for that literal
      wildcard-scoped grant object - verified live that per-table grants do
      not satisfy it even when every existing table is individually
      granted. Switching to per-table grants makes the server refuse to
      start, before row policies even enter the picture.
   2. `SHOW TABLES` / schema discovery is grant-scoped in ClickHouse: with
      per-table grants, a table nobody's granted yet isn't just
      inaccessible, it's *invisible* to `clickhouse_list_tables()` - worse
      for the agent than "visible but empty."

   Once the wildcard grant is kept for those two reasons, it says "yes" to
   every table, forgotten or not, by construction - there's no room left in
   the grant layer to say no to one specific table. So the row policy is
   the only layer left that *can* say no, which is why the default-deny
   fallback lives there instead.

10. Run the live coverage check (needs the ClickHouse container only -
    `docker compose -f docker/local-e2e/docker-compose.yml up -d clickhouse`
    is enough, the rest of the stack isn't required):

    ```bash
    export CLICKHOUSE_HOST=127.0.0.1 CLICKHOUSE_PORT=8123 CLICKHOUSE_DATABASE=cbioportal_authz_e2e
    export CLICKHOUSE_ADMIN_USER=local_e2e_admin CLICKHOUSE_ADMIN_PASSWORD=local_e2e_admin_pw
    export CLICKHOUSE_SECURE=false
    uv run python scripts/verify_row_policy_coverage.py
    ```

    Expect `OK: default-deny present; ...` and exit code 0. To
    confirm it actually catches a gap (not just a script that always says
    OK), add a table with no policy and rerun:

    ```bash
    docker exec local-e2e-clickhouse-1 clickhouse-client -q "
      CREATE TABLE cbioportal_authz_e2e.forgotten_table (secret String) ENGINE=Memory
    "
    uv run python scripts/verify_row_policy_coverage.py
    # Expect exit code 1 and `forgotten_table` listed as unclassified.

    docker exec local-e2e-clickhouse-1 clickhouse-client -q "
      DROP TABLE cbioportal_authz_e2e.forgotten_table
    "
    ```

## Local Test Users

| User | Password | Keycloak group | Allowed study |
| ---- | -------- | -------------- | ------------- |
| `alice` | `alice` | `/research/alpha` | `study_alpha` |
| `bob` | `bob` | `/research/beta` | `study_beta` |
| `admin-user` | `admin-user` | `/cbioportal/admins` | all studies |

Keycloak admin console:

```text
http://127.0.0.1:18080/
admin / admin
```

Protected MCP endpoint through the local proxy:

```text
http://127.0.0.1:8001/mcp/
```

Direct MCP endpoint, useful only for debugging trusted headers:

```text
http://127.0.0.1:8000/mcp
```
