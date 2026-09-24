-- ============================================================================
-- Mutation-frequency views (coverage building blocks + frequency recipes)
-- ============================================================================
-- This file is the agent's gene-frequency API (mutation, CNA, SV):
--   - Coverage building-block views: `mutation_panel_gene_coverage`,
--     `mutation_wes_coverage`, and the CNA / SV counterparts
--     (`cna_*_coverage`, `sv_*_coverage`).
--   - Parameterized "frequency by cancer type for cohort Y" recipe.
--   - Parameterized "top-N most-mutated genes" recipes (cohort, single study).
--   - Single-study views mirroring the portal's study-view charts/tables:
--     `gene_mutation_variants_in_study`, `co_altered_genes_in_study`,
--     `top_cna_genes_in_study`, `gene_cna_distribution_in_study`,
--     `top_sv_genes_in_study`.
--
-- Sibling files in this directory:
--   sql/5-gene-expression-views.sql — gene_pair_coexpression and any
--     other expression / copy-number-value / methylation correlation
--     views. Expression / continuous-value data backed by
--     `genetic_alteration_derived` lives there; the discrete CNA
--     distribution (`gene_cna_distribution_in_study`) lives here.
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
-- gene_mutation_frequency_in_study — per-study mutation frequency
-- ============================================================================
-- Same recipe as gene_mutation_frequency_by_cancer_type, but scoped to one
-- explicit study instead of a cohort lookup. Use when the user names a
-- specific study and that study isn't part of a shipped preference.
--
-- Parameters:
--   study  — a cancer_study.cancer_study_identifier
--            (e.g. 'brca_metabric', 'lung_msk_2017')
--   gene   — HUGO gene symbol (e.g. 'TP53', 'KRAS')
--
-- Usage:
--   SELECT *
--   FROM gene_mutation_frequency_in_study(
--       study='brca_metabric',
--       gene='TP53'
--   )
--   ORDER BY frequency_pct DESC;
--
-- For single-cancer-type studies (most named studies), returns one row.
-- For multi-cancer studies that carry a per-sample `CANCER_TYPE`
-- attribute (e.g. msk_chord_2024), returns one row per cancer type with
-- ≥ 50 profiled samples.
-- ============================================================================

DROP VIEW IF EXISTS gene_mutation_frequency_in_study;

CREATE VIEW gene_mutation_frequency_in_study AS
WITH sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier = {study:String}
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.cancer_study_identifier = {study:String}
      AND ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = {gene:String}
      AND cancer_study_identifier = {study:String}
    UNION ALL
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier = {study:String}
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
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
-- gene_mutation_frequency_in_studies — ad-hoc cohort of named studies
-- ============================================================================
-- Same recipe as gene_mutation_frequency_in_study, but takes an Array of
-- study ids. Use when the user names a handful of studies that aren't a
-- shipped preference and there's no time to add one — e.g. "TP53 in
-- METABRIC, MSK-IMPACT, and Pan-LungCancer".
--
-- IMPORTANT: this is your responsibility, not the view's: the studies
-- you pass must NOT share samples. Sample IDs are study-prefixed
-- (cancer_study_id_ + sample.stable_id), so the same physical sample
-- in two studies will count twice and you can get >100% frequencies.
-- The shipped preferences (`all_studies_non_redundant`, `pan_cancer_tcga`)
-- are vetted to be non-overlapping; ad-hoc lists are not.
--
-- Parameters:
--   studies  — Array(String) of cancer_study_identifier values
--              (e.g. ['brca_metabric', 'brca_tcga_pan_can_atlas_2018'])
--   gene     — HUGO gene symbol
--
-- Usage:
--   SELECT *
--   FROM gene_mutation_frequency_in_studies(
--       studies=['brca_metabric','brca_tcga_pan_can_atlas_2018'],
--       gene='TP53'
--   )
--   ORDER BY frequency_pct DESC;
-- ============================================================================

DROP VIEW IF EXISTS gene_mutation_frequency_in_studies;

