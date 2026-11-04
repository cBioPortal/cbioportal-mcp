# Study-level access control (row policies)

ClickHouse row policies that limit a restricted MCP database user to the
studies the caller may see: public studies plus whatever was granted to
the caller's email. This covers the database side only. Sending the caller's
identity on every query is the server's job (separate change), and so is
login.

**Never applied to the public clone.** The daily public clone runs
`scripts/apply_sql.sh` over `sql/`; this tooling lives in
`src/cbioportal_mcp/study_authz/` (`*.sql.tmpl` templates, so no `*.sql` glob
picks it up), has to be run explicitly against a named database, and refuses
any database name containing `public` unless you pass
`--allow-public-database-name`. Filtering stays off on public databases.

## Requirements

- ClickHouse **>= 24.10**: the policies use `getSettingOrDefault` and the
  refresh is a refreshable materialized view (no longer experimental there).
  Tested on 25.8.
- `custom_settings_prefixes` must include `SQL_` (it does by default).
- An admin user that can `CREATE ROLE`, `CREATE ROW POLICY`, `CREATE USER`
  and create tables/views in the target database.

## Design

### Policies

All policies are attached `TO` one **policy target**, chosen at apply time:

- `--policy-role NAME` (the default is `cbioportal_mcp_study_restricted`).
  `apply` creates the role. The MCP runtime user gets the role and nothing
  else (`user-sql`).
- `--policy-user NAME`: an *existing* user, with no role involved. Use this
  when the runtime user is already provisioned, e.g. a user that has direct
  `GRANT SELECT` on several public databases and no roles. `apply` refuses a
  user that does not exist.

Either way, `apply` ends with `GRANT SHOW ROW POLICIES` (for the server's
startup check) and `GRANT SELECT ON <db>.* TO <target>`, issued after every
policy. Everyone except the target gets `USING 1`. The policies
are scoped to this one database, so the target's access to its other
databases is unchanged.

| Object | Policy |
|---|---|
| every table in the DB (`db.*`) | `USING 0`: default-deny, so an unclassified or new table fails closed. Everyone except the target gets `USING 1`. |
| 45 study tables (`catalog.STUDY_SCOPED_TABLE_PATHS`) | resolve the row's study through its real FK path (`sample -> patient -> cancer_study`, `mutation -> genetic_profile -> cancer_study`, `mutation_event <- mutation -> ...`) and check it against `user_study` |
| `user_study` | the caller's rows plus the public rows only |
| 17 reference tables (genes, panels, OncoTree...) | `USING 1` |
| 21 views (`sql/4`, `sql/5`, `sql/7`) | `USING 1` pass-through. This is safe because every view is `SQL SECURITY INVOKER`, so the underlying tables' policies filter the rows. |
| `users`, `authorities`, `data_access_tokens`, `user_study_manual`, `user_study_jwt`, `user_study_refresh` | no policy, so the default-deny returns zero rows |

A study is visible when its identifier is in:

```sql
SELECT cancer_study_identifier FROM <db>.user_study
WHERE (email = '' OR email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', '')))
  AND valid_until > now64(3)
```

- The MCP sends the verified email as the per-query setting `SQL_cbio_user`
  **together with `readonly=1`**. Under `readonly=1`, any
  `SETTINGS SQL_cbio_user = ...` in the query text is rejected (`READONLY`,
  code 164). Without `readonly=1`, a query could impersonate another caller.
  The live tests prove both cases.
- If the setting is missing or empty, the caller sees public studies only,
  because `getSettingOrDefault` returns `''`. Nothing here relies on a
  settings-profile default for `SQL_cbio_user`, which earlier testing found
  unreliable on 24.12/25.8. Do not add one either: it would make every query
  without an identity act as that email.
- **An omitted `SQL_cbio_user` is not an error: the query silently returns
  public studies only.** This is by design (fail closed), but it hides bugs.
  The server must therefore send the setting on *every* query against a
  restricted database, and treat a request without a verified email as a bug
  (refuse it or log loudly) rather than relying on the public-only fallback.
- **clickhouse_connect blocks the setting by default.** The server reaches
  ClickHouse through mcp-clickhouse, which uses clickhouse_connect. That
  client checks settings against the server's list before sending them, and
  `SQL_cbio_user` is not on it. With its default
  `invalid_setting_action='error'`, `client.query(..., settings={'SQL_cbio_user': ...})`
  raises `Setting SQL_cbio_user is unknown or readonly` without contacting
  the server.
  - The server change must set
    `clickhouse_connect.common.set_setting('invalid_setting_action', 'send')`
    before it creates the client.
  - Never use `'drop'`: it silently drops the identity, and every caller then
    sees public studies only.
  - `test_clickhouse_connect_needs_invalid_setting_action_send` shows both
    behaviors. The server on `beta` does not set this option and does not
    send `SQL_cbio_user` yet.
  A parallel live validation of #182 confirmed this behavior, and
  the `None` cases of `test_caller_sees_own_grants_plus_public` pin it.
