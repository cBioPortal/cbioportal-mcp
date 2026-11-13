# Study filtering rollout runbook (private deployments)

This runbook turns on `CBIOPORTAL_MCP_STUDY_FILTERING=on` for a **private**
deployment: one whose ClickHouse database holds studies that not every
logged-in user may see. The design and the full list of checks are in
[`study-authz.md`](study-authz.md); this page is the operator checklist.

**The public deployment keeps filtering `off` permanently.** The public clone
(`scripts/apply_sql.sh` over `sql/`) has no gated studies and never gets row
policies. P4's tool refuses any database name containing `public` unless you
pass `--allow-public-database-name`. Do not pass it for the public clone.

All emails below are placeholders (`@example.org`). Real tester emails belong
only in the grant table (`user_study_manual`), never in this repository, in
env files that are committed, or in tickets.

## 1. Prerequisites

- **ClickHouse 24.10 or later.** The policies use `getSettingOrDefault`, and
  the grant refresh is a refreshable materialized view. Tested on Docker 25.8
  and 26.6, and on ClickHouse Cloud 26.6.1.
- **`custom_settings_prefixes` includes `SQL_`.** It does by default (also on
  Cloud). The MCP sends the caller's email as the custom setting
  `SQL_cbio_user`.
- **The MCP's ClickHouse user must be able to change settings per query
  (`readonly=0`).** The MCP sends `readonly=1` together with `SQL_cbio_user`
  on every study-data query. A user or settings profile that already forces
  `readonly` rejects those per-query settings, and the startup check disables study data
  (`SQL_cbio_user rejected`). Do not attach a read-only settings profile; the
  user's grants keep it read-only instead (see §3).
- **An admin ClickHouse user** that can `CREATE ROLE`, `CREATE ROW POLICY`,
  `CREATE USER` and create tables/views in the protected database. Use it only
  for the steps in §2 and §4, never as the MCP runtime user.
- **Login.** `CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce` needs Google OAuth (all
  three `CBIOPORTAL_MCP_GOOGLE_*` variables) and/or the LibreChat bridge
  (`CBIOPORTAL_MCP_BRIDGE_SECRET`). See the README's "Required login" section.

## 2. Generate and apply the row policies

Use P4's tool with admin credentials. Choose **one** policy target:

- **Role target** (default role `cbioportal_mcp_study_restricted`): `apply`
  creates the role; the runtime user gets the role and nothing else.
- **User target** (`--policy-user <existing_user>`): the policies name an
  existing runtime user directly. Use this when the runtime user is already
  provisioned (for example it also reads other databases).

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_PORT=8443 CLICKHOUSE_SECURE=true
export CLICKHOUSE_ADMIN_USER=... CLICKHOUSE_ADMIN_PASSWORD=...

# Role target
uv run python -m cbioportal_mcp.study_authz apply --database <protected_db> \
    --public-group <PUBLIC_GROUP> --app-name <app_name>
CBIOPORTAL_MCP_RESTRICTED_PASSWORD=... uv run python -m cbioportal_mcp.study_authz \
    user-sql --user <mcp_user> --database <protected_db> --apply
uv run python -m cbioportal_mcp.study_authz check --database <protected_db>

# ...or user target
uv run python -m cbioportal_mcp.study_authz apply --database <protected_db> \
    --public-group <PUBLIC_GROUP> --app-name <app_name> --policy-user <mcp_user>
uv run python -m cbioportal_mcp.study_authz check --database <protected_db> \
    --target <mcp_user>