CREATE VIEW gene_mutation_frequency_in_studies AS
WITH sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier IN {studies:Array(String)}
      AND cd.attribute_name = 'CANCER_TYPE'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.cancer_study_identifier IN {studies:Array(String)}
      AND ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = {gene:String}
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = {gene:String}
      AND cancer_study_identifier IN {studies:Array(String)}
    UNION ALL
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier IN {studies:Array(String)}
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
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
-- gene_alteration_frequency_by_cancer_type — generalize to CNA + SV
-- ============================================================================
-- gene_mutation_frequency_by_cancer_type only handles point mutations.
-- This view generalizes the same recipe to copy-number alterations
-- (amplification / deep deletion) and structural variants. The recipe is
-- identical to the mutation case, with two differences:
--   - numerator filter switches based on the alteration kind
--   - denominator looks up samples profiled for the matching
--     alteration_type in sample_to_gene_panel_derived
--     (MUTATION_EXTENDED / COPY_NUMBER_ALTERATION / STRUCTURAL_VARIANT)
--
-- Parameters:
--   preference  — cancer_study_query_preferences.preference_name (same as
--                 gene_mutation_frequency_by_cancer_type)
--   gene        — HUGO gene symbol
--   alteration  — one of:
--                   'mutation'           — point mutations (UNCALLED excluded)
--                   'amplification'      — CNA == +2 (high-level amp)
--                   'deep_deletion'      — CNA == -2 (homozygous deletion)
--                   'structural_variant' — fusion / SV
--
-- Usage:
--   SELECT * FROM gene_alteration_frequency_by_cancer_type(
--       preference='pan_cancer_tcga', gene='MYC', alteration='amplification'
--   ) ORDER BY frequency_pct DESC;
--
-- Returns the same columns as gene_mutation_frequency_by_cancer_type:
-- (cancer_type, altered_samples, profiled_samples, frequency_pct).
--
-- For point mutations the result equals the mutation-only view;
-- gene_mutation_frequency_by_cancer_type is kept as the cleaner
-- shorthand for that case.
-- ============================================================================

DROP VIEW IF EXISTS gene_alteration_frequency_by_cancer_type;

CREATE VIEW gene_alteration_frequency_by_cancer_type AS
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
    WHERE ged.hugo_gene_symbol = {gene:String}
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
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    -- Map the user-facing alteration token to the alteration_type stored
    -- on sample_to_gene_panel_derived. Same gene-in-panel-or-WES branch
    -- as the mutation view, but with the matching alteration_type filter.
    SELECT stgp.sample_unique_id, stgp.cancer_study_identifier
    FROM sample_to_gene_panel_derived stgp
    JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
    WHERE g.hugo_gene_symbol = {gene:String}
      AND stgp.alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
    UNION ALL
    SELECT sample_unique_id, cancer_study_identifier
    FROM sample_to_gene_panel_derived
    WHERE gene_panel_id = 'WES'
      AND alteration_type = multiIf(
          {alteration:String} = 'mutation',           'MUTATION_EXTENDED',
          {alteration:String} = 'amplification',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'deep_deletion',      'COPY_NUMBER_ALTERATION',
          {alteration:String} = 'structural_variant', 'STRUCTURAL_VARIANT',
          '')
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


-- ============================================================================
-- top_mutated_genes_in_study — top-N most-mutated genes in one study
-- ============================================================================
-- Same recipe as top_mutated_genes_in_cohort, scoped to a single
-- cancer_study_identifier instead of a preference cohort. Use this for
-- "top N mutated genes in study X" — top_mutated_genes_in_cohort with
-- 'pan_cancer_tcga' spans all 32 TCGA studies, not one cancer type.
--
-- Parameters:
--   study  — cancer_study_identifier (e.g. 'brca_tcga_pan_can_atlas_2018')
--   top_n  — UInt32, max number of genes to return
--
-- Usage:
--   SELECT *
--   FROM top_mutated_genes_in_study(
--       study='brca_tcga_pan_can_atlas_2018',
--       top_n=5
--   );
--
-- Returns the same columns and ordering as top_mutated_genes_in_cohort.
-- ============================================================================