- Policies are created first and the `GRANT SELECT ON <db>.*` is issued last,
  so the target never sees an unfiltered table.

### `user_study` and its sources

```
user_study(email, cancer_study_identifier, valid_until DateTime64(3,'UTC'),
           source LowCardinality(String), issued_at DateTime64(3,'UTC'))
ORDER BY (email, cancer_study_identifier)
```

Only the row policies read `user_study`, and the refreshable materialized
view `user_study_refresh` is the only thing that writes it. Each refresh
rebuilds the whole table into a staging table and EXCHANGEs it in (that is
how a non-APPEND refreshable MV works), so readers always see one complete
snapshot. The refresh runs as its definer (the admin who applied it). That is
how it can read `user_study_manual`.

- **public**: rows with `email = ''`, one per study whose `cancer_study.groups`
  contains the public group. The rule: split on `;`, drop empty entries,
  compare each whole entry case-insensitively, no trimming, no LIKE. So
  `' PUBLIC'` and `PUBLICX` are not public. These rows get
  `valid_until = 2299-12-31`.
- **manual**: `user_study_manual(email, grant_key, valid_until, note)`,
  maintained by an admin. `grant_key` is a study id, a whole `groups` entry,
  or `ALL` / `ALL_TCGA` / `ALL_NCI_TARGET`. Emails are lower-cased, and rows
  with an empty email are ignored, so they can never become public.
  - **Group unpacking.** The refresh expands each entry into one
    `(email, cancer_study_identifier)` row per matching study, so the policies
    only ever look at `(email, study)` pairs.
  - **What matches.** An entry matches a study when it equals the study id
    (case-insensitive), or when it equals a whole entry of that study's
    `groups`. Groups use the same rule as public: split on `;`, drop empty
    entries, compare case-insensitively, no trimming. So grant `X` does not
    match a `groups` value of `PUBLIC; X`.
  - **Expiry.** Expired entries are skipped.
- **jwt (future)**: see below.

**Failure modes (fail closed).** Each private row's `valid_until` is the
earlier of the source's `valid_until` and refresh time + `--private-grace-minutes`
(default 15; it must exceed `--refresh-minutes`, default 5).

- If the refresh stops or keeps failing, `user_study` keeps its last
  snapshot. Every private row then expires within the grace period, and the
  public rows stay.
- A revoked manual grant (or a deleted row) stops granting at the next
  refresh, and within the grace period at worst.
- An IdP outage can only stop JWT rows from arriving, which leaves public
  rows untouched.

The public and private rows share one refresh. "Independent" here means their
lifetimes are independent. A broken private source makes the refresh fail,
and that only freezes the public rows, which stay valid.

**Why a refreshable MV rather than an external script:** it runs inside
ClickHouse (Cloud included), is created per database by the same `apply`, and
needs no CronJob or extra credentials. Its health is visible in
`system.view_refreshes`, and `check` reports a failing refresh. An external
script would need its own scheduler and admin secret, and when it is down it
fails in the same way as a stopped refresh.

### Future per-email (JWT) source

The goal is that each authentication replaces that email's whole grant set,
written by an INSERT-only account, and that an older token never brings back
grants a newer token dropped. The layout reserves that source now:

```sql
CREATE TABLE <db>.user_study_jwt (
    email String, grant_key String,          -- normalized claim, '' = sentinel
    issued_at DateTime64(3, 'UTC'),          -- token iat: orders replacements
    valid_until DateTime64(3, 'UTC'),        -- token / grant expiry
    token_id String
) ENGINE = MergeTree ORDER BY (email, issued_at);
GRANT INSERT ON <db>.user_study_jwt TO <jwt_writer>;   -- nothing else
```

- **Writer.** For each token, the writer appends one row per grant key,
  normalized with `groups.normalize_authority`, plus one sentinel row with
  `grant_key = ''`. The sentinel lets a token with no grants still replace an
  older one.
- **Refresh.** Add one `UNION ALL` branch to the `grants` CTE in
  `0-user-study.sql.tmpl` that keeps only each email's newest token:
  `WHERE (lowerUTF8(email), issued_at) IN (SELECT lowerUTF8(email), max(issued_at) FROM user_study_jwt GROUP BY 1) AND grant_key != ''`,
  with `source = 'jwt'` and `issued_at` = the token's iat. A late-arriving
  older token loses to the newer one, so it cannot bring back a revoked grant.
- **No policy, catalog or `user_study` schema change is needed.**
  `user_study_jwt` is already in `DENIED_TABLES`. Policy lookups stay a
  primary-key read of a small `user_study`.

