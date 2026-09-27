# LLM-prep SQL

The daily clone CronJob (see `knowledgesystems-k8s-deployment`) executes every
`*.sql` file in this directory **in lexicographic (text) order** against the freshly-cloned
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
| 99 | `99-dictionaries.sql` | ID lookup dictionaries for `dictGet`/`dictHas`: genes (Entrez ID ↔ Hugo symbol), studies (identifier → name, cancer type, sample counts) and genetic profiles (stable ID → study, alteration type). Recreated on every daily clone; existing views unchanged. See [ID lookup dictionaries](#id-lookup-dictionaries-99-dictionariessql). |

Everything under `sql/` directly is **portable** — works against any cBioPortal deployment. Deployment-specific SQL lives under `sql/portal-specific/<portal-name>/`:

| Path | Scope |
|------|-------|
| `sql/portal-specific/public-portal/0-preferences.sql` | Public cBioPortal (`cbioportal.org`). Loads `all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes` preferences. All INSERTs gated on `cancer_study` existence, so on other deployments this is a no-op rather than an error. |

`apply_sql.sh` and the daily clone cron apply the portable files first (in text order), then iterate every subdirectory of `portal-specific/`. A deployer image can ship multiple subdirs if it needs to, but typically only contains the one for that portal.

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

## ID lookup dictionaries (`99-dictionaries.sql`)

`scripts/apply_sql.sh` uses Bash's sorted glob; the public clone job uses
`ls /workdir/sql/*.sql | sort`. Both use text order, not numeric order.
The `99-` prefix sorts after portable scripts 0–7 and the reserved
`8-precomputed-aggregates.sql` (#154) and `9-projections.sql` (#156), under both
text and version (`sort -V`) order. A `10-` prefix would sort between `1-` and
`2-`. Portal-specific SQL remains a separate, later phase; these dictionaries
do not depend on portal-specific preferences.

This optional lookup layer adds four `COMPLEX_KEY_HASHED` dictionaries. Existing
views and their results are unchanged; no join-to-`dictGet` rewrite is included.

| Dictionary | Tuple key | Attributes |
|------------|-----------|------------|
| `gene_by_entrez_dict` | `tuple(toInt64(entrez_id))` | `hugo_gene_symbol` |
| `gene_by_symbol_dict` | `tuple(symbol)` | `entrez_gene_ids` (sorted distinct `Array(Int64)`) |
| `study_by_identifier_dict` | `tuple(study_identifier)` | `cancer_study_id`, `name`, `type_of_cancer_id`, `sample_count`, `mutation_sample_count`, `cna_sample_count` |
| `genetic_profile_by_stable_id_dict` | `tuple(profile_stable_id)` | `genetic_profile_id`, `cancer_study_id`, `genetic_alteration_type` |

Examples (run in the prepped database):

```sql
SELECT dictGet('gene_by_entrez_dict', 'hugo_gene_symbol', tuple(toInt64(7157)));
SELECT dictGet('gene_by_symbol_dict', 'entrez_gene_ids', tuple('TP53'));
SELECT dictGet('study_by_identifier_dict', ('name', 'sample_count'), tuple('brca_tcga'));
SELECT dictGet('genetic_profile_by_stable_id_dict',
               ('cancer_study_id', 'genetic_alteration_type'), tuple('brca_tcga_mutations'));
-- Check existence before interpreting a default value as real data:
SELECT dictHas('study_by_identifier_dict', tuple('brca_tcga'));
```

Sources are the clone's own `gene`, `genetic_profile`, `cancer_study`, `sample_list`,
and `sample_list_list` tables. Two new source views aggregate symbol matches and
study sample-list counts. The counts follow script 6: membership-row counts in
`<study>_all`, `<study>_sequenced`, and `<study>_cna`, including zero for studies
without those lists. They are not counts of patients, mutation events, or unique
samples across overlapping studies. Null study identifiers are excluded. Gene
symbols are case-sensitive; aliases are not resolved. Signed Entrez IDs are
preserved, and ambiguous symbols return all distinct IDs rather than picking one.
Other keys assume the upstream logical uniqueness of Entrez IDs, study identifiers,
and profile stable IDs; conflicting duplicate rows can produce an arbitrary value.
A dictionary lookup does not preserve the row multiplicity of a join.

All objects use `CREATE OR REPLACE`. `LIFETIME(0)` disables periodic refresh:
rerunning the daily clone's prep scripts replaces the dictionaries, and their
first subsequent lookup loads the new snapshot. After manual source changes,
rerun this file or use `SYSTEM RELOAD DICTIONARY <name>`.

The blue/green clone job drops and recreates the **inactive** database, applies
all prep SQL with `--database DST_DB`, then updates `CLICKHOUSE_DATABASE` in the
active ConfigMap and rolls the MCP deployment. It does not rename databases or
move an old dictionary into the new database. Each color therefore owns fresh
dictionaries sourced from its own completed clone; `LIFETIME(0)` does not carry
an old color's cached IDs into the new color. Reusing a color on the next cycle
also recreates its dictionaries. Existing requests on the old deployment can
still use the old database until the rollout finishes; this is not an atomic
cutover of all clients. In-place source mutation requires an explicit reload.

Local `CLICKHOUSE(TABLE ...)` sources bind to the database containing the dictionary,
so no deployment database name or remote password is embedded. This follows the
[ClickHouse 24.8 source's default database handling](https://github.com/ClickHouse/ClickHouse/blob/v24.8.14.39-lts/src/Dictionaries/ClickHouseDictionarySource.cpp)
and the [zero-lifetime refresh policy](https://clickhouse.com/docs/reference/statements/create/dictionary/lifetime). In ClickHouse 24.8
these sources authenticate as the local `default` user; it must be enabled with
an empty password and have SELECT access to the source tables/views. Deployments
that disable or password-protect that account must configure dictionary source
credentials locally before applying this file. Do not commit secrets. The MCP
runtime account also needs permission to call `dictGet`/`dictHas` on these
objects. Creation/loading and any grants belong to the deployment admin.

Expected gains are likely small: these dimension tables are small already, and
existing queries do not automatically use the dictionaries. No latency improvement
is claimed or benchmarked. Tradeoffs are memory for loaded dictionaries, first-use
load latency (including aggregation of sample lists), and stale values until the
next prep/reload. Missing keys return type defaults (empty string, zero, empty
array); use `dictHas` to distinguish absence from a legitimate value. Check
`system.dictionaries` (`status`, `last_exception`, `bytes_allocated`) when diagnosing
load failures. See the [ClickHouse dictionary reference](https://clickhouse.com/docs/reference/statements/create/dictionary).

### Verification

`tests/fixtures/dictionary_upstream_schema.sql` contains verbatim table excerpts
from a pinned upstream cBioPortal ClickHouse schema (source URL in the fixture).
Static tests check idempotent DDL, source columns, local source configuration, and
refresh policy without network access. For execution and lookup-equivalence checks:

```bash
docker run -d --name ch-dict-test clickhouse/clickhouse-server:24.8
CH_DICTIONARY_TEST_CONTAINER=ch-dict-test uv run pytest -q tests/test_dictionaries_sql.py
docker rm -f ch-dict-test
```

The integration test creates and removes its own isolated databases. It compares
all four dictionaries with source SELECTs on synthetic data, checks known study
counts (including an empty study), duplicate symbols, negative IDs, null study
identifiers, missing keys, a lookup from another database, and replacement after
source changes. It also switches between two databases with different IDs for the
same lookup keys, then drops/recreates the first database to exercise color reuse.
These live checks require a working ClickHouse container and are not covered by
the static tests. This validates the new lookups, not equivalence of rewritten views:
no existing views are rewritten. The Docker test is skipped unless the container
environment variable is set.