DROP VIEW IF EXISTS top_mutated_genes_in_study;

CREATE VIEW top_mutated_genes_in_study AS
WITH wes_profiled_count AS (
    SELECT COUNT(DISTINCT sample_unique_id) AS n
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier = {study:String}
),
panel_profiled_per_gene AS (
    SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS n
    FROM mutation_panel_gene_coverage
    WHERE cancer_study_identifier = {study:String}
    GROUP BY hugo_gene_symbol
),
altered_per_gene AS (
    SELECT hugo_gene_symbol,
           COUNT(DISTINCT sample_unique_id) AS altered_samples,
           COUNT(*) AS total_mutation_events
    FROM genomic_event_derived
    WHERE cancer_study_identifier = {study:String}
      AND variant_type = 'mutation'
      AND mutation_status != 'UNCALLED'
      AND off_panel = 0
    GROUP BY hugo_gene_symbol
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


-- ============================================================================
-- gene_mutation_variants_in_study — protein changes of one gene in one study
-- ============================================================================
-- The per-variant breakdown behind the portal's Mutations tab / lollipop:
-- for each protein change of `gene`, how many samples carry it and what
-- fraction of the samples profiled for the gene that is. Use for "most
-- common KRAS mutation in LUAD", "how often is IDH1 R132H", etc.
--
-- Parameters:
--   study  — cancer_study_identifier
--   gene   — HUGO gene symbol
--
-- Usage:
--   SELECT *
--   FROM gene_mutation_variants_in_study(
--       study='luad_tcga_pan_can_atlas_2018',
--       gene='KRAS'
--   );
--
-- Returns one row per (mutation_variant, mutation_type):
-- (mutation_variant, mutation_type, altered_samples, profiled_samples,
-- frequency_pct, total_mutation_events). profiled_samples is the same on
-- every row: samples profiled for mutations in the gene (panel + WES).
-- Sorted by altered_samples DESC, then mutation_variant ASC.
-- ============================================================================

DROP VIEW IF EXISTS gene_mutation_variants_in_study;

CREATE VIEW gene_mutation_variants_in_study AS
WITH profiled_count AS (
    SELECT COUNT(DISTINCT sample_unique_id) AS n
    FROM (
        SELECT sample_unique_id
        FROM mutation_panel_gene_coverage
        WHERE cancer_study_identifier = {study:String}
          AND hugo_gene_symbol = {gene:String}
        UNION ALL
        SELECT sample_unique_id
        FROM mutation_wes_coverage
        WHERE cancer_study_identifier = {study:String}
    )
),
altered_per_variant AS (
    SELECT mutation_variant,
           mutation_type,
           COUNT(DISTINCT sample_unique_id) AS altered_samples,
           COUNT(*) AS total_mutation_events
    FROM genomic_event_derived
    WHERE cancer_study_identifier = {study:String}
      AND hugo_gene_symbol = {gene:String}
      AND variant_type = 'mutation'
      AND mutation_status != 'UNCALLED'
      AND off_panel = 0
    GROUP BY mutation_variant, mutation_type
)
SELECT a.mutation_variant,
       a.mutation_type,
       a.altered_samples,
       (SELECT n FROM profiled_count) AS profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF((SELECT n FROM profiled_count), 0), 1) AS frequency_pct,
       a.total_mutation_events
FROM altered_per_variant a
ORDER BY altered_samples DESC, mutation_variant ASC;

