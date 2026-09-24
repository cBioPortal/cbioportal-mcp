-- ============================================================================
-- Gene expression / molecular-correlation views
-- ============================================================================
-- Companion to sql/4-mutation-frequency-views.sql but for the continuous-
-- value side of the cBioPortal schema: gene expression, copy-number
-- values, methylation. Backed by `genetic_alteration_derived` (a long
-- table keyed by sample + study + gene + profile_type).
--
-- The agent-facing docs are at `cbioportal://gene-expression-guide`.
-- ============================================================================

-- ============================================================================
-- gene_pair_coexpression — Spearman correlation between two genes
-- ============================================================================
-- Mirrors cbioportal-backend's ClickhouseCoExpressionMapper.getCoExpressions,
-- simplified to a pair-of-genes lookup (the backend computes one ref
-- gene vs all other genes for the coexpression page; here the agent
-- asks about a specific pair like "TP53 vs MYC").
--
-- Parameters:
--   study         — cancer_study.cancer_study_identifier
--                   (single study; expression profiles are study-scoped)
--   gene_a        — first HUGO gene symbol
--   gene_b        — second HUGO gene symbol
--   profile_type  — one of the values in genetic_alteration_derived.profile_type
--                   Common: 'mrna', 'mrna_median_Zscores',
--                   'mrna_seq_v2_rsem', 'mrna_seq_v2_rsem_Zscores'
--                   Discover available: SELECT DISTINCT profile_type
--                   FROM genetic_alteration_derived
--                   WHERE cancer_study_identifier = '<study>'
--
-- Usage:
--   SELECT * FROM gene_pair_coexpression(
--       study='brca_metabric',
--       gene_a='TP53',
--       gene_b='MYC',
--       profile_type='mrna'
--   );
--
-- Returns one row: (gene_a, gene_b, profile_type,
-- spearman_correlation, num_samples). spearman_correlation is in
-- [-1, 1]; NULL if fewer than 3 samples have valid pair values.
-- ============================================================================

DROP VIEW IF EXISTS gene_pair_coexpression;

CREATE VIEW gene_pair_coexpression AS
WITH a AS (
    SELECT sample_unique_id, toFloat64OrNull(alteration_value) AS v
    FROM genetic_alteration_derived
    WHERE cancer_study_identifier = {study:String}
      AND profile_type = {profile_type:String}
      AND hugo_gene_symbol = {gene_a:String}
      AND alteration_value NOT IN ('', 'NA')
      AND toFloat64OrNull(alteration_value) IS NOT NULL
),
b AS (
    SELECT sample_unique_id, toFloat64OrNull(alteration_value) AS v
    FROM genetic_alteration_derived
    WHERE cancer_study_identifier = {study:String}
      AND profile_type = {profile_type:String}
      AND hugo_gene_symbol = {gene_b:String}
      AND alteration_value NOT IN ('', 'NA')
      AND toFloat64OrNull(alteration_value) IS NOT NULL
)
SELECT {gene_a:String}      AS gene_a,
       {gene_b:String}      AS gene_b,
       {profile_type:String} AS profile_type,
       if(count() >= 3, rankCorr(a.v, b.v), NULL) AS spearman_correlation,
       count() AS num_samples
FROM a INNER JOIN b USING (sample_unique_id);
