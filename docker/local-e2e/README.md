# Local Keycloak Study Authz E2E

This stack validates the restricted-study access flow end to end:

1. Keycloak issues a bearer token for a local user.
2. A local dev auth proxy validates the token via Keycloak userinfo.
3. The proxy strips spoofable identity headers and injects trusted headers.
4. cBioPortal MCP maps Keycloak groups to allowed studies through `study-acl.json`.
5. ClickHouse queries are allowed or denied by study.

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
