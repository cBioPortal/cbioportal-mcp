-- ============================================================================
-- Precomputed alteration-frequency aggregates (backing tables for the
-- get_alteration_frequency / get_top_altered_genes /
-- get_gene_frequency_by_cancer_type / get_profiled_counts MCP tools)
-- ============================================================================
-- The recipe views in sql/4-mutation-frequency-views.sql are correct but
-- scan genomic_event_derived + sample_to_gene_panel_derived on every call,
-- and the agent usually needs several exploratory queries before it finds
-- them. These tables hold the SAME numerators and denominators, computed
-- once per database build, so one tool call answers the common templates:
--
--   study_gene_alteration_counts        per study x gene x alteration type
--   cancer_type_gene_alteration_counts  per preference x CANCER_TYPE x gene
--                                       x alteration type
--   study_profiled_counts               per study x profile type
--                                       (profiled samples / patients)
--
-- Semantics are copied from the recipe views, not reinvented:
--   numerator    COUNT(DISTINCT sample_unique_id) over genomic_event_derived
--                with off_panel = 0 and
--                  mutation            variant_type = 'mutation'
--                                      AND mutation_status != 'UNCALLED'
--                  amplification       variant_type = 'cna' AND cna_alteration = 2
--                  deep_deletion       variant_type = 'cna' AND cna_alteration = -2
--                  structural_variant  variant_type = 'structural_variant'
--                                      (per-study table: AND mutation_status
--                                      != 'UNCALLED', as top_sv_genes_in_study)
--                  any                 a sample matching ANY of the four above
--   denominator  distinct samples profiled for the gene for the matching
--                sample_to_gene_panel_derived.alteration_type
--                (MUTATION_EXTENDED / COPY_NUMBER_ALTERATION /
--                STRUCTURAL_VARIANT): on a named panel whose
--                gene_panel_list includes the gene, OR gene_panel_id = 'WES'.
--                Per-study table: CNA rows only from DISCRETE profiles, as
--                cna_*_coverage / top_cna_genes_in_study. Per-cancer-type
--                table: every COPY_NUMBER_ALTERATION profile, exactly as
--                gene_alteration_frequency_by_cancer_type does today.
--                For 'any', a sample counts if it is profiled for the gene
--                under at least one of the three alteration types.
--
-- Each table reproduces the recipe that answers the same question; where
-- the upstream recipes disagree (DISCRETE-only CNA and UNCALLED SVs above),
-- the tables inherit the disagreement rather than silently picking one.
--
-- How the denominator stays exact without a samples x genes explosion
-- ------------------------------------------------------------------------
-- The recipes compute COUNT(DISTINCT sample) over (panel rows UNION ALL WES
-- rows). Materialising that for every gene would expand every WES sample
-- to ~20k gene rows. Instead each sample is put in exactly ONE bucket per
-- (scope, profile type):
--
--   * WES bucket    — the sample has a WES row for that profile type. It is
--                     profiled for every gene, so it adds 1 to every gene.
--   * panel-set     — otherwise, the sorted set of named panels the sample
--     signature       is on for that profile type (usually one panel; two
--                     when a sample has two mutation profiles on different
--                     panels). It adds 1 to every gene listed by ANY panel
--                     in the set.
--
-- The buckets partition the samples, so
--   profiled(gene) = |WES bucket| + SUM(|signature| for signatures listing gene)
-- is the same number as the recipe's COUNT(DISTINCT ...) of the union — a
-- sum over disjoint sets, NOT a sum of overlapping per-block distinct
-- counts. (top_mutated_genes_in_cohort / top_*_genes_in_study add WES +
-- panel counts directly, which double-counts a sample that is WES for one
-- profile and on a panel for another of the same type; the
-- gene_*_frequency_* and gene_mutation_variants_in_study views count the
-- union and cannot exceed 100%. These tables use the union. Where no sample
-- is in both, which is the usual case, the numbers are identical.)
--
-- Only rows with altered_samples > 0 are stored, exactly like the recipes'
-- INNER JOIN of altered to profiled. A gene with no row for a study is
-- either unaltered there or not a valid symbol; the MCP tools fall back to
-- the live recipe SQL for that case so they can report 0 / N and tell the
-- two apart. The >= 50 profiled-samples threshold of the by-cancer-type
-- recipe is NOT applied here; the tool applies it at query time
-- (min_profiled, default 50) so the stored counts stay threshold-free.
--
-- Refresh / rebuild coupling
-- ------------------------------------------------------------------------
-- These are plain MergeTree tables rebuilt by this file (DROP + CREATE +
-- INSERT ... SELECT), NOT refreshable materialized views, because:
--   1. The source tables are immutable between builds. The LLM database is
--      a daily blue/green CLONE of production (cbioagent-clickhouse-clone-
--      daily), and production's *_derived tables are themselves rebuilt by
--      cbioportal's db-scripts/clickhouse/clickhouse.sql after each import.
--      A timer-driven refresh would re-scan identical data all day.
--   2. The clone job applies sql/*.sql BEFORE it flips the MCP to the new
--      buffer, so an INSERT ... SELECT here is fully populated before any
--      reader can see it. A refreshable MV populates asynchronously after
--      CREATE, so the first reads after the flip could hit an empty table.
--   3. Refreshable MVs are only GA from ClickHouse 24.10 (experimental
--      before, behind allow_experimental_refreshable_materialized_view),
--      while this file must also run on the 24.x self-hosted servers.
-- Consequences:
--   - Any rebuild of the *_derived tables (re-running cbioportal's
--     clickhouse.sql, or a fresh clone) MUST be followed by re-applying this
--     file. The daily clone job already does that (step 6 of clone.sh runs
--     every sql/*.sql after the CLONE). For a manual rebuild, run
--     scripts/apply_sql.sh.
--   - Between the DROP and the end of the INSERT a manual re-apply on a
--     buffer the MCP is actively reading leaves the table missing or
--     partially filled. The tools fall back to live SQL when a table is
--     missing or empty, but not when it is half-filled — prefer re-applying
--     on the inactive buffer.
--   - built_at records when each table was built; the tools report it.
--
-- Requires ClickHouse 23.1+ (same as the parameterized recipe views in
-- sql/4); tested on 24.8.
-- ============================================================================

-- ============================================================================
-- study_gene_alteration_counts
-- ============================================================================
-- Study-level counterpart of the single-study recipes
-- (top_mutated_genes_in_study, top_cna_genes_in_study, top_sv_genes_in_study,
-- gene_mutation_frequency_in_study) without the CANCER_TYPE split: every
-- sample of the study counts, as on the cBioPortal study view. It follows
-- the single-study recipes where they differ from the cross-study one:
--   - CNA denominators count only DISCRETE copy-number profiles
--     (cna_panel_gene_coverage / cna_wes_coverage). Continuous log2
--     profiles share alteration_type COPY_NUMBER_ALTERATION but carry no
--     AMP / HOMDEL calls.
--   - Structural variants with mutation_status (sv_status) = 'UNCALLED' are
--     excluded, as in top_sv_genes_in_study.
-- ============================================================================

DROP TABLE IF EXISTS study_gene_alteration_counts;

CREATE TABLE study_gene_alteration_counts
(
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    hugo_gene_symbol        String                 COMMENT 'HUGO gene symbol',
    alteration_type         LowCardinality(String) COMMENT 'mutation | amplification | deep_deletion | structural_variant | any',
    altered_samples         UInt64                 COMMENT 'Distinct samples with this alteration in this gene (off_panel = 0, UNCALLED mutations excluded)',
    profiled_samples        UInt64                 COMMENT 'Distinct samples profiled for this gene for the matching alteration_type (gene on the sample panel, or WES). Frequency denominator.',
    altered_events          UInt64                 COMMENT 'Number of qualifying event rows (e.g. total mutations, one sample can carry several)',
    built_at                DateTime               COMMENT 'When sql/8-precomputed-aggregates.sql built this table'
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, alteration_type, hugo_gene_symbol)
COMMENT 'Precomputed per-study gene alteration frequencies (numerator + gene-specific profiled denominator). Backs get_alteration_frequency / get_top_altered_genes. frequency = altered_samples / profiled_samples.';

INSERT INTO study_gene_alteration_counts
WITH
alteration_to_profile AS (
    SELECT t.1 AS alteration_type, t.2 AS profile_type
    FROM (
        SELECT arrayJoin([
            ('mutation',           'MUTATION_EXTENDED'),
            ('amplification',      'COPY_NUMBER_ALTERATION'),
            ('deep_deletion',      'COPY_NUMBER_ALTERATION'),
            ('structural_variant', 'STRUCTURAL_VARIANT'),
            ('any',                'ANY')
        ]) AS t
    )
),
events AS (
    SELECT cancer_study_identifier,
           hugo_gene_symbol,
           sample_unique_id,
           multiIf(
               variant_type = 'mutation' AND mutation_status != 'UNCALLED', 'mutation',
               variant_type = 'cna' AND cna_alteration = 2,                  'amplification',
               variant_type = 'cna' AND cna_alteration = -2,                 'deep_deletion',
               variant_type = 'structural_variant' AND mutation_status != 'UNCALLED',
                                                                             'structural_variant',
               '') AS event_type
    FROM genomic_event_derived
    WHERE off_panel = 0
),
altered AS (
    -- event_type, not alteration_type: in ClickHouse a SELECT alias shadows
    -- the source column in WHERE, so `'any' AS alteration_type` below would
    -- turn a WHERE on alteration_type into a constant.
    SELECT cancer_study_identifier, hugo_gene_symbol, event_type AS alteration_type,
           COUNT(DISTINCT sample_unique_id) AS altered_samples,
           COUNT(*) AS altered_events
    FROM events
    WHERE event_type != ''
    GROUP BY cancer_study_identifier, hugo_gene_symbol, event_type
    UNION ALL
    SELECT cancer_study_identifier, hugo_gene_symbol, 'any' AS alteration_type,
           COUNT(DISTINCT sample_unique_id) AS altered_samples,
           COUNT(*) AS altered_events
    FROM events
    WHERE event_type != ''
    GROUP BY cancer_study_identifier, hugo_gene_symbol
),
typed_profile_rows AS (
    -- Same profile filters as the {mutation,cna,sv}_*_coverage views:
    -- CNA rows only from DISCRETE profiles.
    SELECT cancer_study_identifier, alteration_type AS profile_type, sample_unique_id, gene_panel_id
    FROM sample_to_gene_panel_derived
    WHERE alteration_type IN ('MUTATION_EXTENDED', 'STRUCTURAL_VARIANT')
       OR (alteration_type = 'COPY_NUMBER_ALTERATION'
           AND genetic_profile_id IN (SELECT stable_id FROM genetic_profile WHERE datatype = 'DISCRETE'))
),
profile_rows AS (
    SELECT cancer_study_identifier, profile_type, sample_unique_id, gene_panel_id
    FROM typed_profile_rows
    UNION ALL
    SELECT cancer_study_identifier, 'ANY' AS profile_type, sample_unique_id, gene_panel_id
    FROM typed_profile_rows
),
sample_buckets AS (
    -- One row per sample per profile type: its panel-set signature.
    SELECT cancer_study_identifier, profile_type, sample_unique_id,
           arraySort(groupUniqArray(gene_panel_id)) AS panels
    FROM profile_rows
    GROUP BY cancer_study_identifier, profile_type, sample_unique_id
),
wes_profiled AS (
    SELECT cancer_study_identifier, profile_type, COUNT(*) AS n
    FROM sample_buckets
    WHERE has(panels, 'WES')
    GROUP BY cancer_study_identifier, profile_type
),
signature_counts AS (
    SELECT cancer_study_identifier, profile_type, panels, COUNT(*) AS n
    FROM sample_buckets
    WHERE NOT has(panels, 'WES')
    GROUP BY cancer_study_identifier, profile_type, panels
),
panel_genes AS (
    -- Same panel -> gene chain as mutation_panel_gene_coverage.
    SELECT gp.stable_id AS gene_panel_id, g.hugo_gene_symbol AS hugo_gene_symbol
    FROM gene_panel gp
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
),
panel_profiled AS (
    SELECT cancer_study_identifier, profile_type, hugo_gene_symbol, SUM(n) AS n
    FROM (
        -- DISTINCT: a gene listed by two panels of one signature counts once.
        SELECT DISTINCT s.cancer_study_identifier, s.profile_type, s.panels, s.n, pg.hugo_gene_symbol
        FROM (
            SELECT cancer_study_identifier, profile_type, panels, n,
                   arrayJoin(panels) AS gene_panel_id
            FROM signature_counts
        ) s
        JOIN panel_genes pg ON s.gene_panel_id = pg.gene_panel_id
    )
    GROUP BY cancer_study_identifier, profile_type, hugo_gene_symbol
)
SELECT a.cancer_study_identifier,
       a.hugo_gene_symbol,
       a.alteration_type,
       a.altered_samples,
       COALESCE(w.n, 0) + COALESCE(p.n, 0) AS profiled_samples,
       a.altered_events,
       now() AS built_at
FROM altered a
JOIN alteration_to_profile m ON a.alteration_type = m.alteration_type
LEFT JOIN wes_profiled w
    ON w.cancer_study_identifier = a.cancer_study_identifier
   AND w.profile_type = m.profile_type
LEFT JOIN panel_profiled p
    ON p.cancer_study_identifier = a.cancer_study_identifier
   AND p.profile_type = m.profile_type
   AND p.hugo_gene_symbol = a.hugo_gene_symbol;

-- ============================================================================
-- cancer_type_gene_alteration_counts
-- ============================================================================
-- Precomputed gene_alteration_frequency_by_cancer_type(preference, gene,
-- alteration) for every preference in cancer_study_query_preferences, every
-- gene and every alteration type, plus 'any'. cancer_type is the sample's
-- CANCER_TYPE clinical attribute, as in the recipe; samples without one are
-- excluded from numerator and denominator alike. No >= 50 threshold here —
-- see header.
-- ============================================================================

DROP TABLE IF EXISTS cancer_type_gene_alteration_counts;

CREATE TABLE cancer_type_gene_alteration_counts
(
    preference_name  LowCardinality(String) COMMENT 'cancer_study_query_preferences.preference_name (the cohort), e.g. pan_cancer_tcga',
    cancer_type      String                 COMMENT 'Per-sample CANCER_TYPE clinical attribute value',
    hugo_gene_symbol String                 COMMENT 'HUGO gene symbol',
    alteration_type  LowCardinality(String) COMMENT 'mutation | amplification | deep_deletion | structural_variant | any',
    altered_samples  UInt64                 COMMENT 'Distinct cohort samples of this cancer type with this alteration in this gene',
    profiled_samples UInt64                 COMMENT 'Distinct cohort samples of this cancer type profiled for this gene (panel lists gene, or WES). Frequency denominator.',
    built_at         DateTime               COMMENT 'When sql/8-precomputed-aggregates.sql built this table'
)
ENGINE = MergeTree
ORDER BY (preference_name, hugo_gene_symbol, alteration_type, cancer_type)
COMMENT 'Precomputed gene_alteration_frequency_by_cancer_type for every preference x gene x alteration type (no min-profiled threshold applied). Backs get_gene_frequency_by_cancer_type.';

INSERT INTO cancer_type_gene_alteration_counts
WITH
alteration_to_profile AS (
    SELECT t.1 AS alteration_type, t.2 AS profile_type
    FROM (
        SELECT arrayJoin([
            ('mutation',           'MUTATION_EXTENDED'),
            ('amplification',      'COPY_NUMBER_ALTERATION'),
            ('deep_deletion',      'COPY_NUMBER_ALTERATION'),
            ('structural_variant', 'STRUCTURAL_VARIANT'),
            ('any',                'ANY')
        ]) AS t
    )
),
sample_cancer_type AS (
    SELECT q.preference_name AS preference_name,
           cd.cancer_study_identifier AS cancer_study_identifier,
           cd.sample_unique_id AS sample_unique_id,
           cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    JOIN cancer_study_query_preferences q ON cd.cancer_study_identifier = q.cancer_study_identifier
    WHERE cd.attribute_name = 'CANCER_TYPE'
),
events AS (
    SELECT cancer_study_identifier,
           hugo_gene_symbol,
           sample_unique_id,
           multiIf(
               variant_type = 'mutation' AND mutation_status != 'UNCALLED', 'mutation',
               variant_type = 'cna' AND cna_alteration = 2,                  'amplification',
               variant_type = 'cna' AND cna_alteration = -2,                 'deep_deletion',
               variant_type = 'structural_variant',                          'structural_variant',
               '') AS event_type
    FROM genomic_event_derived
    WHERE off_panel = 0
      AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM cancer_study_query_preferences)
),
typed_events AS (
    SELECT sct.preference_name AS preference_name, sct.cancer_type AS cancer_type,
           e.hugo_gene_symbol AS hugo_gene_symbol, e.event_type AS event_type,
           e.sample_unique_id AS sample_unique_id
    FROM events e
    JOIN sample_cancer_type sct
        ON e.cancer_study_identifier = sct.cancer_study_identifier
       AND e.sample_unique_id = sct.sample_unique_id
    WHERE e.event_type != ''
),
altered AS (
    SELECT preference_name, cancer_type, hugo_gene_symbol, event_type AS alteration_type,
           COUNT(DISTINCT sample_unique_id) AS altered_samples
    FROM typed_events
    GROUP BY preference_name, cancer_type, hugo_gene_symbol, event_type
    UNION ALL
    SELECT preference_name, cancer_type, hugo_gene_symbol, 'any' AS alteration_type,
           COUNT(DISTINCT sample_unique_id) AS altered_samples
    FROM typed_events
    GROUP BY preference_name, cancer_type, hugo_gene_symbol
),
profile_rows AS (
    SELECT cancer_study_identifier, alteration_type AS profile_type, sample_unique_id, gene_panel_id
    FROM sample_to_gene_panel_derived
    WHERE alteration_type IN ('MUTATION_EXTENDED', 'COPY_NUMBER_ALTERATION', 'STRUCTURAL_VARIANT')
    UNION ALL
    SELECT cancer_study_identifier, 'ANY' AS profile_type, sample_unique_id, gene_panel_id
    FROM sample_to_gene_panel_derived
    WHERE alteration_type IN ('MUTATION_EXTENDED', 'COPY_NUMBER_ALTERATION', 'STRUCTURAL_VARIANT')
),
sample_buckets AS (
    SELECT sct.preference_name AS preference_name, sct.cancer_type AS cancer_type,
           pr.profile_type AS profile_type, pr.sample_unique_id AS sample_unique_id,
           arraySort(groupUniqArray(pr.gene_panel_id)) AS panels
    FROM profile_rows pr
    JOIN sample_cancer_type sct
        ON pr.cancer_study_identifier = sct.cancer_study_identifier
       AND pr.sample_unique_id = sct.sample_unique_id
    GROUP BY preference_name, cancer_type, profile_type, sample_unique_id
),
wes_profiled AS (
    SELECT preference_name, cancer_type, profile_type, COUNT(*) AS n
    FROM sample_buckets
    WHERE has(panels, 'WES')
    GROUP BY preference_name, cancer_type, profile_type
),
signature_counts AS (
    SELECT preference_name, cancer_type, profile_type, panels, COUNT(*) AS n
    FROM sample_buckets
    WHERE NOT has(panels, 'WES')
    GROUP BY preference_name, cancer_type, profile_type, panels
),
panel_genes AS (
    SELECT gp.stable_id AS gene_panel_id, g.hugo_gene_symbol AS hugo_gene_symbol
    FROM gene_panel gp
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
),
panel_profiled AS (
    SELECT preference_name, cancer_type, profile_type, hugo_gene_symbol, SUM(n) AS n
    FROM (
        SELECT DISTINCT s.preference_name, s.cancer_type, s.profile_type, s.panels, s.n,
                        pg.hugo_gene_symbol
        FROM (
            SELECT preference_name, cancer_type, profile_type, panels, n,
                   arrayJoin(panels) AS gene_panel_id
            FROM signature_counts
        ) s
        JOIN panel_genes pg ON s.gene_panel_id = pg.gene_panel_id
    )
    GROUP BY preference_name, cancer_type, profile_type, hugo_gene_symbol
)
SELECT a.preference_name,
       a.cancer_type,
       a.hugo_gene_symbol,
       a.alteration_type,
       a.altered_samples,
       COALESCE(w.n, 0) + COALESCE(p.n, 0) AS profiled_samples,
       now() AS built_at
FROM altered a
JOIN alteration_to_profile m ON a.alteration_type = m.alteration_type
LEFT JOIN wes_profiled w
    ON w.preference_name = a.preference_name
   AND w.cancer_type = a.cancer_type
   AND w.profile_type = m.profile_type
LEFT JOIN panel_profiled p
    ON p.preference_name = a.preference_name
   AND p.cancer_type = a.cancer_type
   AND p.profile_type = m.profile_type
   AND p.hugo_gene_symbol = a.hugo_gene_symbol;

-- ============================================================================
-- study_profiled_counts
-- ============================================================================
-- "How many samples / patients were profiled for X in study Y". One row per
-- study per profile_type:
--   ALL_SAMPLES       every sample in sample_derived (no profiling filter)
--   ANY_MUT_CNA_SV    profiled for mutation, DISCRETE CNA or SV (the 'any'
--                     scope of study_gene_alteration_counts)
--   COPY_NUMBER_ALTERATION_DISCRETE
--                     profiled by a DISCRETE CNA profile — the CNA
--                     denominator scope (cna_wes_coverage /
--                     cna_panel_gene_coverage)
--   <alteration_type> every sample_to_gene_panel_derived.alteration_type
--                     present, as stored, e.g. MUTATION_EXTENDED,
--                     COPY_NUMBER_ALTERATION (discrete AND continuous
--                     log2 profiles), STRUCTURAL_VARIANT, MRNA_EXPRESSION
-- These count the samples that have a profile, which can differ from the
-- portal's case-list counts in cancer_study.*_sample_count
-- (sql/6-add-study-data-type-counts.sql) when a case list and
-- sample_profile disagree.
-- These are study-wide counts; the per-gene denominator (gene may be absent
-- from some panels) is profiled_samples in study_gene_alteration_counts.
-- ============================================================================

DROP TABLE IF EXISTS study_profiled_counts;

CREATE TABLE study_profiled_counts
(
    cancer_study_identifier LowCardinality(String) COMMENT 'cancer_study.cancer_study_identifier',
    profile_type            LowCardinality(String) COMMENT 'ALL_SAMPLES | ANY_MUT_CNA_SV (mutation, discrete CNA or SV) | COPY_NUMBER_ALTERATION_DISCRETE | a sample_to_gene_panel_derived.alteration_type as stored (MUTATION_EXTENDED, COPY_NUMBER_ALTERATION incl. log2, STRUCTURAL_VARIANT, ...)',
    samples                 UInt64                 COMMENT 'Distinct samples (profiled for profile_type, or all samples for ALL_SAMPLES)',
    patients                UInt64                 COMMENT 'Distinct patients of those samples',
    wes_samples             UInt64                 COMMENT 'Of those samples, how many are profiled by WES (all genes) rather than a named panel. Always 0 for ALL_SAMPLES.',
    built_at                DateTime               COMMENT 'When sql/8-precomputed-aggregates.sql built this table'
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, profile_type)
COMMENT 'Precomputed per-study sample / patient counts, overall and per profiled alteration type. Backs get_profiled_counts.';

INSERT INTO study_profiled_counts
WITH
sample_patient AS (
    SELECT sample_unique_id, patient_unique_id
    FROM sample_derived
),
discrete_cna_profiles AS (
    SELECT stable_id FROM genetic_profile WHERE datatype = 'DISCRETE'
),
profiled AS (
    SELECT cancer_study_identifier, alteration_type AS profile_type, sample_unique_id, gene_panel_id
    FROM sample_to_gene_panel_derived
    UNION ALL
    SELECT cancer_study_identifier, 'COPY_NUMBER_ALTERATION_DISCRETE' AS profile_type,
           sample_unique_id, gene_panel_id
    FROM sample_to_gene_panel_derived
    WHERE alteration_type = 'COPY_NUMBER_ALTERATION'
      AND genetic_profile_id IN (SELECT stable_id FROM discrete_cna_profiles)
    UNION ALL
    SELECT cancer_study_identifier, 'ANY_MUT_CNA_SV' AS profile_type, sample_unique_id, gene_panel_id
    FROM sample_to_gene_panel_derived
    WHERE alteration_type IN ('MUTATION_EXTENDED', 'STRUCTURAL_VARIANT')
       OR (alteration_type = 'COPY_NUMBER_ALTERATION'
           AND genetic_profile_id IN (SELECT stable_id FROM discrete_cna_profiles))
)
SELECT cancer_study_identifier,
       'ALL_SAMPLES' AS profile_type,
       COUNT(DISTINCT sample_unique_id) AS samples,
       COUNT(DISTINCT patient_unique_id) AS patients,
       toUInt64(0) AS wes_samples,
       now() AS built_at
FROM sample_derived
GROUP BY cancer_study_identifier
UNION ALL
SELECT p.cancer_study_identifier,
       p.profile_type,
       COUNT(DISTINCT p.sample_unique_id) AS samples,
       COUNT(DISTINCT nullIf(sp.patient_unique_id, '')) AS patients,
       COUNT(DISTINCT if(p.gene_panel_id = 'WES', p.sample_unique_id, NULL)) AS wes_samples,
       now() AS built_at
FROM profiled p
LEFT JOIN sample_patient sp ON p.sample_unique_id = sp.sample_unique_id
GROUP BY p.cancer_study_identifier, p.profile_type;
