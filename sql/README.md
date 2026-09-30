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
| 9 | `9-projections.sql` | ClickHouse projections that re-sort `genomic_event_derived` (study-first and gene-first) and `sample_to_gene_panel_derived` (study-first) for frequency / co-occurrence query shapes. Performance only: deterministic queries return the same rows, but **only** with `optimize_use_implicit_projections = 0` pinned for the MCP user (the MCP refuses to start otherwise). Order-dependent aggregates on ties (`any`, `argMin`/`argMax`) may pick a different row. See [Projections](#projections). |

Everything under `sql/` directly is **portable** — works against any cBioPortal deployment. Deployment-specific SQL lives under `sql/portal-specific/<portal-name>/`:

| Path | Scope |
|------|-------|
| `sql/portal-specific/public-portal/0-preferences.sql` | Public cBioPortal (`cbioportal.org`). Loads `all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes` preferences. All INSERTs gated on `cancer_study` existence, so on other deployments this is a no-op rather than an error. |

`apply_sql.sh` and the daily clone cron apply the portable files first (in numeric order), then iterate every subdirectory of `portal-specific/`, then apply `final/`. A deployer image can ship multiple subdirs if it needs to, but typically only contains the one for that portal.

### `final/`: aggregates built after every preference exists

Files in `sql/final/` are portable too, but they run in a **third phase, after `portal-specific/`**, because they aggregate over `cancer_study_query_preferences`. That table only holds the portal-specific cohorts (`all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes`, …) once phase 2 has run.

| Path | Scope |
|------|-------|
| `sql/final/0-precomputed-aggregates.sql` | Precomputed tables behind the `get_alteration_frequency`, `get_top_altered_genes`, `get_gene_frequency_by_cancer_type` and `get_profiled_counts` MCP tools: `study_gene_alteration_counts` (study x gene x alteration type: altered samples + gene-specific profiled samples), `cancer_type_gene_alteration_counts` (preference x CANCER_TYPE x gene; the >= 50 threshold is applied at query time, not stored) and `study_profiled_counts` (samples / patients per profiled data type). Each table reproduces a named `sql/4` recipe: the per-study table matches `top_{mutated,cna,sv}_genes_in_study` (DISCRETE-only CNA denominator), the per-cancer-type table matches `gene_alteration_frequency_by_cancer_type` (every CNA profile, including log2). The two differ on CNA denominators because those recipes do. Plain tables rebuilt on every apply: re-apply after any `*_derived` rebuild or preference change. A clone job without the final phase never builds them, and the tools then use live SQL. |

## Projections

`9-projections.sql` adds alternate sort orders ("projections") to two derived
tables. The base tables are sorted for the cBioPortal backend
(`genetic_profile_stable_id` first, and no study column in
`sample_to_gene_panel_derived`'s key), while most agent queries filter by
study + gene + variant type, so they read most granules of the table:

| Table | Projection | ORDER BY | Serves |
|-------|------------|----------|--------|
| `genomic_event_derived` | `ged_by_study_gene` | `cancer_study_identifier, hugo_gene_symbol, variant_type, sample_unique_id` | gene X in study S, two-gene co-occurrence in S, `gene_*_in_study(ies)`, `top_mutated_genes_in_study`, `co_altered_genes_in_study`, `top_sv_genes_in_study` |
| `genomic_event_derived` | `ged_by_gene_study` | `hugo_gene_symbol, variant_type, cancer_study_identifier, sample_unique_id` | gene X across a cohort / cancer types (`gene_*_by_cancer_type`, whose `JOIN cohort` can't use a study-leading key) |
| `sample_to_gene_panel_derived` | `stgp_by_study` | `cancer_study_identifier, alteration_type, gene_panel_id, sample_unique_id` | per-study profiled-sample denominators |

Not every recipe benefits. `top_mutated_genes_in_cohort` has no gene or
literal-study filter. `gene_mutation_variants_in_study`, `top_cna_genes_in_study`
and `gene_cna_distribution_in_study` read columns the projections don't hold
(or a different table), so they read the base table.

The `genomic_event_derived` projections hold only seven columns:
`sample_unique_id`, `cancer_study_identifier`, `hugo_gene_symbol`,
`variant_type`, `mutation_status` and `off_panel` (read by every mutation
recipe), plus `cna_alteration` (used by the amplification / deep-deletion
branch of `gene_alteration_frequency_by_cancer_type`; it's a
`Nullable(Int8)`, about a byte per row). A query that reads any other column
(`patient_unique_id`, `mutation_variant`, `cna_cytoband`, ...) uses the base
table. So does a query where a projection wouldn't read fewer marks.

### Required setting: `optimize_use_implicit_projections = 0`

**Do not apply `9-projections.sql` to a database that anyone queries with
ClickHouse's default settings.** With the default
`optimize_use_implicit_projections = 1`, a `count()` (or `sum(1)`, or a count
over a subquery) whose `WHERE` the base primary key can answer over-counts
once a normal projection exists. The query plan combines two sources:
`ReadFromPreparedSource (_exact_count_projection)` (24.8 labels it
"Optimized trivial count") counts the base granules the primary key proves
fully matching, and `ReadFromMergeTree (<projection>)` counts every matching
row again. For example, 36384 rows are reported instead of 20000, the extra
16384 being those two full granules. Reproduced on 24.8.14, 26.2.19, 26.8.12
and 26.9.3. In the parity runs `GROUP BY` counts, `count(DISTINCT ...)`,
`uniqExact` and the `sql/4` recipe views were not affected; bare counts
were, and agents write them.
`optimize_trivial_count_query = 0`, disabling the analyzer, or narrowing the
projection doesn't help. A `_part_offset`-only "projection index" hits the
same bug on 26.9. Only `optimize_use_implicit_projections = 0` fixes it.

Pin it in the MCP ClickHouse user's profile so a query can't turn it back on:

```sql
ALTER USER llm_user SETTINGS optimize_use_implicit_projections = 0 CONST;
-- or in a settings profile the user has:
-- ALTER SETTINGS PROFILE <profile> SETTINGS optimize_use_implicit_projections = 0 CONST;
```

`CONST` makes a query's `SETTINGS optimize_use_implicit_projections = 1` fail
(error 452) even under `readonly = 2`. Under `readonly = 1`, which
mcp-clickhouse applies to every query when the server allows writes, any
setting change is refused anyway (error 164).

The MCP enforces this at startup. `ensure_db_permissions` runs
`ensure_projection_safe_settings`: if any table in the database defines a
projection, it checks, through the same `run_query` path agent SQL takes,
that the setting is `0` and that a query can't change it. Otherwise it exits
with the `ALTER USER` above. The clone job flips the MCP to a new database by
restarting it, so the check runs for every freshly built database.
Deployments that don't apply `9-projections.sql` are unaffected.

`tests/test_projection_parity_live.py` checks this end to end. Run it with
`CLICKHOUSE_BINARY=/path/to/clickhouse` (it uses `clickhouse local`; no server
or Docker is needed). It builds the upstream DDL, loads three data layouts,
applies `sql/4` and this file, and requires every ad-hoc count/co-occurrence
shape and every `sql/4` view to return the same rows with projections on
(plus the required setting) and off. It also requires the `aligned` layout
to still over-count at ClickHouse defaults, so a data change can't quietly
turn the regression check off.

"The same rows" holds for deterministic queries. A projection reads rows in
a different physical order than the base table, so order-dependent
aggregates can legitimately pick a different row among ties: `any()`,
`argMin()` / `argMax()` with tied keys, `groupArray()` order, and
`LIMIT n` without an `ORDER BY` that breaks ties (in review, an
order-dependent aggregate over tied rows returned `GENE23` from the base
table and `GENE453` from a projection). Both answers are valid, but they differ. When
the winning row matters, give it an explicit `ORDER BY` with a unique
tie-break (e.g. `ORDER BY n DESC, hugo_gene_symbol`), as the `sql/4` views
do.

### When to (re)apply

The derived tables are dropped and recreated on every cBioPortal data load.
The daily clone then builds each LLM table with `CREATE TABLE ... CLONE AS`
from the production database, which has no projections, and runs every
`sql/*.sql`, so `9-projections.sql` recreates them on each clone. For a
manual rebuild, re-run `scripts/apply_sql.sh`.

`CLONE AS` depends on the server version:

- **24.8** doesn't parse it (syntax error).
- **24.10** (checked on 24.10.4) parses it. The clone job's form,
  `CREATE TABLE dst_db.t CLONE AS src_db.t`, creates the table's structure
  (including projection definitions) and then fails to copy the data with
  `Code: 60 ... Table default.t does not exist` whenever `src_db` isn't the
  session's current database. The empty table is left behind. With `set -e`
  the client's non-zero exit stops the job, but a retry, an `IF NOT EXISTS`,
  or a caller that ignores the error proceeds with **empty tables**.
- **26.2** (plain `MergeTree`) copies the data, the projection definitions
  and the projection parts.

Because an empty clone would make every count zero rather than fail, the
clone job (which lives in the k8s deployment repo) should assert after
cloning that each table's row count matches its source, e.g.
`SELECT count() FROM dst_db.t` vs `SELECT count() FROM src_db.t`, or
`total_rows` in `system.tables` for both databases, and fail before
switching the MCP to the new database.

The file can safely run more than once. `ADD PROJECTION IF NOT EXISTS` does
nothing when the projection already exists, and `MATERIALIZE PROJECTION` on
parts that already carry it rewrites no rows (it only bumps the part's
mutation version). Each `MATERIALIZE` runs with `mutations_sync = 2`, so the
clone job waits until every replica has built the projection before it
switches the MCP server to the new database.

An `INSERT` into a table that already has a projection builds the
projection for the new part. `MATERIALIZE` is only needed for parts that
existed before the projection was added.

### Storage and build cost

Each projection is a second sorted copy of the columns it holds. These
figures come from a synthetic benchmark on ClickHouse 26.2: 14.56M
`genomic_event_derived` rows, 260k samples, 301 studies.

| Object | Compressed size | vs. base table |
|--------|-----------------|----------------|
| `genomic_event_derived` (base) | 287.5 MiB | – |
| `ged_by_study_gene` (7 columns) | 111.7 MiB | +39% |
| `ged_by_gene_study` (7 columns) | 125.9 MiB | +44% |
| `sample_to_gene_panel_derived` (base) | 3.4 MiB | – |
| `stgp_by_study` | 1.8 MiB | +54% |

On that benchmark, `genomic_event_derived` storage goes up about 1.8×
(the earlier 14-column projections cost 2.3× for the same granule pruning).
Building all three took 58 s in `clickhouse local` on a laptop, and
re-applying the file took 1 s. Expect build time and storage to grow roughly
linearly with row count.

Every LLM database (blue and green) holds its own copy. On ClickHouse Cloud,
the projection parts are new data that the zero-copy clone cannot share.

### Verifying that a query uses a projection

```sql
-- Plan-time: the read step names the projection and shows granules pruned.
EXPLAIN indexes = 1, projections = 1
SELECT count(DISTINCT sample_unique_id)
FROM genomic_event_derived
WHERE cancer_study_identifier = 'msk_impact_2017'
  AND hugo_gene_symbol = 'TP53' AND variant_type = 'mutation';
-- Expect: ReadFromMergeTree (ged_by_study_gene) ... Granules: <small>/<total>
-- A "Projections:" block lists candidates the optimizer rejected and why.
-- (`projections = 1` needs ClickHouse 25.x+: 24.8 and 24.10 reject it with
-- Code 115. There, use `EXPLAIN indexes = 1`; the read step is still named
-- after the chosen projection.)
-- A bare count() plan must NOT contain _exact_count_projection /
-- "Optimized trivial count" next to a projection read; if it does, the
-- required setting is missing for the user running the query.

-- Run-time: which projections served recent queries, and how much they read.
SELECT event_time, query_duration_ms, read_rows, formatReadableSize(read_bytes), projections, query
FROM system.query_log
WHERE type = 'QueryFinish' AND has(tables, currentDatabase() || '.genomic_event_derived')
ORDER BY event_time DESC LIMIT 20;

-- Presence / size / materialization status.
SELECT table, name, sum(rows), formatReadableSize(sum(data_compressed_bytes))
FROM system.projection_parts WHERE active AND database = currentDatabase()
GROUP BY table, name;
SELECT command, is_done, latest_fail_reason FROM system.mutations
WHERE database = currentDatabase() AND command LIKE '%PROJECTION%';
```

(In `system.projection_parts`, `parent_name` is the parent *part* name; filter
on `table` to select a table.)

When benchmarking on ClickHouse 25.3 or later, add
`SETTINGS use_query_condition_cache = 0`. Otherwise, repeats of the same
filter skip granules because of the cache, which hides whether a projection
helped. To force the base table for a comparison, use
`SETTINGS optimize_use_projections = 0`.

### Dropping

```sql
ALTER TABLE genomic_event_derived        DROP PROJECTION IF EXISTS ged_by_study_gene;
ALTER TABLE genomic_event_derived        DROP PROJECTION IF EXISTS ged_by_gene_study;
ALTER TABLE sample_to_gene_panel_derived DROP PROJECTION IF EXISTS stgp_by_study;
```

To keep the projections from coming back on the next clone, delete or edit
`9-projections.sql`. The clone copies tables from the production database,
not from the previous LLM database, so nothing else carries them over.

### Caveats

These were checked on ClickHouse 24.8.14 and 26.2.19 (plain `MergeTree`):

- A type-changing `ALTER TABLE ... MODIFY COLUMN` on a projected column is
  accepted, including on a projection `ORDER BY` column.
- Lightweight `DELETE FROM` on a table with projections fails at the default
  `lightweight_mutation_projection_mode = 'throw'`. On 24.8 it's a query
  setting (`throw` | `drop`). On 26.2 the table-level `MergeTree` setting
  (`throw` | `drop` | `rebuild`) governs, and a query-level value doesn't
  make the `DELETE` pass. There is no `delete` value. A heavy
  `ALTER TABLE ... DELETE` mutation works at the default. No file in `sql/`
  runs either on these tables.
- `deduplicate_merge_projection_mode` is a `MergeTree` table setting (not a
  query setting) on both versions, and matters only for `Replacing` /
  `Collapsing` engines. Both tables are plain `MergeTree`. If the upstream DDL
  ever switches either one, add
  `MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild'` before the
  `ADD PROJECTION`.

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
