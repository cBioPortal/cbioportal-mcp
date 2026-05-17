-- ============================================================================
-- mutation_panel_gene_coverage + mutation_wes_coverage
-- ============================================================================
-- The canonical "is this sample profiled for gene G?" query has to handle
-- two cases:
--
--   1. The sample is on a NAMED gene panel (MSK-IMPACT, METABRIC_173,
--      ARCHER-SOLID, ...) and the panel's `gene_panel_list` entries do
--      or don't include G.
--   2. The sample is sequenced via WES (`gene_panel_id = 'WES'`), in
--      which case ALL genes are considered profiled. WES is NOT in the
--      `gene_panel` table at all, so any JOIN through `gene_panel` /
--      `gene_panel_list` silently drops every WES row — this is the bug
--      that produced 172% TP53 frequencies in cross-cancer-type queries
--      before the views below existed.
--
-- These two views together let queries answer "samples profiled for
-- mutations in gene G" with one UNION ALL instead of re-deriving the
-- WES branch every time:
--
--   SELECT sample_unique_id, cancer_study_identifier
--   FROM mutation_panel_gene_coverage WHERE hugo_gene_symbol = 'TP53'
--   UNION ALL
--   SELECT sample_unique_id, cancer_study_identifier
--   FROM mutation_wes_coverage;
--
-- See cbioportal://mutation-frequency-guide for the full canonical
-- frequency-by-cancer-type recipe.
--
-- Refreshed (DROP + CREATE) on every clone. Views are cheap — they hold
-- no data, just the query definition.
-- ============================================================================

DROP VIEW IF EXISTS mutation_panel_gene_coverage;

CREATE VIEW mutation_panel_gene_coverage AS
SELECT
    stgp.sample_unique_id,
    stgp.cancer_study_identifier,
    g.hugo_gene_symbol,
    stgp.gene_panel_id
FROM sample_to_gene_panel_derived stgp
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED';

DROP VIEW IF EXISTS mutation_wes_coverage;

CREATE VIEW mutation_wes_coverage AS
SELECT
    sample_unique_id,
    cancer_study_identifier
FROM sample_to_gene_panel_derived
WHERE alteration_type = 'MUTATION_EXTENDED'
  AND gene_panel_id = 'WES';

-- ============================================================================
-- gene_mutation_frequency_by_cancer_type — parameterized full recipe
-- ============================================================================
-- The agent's canonical "gene X mutation frequency across cancer types in
-- cohort Y" computation, exposed as a parameterized view so the agent
-- doesn't have to write (and can't get wrong) the JOIN chain.
--
-- Parameters:
--   preference  — a row in cancer_study_query_preferences.preference_name
--                 (e.g. 'all_studies_non_redundant', 'pan_cancer_tcga',
--                 'large_genomic_cohort', 'treatment_outcomes')
--   gene        — HUGO gene symbol (e.g. 'TP53', 'KRAS')
--
-- Usage:
--   SELECT *
--   FROM gene_mutation_frequency_by_cancer_type(
--       preference='all_studies_non_redundant',
--       gene='TP53'
--   )
--   ORDER BY frequency_pct DESC;
--
-- Returns one row per cancer type with at least 50 profiled samples for
-- the gene in the cohort: (cancer_type, altered_samples,
-- profiled_samples, frequency_pct).
--
-- ClickHouse parameterized views (23.1+) are the documented equivalent
-- of stored procedures for reusable parameterized SELECTs — see
-- https://clickhouse.com/docs/guides/developer/stored-procedures-and-prepared-statements
-- ============================================================================

DROP VIEW IF EXISTS gene_mutation_frequency_by_cancer_type;

CREATE VIEW gene_mutation_frequency_by_cancer_type AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    JOIN cohort c USING (cancer_study_identifier)
    WHERE cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN cohort c USING (cancer_study_identifier)
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = {gene:String}
    UNION ALL
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_wes_coverage
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
    JOIN cohort c USING (cancer_study_identifier)
    JOIN sample_cancer_type sct USING (sample_unique_id)
    GROUP BY sct.cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50;