**Trade-off.** JWT grants and revocations take effect at the next refresh, not
the moment the token is written. That delay is at most `--refresh-minutes`;
lower it to 1 if needed. The alternative was letting the writer insert into
`user_study` directly. That would either lose rows to the next EXCHANGE, or
force every policy subquery to resolve "newest token per email" at query time
(an `argMax` on every query, and a different predicate than the agreed one).
Same-millisecond `issued_at` ties grant the union of both tokens.

### cBioPortal matching rules (for the JWT source)

The app name is a required parameter now. It is recorded in `user_study`'s
table comment for the JWT source's claim matching. cBioPortal's rules, from
`CancerStudyPermissionEvaluator`, are implemented in `groups.py`:

- a leading `ROLE_` is stripped from the authority;
- with `filter_groups_by_appname` (cBioPortal's default), only `<app>:<X>`
  authorities count (the app name is compared case-insensitively) and `X` is
  the key; otherwise the whole authority is the key;
- the upper-cased key grants a study when it equals the study id or one whole
  `groups` entry;
- the special keys: `ALL` grants every study, `ALL_TCGA` every `*_TCGA`
  study, and `ALL_NCI_TARGET` every `*_TARGET` study plus
  `ALL_TARGET_PHASE1` / `ALL_TARGET_PHASE2`.

Manual `grant_key`s are already bare keys, so `ROLE_` and app-prefix
handling does not apply to them.

## How to apply

These commands need admin credentials only. Never use the MCP runtime user
for them.

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_PORT=8443 CLICKHOUSE_SECURE=true
export CLICKHOUSE_ADMIN_USER=... CLICKHOUSE_ADMIN_PASSWORD=...

# 1. Tables, refresh and policies, for one (blue or green) database. Idempotent.
uv run python -m cbioportal_mcp.study_authz apply --database cbio_restricted_green \
    --public-group PUBLIC --app-name cbioportal

# 2. Verify that every table and view is covered and the refresh is healthy.
uv run python -m cbioportal_mcp.study_authz check --database cbio_restricted_green

# 3. The MCP runtime user. It reads ONLY through the role: no direct grants,
#    no settings profile forcing readonly.
CBIOPORTAL_MCP_RESTRICTED_PASSWORD=... uv run python -m cbioportal_mcp.study_authz \
    user-sql --user mcp_restricted --database cbio_restricted_green [--apply]
```

- `render` (same flags as `apply`) prints the SQL for `clickhouse-client`.
- After changing `catalog.py`, run `generate` to rewrite
  `templates/1-row-policies.sql.tmpl`. `tests/test_study_authz_sql.py` fails
  if you forget.
- `--public-group` and `--app-name` have no defaults.
- Re-running `apply` keeps `user_study_manual`.

### Manual grants

```sql
INSERT INTO <db>.user_study_manual (email, grant_key, valid_until, note)
VALUES ('someone@example.org', 'TEAM_A', '2027-01-01 00:00:00', 'ticket-123');
DELETE FROM <db>.user_study_manual WHERE email = 'someone@example.org' AND grant_key = 'TEAM_A';
SYSTEM REFRESH VIEW <db>.user_study_refresh;   -- optional: apply now, not at the next tick
```

## Test database for an end-to-end check

`make-test-db` builds a small database from a source database. It contains
only the chosen studies' rows across all 45 study tables (using the policies'
own FK paths), the reference tables in full, and the catalog views. It never
copies the auth tables. It rewrites `cancer_study.groups`: each `--gate` study
gets its private group (default `PRIVATE_<STUDY>`), and every `--public` study
gets the public group. Then it applies the study-authz SQL and seeds
`user_study_manual` from a CSV.

```bash
cat > grants.csv <<'EOF'
email,grant_key,valid_until
tester-a@example.org,TEAM_A,2027-01-01T00:00:00Z
tester-b@example.org,study_c,
EOF
# An empty valid_until means 7 days from now.

uv run python -m cbioportal_mcp.study_authz make-test-db \
    --source-database <prepped_source_db> --database authz_e2e_test \
    --gate study_a=TEAM_A --gate study_b=TEAM_A --gate study_c \
    --public study_d --public study_e \
    --public-group PUBLIC --app-name cbioportal --grants-csv grants.csv

CBIOPORTAL_MCP_RESTRICTED_PASSWORD=... uv run python -m cbioportal_mcp.study_authz \
    user-sql --user mcp_e2e --database authz_e2e_test --apply
```

To attach the policies straight to an existing user instead (no role, no new
user), add `--policy-user <existing_user>` to `make-test-db` and skip
`user-sql`. Then run `check --database authz_e2e_test --target <existing_user>`.

Study IDs, group names and emails are run-time arguments only: none of the
real ones belong in this repository.

With this setup, `tester-a` sees study_a, study_b and the public studies;
`tester-b` sees study_c and the public studies; everyone else sees only the
public studies. `--replace` rebuilds the database, but only if this tool
created it (it checks the database comment). The emails in this repository
are all example.org fakes; put real ones only in your local CSV.

## Server startup check (study filtering on)

With `CBIOPORTAL_MCP_STUDY_FILTERING=on` the server refuses to start (one
`CRITICAL` line, exit code 2) unless its own ClickHouse user is covered. It
runs `study_authz/startup_check.py` over a client built like mcp-clickhouse's,
as `currentUser()`, and never logs an email:

- `SQL_cbio_user` reaches `getSettingOrDefault` under `readonly=1` (ClickHouse
  >= 24.10 and the `SQL_` custom settings prefix).
- The database-wide default-deny targets the user itself or one of its
  *default* roles. A policy role that is granted but not default is refused.
- `coverage.evaluate` (the same rules as `check`) passes over exactly the
  policies that apply to the user and its enabled roles: every study table,
  reference table and view has its own policy, `user_study` has only the
  identity predicate, and `user_study_manual` and the other denied tables have
  none. Unclassified tables are refused too.
- Role target: no direct `SELECT` on the database (or `*.*`), and no other
  granted role that gives `SELECT ON <db>.*` without the policy role.
  User target: direct grants are expected; only non-read direct grants on the
  database, `WITH GRANT OPTION` and `WITH ADMIN OPTION` are refused.
- No access management (`CREATE/ALTER/DROP USER/ROLE/ROW POLICY`, `ROLE ADMIN`,
  ...), `SYSTEM`, `CREATE TEMPORARY TABLE`, table functions, `dictGet`, or
  write/DDL privileges on the database or on `user_study` /
  `user_study_manual` / `cancer_study`. `CHECK GRANT` runs without
  `readonly=1`, because readonly hides write privileges.
- As a random `@startup-probe.invalid` caller: visible `cancer_study` rows
  equal the public studies in `user_study` (`email = ''`), no other caller's
  `user_study` rows, and zero `user_study_manual` rows.

The check reads `system.row_policies`, so the policy template grants the
target `SHOW ROW POLICIES ON <db>.*` (re-run `apply` on existing databases).
ClickHouse then lists policies of every database to that user; policy text
holds table names and predicates, never emails. The refresh status
(`system.view_refreshes`) is admin-only and stays with `check`. With filtering
off, none of this runs.

## Security notes and caveats

- **Direct grant bypass (role targets only).** With role-targeted policies,
  give the runtime user only the role. A user with the role **and** a direct
  `SELECT` can run `SET ROLE NONE` and then reads everything through the
  `ALL EXCEPT <role>` policy. The live tests demonstrate this. Without a
  direct grant, `SET ROLE NONE` leaves no privilege at all. `user-sql` uses
  `CREATE USER OR REPLACE`, which drops any old direct grant.
- **User targets have no such bypass.** The policies name the user itself, so
  its direct grants, its roles, and `SET ROLE NONE` change nothing; the live
  tests prove this too. That is why the server's startup check (below)
  verifies that *every study table and view in the connected database has a
  policy that applies to the current user*, and refuses direct grants only
  for role targets. `check --target` verifies the same policies as admin.
- Do not grant the target `CREATE TEMPORARY TABLE`, `REMOTE` / table functions,
  `dictGet`, or create `DEFINER` views. All of these can read around row
  policies. `check` rejects DEFINER/NONE views, extra materialized views and
  dictionaries in the database.
- A role target keeps `SELECT` on every database it was applied to. Both blue and
  green are filtered, so that is safe, but revoke the old one when you retire
  it: `REVOKE SELECT ON <old>.* FROM cbioportal_mcp_study_restricted`.
- Group entries are never trimmed, matching cBioPortal: `'A; PUBLIC'` splits
  into `A` and ` PUBLIC`, and ` PUBLIC` is **not** the public group. Write
  `groups` without spaces around `;`.
- `check` verifies that the refresh view exists and that its last refresh
  succeeded. It does not compare the view's SQL text (#182's startup check
  did), so changing the refresh query needs no matching expected-text update.
- Moving a public study to private inside a *live* database takes effect at
  the next successful refresh. A new clone picks it up immediately.
- `mutation_event` / `cna_event` policies go through `mutation` /
  `sample_cna_event`, which is correct but can be slow on very large tables.
- Only tested on ClickHouse 25.8 in Docker, not on ClickHouse Cloud.
  Refreshable MVs in a Replicated database (Cloud's default) are documented
  to be coordinated across replicas. Run `check` after the first apply there.