-- ============================================================================
-- co_altered_genes_in_study — genes enriched in X-mutant vs X-wild-type
-- ============================================================================
-- "What else is mutated in KRAS-mutant tumors?" Frequencies inside the
-- mutant group alone are dominated by large passenger genes (TTN, MUC16)
-- that are just as common in wild-type samples, so this view reports each
-- other gene's mutation frequency in both groups and ranks by the
-- difference.
--
-- Groups (both restricted to samples profiled for mutations in `gene`):
--   mutant    — at least one on-panel, non-UNCALLED mutation in `gene`
--   wild-type — profiled for `gene` and no such mutation in it
-- Each other gene's denominator per group is the group's samples that are
-- profiled for that gene (WES samples + samples on a panel listing it).
-- Genes mutated in fewer than 10 samples across both groups are dropped
-- (percentages on a handful of samples are noise).
--
-- Parameters:
--   study  — cancer_study_identifier
--   gene   — HUGO gene symbol defining the groups
--   top_n  — UInt32, max number of genes to return
--
-- Usage:
--   SELECT *
--   FROM co_altered_genes_in_study(
--       study='luad_tcga_pan_can_atlas_2018',
--       gene='KRAS',
--       top_n=20
--   );
--
-- Returns one row per other gene: (hugo_gene_symbol, mutant_altered,
-- mutant_profiled, mutant_pct, wildtype_altered, wildtype_profiled,
-- wildtype_pct, pct_difference = mutant_pct - wildtype_pct). Sorted by
-- abs(pct_difference) DESC. No p-values: significance needs Fisher's
-- exact test (cBioPortal Comparison / Mutual Exclusivity tab).
-- ============================================================================

DROP VIEW IF EXISTS co_altered_genes_in_study;

CREATE VIEW co_altered_genes_in_study AS
WITH wes_samples AS (
    SELECT DISTINCT sample_unique_id
    FROM mutation_wes_coverage
    WHERE cancer_study_identifier = {study:String}
),
panel_samples_per_gene AS (
    SELECT DISTINCT sample_unique_id, hugo_gene_symbol
    FROM mutation_panel_gene_coverage
    WHERE cancer_study_identifier = {study:String}
),
profiled_for_gene AS (
    SELECT sample_unique_id FROM wes_samples
    UNION DISTINCT
    SELECT sample_unique_id FROM panel_samples_per_gene
    WHERE hugo_gene_symbol = {gene:String}
),
mutated AS (
    SELECT DISTINCT sample_unique_id, hugo_gene_symbol
    FROM genomic_event_derived
    WHERE cancer_study_identifier = {study:String}
      AND variant_type = 'mutation'
      AND mutation_status != 'UNCALLED'
      AND off_panel = 0
),
sample_groups AS (
    SELECT sample_unique_id,
           sample_unique_id IN (
               SELECT sample_unique_id FROM mutated WHERE hugo_gene_symbol = {gene:String}
           ) AS is_mutant
    FROM profiled_for_gene
),
wes_group_sizes AS (
    SELECT countIf(is_mutant) AS mutant_n, countIf(NOT is_mutant) AS wildtype_n
    FROM sample_groups
    WHERE sample_unique_id IN (SELECT sample_unique_id FROM wes_samples)
),
panel_group_sizes AS (
    SELECT p.hugo_gene_symbol,
           countIf(g.is_mutant) AS mutant_n,
           countIf(NOT g.is_mutant) AS wildtype_n
    FROM panel_samples_per_gene p
    JOIN sample_groups g USING (sample_unique_id)
    GROUP BY p.hugo_gene_symbol
),
altered_per_gene AS (
    SELECT m.hugo_gene_symbol,
           countIf(g.is_mutant) AS mutant_altered,
           countIf(NOT g.is_mutant) AS wildtype_altered
    FROM mutated m
    JOIN sample_groups g USING (sample_unique_id)
    WHERE m.hugo_gene_symbol != {gene:String}
    GROUP BY m.hugo_gene_symbol
),
per_gene AS (
    SELECT a.hugo_gene_symbol,
           a.mutant_altered,
           (SELECT mutant_n FROM wes_group_sizes) + COALESCE(p.mutant_n, 0) AS mutant_profiled,
           a.wildtype_altered,
           (SELECT wildtype_n FROM wes_group_sizes) + COALESCE(p.wildtype_n, 0) AS wildtype_profiled
    FROM altered_per_gene a
    LEFT JOIN panel_group_sizes p USING (hugo_gene_symbol)
    WHERE a.mutant_altered + a.wildtype_altered >= 10
)
SELECT hugo_gene_symbol,
       mutant_altered,
       mutant_profiled,
       ROUND(mutant_altered * 100.0 / NULLIF(mutant_profiled, 0), 1) AS mutant_pct,
       wildtype_altered,
       wildtype_profiled,
       ROUND(wildtype_altered * 100.0 / NULLIF(wildtype_profiled, 0), 1) AS wildtype_pct,
       ROUND(mutant_pct - wildtype_pct, 1) AS pct_difference