```

`render` (same flags as `apply`) prints the SQL instead of running it.

**`--public-group` and `--app-name` have no defaults.** Pass the same values
the cBioPortal webapp uses:

- `--public-group`: cBioPortal's `always_show_study_group`. A study is public
  when one whole entry of its `cancer_study.groups` (split on `;`, compared
  case-insensitively, **not trimmed**) equals this value. `'A; PUBLIC'` does
  not make a study public because of the space.
- `--app-name`: cBioPortal's `app.name`. Recorded now for the future JWT grant
  source (cBioPortal `<app>:<group>` authority matching); it does not affect
  the manual source.

`apply` refuses a database that already has row policies it did not create
(any policy not named `cbioportal_mcp_*`), and refuses to move its policies to
a different target. Re-applying to the same target is safe. Run `check` after
every `apply`, and let only one person or job administer row policies on the
database at a time.

### Migrating a database applied before the `user_study` view

Older databases have a plain `user_study` table written by a separate
`user_study_refresh` view (`REFRESH ... TO user_study`). That refresh's table
swap could make filtered queries silently return no rows (measured: 31/400 on
25.8, 23/400 on 26.6 during a forced refresh loop). `apply`, `check` and the
MCP startup check now refuse that layout; `apply` says so in one line and
changes nothing. To migrate:

1. Pick the copy that serves **no traffic**: the inactive blue/green
   database, or this database with study filtering off. Between the drop and
   the create, filtered queries fail with `UNKNOWN_TABLE` (not silent zeros);
   after the create and until the first refresh finishes, `user_study` is
   empty, so filtered queries see no studies at all.
2. `uv run python -m cbioportal_mcp.study_authz apply --migrate --database <protected_db> --public-group <PUBLIC_GROUP> --app-name <app_name> [--policy-user <mcp_user>]`
   drops `user_study_refresh` and the `user_study` table, creates the
   `user_study` refreshable view, re-applies the policies and waits for the
   first refresh. `user_study_manual` is kept.
3. `check`, then restart the MCP (its startup check re-runs) and switch traffic.

## 3. Grants for the MCP runtime user

The runtime user (or, for a role target, the role) needs exactly:

```sql
GRANT SHOW ROW POLICIES ON <protected_db>.* TO <target>;
GRANT SELECT ON <protected_db>.* TO <target>;
```

`apply` issues both, after every policy, so the target never sees an
unfiltered table.

- **No `system` grants of any kind** (`system.*`, `system.query_log`,
  `system.grants`, ...). The startup check degrades the server for these grants.
- **No table- or column-level grants**, on any database. Only database-wide
  `ON <db>.*` grants are accepted; narrower grants hide columns from the
  startup audit, so they disable study data.
- No global (`*.*`) grants, no `GRANT OPTION` / `ADMIN OPTION`, no write,
  DDL, `REMOTE`/table-function, `dictGet` or `CREATE TEMPORARY TABLE`
  privileges.
- Role target: the user's only direct grant is the role, which must be a
  **default** role. A direct `SELECT` next to the role lets `SET ROLE NONE`
  bypass the policies; startup disables study data. `user-sql` produces this shape.

**Recommended: the MCP database user can read only the protected database.**
Every other database the user can `SELECT` from is audited at startup, and
any that looks like study data must either be covered by our policies or be
declared public in `CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES` (§5).
The simplest safe setup is a dedicated user with nothing else.

## 4. Populate grants (`user_study`)

The row policies read only `<protected_db>.user_study`. Never write it
directly: it is itself a refreshable materialized view that rebuilds itself from

- the public studies (stored under the empty email `''`), and
- `user_study_manual`, which an admin maintains:

```sql
INSERT INTO <protected_db>.user_study_manual (email, grant_key, valid_until, note)
VALUES ('tester-a@example.org', 'TEAM_A', '2027-01-01 00:00:00', 'ticket-123');

-- revoke
DELETE FROM <protected_db>.user_study_manual
WHERE email = 'tester-a@example.org' AND grant_key = 'TEAM_A';

