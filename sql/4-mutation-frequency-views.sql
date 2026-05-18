-- ============================================================================
-- Mutation-frequency views (coverage building blocks + frequency recipes)
-- ============================================================================
-- This file is the mutation side of the agent's gene-frequency API:
--   - Two coverage building-block views (`mutation_panel_gene_coverage`,
--     `mutation_wes_coverage`).
--   - Parameterized "frequency by cancer type for cohort Y" recipe.
--   - Parameterized "top-N most-mutated genes in cohort" recipe.
--
-- Sibling files in this directory:
--   sql/5-gene-expression-views.sql — gene_pair_coexpression and any
--     other expression / copy-number-value / methylation correlation
--     views. Anything backed by `genetic_alteration_derived` lives
--     there, not here.
--
-- The agent-facing docs are at `cbioportal://mutation-frequency-guide`.
-- ============================================================================

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
--   preference  — a row in cancer_study_query_preferences.preference_name.
--                 Default is 'pan_cancer_tcga' (32 TCGA PanCancer Atlas
--                 studies, consistent labels, balanced sample sizes,
--                 canonical published reference). Other shipped values:
--                 'large_genomic_cohort' (msk_impact_50k_2026 — biggest
--                 single MSK cohort), 'treatment_outcomes' (msk_chord_2024
--                 — treatment-rich), and 'all_studies_non_redundant' (242
--                 studies, use sparingly — see mutation-frequency-guide;
--                 CANCER_TYPE labels are not normalized across studies).
--   gene        — HUGO gene symbol (e.g. 'TP53', 'KRAS')
--
-- Usage:
--   SELECT *
--   FROM gene_mutation_frequency_by_cancer_type(
--       preference='pan_cancer_tcga',
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

-- ============================================================================
-- top_mutated_genes_in_cohort — top-N most-mutated genes in a cohort
-- ============================================================================
-- Mirrors cbioportal-backend's StudyViewMapper.getMutatedGenes, but with
-- a WES-aware gene-specific profiled denominator (so the percentage
-- reflects real biology instead of being inflated for low-coverage
-- genes).
--
-- The trick to keeping this cheap at cohort scale: WES samples are
-- profiled for ALL genes, so their count is a single number per cohort
-- (computed once). For named-panel samples, profiled count is per-gene
-- and only needs to include the genes the panel actually lists. We
-- compute these two pieces separately and add them per gene.
--
-- Parameters:
--   preference  — cancer_study_query_preferences.preference_name
--                 (e.g. 'pan_cancer_tcga' default; see
--                 gene_mutation_frequency_by_cancer_type docstring)
--   top_n       — UInt32, max number of genes to return
--                 (e.g. 20 for the typical "top 20 mutated genes" question)
--
-- Usage:
--   SELECT *
--   FROM top_mutated_genes_in_cohort(
--       preference='pan_cancer_tcga',
--       top_n=20
--   );
--
-- Returns one row per gene: (hugo_gene_symbol, altered_samples,
-- profiled_samples, frequency_pct, total_mutation_events). Sorted by
-- altered_samples DESC, then hugo_gene_symbol ASC (same as backend).
-- ============================================================================

DROP VIEW IF EXISTS top_mutated_genes_in_cohort;

CREATE VIEW top_mutated_genes_in_cohort AS
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = {preference:String}
),
wes_profiled_count AS (
    -- WES samples are profiled for every gene; one number for the cohort.
    SELECT COUNT(DISTINCT w.sample_unique_id) AS n
    FROM mutation_wes_coverage w
    JOIN cohort c USING (cancer_study_identifier)
),
panel_profiled_per_gene AS (
    -- For each gene, count cohort samples on a named panel that lists it.
    SELECT mpgc.hugo_gene_symbol, COUNT(DISTINCT mpgc.sample_unique_id) AS n
    FROM mutation_panel_gene_coverage mpgc
    JOIN cohort c USING (cancer_study_identifier)
    GROUP BY mpgc.hugo_gene_symbol
),
altered_per_gene AS (
    SELECT ged.hugo_gene_symbol,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples,
           COUNT(*) AS total_mutation_events
    FROM genomic_event_derived ged
    JOIN cohort c USING (cancer_study_identifier)
    WHERE ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.off_panel = 0
    GROUP BY ged.hugo_gene_symbol
)
SELECT a.hugo_gene_symbol,
       a.altered_samples,
       (SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0) AS profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF((SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0), 0), 1) AS frequency_pct,
       a.total_mutation_events
FROM altered_per_gene a
LEFT JOIN panel_profiled_per_gene p USING (hugo_gene_symbol)
ORDER BY altered_samples DESC, hugo_gene_symbol ASC
LIMIT {top_n:UInt32};

