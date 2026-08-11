# Local Keycloak Study Authz E2E

This stack validates the restricted-study access flow end to end:

1. Keycloak issues a bearer token for a local user.
2. A local dev auth proxy validates the token via Keycloak userinfo.
3. The proxy strips spoofable identity headers and injects trusted headers.
4. cBioPortal MCP maps Keycloak groups to allowed studies through `study-acl.json`.
5. cBioPortal MCP passes the resolved allowlist to ClickHouse as
   `SQL_cbiomcp_allowed_studies`.
6. ClickHouse row policies filter direct study tables and indirect tables
   where study provenance is derived through a join: `mutation` and
   `clinical_event` are one join away (`sample_id`/`patient_id`), and
   `clinical_event_data` is two joins away (`clinical_event_id` ->
   `clinical_event.patient_id` -> `patient.cancer_study_identifier`).
7. A database-wide default-deny row policy (`ON cbioportal_authz_e2e.*
   USING 0`) backstops every table, including ones added later. ClickHouse
   combines multiple PERMISSIVE row policies on a table with OR, so a table
   with no policy of its own is stuck at `0` (nothing visible) even though it
   inherits the database-wide `GRANT SELECT` the server's startup check
   requires; a table with its own policy gets `0 OR <real condition>`. This
   was verified directly against a running ClickHouse 24.12 container: a
   table created *after* the default-deny policy, with no policy of its own,
   returned zero rows to the restricted user rather than leaking. See
   `tests/test_clickhouse_init_sql_coverage.py`, which fails CI if a table in
   this file is missing its own row policy or isn't classified in
   `study_access.py`'s `PROTECTED_QUERY_MARKERS` / `STUDY_AGNOSTIC_REFERENCE_TABLES`.
8. `scripts/verify_row_policy_coverage.py` performs the same coverage check
   *live*, against a running ClickHouse's `system.tables` / `system.row_policies`,
   rather than by parsing the mock SQL file - this is what a real deployment
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
   this admin identity needs only `GRANT SHOW TABLES ON <db>.*` (metadata,
   no row data) plus `GRANT SELECT ON system.row_policies` - nothing else.

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

Expected output:

```text
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
   LOCAL_AUTH_PROXY_UPSTREAM_MCP_URL: http://mcp:8000/mcp/
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
   - Alice can query raw `mutation` without a study column and receives only
     the mutation derived from `study_alpha` through `mutation.sample_id`
   - Alice can query raw `clinical_event_data`, which has no study *or*
     patient column, and receives only the row two joins removed from her
     study (`clinical_event_data` -> `clinical_event.patient_id` ->
     `patient.cancer_study_identifier`)

6. Expected success output:

   ```text
   Local Keycloak authz e2e passed.
   Alice access: {'user_id': '...', 'user_email': 'alice@example.org', ...}
   Bob access: {'user_id': '...', 'user_email': 'bob@example.org', ...}
   ```

7. If the indirect mutation assertion fails with `UNKNOWN_TABLE mutation`,
   the ClickHouse volume was initialized before the mutation seed table existed.
   Run `docker compose -f docker/local-e2e/docker-compose.yml down -v`, start
   the stack again, and rerun the verifier.

8. Run the full test suite, including the SQL-file drift guard:

   ```bash
   uv run pytest -q
   ```

   `tests/test_clickhouse_init_sql_coverage.py` fails if any table in
   `clickhouse-init.sql` is missing the wildcard grant, the default-deny
   wildcard row policy, its own table-specific row policy, or a
   classification in `study_access.py`'s `PROTECTED_QUERY_MARKERS` /
   `STUDY_AGNOSTIC_REFERENCE_TABLES`.

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

    Expect `OK: every table has row-policy coverage ...` and exit code 0. To
    confirm it actually catches a gap (not just a script that always says
    OK), add a table with no policy and rerun:

    ```bash
    docker exec local-e2e-clickhouse-1 clickhouse-client -q "
      CREATE TABLE cbioportal_authz_e2e.forgotten_table (secret String) ENGINE=Memory
    "
    uv run python scripts/verify_row_policy_coverage.py
    # Expect exit code 1 and `forgotten_table` listed as a coverage gap.

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
http://127.0.0.1:8000/mcp/
```
