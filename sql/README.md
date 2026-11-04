# LLM-prep SQL

The daily clone CronJob (see `knowledgesystems-k8s-deployment`) executes every
`*.sql` file in this directory **in numeric order** against the freshly-cloned
LLM database. The files re-shape and annotate the schema so the cBioPortal
MCP agent can reason about it.

## Files

| Order | File | Scope |
|-------|------|-------|
| 0 | `0-cleanup-for-llm.sql` | Drop columns that mislead the agent (e.g. `sample.sample_type`). |
| 1 | `1-add-column-comments.sql` | Attach human-readable `COMMENT`s so the agent can self-introspect column meaning. |
| 2 | `2-add-oncotree-fields.sql` | Add OncoTree fields to `type_of_cancer` (auto-generated). |
| 3 | `3-add-cancer-study-query-preferences.sql` | Creates `cancer_study_query_preferences` table + pattern-detected preferences (currently `pan_cancer_tcga`). |
| 4 | `4-mutation-frequency-views.sql` | Gene-frequency parameterized views: mutation (`gene_mutation_frequency_by_cancer_type`, `gene_mutation_frequency_in_study`, `gene_mutation_frequency_in_studies`, `top_mutated_genes_in_cohort`, `top_mutated_genes_in_study`), any alteration (`gene_alteration_frequency_by_cancer_type`), single-study views mirroring the portal's study-view charts (`gene_mutation_variants_in_study`, `co_altered_genes_in_study`, `top_cna_genes_in_study`, `gene_cna_distribution_in_study`, `top_sv_genes_in_study`), plus coverage building-block views (`mutation_panel_gene_coverage`, `mutation_wes_coverage`, `cna_panel_gene_coverage`, `cna_wes_coverage`, `sv_panel_gene_coverage`, `sv_wes_coverage`). Handles the "WES is not in `gene_panel`" trap that produces >100% frequencies. See `cbioportal://mutation-frequency-guide`. |
| 5 | `5-gene-expression-views.sql` | Gene-expression / copy-number-value / methylation views, backed by `genetic_alteration_derived`. Currently `gene_pair_coexpression(study, gene_a, gene_b, profile_type)` for Spearman correlation between two genes. See `cbioportal://gene-expression-guide`. |
| 6 | `6-add-study-data-type-counts.sql` | Per-study sample counts by data type on `cancer_study` (`sample_count`, `mutation_sample_count`, `cna_sample_count`, …, `treatment_patient_count`, `resource_sample_counts`), computed like cBioPortal's DETAILED study projection so they match the portal's study list and "Data type" filter. Rebuilds the table and swaps it in with `EXCHANGE TABLES`. See `cbioportal://sample-filtering-guide` §4. |
| 7 | `7-clinical-views.sql` | Study-view chart views for one study: `treatment_counts_in_study(study)` (patients per treatment agent, with type/subtype arrays — the portal's Treatment chart), `treatment_regimens_in_study(study)` (same-day agent combinations), and `clinical_attribute_counts(study, attribute)` (categorical clinical chart with the portal's NA row). See `cbioportal://clinical-data-guide`. |

Everything under `sql/` directly is **portable** — works against any cBioPortal deployment. Deployment-specific SQL lives under `sql/portal-specific/<portal-name>/`:

| Path | Scope |
|------|-------|
| `sql/portal-specific/public-portal/0-preferences.sql` | Public cBioPortal (`cbioportal.org`). Loads `all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes` preferences. All INSERTs gated on `cancer_study` existence, so on other deployments this is a no-op rather than an error. |

`apply_sql.sh` and the daily clone cron apply the portable files first (in numeric order), then iterate every subdirectory of `portal-specific/`. A deployer image can ship multiple subdirs if it needs to, but typically only contains the one for that portal.

## Applying these files manually

For a one-off apply outside the daily clone CronJob (e.g. you just edited
`sql/5-*.sql` and want to test against a prepped database without re-cloning
the data), use `scripts/apply_sql.sh`:

