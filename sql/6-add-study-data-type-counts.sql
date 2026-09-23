-- ============================================================================
-- Per-study sample counts by data type, on cancer_study
-- ============================================================================
-- Adds the counts cBioPortal serves in its DETAILED study projection
-- (/api/studies?projection=DETAILED) as columns on cancer_study, so "which
-- studies have mutation and CNA data?" or "how many samples does X have?" is
-- a plain filter on one table, and the numbers match the portal's study list
-- and its "Data type" filter.
--
-- The counts use the same rules as cBioPortal's own ClickHouse mapper
-- (cbioportal: src/main/resources/mappers/clickhouse/cancerstudy/
-- CancerStudyMapper.xml): molecular counts are the sizes of each study's
-- standard sample lists (<study>_sequenced, <study>_cna, ...), treatment is
-- distinct patients with Treatment clinical events, structural variants are
-- distinct samples with an SV, and imaging/other resources are counted per
-- resource_definition.display_name (getResourceCountsForAllStudies).
--
-- The table is rebuilt rather than updated in place: ALTER TABLE ... UPDATE
-- runs as a background mutation without a default database, so it can't
-- reference a helper table (joinGet/dictGet) without hard-coding the database
-- name, which the clone job switches between blue and green. Rebuilding with
-- CREATE TABLE ... AS keeps cancer_study's engine and column comments, and
-- EXCHANGE TABLES swaps it in atomically.
-- ============================================================================

-- Re-runnable: drop count columns left from an earlier run
ALTER TABLE cancer_study DROP COLUMN IF EXISTS sample_count, DROP COLUMN IF EXISTS mutation_sample_count, DROP COLUMN IF EXISTS cna_sample_count, DROP COLUMN IF EXISTS structural_variant_sample_count, DROP COLUMN IF EXISTS rna_seq_sample_count, DROP COLUMN IF EXISTS mrna_microarray_sample_count, DROP COLUMN IF EXISTS mirna_sample_count, DROP COLUMN IF EXISTS rppa_sample_count, DROP COLUMN IF EXISTS mass_spectrometry_sample_count, DROP COLUMN IF EXISTS treatment_patient_count, DROP COLUMN IF EXISTS resource_sample_counts;

DROP TABLE IF EXISTS cancer_study_with_counts;
CREATE TABLE cancer_study_with_counts AS cancer_study;

