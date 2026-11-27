# Access control

The server can run three ways. Pick one with four environment variables.

| Setup | Who it's for | What it does |
|---|---|---|
| **Off** | Public portals (the default for every current deployment) | No login checks beyond the existing Google/bridge setup, no study filtering. |
| **Public-database check** | Public portals that want a safety net | At startup, refuses to serve study data if the database contains anything that looks private. |
| **Study filtering** | Private portals with studies not every user may see | Each user sees public studies plus the studies granted to their email. Needs login and ClickHouse row policies. |

## The switches

All four are required over HTTP/SSE: the server refuses to start if one is missing or misspelled.

| Variable | Values | Meaning |
|---|---|---|
| `CBIOPORTAL_MCP_STUDY_FILTERING` | `off` / `on` | Filter studies per user (needs the row policies, see [setup](study-filtering-setup.md)). |
| `CBIOPORTAL_MCP_AUTH_ENFORCEMENT` | `off` / `enforce` | `enforce`: every request needs a verified login. |
| `CBIOPORTAL_MCP_AUTH_TEST_USERS` | comma-separated emails, may be empty | With enforcement `off`, these users get the full login checks (for trying it out). Ignored under `enforce`. |
| `CBIOPORTAL_MCP_PUBLIC_DB_CHECK` | `off` / `enforce` | Run the public-database check at startup. |

**Turning it all off** (what every current deployment uses):

```bash
CBIOPORTAL_MCP_STUDY_FILTERING=off
CBIOPORTAL_MCP_AUTH_ENFORCEMENT=off
CBIOPORTAL_MCP_AUTH_TEST_USERS=
CBIOPORTAL_MCP_PUBLIC_DB_CHECK=off
```

With these values the server behaves as it did before access control existed: it checks its ClickHouse grants and serves.

Local stdio needs `CBIOPORTAL_MCP_LOCAL_DEV=1` and `CBIOPORTAL_MCP_STUDY_FILTERING`; the other three switches then default to off.

The startup log prints each value (and the number of test users, never their emails). The switches are temporary: the `off` paths are scheduled for removal after 2027-06-30 (`cbioportal_mcp.rollout.EXPIRES`).

## Login

Two login routes can run side by side on one endpoint.

- **Google OAuth** (e.g. the claude.ai connector): set `CBIOPORTAL_MCP_GOOGLE_CLIENT_ID`, `CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET` and `CBIOPORTAL_MCP_GOOGLE_BASE_URL` (all three, or the server refuses to start). Register `<base-url>/auth/callback` with the `openid email profile` scopes. Google must report the email as verified. Use `REDIS_URL` for OAuth client storage; without it, registrations live on local disk and are lost when the pod is replaced.
- **LibreChat bridge**: set the same `CBIOPORTAL_MCP_BRIDGE_SECRET` in both services. LibreChat sends `x-user-email`, `x-user-id`, `x-user-email-verified` (must be `true`, from real account verification) and `x-cbioportal-mcp-bridge-secret` on every request. Have the ingress strip these headers from client requests, and use TLS between the services.

How the enforcement switch changes things:

- **`enforce`**: every request, including listing tools, needs a verified login through one of the routes. At least one route must be configured.
- **`off`**: as before. With Google configured, a request needs a valid bearer token or bridge secret; without Google, anonymous requests are allowed. Emails in `CBIOPORTAL_MCP_AUTH_TEST_USERS` get the `enforce` checks.
- In both modes, invalid credentials are rejected, and a failed credential never falls back to the other route.

The old `AUTH_REQUIRED` / `ALLOW_UNAUTHENTICATED` settings no longer do anything.

FastMCP stays pinned at 2.13.3: a small adapter mounts the Google provider's OAuth endpoints and shares admission with the bridge, which avoids a FastMCP 3 upgrade (it conflicts with the current mcp-clickhouse).

## Public-database check

For a portal that should hold only public data. With filtering `off` and `CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce`, the server checks the connected database once at startup and blocks study data if:

- any study has `cancer_study.public = 0`,
- any study's `groups` entry is not in `CBIOPORTAL_MCP_PUBLIC_DB_ALLOWED_GROUPS`, or
- an `authorities` table exists and has rows.

`CBIOPORTAL_MCP_PUBLIC_DB_ALLOWED_GROUPS` is required in this mode: a comma-separated list, or an empty string if no groups are allowed. Build it from a copy you know is public:

```sql
SELECT DISTINCT arrayJoin(splitByChar(';', ifNull(groups, ''))) AS group_entry
FROM cancer_study WHERE group_entry != '' ORDER BY group_entry;
```

Entries are compared case-insensitively, but whitespace counts (as in cBioPortal): `' PUBLIC'` is not `PUBLIC`.

## Study filtering

Each user sees the public studies plus those granted to their email. ClickHouse enforces this with row policies, so it also holds for raw SQL. The server sends the user's verified email with every query. Setup, grants and rollback: [study-filtering-setup.md](study-filtering-setup.md). How it works and why: [study-filtering-design.md](study-filtering-design.md).

Study data is served only when all of these hold:

1. `CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce` and `CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce`,
2. `CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES` is set (empty string if the database user reads only the protected database), and
3. the startup check passes: the server's own database user is fully covered by the row policies and holds no grants that could get around them.

## When something fails: degraded mode and `/health`

A failed public-database check or study-filtering check doesn't stop the server. It keeps running in **degraded mode**: every study-data tool and resource returns an error (never an empty result), while login, schema listings and OncoTree keep working. The server logs one `CRITICAL` line saying which check failed. Fix the cause and restart: checks run only at startup.

Wrong switch values, broken login configuration and missing ClickHouse grants still refuse startup outright.

`GET /health` returns HTTP 200 once the server is up, even when degraded (so Kubernetes keeps routing login requests). Look at the JSON instead:

| `reason` | `study_data_available` | Meaning |
|---|---|---|
| `filtering_off` | true | Everything off, serving normally |
| `ready` | true | The enabled checks passed |
| `public_db_check_failed` | false | The public-database check found private data or couldn't run |
| `filtering_check_failed` | false | The study-filtering startup check failed |
| `rollout_not_enforced` | false | Filtering is on but a switch isn't `enforce` |
| `checks_pending` | false | Checks still running (HTTP 503; only seen on hosts that skip the startup hook) |

Alert on `study_data_available: false` or on the `Study data unavailable` log line, not on the status code. The Datadog monitor **Study data blocked** fires when the public-database check fails. Refused calls never reach the tool metrics.

## Logs

Log records from this package and the ClickHouse client stack are scrubbed when they are created: passwords, tokens, secrets and `Authorization` header values become `<redacted>`. Scrubbing errs on the side of removing too much. Database errors are reported as the exception class and ClickHouse error code; reproduce with `clickhouse-client` when you need the full text.
