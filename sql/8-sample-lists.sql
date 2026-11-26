-- ============================================================================
-- sample_list_members — the portal's case sets, one row per sample
-- ============================================================================
-- Built at clone time from sample_list / sample_list_list / sample / patient,
-- which the clone does not leave readable. A study's case sets are named
-- <study>_all, <study>_sequenced, <study>_cnaseq, <study>_complete, ... and
-- are the denominators the portal uses for mutation + CNA queries.
--
-- Columns: cancer_study_identifier, list_stable_id, list_name, category,
--          sample_unique_id (joins to *_derived tables)
--
-- Usage:
--   SELECT count() FROM sample_list_members
--   WHERE list_stable_id = 'gbm_tcga_pan_can_atlas_2018_cnaseq';
-- ============================================================================

DROP TABLE IF EXISTS sample_list_members;

CREATE TABLE sample_list_members
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, list_stable_id)
AS
SELECT
    cs.cancer_study_identifier AS cancer_study_identifier,
    sl.stable_id AS list_stable_id,
    sl.name AS list_name,
    sl.category AS category,
    concat(cs.cancer_study_identifier, '_', s.stable_id) AS sample_unique_id
FROM sample_list sl
JOIN cancer_study cs ON sl.cancer_study_id = cs.cancer_study_id
JOIN sample_list_list sll ON sll.list_id = sl.list_id
JOIN sample s ON s.internal_id = sll.sample_id;
