-- ============================================================================
-- Study-scoped tables: every table the agent can read that belongs to a
-- study carries cancer_study_identifier, so study access is one column check.
-- ============================================================================

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
    assumeNotNull(cs.cancer_study_identifier) AS cancer_study_identifier,
    assumeNotNull(sl.stable_id) AS list_stable_id,
    sl.name AS list_name,
    sl.category AS category,
    concat(assumeNotNull(cs.cancer_study_identifier), '_', assumeNotNull(s.stable_id)) AS sample_unique_id
FROM sample_list sl
JOIN cancer_study cs ON sl.cancer_study_id = cs.cancer_study_id
JOIN sample_list_list sll ON sll.list_id = sl.list_id
JOIN sample s ON s.internal_id = sll.sample_id;

-- ============================================================================
-- cancer_study_identifier on the small study tables
-- ============================================================================
-- genetic_profile, clinical_attribute_meta, resource, resource_definition and
-- cancer_study_tags only carry the numeric cancer_study_id. Each is rebuilt
-- with cancer_study_identifier added and swapped in with EXCHANGE TABLES, as
-- sql/6 does for cancer_study (CREATE TABLE ... AS keeps the engine and the
-- column comments). The tables are small (thousands of rows).
-- ============================================================================

ALTER TABLE genetic_profile DROP COLUMN IF EXISTS cancer_study_identifier;
DROP TABLE IF EXISTS genetic_profile_with_study;
CREATE TABLE genetic_profile_with_study AS genetic_profile;
ALTER TABLE genetic_profile_with_study
    ADD COLUMN cancer_study_identifier LowCardinality(String) COMMENT 'Study identifier (cancer_study.cancer_study_identifier) for cancer_study_id.';
INSERT INTO genetic_profile_with_study
SELECT t.*, assumeNotNull(cs.cancer_study_identifier)
FROM genetic_profile AS t
JOIN cancer_study AS cs ON t.cancer_study_id = cs.cancer_study_id;
EXCHANGE TABLES genetic_profile AND genetic_profile_with_study;
DROP TABLE genetic_profile_with_study;

ALTER TABLE clinical_attribute_meta DROP COLUMN IF EXISTS cancer_study_identifier;
DROP TABLE IF EXISTS clinical_attribute_meta_with_study;
CREATE TABLE clinical_attribute_meta_with_study AS clinical_attribute_meta;
ALTER TABLE clinical_attribute_meta_with_study
    ADD COLUMN cancer_study_identifier LowCardinality(String) COMMENT 'Study identifier (cancer_study.cancer_study_identifier) for cancer_study_id.';
INSERT INTO clinical_attribute_meta_with_study
SELECT t.*, assumeNotNull(cs.cancer_study_identifier)
FROM clinical_attribute_meta AS t
JOIN cancer_study AS cs ON t.cancer_study_id = cs.cancer_study_id;
EXCHANGE TABLES clinical_attribute_meta AND clinical_attribute_meta_with_study;
DROP TABLE clinical_attribute_meta_with_study;

ALTER TABLE resource DROP COLUMN IF EXISTS cancer_study_identifier;
DROP TABLE IF EXISTS resource_with_study;
CREATE TABLE resource_with_study AS resource;
ALTER TABLE resource_with_study
    ADD COLUMN cancer_study_identifier LowCardinality(String) COMMENT 'Study identifier (cancer_study.cancer_study_identifier) for cancer_study_id.';
INSERT INTO resource_with_study
SELECT t.*, assumeNotNull(cs.cancer_study_identifier)
FROM resource AS t
JOIN cancer_study AS cs ON t.cancer_study_id = cs.cancer_study_id;
EXCHANGE TABLES resource AND resource_with_study;
DROP TABLE resource_with_study;

ALTER TABLE resource_definition DROP COLUMN IF EXISTS cancer_study_identifier;
DROP TABLE IF EXISTS resource_definition_with_study;
CREATE TABLE resource_definition_with_study AS resource_definition;
ALTER TABLE resource_definition_with_study
    ADD COLUMN cancer_study_identifier LowCardinality(String) COMMENT 'Study identifier (cancer_study.cancer_study_identifier) for cancer_study_id.';
INSERT INTO resource_definition_with_study
SELECT t.*, assumeNotNull(cs.cancer_study_identifier)
FROM resource_definition AS t
JOIN cancer_study AS cs ON t.cancer_study_id = cs.cancer_study_id;
EXCHANGE TABLES resource_definition AND resource_definition_with_study;
DROP TABLE resource_definition_with_study;

ALTER TABLE cancer_study_tags DROP COLUMN IF EXISTS cancer_study_identifier;
DROP TABLE IF EXISTS cancer_study_tags_with_study;
CREATE TABLE cancer_study_tags_with_study AS cancer_study_tags;
ALTER TABLE cancer_study_tags_with_study
    ADD COLUMN cancer_study_identifier LowCardinality(String) COMMENT 'Study identifier (cancer_study.cancer_study_identifier) for cancer_study_id.';
INSERT INTO cancer_study_tags_with_study
SELECT t.*, assumeNotNull(cs.cancer_study_identifier)
FROM cancer_study_tags AS t
JOIN cancer_study AS cs ON t.cancer_study_id = cs.cancer_study_id;
EXCHANGE TABLES cancer_study_tags AND cancer_study_tags_with_study;
DROP TABLE cancer_study_tags_with_study;