-- optional: apply now instead of at the next refresh
SYSTEM REFRESH VIEW <protected_db>.user_study;
```

`grant_key` is a study id, a whole `cancer_study.groups` entry, or `ALL` /
`ALL_TCGA` / `ALL_NCI_TARGET`. Emails are compared lower-cased; the
MCP sends the verified login email lower-cased too.

**Grants refresh every 5 minutes** (`apply --refresh-minutes`, default 5).
New grants and revocations take effect at the next refresh, with no MCP
restart. If the refresh stops, private rows expire after the grace period
(`--private-grace-minutes`, default 15) and only public studies stay visible.
`check` reports a failing refresh.

## 5. Configure and start the MCP

```bash
# Connect as the runtime user, to the protected database.
export CLICKHOUSE_HOST=... CLICKHOUSE_PORT=8443 CLICKHOUSE_SECURE=true
export CLICKHOUSE_USER=<mcp_user> CLICKHOUSE_PASSWORD=...
export CLICKHOUSE_DATABASE=<protected_db>

export CBIOPORTAL_MCP_STUDY_FILTERING=on
export CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce
export CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce
export CBIOPORTAL_MCP_AUTH_TEST_USERS=          # must be set; ignored under enforce
export CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES=''   # see below

# Login: Google OAuth and/or the LibreChat bridge
export CBIOPORTAL_MCP_GOOGLE_CLIENT_ID=... CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET=...
export CBIOPORTAL_MCP_GOOGLE_BASE_URL=https://<this-server>
# export CBIOPORTAL_MCP_BRIDGE_SECRET=...
```

With `CBIOPORTAL_MCP_STUDY_FILTERING=on`, study data remains blocked unless
**all** of these hold (the server stays running in degraded mode on failure):

1. `CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce`;
2. `CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce` (see "Known limits": the check
   behind this switch, P10, is not built yet);
3. `CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES` is explicitly set;
4. the database-side startup check passes (study-authz.md, "Server startup
   check").

**`CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES`** has no default. Set it to
`''` when the runtime user reads only the protected database (recommended).
Otherwise list, comma-separated, the other readable databases that hold
**only data every logged-in user may see**. Listing a database does not grant
access and never overrides the unsafe-object checks. Never list a private
source copy to clear degraded status.

**Success** looks like these INFO lines, in this order:

```
ClickHouse query pool: 4 workers
🔐 Checking ClickHouse privileges for user '<mcp_user>' on DB '<protected_db>'.
✅ ClickHouse permission checks passed for user '<mcp_user>' on DB '<protected_db>'.
Study filtering: <mcp_user> is covered by the row policies on <protected_db> (role cbioportal_mcp_study_restricted, <N> objects)
cBioPortal MCP startup: study_filtering=on transport=http
cBioPortal MCP rollout switches: study_filtering=on auth_enforcement=enforce auth_test_users_count=0 public_db_check=enforce
```

(`(user <mcp_user>, ...)` for a user target.) A study-filtering failure logs
one redacted `CRITICAL` line at startup and leaves the server running. Every
refused call logs one redacted line and returns an MCP tool/resource error,
never an empty or zero result. Raw SQL, study lists, study/deployment guides
(including static files), frequency tools and direct resource reads are all
blocked, including public data. Login, schema listings and OncoTree remain
available, with login admission still enforced as configured. No study cache
is preloaded in degraded mode. Checks run once per process; fix configuration
or grants and restart to retry.

`GET /health` returns only `study_data_available` (boolean) and `reason`:
`checks_pending`, `rollout_not_enforced`, `filtering_check_failed`, `ready`,
or `filtering_off`. Normal uvicorn startup binds the listening socket only
after the lifespan and checks finish: probes during checks get connection
refused, not HTTP 503. Hosts that skip the lifespan can reach the endpoint
before checks and receive HTTP 503 (`checks_pending`). After normal startup
it returns HTTP 200 even when degraded. This keeps Kubernetes readiness
routing login and schema requests to a running server without restart loops. Operators must
inspect the JSON availability flag or the startup log; HTTP 200 alone does
not establish study-data availability. No identities, secrets, hosts or
study counts appear in the response.

Refused calls stop before the telemetry middleware and do **not** appear in
tool metrics. Alert on the `Study data unavailable` refusal log line or on
`/health` JSON with `study_data_available: false`. A probe that checks only
HTTP status 200 will miss degraded mode.

The general ClickHouse permission gate still exits with code 2. Missing or
invalid filtering/rollout switch values, login configuration errors and
transport/stdio violations still refuse startup. Driver errors are reported
by class and ClickHouse error code, never free-form text.

Log records from the ClickHouse client stack (`mcp-clickhouse`,
`clickhouse_connect`, `urllib3`) and from this package are scrubbed when they
are created: credential values (URL userinfo; `password`, `passwd`, `pwd`,
`secret`, `token`, `api_key`, `client_secret`, `authorization` keys with or
without quotes and with `=` or `:`; the **whole** value of any
`Authorization` / `Proxy-Authorization` header, whatever its scheme, including
Digest parameter lists; bare Basic/Bearer/Digest credentials) are replaced
with `<redacted>`. Redaction fails closed and may remove more than the secret:
an unquoted `Authorization` value takes the rest of its line, including any
neighbouring fields on it (and folded continuation lines); a quoted value that
never closes, or closes mid-token, takes everything to the end of the message. WARNING-and-above records are collapsed to one physical
line, with any traceback or stack folded into it after ` | `. A message that
cannot be rendered is logged as `<unloggable message redacted>`; scrubbing
never raises into the code that logs. Use the error code and check name to diagnose; reproduce with
`clickhouse-client` as the runtime user when you need the full server text.

## 6. Verify

After each restart, check `/health` for `study_data_available: true` and
`reason: ready`, and inspect the startup log. Process uptime or HTTP 200 is
insufficient. In a deliberately misconfigured test deployment, confirm
`study_data_available: false`, a stable failure reason and actual MCP errors
with no rows from both raw SQL and `list_studies`; public studies must also
be blocked. Login, schema listings and OncoTree should remain usable.

Run as the **runtime user** (an admin is in the policies' `ALL EXCEPT`
catch-all and sees everything), e.g. with `clickhouse-client --user <mcp_user>`.
Use a granted placeholder email, an ungranted one, and the empty email:

```sql
-- Granted: public studies plus the granted ones
SELECT cancer_study_identifier FROM <protected_db>.cancer_study ORDER BY 1
SETTINGS SQL_cbio_user = 'tester-a@example.org';

