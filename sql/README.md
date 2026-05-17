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
| 3 | `3-add-cancer-study-query-preferences.sql` | **Portable.** Creates `cancer_study_query_preferences` table + pattern-detected preferences (currently `pan_cancer_tcga`). Safe for any deployment. |
| 4 | `4-public-portal-preferences.sql` | **Public-cBioPortal-specific.** Loads `all_studies_non_redundant`, `large_genomic_cohort`, `treatment_outcomes`. All INSERTs gated on `cancer_study` existence, so on other deployments this becomes a no-op rather than an error. |

## Deploying for a non-public portal

Two options for adding your own preferences:

1. **Leave `4-public-portal-preferences.sql` in place** — its existence-checked
   INSERTs are no-ops if you don't have the cBioPortal-public studies. Add
   your own preferences in a higher-numbered file, e.g.
   `5-mydeployment-preferences.sql`. The cron will pick it up automatically.

2. **Replace `4-public-portal-preferences.sql`** in your image / mount with
   your own preferences file. Use the same defensive existence-checking
   pattern so a missing study doesn't write a phantom row.

## Conventions for new preference files

- Numeric prefix so order is intrinsic. New files start at `5-` and up.
- Every `INSERT INTO cancer_study_query_preferences` must gate on
  `WHERE cancer_study_identifier IN (SELECT cancer_study_identifier FROM cancer_study)`
  (or equivalent) so the file is harmless on databases that don't have your
  studies.
- One `preference_name` per query-intent, lower_snake_case. Document its
  purpose in the `notes` column so the agent can self-explain to users.
- The agent discovers available preferences via
  `SELECT DISTINCT preference_name FROM cancer_study_query_preferences` —
  no hardcoded list anywhere outside SQL.
