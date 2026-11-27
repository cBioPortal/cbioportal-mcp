# How study filtering works

Reference for the row-policy side of study filtering: what the admin tool creates in ClickHouse, why, and what the server checks at startup. For the switches and login see [access-control.md](access-control.md); for setup steps see [study-filtering-setup.md](study-filtering-setup.md).

## Overview

```
verified login email ──► server sends it with every query as SQL_cbio_user (+ readonly=1)
                                        │
                                        ▼
            ClickHouse row policies on every study table:
            "row's study ∈ user_study for this email, or public"
                                        │
                                        ▼
     user_study (refreshed every 5 min) = public studies + user_study_manual grants
```

The admin tool (`python -m cbioportal_mcp.study_authz`, code in `src/cbioportal_mcp/study_authz/`) creates the policies and `user_study`. It only runs when invoked explicitly against a named database, and refuses names containing `public`; the public clone never gets policies.

## Requirements

- ClickHouse ≥ 24.10 (`getSettingOrDefault`, refreshable materialized views). Tested on 25.8, 26.6 and Cloud 26.6.1.
- `custom_settings_prefixes` includes `SQL_` (the default).
- An admin user that can `CREATE ROLE`, `CREATE ROW POLICY`, `CREATE USER` and create tables/views.

## Row policies

All policies attach to one **target**: a role (`--policy-role`, default `cbioportal_mcp_study_restricted`, which the MCP user is given) or an existing user (`--policy-user`). Everyone else gets `USING 1`, so admins and the refresh see all rows. `apply` creates the policies first and grants `SELECT ON <db>.*` and `SHOW ROW POLICIES ON <db>.*` last, so the target never sees an unfiltered table. The policies are scoped to the one database.

| Object | Policy for the target |
|---|---|
| every table (`db.*`) | `USING 0`: default deny, so a new or unclassified table returns nothing |
| study tables (`catalog.py`) | the row's study must be visible (below), found through its own `cancer_study_identifier` column or the foreign-key path to `cancer_study` that `catalog.py` defines |
| `user_study` | only the caller's rows and the public rows |
| reference tables (genes, panels, OncoTree, ...) | `USING 1` |
| views | `USING 1`; safe because every view is `SQL SECURITY INVOKER`, so the underlying tables' policies apply |
| `users`, `authorities`, `data_access_tokens`, `user_study_manual`, `user_study_jwt`, `user_study`'s inner tables | none, so the default deny returns zero rows |

A study is visible when its identifier is in:

```sql
SELECT cancer_study_identifier FROM <db>.user_study
WHERE (email = '' OR email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', '')))
  AND valid_until > now64(3)
```

After changing `catalog.py`, run `generate` to rewrite `templates/1-row-policies.sql.tmpl`; `tests/test_study_authz_sql.py` fails if you forget.

## Policy ownership

