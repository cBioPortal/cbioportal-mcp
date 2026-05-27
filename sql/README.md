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
| 4 | `4-mutation-frequency-views.sql` | Mutation-frequency parameterized views (`gene_mutation_frequency_by_cancer_type`, `top_mutated_genes_in_cohort`) and the two coverage building-block views (`mutation_panel_gene_coverage`, `mutation_wes_coverage`). Handles the "WES is not in `gene_panel`" trap that produces >100% frequencies. See `cbioportal://mutation-frequency-guide`. |
| 5 | `5-gene-expression-views.sql` | Gene-expression / copy-number-value / methylation views, backed by `genetic_alteration_derived`. Currently `gene_pair_coexpression(study, gene_a, gene_b, profile_type)` for Spearman correlation between two genes. See `cbioportal://gene-expression-guide`. |
| 6 | `6-study-auth-row-policies.sql` | ClickHouse row policies that enforce request-local study authorization using MCP-provided `cbioportal_*` custom settings. See `docs/study-auth-row-policies.md`. |

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
