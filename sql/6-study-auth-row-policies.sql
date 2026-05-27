-- ============================================================================
-- Study-level row policies for public/semi-public MCP deployments
-- ============================================================================
--
-- These policies are the database enforcement boundary for study authorization.
-- The MCP passes request-local authorization state as ClickHouse custom settings
-- on every query:
--
--   cbioportal_allowed_studies = 'brca_tcga,luad_tcga'
--   cbioportal_allow_all = '0' or '1'
--
-- Required ClickHouse server/user configuration:
--
--   <custom_settings_prefixes>cbioportal_</custom_settings_prefixes>
--
-- The MCP runtime user should remain read-only. In deployments that constrain
-- readonly at the user/profile level, validate that per-query custom settings
-- can still be supplied. ClickHouse readonly=2 is commonly used for read-only
-- users that may change query-level settings.
--
-- Rollback:
--
--   DROP ROW POLICY IF EXISTS cbioportal_study_policy_cancer_study ON cancer_study;
--   DROP ROW POLICY IF EXISTS cbioportal_study_policy_clinical_data_derived ON clinical_data_derived;
--   ...repeat for policies below...
--
-- WARNING: Apply only to cloned/prepped MCP databases, not directly to the
-- production cBioPortal application database.
-- ============================================================================

-- Direct study-scoped tables: table contains cancer_study_identifier.
CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_cancer_study
ON cancer_study
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_cancer_study_query_preferences
ON cancer_study_query_preferences
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_clinical_data_derived
ON clinical_data_derived
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_genomic_event_derived
ON genomic_event_derived
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_sample_to_gene_panel_derived
ON sample_to_gene_panel_derived
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_genetic_alteration_derived
ON genetic_alteration_derived
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_clinical_event_derived
ON clinical_event_derived
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR has(
        splitByChar(',', getSettingOrDefault('cbioportal_allowed_studies', '')),
        cancer_study_identifier
    )
)
TO app_user;

-- Indirect study-scoped tables: table carries cancer_study_id, so authorization
-- is resolved through the already-filtered cancer_study table.
CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_genetic_profile
ON genetic_profile
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM cancer_study
    )
)
TO app_user;

CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_patient
ON patient
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM cancer_study
    )
)
TO app_user;

-- sample joins to patient rather than cancer_study directly in the cBioPortal
-- schema. The patient subquery is policy-filtered through cancer_study.
CREATE ROW POLICY IF NOT EXISTS cbioportal_study_policy_sample
ON sample
FOR SELECT
USING (
    getSettingOrDefault('cbioportal_allow_all', '0') = '1'
    OR patient_id IN (
        SELECT internal_id
        FROM patient
    )
)
TO app_user;