```bash
export CLICKHOUSE_HOST=...
export CLICKHOUSE_DATABASE=cbioportal_public_librechat_blue   # your prepped DB
export CLICKHOUSE_ADMIN_USER=librechat_admin                   # NOT the MCP SELECT-only user
export CLICKHOUSE_ADMIN_PASSWORD=...
./scripts/apply_sql.sh
```

Requires the `clickhouse-client` binary on `PATH`. The script uses dedicated
`CLICKHOUSE_ADMIN_USER` / `CLICKHOUSE_ADMIN_PASSWORD` env vars instead of the
MCP server's `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD`, so the SELECT-only
runtime user never sees DDL credentials.

## Study-level row policies (opt-in)

`sql/study-authz/` holds the ClickHouse row policies for restricted
deployments (`CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted` with
`CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true`, see the root
[README](../README.md#study-level-access-control)). It is **not** part of the
default run: public deployments don't need it, and it needs an admin allowed
to `CREATE ROLE` / `CREATE ROW POLICY` (plus `CREATE`/`SYSTEM REFRESH VIEW` for
the authorities source). Two policy sets, matching the server's
`CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE`:

| Source | Files | The server sends |
|--------|-------|------------------|
| `acl` (default) | `study-authz/0-row-policies.sql` | the resolved study list (`SQL_cbiomcp_allowed_studies`) |
| `authorities` | `study-authz/authorities/0-user-study.sql`, then `1-row-policies.sql` | only the verified email (`SQL_cbio_user`); ClickHouse resolves studies from `user_study` |

Both policy files are **generated** from
`src/cbioportal_mcp/authentication/row_policy_catalog.py` by
`scripts/generate_row_policy_sql.py` - edit the catalog, regenerate, commit
(`tests/test_row_policy_sql.py` fails on drift). They keep placeholders
(`__CLICKHOUSE_DATABASE__`, `__CBIOMCP_ALLOWED_STUDIES_SETTING__`,
`__CBIOMCP_USER_SETTING__`, ...) that `study-authz/render.sh` fills in from the
same env vars the server reads, so the setting names in the policies always
match what the server sends. Apply with:

```bash
APPLY_STUDY_AUTHZ_SQL=true ./scripts/apply_sql.sh                                 # acl
APPLY_STUDY_AUTHZ_SQL=true STUDY_AUTHZ_SOURCE=authorities ./scripts/apply_sql.sh  # authorities
```

or render a single file yourself:

```bash
CLICKHOUSE_DATABASE=<db> sql/study-authz/render.sh sql/study-authz/0-row-policies.sql \
  | clickhouse-client --database <db> --multiquery
```

Every table in them is qualified with `__CLICKHOUSE_DATABASE__` because an
unqualified table name inside a row policy's subquery is resolved against the
*querying session's* current database, not the policy's. The files create the
`cbioportal_mcp_study_restricted` role, grant it `SELECT ON <db>.*`, and
install a database-wide default-deny plus one policy per table. Policies are
keyed by table name, so they survive the `DROP`/`CREATE` and `EXCHANGE TABLES`
the files above do; re-applying is idempotent (`CREATE ROW POLICY OR REPLACE`,
`CREATE TABLE IF NOT EXISTS`, and the refresh MV is dropped and recreated -
its target table keeps its rows meanwhile).

### authorities source: `user_study`

`authorities/0-user-study.sql` builds `user_study(email,
cancer_study_identifier, cancer_study_id)` as a **refreshable materialized
view** (`REFRESH EVERY $STUDY_AUTHZ_REFRESH_MINUTES MINUTE`, default 5) over
`authorities`, `users` and `cancer_study`, following cBioPortal's
`CancerStudyPermissionEvaluator`: a leading `ROLE_` is dropped; with
`STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME=true` (cBioPortal's default) only
`<STUDY_AUTHZ_APP_NAME>:<X>` authorities count; `X` matches a study id, or one
of the study's `;`-separated groups, case-insensitively; `ALL`, `ALL_TCGA`,
`ALL_NCI_TARGET` work as in cBioPortal; and every user present and enabled in
`users` gets the `STUDY_AUTHZ_PUBLIC_GROUP` (default `PUBLIC`) studies. Users
not in `users`, or disabled there, get nothing (cBioPortal refuses their
login). Emails are lower-cased on both sides.

Refresh and revocation latency: each refresh recomputes the whole table and
swaps it in atomically, so a deleted authority, a disabled user or a regrouped
study stops granting rows at most one interval (plus the refresh's own run
time) later. An insert-triggered MV is deliberately not used: it never sees
deletes. If a refresh fails, the previous contents stay;
`scripts/verify_row_policy_coverage.py --source authorities` reports a failed
or stale (> 3 intervals) refresh.

Freshness bound (enforced in ClickHouse): each refresh stamps its rows with
`refreshed_at = now()`, and every authorities policy - including
`user_study`'s own - only accepts rows with `refreshed_at >= now() -
STUDY_AUTHZ_MAX_STALENESS_SECONDS` (default 2 × the refresh interval, i.e.
600 s; must be larger than the interval). If the refresh is stopped or keeps
failing, every caller loses all study access once the bound passes and gets
it back with the next successful refresh. A revocation therefore takes
effect within one interval normally and within the bound at worst. Existing
`user_study` tables get the column via `ADD COLUMN IF NOT EXISTS`
(defaulting to the epoch, i.e. stale until the first refresh). Assumes the
nodes serving queries and running the refresh agree on the clock.

The MCP's startup/periodic check (and the coverage verifier) exempts only
this MV, and only if its stored definition (`system.tables.create_table_query`)
is **exactly** the shipped template rendered for this database, as formatted by
the server's own `formatQuerySingleLine()`. Only the refresh interval (in any
form ClickHouse normalizes it to, e.g. `1 HOUR`), app name, app-name filter,
PUBLIC group, the definer user and the target column list ClickHouse inserts
may differ. Any other change - entitlement logic, sources, joins, the
`refreshed_at` expression, `APPEND`, another target or database - refuses to
start. The template lives in
`src/cbioportal_mcp/authentication/user_study_sql.py` (shipped with the
server); `authorities/0-user-study.sql` is generated from it - edit the Python
template and run `scripts/generate_row_policy_sql.py`. Verified on ClickHouse
26.9 (native tests) and 25.8 (local e2e); the comparison relies on the stored
DDL and `formatQuerySingleLine` sharing a formatter, which holds on one server,
so after a ClickHouse upgrade both sides re-format the same way. The MV runs as its definer (the admin that
applied the file), which is how it reads `authorities`/`users`; the restricted
role can read only its own rows of `user_study` and nothing of the MV object
or the auth tables. Needs ClickHouse 24.10+.

### Where the daily clone job calls it

The clone/sync job builds each day's database under a new name and then
switches the MCP to it. Call the apply script **as the last step of building
the new database, before the switch**, with the new name:

```bash
# 1. clone + the portable SQL, as today
CLICKHOUSE_DATABASE=<new_db> ./scripts/apply_sql.sh
# ...or in one go with the study authz step (same script, same run):
CLICKHOUSE_DATABASE=<new_db> APPLY_STUDY_AUTHZ_SQL=true \
  STUDY_AUTHZ_SOURCE=authorities STUDY_AUTHZ_GRANT_ROLE=false \
  ./scripts/apply_sql.sh
# 2. check the new database before it goes live
CLICKHOUSE_DATABASE=<new_db> CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=authorities \
  uv run python scripts/verify_row_policy_coverage.py
# 3. switchover, together: move the role's grant, then point the MCP at <new_db>
#   GRANT SELECT ON <new_db>.* TO cbioportal_mcp_study_restricted;
#   REVOKE SELECT ON <old_db>.* FROM cbioportal_mcp_study_restricted;
```

`STUDY_AUTHZ_GRANT_ROLE=false` matters because the MCP's startup and periodic
checks require the role's grants to be exactly `SELECT ON <its db>.*`: while
the role is also granted the next day's database, the running MCP refuses
study queries and a restarting one refuses to start. Keep the
`GRANT`/`REVOKE` pair next to the MCP's database switch. With the grant left
in (the default, fine for a single long-lived database) the script is a
complete one-shot setup.

When you add a table or view to any file in this directory, classify it in
`src/cbioportal_mcp/authentication/row_policy_catalog.py` (with its foreign-key
path in `STUDY_SCOPED_TABLE_PATHS` if it is study-scoped) and regenerate the
policy SQL (`tests/test_row_policy_sql.py` fails otherwise) - with the
database-wide default-deny, a table or view without a policy returns no rows to
restricted users. Every `CREATE VIEW` here must declare
`SQL SECURITY INVOKER` (`tests/test_row_policy_sql.py` enforces it): a
`DEFINER` or `NONE` view runs without the caller's row policies, and a view
without the clause takes whatever `default_normal_view_sql_security` the
applying profile has. Declaring it in the `CREATE VIEW` keeps the daily DROP +
CREATE safe even before the authz file is re-applied; the generated files also
`ALTER TABLE ... MODIFY SQL SECURITY INVOKER` every cataloged view (it
migrates older views). The clause needs ClickHouse 24.2+ (the public portal
runs 26.6). Order on every run: the portable files, then `study-authz/`
(apply_sql.sh does this).

After applying schema changes on a row-policy deployment, also run
`scripts/verify_row_policy_coverage.py` (add `--source authorities` for that
source) - it checks the live database for missing, unfiltered or fully-denied
coverage, using the same `CLICKHOUSE_ADMIN_USER` split (the runtime
SELECT-only user deliberately can't read `system.row_policies`, since that
table isn't scoped to the querying user's own access and would leak other
databases' policy metadata through the MCP's arbitrary-SQL tool if granted).
The authorities check also needs `SELECT ON system.view_refreshes`.

## Deploying for a non-public portal

Two options for adding your own preferences:

1. **Add `sql/portal-specific/<your-deployment-name>/0-preferences.sql`** alongside
   the existing `public-portal/` subdir. `apply_sql.sh` and the cron will
   iterate every subdir of `portal-specific/`, so your file gets picked up
   automatically. Leaving `public-portal/` in place is harmless — its INSERTs
   are existence-gated and produce zero rows on databases without the
   cBioPortal-public studies.

2. **Or remove the `public-portal/` subdir entirely** from your image / mount
   if you'd rather not even apply its (no-op) INSERTs.

## Conventions for portal-specific preference files

- Live under `sql/portal-specific/<portal-name>/`. The portal-name is a
  free-form slug — pick something that identifies your deployment.
- Numeric prefix within each subdir so order is intrinsic. Start at `0-`.
- Every `INSERT INTO cancer_study_query_preferences` must gate on
  `WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM cancer_study)`
  (or equivalent) so the file is harmless on databases that don't have your
  studies.
- One `preference_name` per query-intent, lower_snake_case. Document its
  purpose in the `notes` column so the agent can self-explain to users.
- The agent discovers available preferences via
  `SELECT DISTINCT preference_name FROM cancer_study_query_preferences` —
  no hardcoded list anywhere outside SQL.

## View execution privileges

Every ordinary view (including parameterized views) explicitly declares
`SQL SECURITY INVOKER`. Create the views with the admin account as usual;
callers need `SELECT` on both the view and its underlying tables. The public
MCP user's direct `GRANT SELECT ON <database>.*` covers both, so the next daily
clone preserves query results while ensuring views execute with the caller's
privileges and any applicable row policies.

The clause requires **ClickHouse 24.2 or newer** (see the
[24.2 release presentation](https://presentations.clickhouse.com/2024-release-24.2/)).
This is the minimum for the security clause, not a compatibility guarantee for
all other SQL in this directory. Parameterized views keep their existing
arguments and table-function call syntax. There are currently no materialized
views under `sql/` and no conversion exceptions. Future materialized views need
separate review: ClickHouse explicitly disallows `SQL SECURITY INVOKER` for them
(see [view security semantics](https://clickhouse.com/docs/sql-reference/statements/create/view#sql_security)).

`tests/test_sql_view_security.py` checks every `*.sql` recursively, including
portal-specific files, to prevent ordinary views from losing the explicit clause.
