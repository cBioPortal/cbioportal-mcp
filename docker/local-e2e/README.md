# Local Keycloak Study Authz E2E

This stack validates the restricted-study access flow end to end:

1. Keycloak issues a bearer token for a local user.
2. A local dev auth proxy validates the token via Keycloak userinfo.
3. The proxy strips spoofable identity headers and injects trusted headers.
4. cBioPortal MCP maps Keycloak groups to allowed studies through `study-acl.json`.
5. cBioPortal MCP passes the resolved allowlist to ClickHouse as
   `SQL_cbiomcp_allowed_studies`.
6. ClickHouse row policies filter direct study tables and an indirect
   `mutation` table where study provenance is derived through `sample_id`.

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