ClickHouse ORs permissive row policies together. Our catch-all `USING 1 TO ALL EXCEPT <target>` would therefore silently **widen** any other policy on these tables (a user limited by someone else's `USING cancer_study_id = 1` would see every study). So the tool treats every row policy on the database as its own, recognised by the `cbioportal_mcp_` name prefix:

- `apply` refuses, before changing anything, if the database has a policy with another name, or if our policies are attached to a different target (switching would leave the old target unfiltered).
- Re-applying to the same target is fine (`CREATE ... OR REPLACE`).
- `check` and the server's startup check fail in the same situations, catching a policy added later.

To restrict another principal inside such a database, use this tool rather than adding a policy. Only one person or job should administer row policies at a time, and `check` should follow every `apply` (the conflict check is a preflight, so a policy created in between would still be widened).

## The caller's email

The server sends the verified email, lower-cased, as the per-query setting `SQL_cbio_user` **together with `readonly=1`** on every study-data query:

- Under `readonly=1`, a `SETTINGS SQL_cbio_user = ...` in the query text is rejected, so a query can't impersonate someone else. The server also rejects SQL that mentions the setting, reads settings (`getSetting`, `getSettingOrDefault`) or sets any `SQL_` setting.
- A missing or empty setting means public studies only. That fails closed but would hide bugs, so the server refuses study-data queries without a verified email rather than relying on the fallback.
- Don't add a settings-profile default for `SQL_cbio_user`: every query without an identity would act as that email.
- clickhouse_connect refuses unknown settings client-side by default. The server sets `invalid_setting_action='send'`; `'drop'` would silently drop the identity.

With filtering on, the server also bypasses its study caches (study lists, generated study guides), so a revocation takes effect as soon as `user_study` refreshes.

## `user_study` and its sources

```
user_study(email, cancer_study_identifier, valid_until DateTime64(3,'UTC'),
           source LowCardinality(String), issued_at DateTime64(3,'UTC'))
ORDER BY (email, cancer_study_identifier)
```

`user_study` is a refreshable materialized view **without `TO`** (`REFRESH EVERY 5 MINUTE`). Each refresh rebuilds the whole snapshot in a hidden table and swaps it in, so readers always see a complete snapshot. The refresh runs as the admin who applied it, which is how it reads `user_study_manual`. Its hidden inner tables get no policy (default deny).

Why no `TO` table: with a separate target table, a query could resolve `user_study` just before a refresh swapped it and then look up the row policy under the old name, silently getting zero rows (measured 23–31 out of 400 reads during forced refreshes). Without `TO`, the name the policy is looked up by never moves (0/300). `test_refresh_swap_never_empties_policy_filtered_reads` guards this. `check` and the startup check accept a materialized view only for `user_study`, and only refreshable, without `TO` and without `APPEND` (which would never remove revoked grants).

Sources:

- **public**: one row with `email = ''` per study whose `groups` contains the public group (split on `;`, drop empty entries, compare whole entries case-insensitively, no trimming: `' PUBLIC'` is not public). These never expire.
- **manual**: `user_study_manual(email, grant_key, valid_until, note)`, maintained by an admin. `grant_key` is a study id, a whole `groups` entry, or `ALL` / `ALL_TCGA` / `ALL_NCI_TARGET`; the refresh expands it into one row per matching study. Emails are lower-cased; empty emails are ignored so they can never become public. Expired rows are skipped.
- **jwt**: not built yet (below).

**Failure modes fail closed.** Each private row expires at the earlier of its own `valid_until` and refresh time + 15 minutes (`--private-grace-minutes`, must exceed `--refresh-minutes`). If the refresh stops, private grants lapse within the grace period while public rows stay; a revoked grant stops at the next refresh.

A refreshable view rather than an external script: it runs inside ClickHouse (Cloud included), is created by the same `apply`, needs no CronJob or extra credentials, and its health is in `system.view_refreshes`.

### Future per-email (JWT) source

Goal: each login replaces that email's whole grant set, written by an INSERT-only account, and an older token never restores grants a newer one dropped. The table is reserved (and already denied):

```sql
CREATE TABLE <db>.user_study_jwt (
    email String, grant_key String,          -- normalized claim, '' = sentinel
    issued_at DateTime64(3, 'UTC'),          -- token iat: orders replacements
    valid_until DateTime64(3, 'UTC'),
    token_id String
) ENGINE = MergeTree ORDER BY (email, issued_at);
GRANT INSERT ON <db>.user_study_jwt TO <jwt_writer>;
```

The writer appends one row per grant key (normalized with `groups.normalize_authority`) plus a sentinel row with `grant_key = ''`, so a token with no grants still replaces an older one. The refresh gets one more `UNION ALL` branch keeping only each email's newest `issued_at`. No policy or schema change is needed. Grants then take effect at the next refresh (lower `--refresh-minutes` if needed).

cBioPortal's matching rules, from `CancerStudyPermissionEvaluator`, are in `groups.py`: strip a leading `ROLE_`; with `filter_groups_by_appname` only `<app>:<X>` authorities count (app name case-insensitive) and `X` is the key; the upper-cased key grants a study when it equals the study id or a whole `groups` entry; `ALL` grants everything, `ALL_TCGA` every `*_TCGA` study, `ALL_NCI_TARGET` every `*_TARGET` study plus `ALL_TARGET_PHASE1`/`2`. Manual grant keys are already bare keys.

## Admin tool

Admin credentials only, never the MCP user:

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_PORT=8443 CLICKHOUSE_SECURE=true
export CLICKHOUSE_ADMIN_USER=... CLICKHOUSE_ADMIN_PASSWORD=...

uv run python -m cbioportal_mcp.study_authz apply --database <db> --public-group PUBLIC --app-name cbioportal
uv run python -m cbioportal_mcp.study_authz check --database <db>
CBIOPORTAL_MCP_RESTRICTED_PASSWORD=... uv run python -m cbioportal_mcp.study_authz \
    user-sql --user <mcp_user> --database <db> [--policy-role <role>] [--apply]
```

- `apply` is idempotent. It keeps `user_study_manual` and updates `user_study` in place (`ALTER TABLE ... MODIFY QUERY/REFRESH/SQL SECURITY/COMMENT`), then waits for one refresh.
- `render` prints the SQL; `generate` rewrites the policy template from `catalog.py`.
- `user-sql` creates the MCP user with only the role (`CREATE USER OR REPLACE`, which drops old direct grants). `--user-target` creates a user that is itself the policy target, with a plain `CREATE USER` so it never replaces an existing one.
- `--public-group` and `--app-name` have no defaults.

Refresh status and forcing a refresh:

```sql
SELECT status, last_success_time, exception FROM system.view_refreshes
WHERE database = '<db>' AND view = 'user_study';
SYSTEM REFRESH VIEW <db>.user_study;   -- then SYSTEM WAIT VIEW <db>.user_study;
```

Manual grants: see [study-filtering-setup.md](study-filtering-setup.md#4-grant-studies-to-users).

### Migrating from `user_study_refresh`

Older databases have a plain `user_study` table filled by a `user_study_refresh` view. `apply` refuses them in one line and changes nothing; `check` and a filtering-on server refuse them too. To migrate, take the copy that serves no traffic (the inactive blue/green database, or run with filtering off): between the drop and the create, filtered queries fail with `UNKNOWN_TABLE`, and until the first refresh they see no studies. Then:

```bash
uv run python -m cbioportal_mcp.study_authz apply --migrate --database <db> --public-group PUBLIC --app-name cbioportal
```

It drops both old objects, creates the view, re-applies the policies and waits for the first refresh; `user_study_manual` is untouched. Run `check`, then switch traffic.

### Test database

`make-test-db` builds a small database from a prepared source: the chosen studies' rows in every study table, the reference tables, the views, never the auth tables. It sets `groups` so `--gate` studies are private and `--public` studies public, applies the policies and seeds grants from a CSV (empty `valid_until` = 7 days):

```bash
cat > grants.csv <<'EOF'
email,grant_key,valid_until
tester-a@example.org,TEAM_A,2027-01-01T00:00:00Z
tester-b@example.org,study_c,
EOF
uv run python -m cbioportal_mcp.study_authz make-test-db \
    --source-database <prepped_source_db> --database authz_e2e_test \
    --gate study_a=TEAM_A --gate study_b=TEAM_A --gate study_c \
    --public study_d --public study_e \
    --public-group PUBLIC --app-name cbioportal --grants-csv grants.csv
CBIOPORTAL_MCP_RESTRICTED_PASSWORD=... uv run python -m cbioportal_mcp.study_authz \
    user-sql --user mcp_e2e --database authz_e2e_test --apply
```

`tester-a` then sees study_a, study_b and the public studies; `tester-b` sees study_c and the public ones; everyone else only the public ones. Add `--policy-user <existing_user>` to attach the policies to an existing user instead (then `check --target <user>`). `--replace` rebuilds the database only if the tool created it. Real study ids, groups and emails belong only in your local CSV.

## Server startup check

With filtering on, the server checks at startup, as its own ClickHouse user, that the row policies really cover it. Any failure puts it in degraded mode (study data blocked, one redacted `CRITICAL` log line naming the check). It checks:

- **Identity reaches the policies**: `SQL_cbio_user` is accepted under `readonly=1`.
- **Coverage**: the same rules as `check` over the policies that apply to this user and its default roles. Every study table, reference table and view has its policy; `user_study` has exactly the identity predicate; the denied tables have none; there are no unclassified tables and no extra materialized views, dictionaries, live or window views. The default deny must target the user or one of its *default* roles.
- **Ownership**: no foreign row policies, none of ours on another target.
- **Grants** (`SHOW GRANTS` and `SHOW GRANTS FINAL`, parsed fail-closed): only whole-database `SELECT` and `SHOW ROW POLICIES`; for a role target, the only direct grant is the role, without ADMIN OPTION. Grants on `system`, global or wildcard grants, grant options and table/column-level grants are refused.
- **Other readable databases**: only plain storage engines (MergeTree families including Shared/Replicated, Memory, Log variants, Null, Set, Join); views must be `SQL SECURITY INVOKER`. A database that looks like study data (catalog table names, or `cancer_study_identifier` / `cancer_study_id` / other catalog link columns) must have our default deny or be listed in `CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES`. Listing never overrides the engine and view checks.
- **A probe**: a random caller sees exactly the public studies, and none of anyone's grants.

It never logs emails or raw driver errors (only the check name, exception class and ClickHouse error code), and it runs once per process: restart after grant, role, policy or schema changes.

## Security notes

- **Role targets: no direct grants.** A user with the role *and* a direct `SELECT` can `SET ROLE NONE` and read everything through the `ALL EXCEPT <role>` policy. User targets have no such bypass, since the policies name the user itself.
- Never give the target temporary tables, table functions (`remote`, ...), `dictGet` or `DEFINER` views: all can read around row policies.
- A role keeps `SELECT` on every database it was applied to. Revoke it from a retired blue/green copy: `REVOKE SELECT ON <old>.* FROM cbioportal_mcp_study_restricted`.
- `groups` entries are not trimmed (as in cBioPortal): write them without spaces around `;`.
- Making a public study private in a live database takes effect at the next refresh; a new clone picks it up at once.
- Policies on tables without their own study column resolve the study through a link table, which can be slow on very large tables.
- **Input validation.** Identifiers (database, role, user) must match `^[A-Za-z_][A-Za-z0-9_]{0,127}$` and are always back-quoted. Values (public group, app name, study ids, groups) must match `^[A-Za-z0-9_.-]{1,128}$` and are emitted as escaped literals. Both reject trailing newlines/NUL and template placeholder tokens (`__NAME__`). Templates are filled in one pass and the rendered SQL may only reference the target database.