FROM per_gene
ORDER BY abs(pct_difference) DESC, hugo_gene_symbol ASC
LIMIT {top_n:UInt32};

-- ============================================================================
-- cna_panel_gene_coverage + cna_wes_coverage
-- ============================================================================
-- Copy-number counterparts of mutation_panel_gene_coverage /
-- mutation_wes_coverage: "is this sample profiled for discrete CNA in gene
-- G?". Only DISCRETE profiles (GISTIC / `_cna`) count — the continuous
-- log2 profiles share alteration_type 'COPY_NUMBER_ALTERATION' but are
-- not what AMP / HOMDEL calls come from, and several studies have log2
-- data for samples without discrete calls. Non-panel (genome-wide) CNA
-- profiles carry gene_panel_id = 'WES', like whole-exome mutation data.
-- ============================================================================

DROP VIEW IF EXISTS cna_panel_gene_coverage;

CREATE VIEW cna_panel_gene_coverage AS
SELECT
    stgp.sample_unique_id,
    stgp.cancer_study_identifier,
    g.hugo_gene_symbol,
    stgp.gene_panel_id
FROM sample_to_gene_panel_derived stgp
JOIN genetic_profile gprof ON stgp.genetic_profile_id = gprof.stable_id
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'COPY_NUMBER_ALTERATION'
  AND gprof.datatype = 'DISCRETE';

DROP VIEW IF EXISTS cna_wes_coverage;

CREATE VIEW cna_wes_coverage AS
SELECT
    stgp.sample_unique_id,
    stgp.cancer_study_identifier
FROM sample_to_gene_panel_derived stgp
JOIN genetic_profile gprof ON stgp.genetic_profile_id = gprof.stable_id
WHERE stgp.alteration_type = 'COPY_NUMBER_ALTERATION'
  AND gprof.datatype = 'DISCRETE'
  AND stgp.gene_panel_id = 'WES';

-- ============================================================================
-- top_cna_genes_in_study — the study view's "CNA Genes" table
-- ============================================================================
-- Genes ranked by samples with a high-level amplification (AMP, +2) or
-- homozygous deletion (HOMDEL, -2). One row per (gene, CNA type), like the
-- portal table (gene, cytoband, CNA, # samples, freq). Shallow gains /
-- losses (+1 / -1) are not in genomic_event_derived; for those use
-- gene_cna_distribution_in_study.
--
-- Parameters:
--   study  — cancer_study_identifier
--   top_n  — UInt32, max number of rows to return
--
-- Usage:
--   SELECT *
--   FROM top_cna_genes_in_study(
--       study='gbm_tcga_pan_can_atlas_2018',
--       top_n=20
--   );
--
-- Returns (hugo_gene_symbol, cytoband, cna_type ('AMP' | 'HOMDEL'),
-- altered_samples, profiled_samples, frequency_pct). profiled_samples =
-- samples profiled for discrete CNA in that gene (cna_wes_coverage +
-- cna_panel_gene_coverage). Sorted by altered_samples DESC, then gene.
-- ============================================================================

DROP VIEW IF EXISTS top_cna_genes_in_study;

CREATE VIEW top_cna_genes_in_study AS
WITH wes_profiled_count AS (
    SELECT COUNT(DISTINCT sample_unique_id) AS n
    FROM cna_wes_coverage
    WHERE cancer_study_identifier = {study:String}
),
panel_profiled_per_gene AS (
    SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS n
    FROM cna_panel_gene_coverage
    WHERE cancer_study_identifier = {study:String}
    GROUP BY hugo_gene_symbol
),
altered_per_gene AS (
    SELECT hugo_gene_symbol,
           cna_alteration,
           any(cna_cytoband) AS cytoband,
           COUNT(DISTINCT sample_unique_id) AS altered_samples
    FROM genomic_event_derived
    WHERE cancer_study_identifier = {study:String}
      AND variant_type = 'cna'
      AND cna_alteration IN (2, -2)
      AND off_panel = 0
    GROUP BY hugo_gene_symbol, cna_alteration
)
SELECT a.hugo_gene_symbol,
       a.cytoband,
       if(a.cna_alteration = 2, 'AMP', 'HOMDEL') AS cna_type,
       a.altered_samples,
       (SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0) AS profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF((SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0), 0), 1) AS frequency_pct
