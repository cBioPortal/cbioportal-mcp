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
| 9 | `9-projections.sql` | ClickHouse projections that re-sort `genomic_event_derived` (study-first and gene-first) and `sample_to_gene_panel_derived` (study-first) for frequency / co-occurrence query shapes. Performance only; query results are unchanged. See [Projections](#projections). |

Everything under `sql/` directly is **portable** — works against any cBioPortal deployment. Deployment-specific SQL lives under `sql/portal-specific/<portal-name>/`:

| Path | Scope |
|------|-------|
| `sql/portal-specific/public-portal/0-preferences.sql` | Public cBioPortal (`cbioportal.org`). Loads `all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes` preferences. All INSERTs gated on `cancer_study` existence, so on other deployments this is a no-op rather than an error. |

`apply_sql.sh` and the daily clone cron apply the portable files first (in numeric order), then iterate every subdirectory of `portal-specific/`. A deployer image can ship multiple subdirs if it needs to, but typically only contains the one for that portal.

## Projections

`9-projections.sql` adds alternate sort orders ("projections") to two derived
tables. The base tables are sorted for the cBioPortal backend
(`genetic_profile_stable_id` first, and no study column in
`sample_to_gene_panel_derived`'s key), while the agent filters by study +
gene + variant type:

| Table | Projection | ORDER BY | Serves |
|-------|------------|----------|--------|
| `genomic_event_derived` | `ged_by_study_gene` | `cancer_study_identifier, hugo_gene_symbol, variant_type, sample_unique_id` | gene X in study S, two-gene co-occurrence in S, `gene_*_in_study(ies)` |
| `genomic_event_derived` | `ged_by_gene_study` | `hugo_gene_symbol, variant_type, cancer_study_identifier, sample_unique_id` | gene X across a cohort / cancer types (`gene_*_by_cancer_type`, whose `JOIN cohort` can't use a study-leading key) |
| `sample_to_gene_panel_derived` | `stgp_by_study` | `cancer_study_identifier, alteration_type, gene_panel_id, sample_unique_id` | per-study profiled-sample denominators |

The two `genomic_event_derived` projections are **narrow**. They leave out
`mutation_variant`, `driver_filter_annotation`, `driver_tiers_filter_annotation`,
`cna_cytoband` and `sv_event_info`, and `ged_by_gene_study` also leaves out
`patient_unique_id`. A query that reads any of those columns falls back to the
base table, and so does a query where the projection would not read fewer
marks. The fallback happens automatically and returns the same results.

### When to (re)apply

The derived tables are dropped and recreated on every cBioPortal data load,
and the daily clone rebuilds each table with `CREATE TABLE ... CLONE AS`. The
clone does **not** carry projections over from an earlier LLM database, so
`9-projections.sql` must run after every rebuild. The clone CronJob already
does this because it runs every `sql/*.sql`. For a manual rebuild, re-run
`scripts/apply_sql.sh`.

The file can safely run more than once. `ADD PROJECTION IF NOT EXISTS` does
nothing when the projection already exists, and `MATERIALIZE PROJECTION` on
parts that already have it finishes almost immediately. Each `MATERIALIZE`
runs with `mutations_sync = 2`, so the clone job waits until every replica
has built the projection before it switches the MCP server to the new
database.

A new part created by an `INSERT` builds its projections automatically.
`MATERIALIZE` is only needed for parts that existed before the projection was
added.

### Storage and build cost

Each projection is a second sorted copy of the columns it holds. These
figures come from a synthetic benchmark on ClickHouse 26.2: 14.56M
`genomic_event_derived` rows, 260k samples, 301 studies.

| Object | Compressed size | vs. base table |
|--------|-----------------|----------------|
| `genomic_event_derived` (base) | 287.5 MiB | – |
| `ged_by_study_gene` | 206.9 MiB | +72% |
| `ged_by_gene_study` | 154.6 MiB | +54% |
| `sample_to_gene_panel_derived` (base) | 3.4 MiB | – |
| `stgp_by_study` | 1.8 MiB | +54% |

On that benchmark, the storage used by `genomic_event_derived` goes up by
about 2.3×. Building the projections took about 1.5–3.5 minutes per
`genomic_event_derived` projection on an 8-vCPU laptop container. Expect
build time and storage to grow roughly linearly with row count.

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
-- (`projections = 1` needs ClickHouse 25.x+. On 24.8, use `EXPLAIN indexes = 1`;
-- the read step is still named after the chosen projection.)

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

To keep a projection from coming back on the next clone, also delete or edit
`9-projections.sql`.

### Caveats

- `ALTER TABLE ... MODIFY COLUMN` that changes a column's **type** is refused
  while a projection references that column. Drop the projection first.
  Comment-only changes such as those in `1-add-column-comments.sql` are fine.
- Lightweight `DELETE` / `UPDATE` on a table with projections needs
  `lightweight_mutation_projection_mode`. No file in `sql/` runs either
  statement on these tables today.
- Both tables are plain `MergeTree`, so `deduplicate_merge_projection_mode`
  (needed only for `Replacing`/`Collapsing` engines) does not apply. If the
  upstream DDL ever switches either table to `ReplacingMergeTree`, add
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
