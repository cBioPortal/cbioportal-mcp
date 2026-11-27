# Setting up study filtering (private portals)

Steps to run with `CBIOPORTAL_MCP_STUDY_FILTERING=on`: each user sees the public studies plus the studies granted to their email. Public portals don't need this; they run with filtering `off` (see [access-control.md](access-control.md)). How the row policies work: [study-filtering-design.md](study-filtering-design.md).

Emails below are placeholders. Real emails go only in the grant table, never in this repository or in committed env files.

## 1. Prerequisites

- **ClickHouse 24.10 or later**, with `custom_settings_prefixes` including `SQL_` (the default, also on Cloud). Tested on 25.8, 26.6 and Cloud 26.6.1.
- **A dedicated ClickHouse user for the MCP** that can change settings per query: no read-only settings profile. The MCP sends `readonly=1` itself on every query; grants (step 3) keep the user read-only.
- **An admin ClickHouse user** that can create roles, row policies, users and tables in the protected database. Use it only for steps 2 and 4.
- **Login**: Google OAuth and/or the LibreChat bridge ([access-control.md](access-control.md#login)).

## 2. Apply the row policies

Run the admin tool with admin credentials. Attach the policies either to a **role** (default `cbioportal_mcp_study_restricted`; the MCP user gets only this role) or directly to an **existing user** (`--policy-user`).

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_PORT=8443 CLICKHOUSE_SECURE=true
export CLICKHOUSE_ADMIN_USER=... CLICKHOUSE_ADMIN_PASSWORD=...

# Role
uv run python -m cbioportal_mcp.study_authz apply --database <protected_db> \
    --public-group <PUBLIC_GROUP> --app-name <app_name>
CBIOPORTAL_MCP_RESTRICTED_PASSWORD=... uv run python -m cbioportal_mcp.study_authz \
    user-sql --user <mcp_user> --database <protected_db> --apply
uv run python -m cbioportal_mcp.study_authz check --database <protected_db>

# ...or an existing user
uv run python -m cbioportal_mcp.study_authz apply --database <protected_db> \
    --public-group <PUBLIC_GROUP> --app-name <app_name> --policy-user <mcp_user>
uv run python -m cbioportal_mcp.study_authz check --database <protected_db> --target <mcp_user>
```

- `--public-group`: cBioPortal's `always_show_study_group`. A study is public when one of its `groups` entries equals it exactly (case-insensitive, spaces count).
- `--app-name`: cBioPortal's `app.name`, kept for future token-based grants.
- `render` (same flags as `apply`) prints the SQL instead of running it.
- `apply` refuses a database that has row policies it didn't create, and won't move its policies to another role or user. Re-applying is safe. Run `check` after every `apply`, and let only one person administer row policies at a time.
- The tool refuses database names containing `public` (so it can't touch the public clone) unless you pass `--allow-public-database-name`.

**Databases set up before the `user_study` view** (a `user_study` table plus a `user_study_refresh` view) are refused by `apply`, `check` and the server. Migrate the copy that serves no traffic with `apply --migrate` (same flags): it rebuilds `user_study`, keeps the manual grants and waits for the first refresh. Then `check`, restart the MCP and switch traffic.

## 3. Grants for the MCP user

The MCP user (or its role) needs exactly what `apply` grants:

```sql
GRANT SHOW ROW POLICIES ON <protected_db>.* TO <target>;
GRANT SELECT ON <protected_db>.* TO <target>;
```

Anything more disables study data at startup:

- grants on `system` tables, global (`*.*`) grants, `GRANT OPTION`/`ADMIN OPTION`;
- table- or column-level grants on any database (only whole-database `ON <db>.*` grants are accepted);
- writes, DDL, table functions, `dictGet`, temporary tables;
- for a role: a direct `SELECT` next to the role (`SET ROLE NONE` would bypass the policies), or a role that isn't a default role. `user-sql` sets this up correctly.

**Simplest: the MCP user reads only the protected database.** Every other readable database is audited at startup; one that looks like study data must be covered by policies or listed in `CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES`.

## 4. Grant studies to users

The policies read `user_study`, a view that rebuilds itself every 5 minutes from the public studies and from `user_study_manual`, which you maintain:

```sql
INSERT INTO <protected_db>.user_study_manual (email, grant_key, valid_until, note)
VALUES ('tester-a@example.org', 'TEAM_A', '2027-01-01 00:00:00', 'ticket-123');

-- revoke
DELETE FROM <protected_db>.user_study_manual
WHERE email = 'tester-a@example.org' AND grant_key = 'TEAM_A';

-- optional: apply now instead of at the next refresh
SYSTEM REFRESH VIEW <protected_db>.user_study;
```

`grant_key` is a study id, a whole `groups` entry, or `ALL` / `ALL_TCGA` / `ALL_NCI_TARGET`. Emails are compared lower-cased. Grant changes need no MCP restart. If the refresh stops, private grants expire after 15 minutes (`--private-grace-minutes`) and only public studies stay visible; `check` reports a failing refresh.

## 5. Configure and start the MCP

```bash
export CLICKHOUSE_USER=<mcp_user> CLICKHOUSE_PASSWORD=...
export CLICKHOUSE_DATABASE=<protected_db>

export CBIOPORTAL_MCP_STUDY_FILTERING=on
export CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce
export CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce
export CBIOPORTAL_MCP_AUTH_TEST_USERS=
export CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES=''

# Login: Google OAuth and/or the LibreChat bridge
export CBIOPORTAL_MCP_GOOGLE_CLIENT_ID=... CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET=...
export CBIOPORTAL_MCP_GOOGLE_BASE_URL=https://<this-server>
# export CBIOPORTAL_MCP_BRIDGE_SECRET=...
```

`CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES`: leave it `''` when the MCP user reads only the protected database. Otherwise list the other readable databases that hold **only data every logged-in user may see**. Listing grants nothing and never overrides the startup checks. Never list a private copy to make a startup error go away.

A successful start logs:

```
Study filtering: <mcp_user> is covered by the row policies on <protected_db> (role cbioportal_mcp_study_restricted, <N> objects)
cBioPortal MCP rollout switches: study_filtering=on auth_enforcement=enforce auth_test_users_count=0 public_db_check=enforce
```

and `/health` shows `study_data_available: true, reason: ready`. On failure the server runs in degraded mode (see [access-control.md](access-control.md#when-something-fails-degraded-mode-and-health)); fix the cause and restart.

## 6. Verify

As the **MCP user** (an admin sees everything), e.g. `clickhouse-client --user <mcp_user>`:

```sql
-- Granted: public + granted studies
SELECT cancer_study_identifier FROM <protected_db>.cancer_study ORDER BY 1
SETTINGS SQL_cbio_user = 'tester-a@example.org';

-- Ungranted, and no email at all: public studies only (identical results)
SELECT cancer_study_identifier FROM <protected_db>.cancer_study ORDER BY 1
SETTINGS SQL_cbio_user = 'nobody@example.org';
SELECT cancer_study_identifier FROM <protected_db>.cancer_study ORDER BY 1
SETTINGS SQL_cbio_user = '';

-- The grant tables leak nothing
SELECT DISTINCT email FROM <protected_db>.user_study
SETTINGS SQL_cbio_user = 'tester-a@example.org';   -- '' and tester-a only
SELECT count() FROM <protected_db>.user_study_manual
SETTINGS SQL_cbio_user = 'tester-a@example.org';   -- 0
```

Then through the MCP, signed in as a granted and an ungranted user: `list_studies` and raw SQL show the same split, `get_study_guide` on a gated study fails for the ungranted user, and SQL that sets or reads `SQL_cbio_user` is rejected.

## 7. Rollback

**Order matters.** Once the policies are dropped, anyone who can still `SELECT` from the protected database sees every study. Changing `CLICKHOUSE_DATABASE` is not enough: the grant stays, and fully qualified table names still work.

1. Set `CBIOPORTAL_MCP_STUDY_FILTERING=off` and `CBIOPORTAL_MCP_PUBLIC_DB_CHECK=off`, restart, and check `/health` shows `reason: filtering_off`. With no email sent, the policies show the MCP only public studies.
2. Remove the MCP's access to the protected database **before dropping any policy**, either:
   - `REVOKE SELECT, SHOW ROW POLICIES ON <protected_db>.* FROM <target>;` (the role or the user), or
   - switch the MCP to a different `CLICKHOUSE_USER` with no access, and `DROP USER <old_mcp_user>`.
3. Confirm nobody non-admin can still read it. As the MCP user: `CHECK GRANT SELECT ON <protected_db>.*` returns 0 and `SELECT count() FROM <protected_db>.cancer_study` fails with `ACCESS_DENIED`. As admin:
   ```sql
   SELECT user_name, role_name, access_type, database FROM system.grants
   WHERE access_type IN ('SELECT', 'ALL') AND (database = '<protected_db>' OR database IS NULL);
   ```
4. Only then drop the policies (review the generated statements before running them):
   ```sql
   SELECT format('DROP ROW POLICY IF EXISTS `{}` ON `{}`.{};', short_name, database,
                 if(table = '', '*', concat('`', table, '`')))
   FROM system.row_policies
   WHERE database = '<protected_db>' AND startsWith(short_name, 'cbioportal_mcp_')
   ORDER BY table = '' FORMAT TSVRaw;
   ```
   Optionally drop `user_study`, `user_study_manual` and the role.

## Known limits

- **Checks run at startup only.** Restart the MCP after any grant, role, policy or schema change. (Changes to `user_study_manual` don't need a restart.)
- **Renamed copies can slip past the audit.** Other databases are recognised as study data by table and column names; a copy that renames both is not detected. The safest setup is an MCP user that reads only the protected database.
- **Only manual grants exist today.** Token-based (JWT/Keycloak) grants are designed but not built ([study-filtering-design.md](study-filtering-design.md#future-per-email-jwt-source)).