FROM altered_per_gene a
LEFT JOIN panel_profiled_per_gene p USING (hugo_gene_symbol)
ORDER BY altered_samples DESC, hugo_gene_symbol ASC
LIMIT {top_n:UInt32};

-- ============================================================================
-- gene_cna_distribution_in_study — all discrete CNA levels for one gene
-- ============================================================================
-- Mirrors the portal's per-gene CNA chart (getCNACounts): sample counts
-- for every discrete copy-number value, including shallow gains / losses
-- and diploid, which genomic_event_derived does not store. Reads the
-- study's DISCRETE COPY_NUMBER_ALTERATION profile(s) from
-- genetic_alteration_derived (profile_type 'gistic' or 'cna').
--
-- The NA row is computed like the portal: all samples in the study minus
-- samples with a value for the gene.
--
-- Parameters:
--   study  — cancer_study_identifier
--   gene   — HUGO gene symbol
--
-- Usage:
--   SELECT *
--   FROM gene_cna_distribution_in_study(
--       study='gbm_tcga_pan_can_atlas_2018',
--       gene='CDKN2A'
--   );
--
-- Returns (profile_type, cna_value, cna_label, samples, profiled_samples,
-- pct_of_profiled). cna_label is Amplified (2) / Gained (1) / Diploid (0)
-- / Heterozygously deleted (-1) / Homozygously deleted (-2) / NA.
-- profiled_samples = samples with a value; pct_of_profiled is NULL on the
-- NA row. Studies with two discrete profiles return one block per profile.
-- ============================================================================

DROP VIEW IF EXISTS gene_cna_distribution_in_study;

CREATE VIEW gene_cna_distribution_in_study AS
WITH discrete_profiles AS (
    SELECT substring(gprof.stable_id, length({study:String}) + 2) AS profile_type
    FROM genetic_profile gprof
    JOIN cancer_study cs ON gprof.cancer_study_id = cs.cancer_study_id
    WHERE cs.cancer_study_identifier = {study:String}
      AND gprof.genetic_alteration_type = 'COPY_NUMBER_ALTERATION'
      AND gprof.datatype = 'DISCRETE'
),
value_counts AS (
    SELECT profile_type,
           alteration_value AS cna_value,
           toInt64(count()) AS samples
    FROM genetic_alteration_derived
    WHERE cancer_study_identifier = {study:String}
      AND hugo_gene_symbol = {gene:String}
      AND profile_type IN (SELECT profile_type FROM discrete_profiles)
      AND alteration_value NOT IN ('', 'NA')
    GROUP BY profile_type, alteration_value
),
profiled AS (
    SELECT profile_type, sum(samples) AS profiled_samples
    FROM value_counts
    GROUP BY profile_type
),
study_sample_count AS (
    SELECT toInt64(count()) AS n
    FROM sample_derived
    WHERE cancer_study_identifier = {study:String}
),
all_rows AS (
    SELECT profile_type, cna_value, samples FROM value_counts
    UNION ALL
    SELECT profile_type, 'NA' AS cna_value,
           (SELECT n FROM study_sample_count) - profiled_samples AS samples
    FROM profiled
)
SELECT r.profile_type,
       r.cna_value,
       multiIf(r.cna_value = '2', 'Amplified',
               r.cna_value = '1', 'Gained',
               r.cna_value = '0', 'Diploid',
               r.cna_value = '-1', 'Heterozygously deleted',
               r.cna_value = '-2', 'Homozygously deleted',
               r.cna_value = 'NA', 'NA',
               'Other') AS cna_label,
       r.samples,
       p.profiled_samples,
       if(r.cna_value = 'NA', NULL,
          ROUND(r.samples * 100.0 / NULLIF(p.profiled_samples, 0), 1)) AS pct_of_profiled