-- New columns (the quoted names are the labels of the portal's "Data type" filter)
ALTER TABLE cancer_study_with_counts
    ADD COLUMN sample_count UInt32 COMMENT 'Samples in the study (members of <study>_all), as shown in the portal study list. Precomputed daily at LLM-prep time.',
    ADD COLUMN mutation_sample_count UInt32 COMMENT 'Samples profiled for mutations (<study>_sequenced) — portal "Data type" filter: "Mutations". 0 = no mutation data.',
    ADD COLUMN cna_sample_count UInt32 COMMENT 'Samples profiled for copy-number alterations (<study>_cna) — "CNA". 0 = no CNA data.',
    ADD COLUMN structural_variant_sample_count UInt32 COMMENT 'Distinct samples with at least one structural variant (fusions etc.). 0 = none.',
    ADD COLUMN rna_seq_sample_count UInt32 COMMENT 'Samples with RNA-Seq expression (<study>_rna_seq_v2_mrna) — "RNA-Seq".',
    ADD COLUMN mrna_microarray_sample_count UInt32 COMMENT 'Samples with microarray mRNA expression (<study>_mrna) — "RNA (microarray)".',
    ADD COLUMN mirna_sample_count UInt32 COMMENT 'Samples with microRNA expression (<study>_microrna) — "miRNA".',
    ADD COLUMN rppa_sample_count UInt32 COMMENT 'Samples with RPPA protein levels (<study>_rppa) — "RPPA".',
    ADD COLUMN mass_spectrometry_sample_count UInt32 COMMENT 'Samples with mass-spectrometry protein quantification (<study>_protein_quantification) — "Protein Mass-Spectrometry".',
    ADD COLUMN treatment_patient_count UInt32 COMMENT 'PATIENTS (not samples) with treatment clinical events — "Treatment". 0 = no treatment data.',
    ADD COLUMN resource_sample_counts Map(String, UInt32) COMMENT 'Samples with each linked resource, keyed by display name: imaging and pathology such as ''Slide Microscopy'', ''Computed Tomography'', ''Magnetic Resonance'', ''H&E Slide'', ''MxIF Image''. Query with mapKeys(resource_sample_counts) or resource_sample_counts[''Slide Microscopy''] > 0.';

INSERT INTO cancer_study_with_counts
WITH
    lists AS (
        SELECT
            sl.cancer_study_id AS cancer_study_id,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_all')) AS sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_sequenced')) AS mutation_sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_cna')) AS cna_sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_rna_seq_v2_mrna')) AS rna_seq_sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_mrna')) AS mrna_microarray_sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_microrna')) AS mirna_sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_rppa')) AS rppa_sample_count,
            countIf(sl.stable_id = concat(cs.cancer_study_identifier, '_protein_quantification')) AS mass_spectrometry_sample_count
        FROM sample_list_list AS sll
        INNER JOIN sample_list AS sl ON sll.list_id = sl.list_id
        INNER JOIN cancer_study AS cs ON sl.cancer_study_id = cs.cancer_study_id
        GROUP BY sl.cancer_study_id
    ),
    treatment AS (
        SELECT cancer_study_identifier, count(DISTINCT patient_unique_id) AS n
        FROM clinical_event_data_derived
        WHERE event_type IN ('Treatment', 'TREATMENT')
        GROUP BY cancer_study_identifier
    ),
    sv AS (
        SELECT cancer_study_identifier, count(DISTINCT sample_unique_id) AS n
        FROM genomic_event_derived
        WHERE variant_type = 'structural_variant'
        GROUP BY cancer_study_identifier
    ),
    resource_rows AS (
        SELECT p.cancer_study_id AS cancer_study_id, rd.display_name AS display_name, count(DISTINCT rs.internal_id) AS n
        FROM resource_sample AS rs
        INNER JOIN resource_definition AS rd ON rd.resource_id = rs.resource_id
        INNER JOIN sample AS s ON s.internal_id = rs.internal_id
        INNER JOIN patient AS p ON p.internal_id = s.patient_id
        GROUP BY cancer_study_id, display_name
        UNION ALL
        SELECT p.cancer_study_id, rd.display_name, count(DISTINCT s.internal_id)
        FROM patient AS p
        INNER JOIN sample AS s ON p.internal_id = s.patient_id
        INNER JOIN resource_patient AS rp ON rp.internal_id = s.patient_id
        INNER JOIN resource_definition AS rd ON rd.resource_id = rp.resource_id
        GROUP BY p.cancer_study_id, rd.display_name
    ),
    resources AS (
        SELECT cancer_study_id, CAST(mapFromArrays(groupArray(display_name), groupArray(n)), 'Map(String, UInt32)') AS counts
        FROM (
            SELECT cancer_study_id, display_name, toUInt32(sum(n)) AS n
            FROM resource_rows
            WHERE upper(display_name) NOT IN ('CHROMOSCOPE', 'FIGURES')
            GROUP BY cancer_study_id, display_name
        )
        GROUP BY cancer_study_id
    )
SELECT
    cs.*,
    lists.sample_count,
    lists.mutation_sample_count,
    lists.cna_sample_count,
    ifNull(sv.n, 0),
    lists.rna_seq_sample_count,
    lists.mrna_microarray_sample_count,
    lists.mirna_sample_count,
    lists.rppa_sample_count,
    lists.mass_spectrometry_sample_count,
    ifNull(treatment.n, 0),
    resources.counts
FROM cancer_study AS cs
LEFT JOIN lists ON lists.cancer_study_id = cs.cancer_study_id
LEFT JOIN treatment ON treatment.cancer_study_identifier = cs.cancer_study_identifier
LEFT JOIN sv ON sv.cancer_study_identifier = cs.cancer_study_identifier
LEFT JOIN resources ON resources.cancer_study_id = cs.cancer_study_id
SETTINGS join_use_nulls = 0;

EXCHANGE TABLES cancer_study AND cancer_study_with_counts;
DROP TABLE cancer_study_with_counts;
