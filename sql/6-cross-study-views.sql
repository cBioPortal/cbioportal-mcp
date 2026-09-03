-- ============================================================================
-- Cross-study views (raw-SQL parity for the cross_study_alteration_frequency tool)
-- ============================================================================
-- The MCP tool `cross_study_alteration_frequency` returns one row per study
-- with a panel-aware denominator, then pools with a random-effects model in
-- Python (see docs/cross-study-meta-analysis-plan.md). It does NOT depend on
-- this file: it builds the same query inline over the coverage views in
-- sql/4-mutation-frequency-views.sql. This view exists so a raw-SQL user (or
-- the agent cross-checking a payload) can get the per-study counts the tool
-- starts from, with the same denominator rules.
--
-- Parameters:
--   studies         — Array(String) of cancer_study_identifier values
--   gene            — HUGO gene symbol
--   alteration      — 'mutation' | 'amplification' | 'deep_deletion' | 'structural_variant'
--   oncotree_codes  — Array(String) of OncoTree codes matched per sample on the
--                     ONCOTREE_CODE clinical attribute; pass [] to take each
--                     study whole
--
-- Usage:
--   SELECT * FROM gene_alteration_counts_per_study(
--       studies        = ['msk_chord_2024', 'luad_tcga_pan_can_atlas_2018'],
--       gene           = 'TP53',
--       alteration     = 'mutation',
--       oncotree_codes = ['LUAD']
--   ) ORDER BY study;
--
-- Returns one row per study that has cohort samples: (study, cohort_samples,
-- profiled_samples, altered_samples, cohort_patients, profiled_patients,
-- altered_patients, frequency_pct). frequency_pct is sample-level:
-- altered_samples / profiled_samples. Studies with 0 profiled samples are the
-- "gene not on this study's panels" case — report them as not covered, never
-- as 0%. Pooling across the rows is NOT SUM/SUM; use the tool for that.
--
-- Two ClickHouse traps this view guards against: LEFT JOIN fills non-matches
-- with '' (not NULL), so counts use uniqExactIf(col, joined != ''); and uniq()
-- is approximate, so everything is uniqExact.
-- ============================================================================

DROP VIEW IF EXISTS gene_alteration_counts_per_study;

CREATE VIEW gene_alteration_counts_per_study AS
WITH cohort AS (
    SELECT sd.cancer_study_identifier, sd.sample_unique_id, sd.patient_unique_id
    FROM sample_derived sd
    WHERE sd.cancer_study_identifier IN {studies:Array(String)}
      AND (
        length({oncotree_codes:Array(String)}) = 0
        OR sd.sample_unique_id IN (
            SELECT sample_unique_id
            FROM clinical_data_derived
            WHERE cancer_study_identifier IN {studies:Array(String)}
              AND attribute_name = 'ONCOTREE_CODE'
              AND upper(attribute_value) IN {oncotree_codes:Array(String)}
        )
      )
),
profiling_type AS (
    SELECT multiIf(
        {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
        {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
        {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
        {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
        '') AS t
),
profiled AS (
    -- Samples on a named panel that lists the gene, plus WES samples (WES is
    -- not a row in gene_panel, which is the >100% trap).
    SELECT stgp.sample_unique_id
    FROM sample_to_gene_panel_derived stgp
    JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
    WHERE g.hugo_gene_symbol = {gene:String}
      AND stgp.alteration_type = (SELECT t FROM profiling_type)
      AND stgp.cancer_study_identifier IN {studies:Array(String)}
    UNION ALL
    SELECT sample_unique_id
    FROM sample_to_gene_panel_derived
    WHERE gene_panel_id = 'WES'
      AND alteration_type = (SELECT t FROM profiling_type)
      AND cancer_study_identifier IN {studies:Array(String)}
),
altered AS (
    SELECT DISTINCT ged.sample_unique_id
    FROM genomic_event_derived ged
    WHERE ged.cancer_study_identifier IN {studies:Array(String)}
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
      AND (
        ({alteration:String} = 'mutation'
            AND ged.variant_type = 'mutation'
            AND ged.mutation_status != 'UNCALLED')
        OR ({alteration:String} = 'amplification'
            AND ged.variant_type = 'cna'
            AND ged.cna_alteration = 2)
        OR ({alteration:String} = 'deep_deletion'
            AND ged.variant_type = 'cna'
            AND ged.cna_alteration = -2)
        OR ({alteration:String} = 'structural_variant'
            AND ged.variant_type = 'structural_variant')
      )
)
SELECT c.cancer_study_identifier AS study,
       uniqExact(c.sample_unique_id) AS cohort_samples,
       uniqExactIf(c.sample_unique_id, p.sample_unique_id != '') AS profiled_samples,
       uniqExactIf(c.sample_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '') AS altered_samples,
       uniqExact(c.patient_unique_id) AS cohort_patients,
       uniqExactIf(c.patient_unique_id, p.sample_unique_id != '') AS profiled_patients,
       uniqExactIf(c.patient_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '') AS altered_patients,
       ROUND(uniqExactIf(c.sample_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '') * 100.0
             / NULLIF(uniqExactIf(c.sample_unique_id, p.sample_unique_id != ''), 0), 1) AS frequency_pct
FROM cohort c
LEFT JOIN profiled p USING (sample_unique_id)
LEFT JOIN altered a USING (sample_unique_id)
GROUP BY study;