-- Ungranted: public studies only
SELECT cancer_study_identifier FROM <protected_db>.cancer_study ORDER BY 1
SETTINGS SQL_cbio_user = 'nobody@example.org';

-- Empty email (what a query without identity gets): public studies only,
-- identical to the ungranted result
SELECT cancer_study_identifier FROM <protected_db>.cancer_study ORDER BY 1
SETTINGS SQL_cbio_user = '';

-- The grant tables leak nothing: only the caller's own rows and the public
-- rows in user_study; user_study_manual returns no rows.
SELECT DISTINCT email FROM <protected_db>.user_study
SETTINGS SQL_cbio_user = 'tester-a@example.org';      -- '' and tester-a only
SELECT count() FROM <protected_db>.user_study_manual
SETTINGS SQL_cbio_user = 'tester-a@example.org';      -- 0
```

Then through the MCP, signed in as a granted and an ungranted account:
`list_studies` shows the same split; a raw `clickhouse_run_select_query` over
a gated table with no study filter returns gated rows only for the granted
account; `get_study_guide` on a gated study is unavailable to the ungranted
account; ungranted callers get the public-only note; and SQL that sets or
reads `SQL_cbio_user` is rejected.

## 7. Rollback

Order matters. While the policies exist and the MCP sends no email, the
runtime user sees only public studies in the protected database. **Once the
policies are dropped, every principal that can still `SELECT` from the
protected database reads every study.** Changing `CLICKHOUSE_DATABASE` is
**not** enough: it only changes the default database, the old grant stays, and
filtering-off SQL accepts fully qualified names such as
`SELECT * FROM <protected_db>.cancer_study`.

1. Set `CBIOPORTAL_MCP_STUDY_FILTERING=off` and restart the MCP. (It no longer
   sends `SQL_cbio_user`, so the policies show it public studies only.)
   Verify `/health` reports `study_data_available: true`, `reason: filtering_off`,
   and check the startup log. A previous degraded status does not survive restart.
2. Remove the runtime access to the protected database, **before any policy is
   dropped**, with exactly one of:
   - **A. Revoke** (admin) from the policy target, i.e. the role for a role
     target or the user for a user target:
     ```sql
     REVOKE SELECT, SHOW ROW POLICIES ON <protected_db>.* FROM <target>;
     ```
   - **B. Replace the credentials:** restart the MCP with a different
     `CLICKHOUSE_USER` that has no grant on the protected database, then
     `DROP USER <old_mcp_user>` (admin). The old user must go too: otherwise
     anyone holding its password reads everything after step 4.
3. Verify, as the runtime user the MCP now uses (e.g.
   `clickhouse-client --user <mcp_user>`), that it can no longer read the
   protected database:
   ```sql
   CHECK GRANT SELECT ON <protected_db>.*;               -- must return 0
   SELECT count() FROM <protected_db>.cancer_study;     -- must fail: ACCESS_DENIED (497)
   SHOW GRANTS FINAL;   -- no line may cover <protected_db>.* or *.*
   ```
   and, as admin, that no other non-admin principal still can:
   ```sql
   SELECT user_name, role_name, access_type, database
   FROM system.grants
   WHERE access_type IN ('SELECT', 'ALL') AND (database = '<protected_db>' OR database IS NULL);
   ```
   Do not continue until both checks are clean.
4. Drop the policies (admin). Generate the statements, review, then run them:
   ```sql
   SELECT format('DROP ROW POLICY IF EXISTS `{}` ON `{}`.{};', short_name, database,
                 if(table = '', '*', concat('`', table, '`')))
   FROM system.row_policies
   WHERE database = '<protected_db>' AND startsWith(short_name, 'cbioportal_mcp_')
   ORDER BY table = ''
   FORMAT TSVRaw;
   ```
   (The database-wide policies have an empty `table`.) Optionally drop
   `user_study` (the view; its inner tables go with it) and
   `user_study_manual`, and the role.

P4 only manages row policies it created. `apply` refuses a database that has
row policies it did not create, so remove or migrate any such policy by hand
before re-applying.

## Known limits

- **Startup-only check.** Grants, roles, policies, columns and engines are
  inspected only when the MCP starts. **Restart the MCP after any grant,
  policy, role or schema change** so the check runs against the new state.
  (`user_study` grant changes do not need a restart.)
- **Renamed study columns can evade the startup check.** Other readable
  databases are classified as study data by table names and study-link column
  names. A copy that renames both the table and every recognized linkage
  column is not detected. Operators must make sure no other readable database
  holds gated data under disguised names; the simplest guarantee is a runtime
  user that reads only the protected database.
- **JWT / Keycloak grant sources are not built yet.** Only the manual source
  (`user_study_manual`) feeds `user_study` today; the JWT layout is reserved
  (study-authz.md, "Future per-email (JWT) source").
- **P10 is pending: `CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce` currently has no
  runtime effect.** It is required for `on` (so a later release can add
  behaviour without changing deployments), validated and shown in the startup
  log, but nothing acts on it. The planned P10 check (with filtering off,
  refuse startup when the database has `cancer_study.public = 0`, group names
  outside the public group or an allowlist, or non-empty authorities) does not
  exist. Today nothing stops a filtering-**off** server from being pointed at
  a database with private studies; keeping private data out of
  filtering-off deployments is an operational responsibility until P10 ships.
  With filtering `on`, the P6 startup check above is what protects the data.
