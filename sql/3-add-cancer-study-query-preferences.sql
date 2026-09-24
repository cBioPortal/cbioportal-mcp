-- ============================================================================
-- cancer_study_query_preferences: which study (or set of studies) to use
-- for which kind of question
-- ============================================================================
-- One row = "for purpose X, use cancer_study_identifier Y". A preference
-- can resolve to a single study or to many; the LLM joins on this table
-- instead of hardcoding study IDs.
--
-- THIS FILE owns the schema and only the truly portable, pattern-detected
-- preferences. Portal-specific preferences live in
-- sql/portal-specific/<portal-name>/*.sql — for the cBioPortal public
-- deployment that's sql/portal-specific/public-portal/0-preferences.sql.
-- Other deployments add their own sql/portal-specific/<their-name>/*.sql;
-- apply_sql.sh and the daily clone cron iterate every portal-specific
-- subdirectory after the portable files.
--
-- Defensive principle: every INSERT into cancer_study_query_preferences,
-- including those in portal-specific/, MUST gate on existence in
-- `cancer_study` so missing studies become no-ops rather than phantom
-- preference rows.
-- ============================================================================

DROP TABLE IF EXISTS cancer_study_query_preferences;

CREATE TABLE cancer_study_query_preferences (
    preference_name LowCardinality(String) COMMENT 'Named purpose. See SELECT DISTINCT preference_name FROM cancer_study_query_preferences for what this deployment loaded.',
    cancer_study_identifier String         COMMENT 'cancer_study.cancer_study_identifier — join key',
    notes String                           COMMENT 'Why this study is recommended for this preference'
)
ENGINE = MergeTree
ORDER BY (preference_name, cancer_study_identifier)
COMMENT 'Curated study recommendations per query intent. Lookup by preference_name, then JOIN on cancer_study_identifier.';

-- pan_cancer_tcga: every TCGA PanCancer Atlas 2018 study that's loaded.
-- Pattern-detected so this works in any deployment that loads PanCanAtlas.
INSERT INTO cancer_study_query_preferences
SELECT 'pan_cancer_tcga', cancer_study_identifier, 'TCGA PanCancer Atlas 2018'
FROM cancer_study
WHERE cancer_study_identifier LIKE '%_tcga_pan_can_atlas_2018';