FROM all_rows r
JOIN profiled p USING (profile_type)
ORDER BY r.profile_type, r.cna_value = 'NA', toFloat64OrNull(r.cna_value) DESC;

-- ============================================================================
-- sv_panel_gene_coverage + sv_wes_coverage
-- ============================================================================
-- Structural-variant counterparts of the mutation coverage views: samples
-- profiled for SVs in gene G via a named panel, or via a non-panel SV
-- profile (gene_panel_id = 'WES', all genes profiled).
-- ============================================================================

DROP VIEW IF EXISTS sv_panel_gene_coverage;

CREATE VIEW sv_panel_gene_coverage AS
SELECT
    stgp.sample_unique_id,
    stgp.cancer_study_identifier,
    g.hugo_gene_symbol,
    stgp.gene_panel_id
FROM sample_to_gene_panel_derived stgp
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'STRUCTURAL_VARIANT';

DROP VIEW IF EXISTS sv_wes_coverage;

CREATE VIEW sv_wes_coverage AS
SELECT
    sample_unique_id,
    cancer_study_identifier
FROM sample_to_gene_panel_derived
WHERE alteration_type = 'STRUCTURAL_VARIANT'
  AND gene_panel_id = 'WES';

-- ============================================================================
-- top_sv_genes_in_study — the study view's "Structural Variant Genes" table
-- ============================================================================
-- Genes ranked by samples with a structural variant / fusion involving
-- them. genomic_event_derived stores one row per partner gene, so a
-- TMPRSS2-ERG fusion counts for both TMPRSS2 and ERG (same as the portal).
--
-- Parameters:
--   study  — cancer_study_identifier
--   top_n  — UInt32, max number of genes to return
--
-- Usage:
--   SELECT *
--   FROM top_sv_genes_in_study(
--       study='prad_tcga_pan_can_atlas_2018',
--       top_n=20
--   );
--
-- Returns (hugo_gene_symbol, altered_samples, profiled_samples,
-- frequency_pct, total_sv_events). profiled_samples = samples profiled
-- for SVs in that gene (sv_wes_coverage + sv_panel_gene_coverage).
-- Sorted by altered_samples DESC, then gene.
-- ============================================================================

DROP VIEW IF EXISTS top_sv_genes_in_study;

CREATE VIEW top_sv_genes_in_study AS
WITH wes_profiled_count AS (
    SELECT COUNT(DISTINCT sample_unique_id) AS n
    FROM sv_wes_coverage
    WHERE cancer_study_identifier = {study:String}
),
panel_profiled_per_gene AS (
    SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS n
    FROM sv_panel_gene_coverage
    WHERE cancer_study_identifier = {study:String}
    GROUP BY hugo_gene_symbol
),
altered_per_gene AS (
    SELECT hugo_gene_symbol,
           COUNT(DISTINCT sample_unique_id) AS altered_samples,
           COUNT(*) AS total_sv_events
    FROM genomic_event_derived
    WHERE cancer_study_identifier = {study:String}
      AND variant_type = 'structural_variant'
      AND mutation_status != 'UNCALLED'
      AND off_panel = 0
    GROUP BY hugo_gene_symbol
)
SELECT a.hugo_gene_symbol,
       a.altered_samples,
       (SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0) AS profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF((SELECT n FROM wes_profiled_count) + COALESCE(p.n, 0), 0), 1) AS frequency_pct,
       a.total_sv_events
FROM altered_per_gene a
LEFT JOIN panel_profiled_per_gene p USING (hugo_gene_symbol)
ORDER BY altered_samples DESC, hugo_gene_symbol ASC
LIMIT {top_n:UInt32};
