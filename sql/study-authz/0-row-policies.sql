-- ============================================================================
-- Study-level row policies for restricted cBioPortal MCP deployments
-- ============================================================================
-- OPT-IN. Not part of the default sql/*.sql run: apply_sql.sh applies it only
-- with APPLY_STUDY_AUTHZ_SQL=true (see sql/README.md). Public deployments do
-- not need it.
--
-- The MCP server (CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true) sends the
-- caller's allowed studies on every SELECT as the custom setting
-- SQL_cbiomcp_allowed_studies: '*' = every study, otherwise a comma-separated
-- list of cancer_study_identifier values ('' = none). The policies below turn
-- that into row filtering for every study-scoped table, following each
-- table's real foreign-key path back to cancer_study.cancer_study_identifier.
-- If the setting is missing, getSetting() raises, so a query that bypasses
-- the MCP fails instead of returning rows.
--
-- The table list mirrors src/cbioportal_mcp/authentication/row_policy_catalog.py
-- (tests/test_row_policy_sql.py keeps them in sync). It covers cBioPortal
-- ClickHouse schema 3.1.0 and the legacy schema (resource_sample /
-- resource_patient / resource_study). A policy on a table that does not exist
-- is a no-op, and policies are keyed by table name, so they survive the
-- DROP/CREATE and EXCHANGE TABLES done by sql/3-7.
--
-- Every table reference is qualified with __CLICKHOUSE_DATABASE__, which the
-- apply scripts replace with the target database. Unqualified names inside a
-- policy's subquery would be resolved against the *querying session's*
-- current database (verified on ClickHouse 24.12), not the policy's.
--
-- Requires: <custom_settings_prefixes> including SQL_ (the ClickHouse default)
-- and an admin user allowed to CREATE ROLE / CREATE ROW POLICY. Do not grant
-- the restricted role CREATE TEMPORARY TABLE or REMOTE (they enable table
-- functions such as merge() / remote()), dictGet, or SQL SECURITY DEFINER
-- views.
--
-- Idempotent: CREATE ... IF NOT EXISTS / CREATE ROW POLICY OR REPLACE.
-- Rollback: DROP ROW POLICY IF EXISTS <name> ON __CLICKHOUSE_DATABASE__.<table>
-- for each policy below (SELECT name FROM system.row_policies
-- WHERE name LIKE 'cbioportal_mcp_%').
-- ============================================================================

CREATE ROLE IF NOT EXISTS cbioportal_mcp_study_restricted;

-- Database-wide grant: the MCP's startup check requires SELECT ON <db>.*, and
-- schema discovery (SHOW TABLES) is grant-scoped. Row filtering is entirely
-- the policies' job. Grant the role to the MCP runtime user separately:
--   GRANT cbioportal_mcp_study_restricted TO <mcp_user>;
--   SET DEFAULT ROLE cbioportal_mcp_study_restricted TO <mcp_user>;
GRANT SELECT ON __CLICKHOUSE_DATABASE__.* TO cbioportal_mcp_study_restricted;

-- ============================================================================
-- 1. Database-wide fallbacks.
-- ============================================================================
-- Default-deny: PERMISSIVE policies on one table combine with OR, so a table
-- with no policy of its own stays at 0 (nothing visible) and a table with one
-- gets `0 OR <condition>`. A table added later - or forgotten here - fails
-- closed. scripts/verify_row_policy_coverage.py flags study-scoped tables
-- that only have this fallback (fully denied by accident).
CREATE ROW POLICY OR REPLACE cbioportal_mcp_default_deny
ON __CLICKHOUSE_DATABASE__.*
AS PERMISSIVE FOR SELECT
USING 0
TO cbioportal_mcp_study_restricted;

-- Everyone else (admin, the public MCP user, the clone job) keeps full access
-- even on servers where users_without_row_policies_can_read_rows = false:
-- once a table has any row policy, ClickHouse otherwise hides its rows from
-- users the policies do not mention.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_unrestricted_others
ON __CLICKHOUSE_DATABASE__.*
AS PERMISSIVE FOR SELECT
USING 1
TO ALL EXCEPT cbioportal_mcp_study_restricted;

-- ============================================================================
-- 2. Study-scoped tables.
-- ============================================================================
-- Every condition starts with the '*' short-circuit so unrestricted callers
-- skip the subqueries. Subqueries repeat the full path to cancer_study rather
-- than relying on the parent table's own policy (which ClickHouse also
-- applies inside the subquery), so each policy is correct on its own.


-- ---------------------------------------------------------------------------
-- 2a. Direct study identifier column.
-- ---------------------------------------------------------------------------

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_cancer_study
ON __CLICKHOUSE_DATABASE__.cancer_study
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_cancer_study_query_preferences
ON __CLICKHOUSE_DATABASE__.cancer_study_query_preferences
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_data_derived
ON __CLICKHOUSE_DATABASE__.clinical_data_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_event_data_derived
ON __CLICKHOUSE_DATABASE__.clinical_event_data_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_event_derived
ON __CLICKHOUSE_DATABASE__.clinical_event_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_genetic_alteration_derived
ON __CLICKHOUSE_DATABASE__.genetic_alteration_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_genomic_event_derived
ON __CLICKHOUSE_DATABASE__.genomic_event_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample_derived
ON __CLICKHOUSE_DATABASE__.sample_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample_to_gene_panel_derived
ON __CLICKHOUSE_DATABASE__.sample_to_gene_panel_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

-- mutation_derived names its study column studyId (camelCase API shape).
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_mutation_derived
ON __CLICKHOUSE_DATABASE__.mutation_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), studyId)
TO cbioportal_mcp_study_restricted;

-- ---------------------------------------------------------------------------
-- 2b. cancer_study_id -> cancer_study.
-- ---------------------------------------------------------------------------

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_cancer_study_tags
ON __CLICKHOUSE_DATABASE__.cancer_study_tags
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_attribute_meta
ON __CLICKHOUSE_DATABASE__.clinical_attribute_meta
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_copy_number_seg
ON __CLICKHOUSE_DATABASE__.copy_number_seg
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_copy_number_seg_file
ON __CLICKHOUSE_DATABASE__.copy_number_seg_file
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_genetic_profile
ON __CLICKHOUSE_DATABASE__.genetic_profile
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_gistic
ON __CLICKHOUSE_DATABASE__.gistic
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_mut_sig
ON __CLICKHOUSE_DATABASE__.mut_sig
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_patient
ON __CLICKHOUSE_DATABASE__.patient
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_resource
ON __CLICKHOUSE_DATABASE__.resource
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_resource_definition
ON __CLICKHOUSE_DATABASE__.resource_definition
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample_list
ON __CLICKHOUSE_DATABASE__.sample_list
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cancer_study_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

-- Legacy schema: resource_study.internal_id is cancer_study.cancer_study_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_resource_study
ON __CLICKHOUSE_DATABASE__.resource_study
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR internal_id IN (
        SELECT cancer_study_id
        FROM __CLICKHOUSE_DATABASE__.cancer_study
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

-- ---------------------------------------------------------------------------
-- 2c. Through patient (patient.cancer_study_id -> cancer_study).
-- ---------------------------------------------------------------------------

-- Production sample has no study column: sample.patient_id -> patient.internal_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample
ON __CLICKHOUSE_DATABASE__.sample
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR patient_id IN (
        SELECT internal_id
        FROM __CLICKHOUSE_DATABASE__.patient
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_event
ON __CLICKHOUSE_DATABASE__.clinical_event
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR patient_id IN (
        SELECT internal_id
        FROM __CLICKHOUSE_DATABASE__.patient
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

-- clinical_patient.internal_id is patient.internal_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_patient
ON __CLICKHOUSE_DATABASE__.clinical_patient
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR internal_id IN (
        SELECT internal_id
        FROM __CLICKHOUSE_DATABASE__.patient
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

-- Legacy schema: resource_patient.internal_id is patient.internal_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_resource_patient
ON __CLICKHOUSE_DATABASE__.resource_patient
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR internal_id IN (
        SELECT internal_id
        FROM __CLICKHOUSE_DATABASE__.patient
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

-- ---------------------------------------------------------------------------
-- 2d. Through sample (sample.patient_id -> patient -> cancer_study).
-- ---------------------------------------------------------------------------

-- clinical_sample.internal_id is sample.internal_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_sample
ON __CLICKHOUSE_DATABASE__.clinical_sample
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR internal_id IN (
        SELECT internal_id
        FROM __CLICKHOUSE_DATABASE__.sample
        WHERE patient_id IN (
            SELECT internal_id
            FROM __CLICKHOUSE_DATABASE__.patient
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
    )
TO cbioportal_mcp_study_restricted;

-- Legacy schema: resource_sample.internal_id is sample.internal_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_resource_sample
ON __CLICKHOUSE_DATABASE__.resource_sample
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR internal_id IN (
        SELECT internal_id
        FROM __CLICKHOUSE_DATABASE__.sample
        WHERE patient_id IN (
            SELECT internal_id
            FROM __CLICKHOUSE_DATABASE__.patient
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
    )
TO cbioportal_mcp_study_restricted;

-- ---------------------------------------------------------------------------
-- 2e. Through genetic_profile (genetic_profile.cancer_study_id -> cancer_study).
-- Every per-sample molecular table carries genetic_profile_id; it is the
-- authoritative study link (sample_id alone would also work, but the profile
-- is what the data was imported under).
-- ---------------------------------------------------------------------------

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_allele_specific_copy_number
ON __CLICKHOUSE_DATABASE__.allele_specific_copy_number
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_alteration_driver_annotation
ON __CLICKHOUSE_DATABASE__.alteration_driver_annotation
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_genetic_alteration
ON __CLICKHOUSE_DATABASE__.genetic_alteration
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_genetic_profile_samples
ON __CLICKHOUSE_DATABASE__.genetic_profile_samples
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_mutation
ON __CLICKHOUSE_DATABASE__.mutation
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_mutation_count_by_keyword
ON __CLICKHOUSE_DATABASE__.mutation_count_by_keyword
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample_cna_event
ON __CLICKHOUSE_DATABASE__.sample_cna_event
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample_profile
ON __CLICKHOUSE_DATABASE__.sample_profile
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_structural_variant
ON __CLICKHOUSE_DATABASE__.structural_variant
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR genetic_profile_id IN (
        SELECT genetic_profile_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

-- A link row exposes both profiles, so both ends must be visible.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_genetic_profile_link
ON __CLICKHOUSE_DATABASE__.genetic_profile_link
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR (
        referring_genetic_profile_id IN (
            SELECT genetic_profile_id
            FROM __CLICKHOUSE_DATABASE__.genetic_profile
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
        AND referred_genetic_profile_id IN (
            SELECT genetic_profile_id
            FROM __CLICKHOUSE_DATABASE__.genetic_profile
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
    )
TO cbioportal_mcp_study_restricted;

-- generic_assay_data_derived has no study column; profile_stable_id is genetic_profile.stable_id.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_generic_assay_data_derived
ON __CLICKHOUSE_DATABASE__.generic_assay_data_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR profile_stable_id IN (
        SELECT stable_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_generic_assay_profile_entity_derived
ON __CLICKHOUSE_DATABASE__.generic_assay_profile_entity_derived
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR profile_stable_id IN (
        SELECT stable_id
        FROM __CLICKHOUSE_DATABASE__.genetic_profile
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

-- ---------------------------------------------------------------------------
-- 2f. Through another study-scoped child table.
-- ---------------------------------------------------------------------------

-- Two joins removed from a study: clinical_event_data -> clinical_event -> patient -> cancer_study.
CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_clinical_event_data
ON __CLICKHOUSE_DATABASE__.clinical_event_data
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR clinical_event_id IN (
        SELECT clinical_event_id
        FROM __CLICKHOUSE_DATABASE__.clinical_event
        WHERE patient_id IN (
            SELECT internal_id
            FROM __CLICKHOUSE_DATABASE__.patient
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_gistic_to_gene
ON __CLICKHOUSE_DATABASE__.gistic_to_gene
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR gistic_roi_id IN (
        SELECT gistic_roi_id
        FROM __CLICKHOUSE_DATABASE__.gistic
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_sample_list_list
ON __CLICKHOUSE_DATABASE__.sample_list_list
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR list_id IN (
        SELECT list_id
        FROM __CLICKHOUSE_DATABASE__.sample_list
        WHERE cancer_study_id IN (
            SELECT cancer_study_id
            FROM __CLICKHOUSE_DATABASE__.cancer_study
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;

-- ---------------------------------------------------------------------------
-- 2g. Shared event dimensions. mutation_event / cna_event hold one row per
-- distinct event across all studies, but an event only exists because some
-- study observed it - so a restricted caller sees only events observed in a
-- study it may read.
-- ---------------------------------------------------------------------------

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_mutation_event
ON __CLICKHOUSE_DATABASE__.mutation_event
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR mutation_event_id IN (
        SELECT mutation_event_id
        FROM __CLICKHOUSE_DATABASE__.mutation
        WHERE genetic_profile_id IN (
            SELECT genetic_profile_id
            FROM __CLICKHOUSE_DATABASE__.genetic_profile
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY OR REPLACE cbioportal_mcp_study_cna_event
ON __CLICKHOUSE_DATABASE__.cna_event
AS PERMISSIVE FOR SELECT
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR cna_event_id IN (
        SELECT cna_event_id
        FROM __CLICKHOUSE_DATABASE__.sample_cna_event
        WHERE genetic_profile_id IN (
            SELECT genetic_profile_id
            FROM __CLICKHOUSE_DATABASE__.genetic_profile
            WHERE cancer_study_id IN (
                SELECT cancer_study_id
                FROM __CLICKHOUSE_DATABASE__.cancer_study
                WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
            )
        )
    )
TO cbioportal_mcp_study_restricted;

-- ============================================================================
-- 3. Study-agnostic reference data: explicitly readable (USING 1).
-- ============================================================================

CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_cosmic_mutation ON __CLICKHOUSE_DATABASE__.cosmic_mutation AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_gene ON __CLICKHOUSE_DATABASE__.gene AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_gene_alias ON __CLICKHOUSE_DATABASE__.gene_alias AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_gene_panel ON __CLICKHOUSE_DATABASE__.gene_panel AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_gene_panel_list ON __CLICKHOUSE_DATABASE__.gene_panel_list AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_gene_panel_to_gene_derived ON __CLICKHOUSE_DATABASE__.gene_panel_to_gene_derived AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_generic_assay_meta_derived ON __CLICKHOUSE_DATABASE__.generic_assay_meta_derived AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_generic_entity_properties ON __CLICKHOUSE_DATABASE__.generic_entity_properties AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_geneset ON __CLICKHOUSE_DATABASE__.geneset AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_geneset_gene ON __CLICKHOUSE_DATABASE__.geneset_gene AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_geneset_hierarchy_leaf ON __CLICKHOUSE_DATABASE__.geneset_hierarchy_leaf AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_geneset_hierarchy_node ON __CLICKHOUSE_DATABASE__.geneset_hierarchy_node AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_genetic_entity ON __CLICKHOUSE_DATABASE__.genetic_entity AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_info ON __CLICKHOUSE_DATABASE__.info AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_reference_genome ON __CLICKHOUSE_DATABASE__.reference_genome AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_reference_genome_gene ON __CLICKHOUSE_DATABASE__.reference_genome_gene AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_reference_type_of_cancer ON __CLICKHOUSE_DATABASE__.type_of_cancer AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;

-- ============================================================================
-- 4. Views (sql/4, sql/5, sql/7): pass-through (USING 1).
-- ============================================================================
-- The database-wide default-deny in section 1 also matches views - without
-- these, every view returns zero rows for the restricted role (verified on
-- ClickHouse 24.12 and 25.8). USING 1 on the view itself is safe because the view's
-- query runs as the invoker (default_normal_view_sql_security = INVOKER), so
-- every underlying table's own policy above still filters the rows. This
-- includes parameterized views such as gene_mutation_frequency_by_cancer_type.
-- That only holds for SQL SECURITY INVOKER: a DEFINER view reads as its
-- definer, and a NONE view runs without the caller's row-policy context, so
-- either would leak every study through its USING 1 policy (NONE verified on
-- 25.8). A view with no SQL SECURITY clause depends on the server default at
-- creation time, so every view is set to INVOKER explicitly below. These
-- ALTERs need the views to exist (ClickHouse has no ALTER ... IF EXISTS):
-- apply this file after sql/4, sql/5 and sql/7, and again whenever they are
-- re-applied, since they DROP and re-CREATE the views.
-- scripts/verify_row_policy_coverage.py and the server's startup check
-- (CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true) refuse any view that is
-- not explicitly INVOKER.

CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_clinical_attribute_counts ON __CLICKHOUSE_DATABASE__.clinical_attribute_counts AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_cna_panel_gene_coverage ON __CLICKHOUSE_DATABASE__.cna_panel_gene_coverage AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_cna_wes_coverage ON __CLICKHOUSE_DATABASE__.cna_wes_coverage AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_co_altered_genes_in_study ON __CLICKHOUSE_DATABASE__.co_altered_genes_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_alteration_frequency_by_cancer_type ON __CLICKHOUSE_DATABASE__.gene_alteration_frequency_by_cancer_type AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_cna_distribution_in_study ON __CLICKHOUSE_DATABASE__.gene_cna_distribution_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_mutation_frequency_by_cancer_type ON __CLICKHOUSE_DATABASE__.gene_mutation_frequency_by_cancer_type AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_mutation_frequency_in_studies ON __CLICKHOUSE_DATABASE__.gene_mutation_frequency_in_studies AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_mutation_frequency_in_study ON __CLICKHOUSE_DATABASE__.gene_mutation_frequency_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_mutation_variants_in_study ON __CLICKHOUSE_DATABASE__.gene_mutation_variants_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_gene_pair_coexpression ON __CLICKHOUSE_DATABASE__.gene_pair_coexpression AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_mutation_panel_gene_coverage ON __CLICKHOUSE_DATABASE__.mutation_panel_gene_coverage AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_mutation_wes_coverage ON __CLICKHOUSE_DATABASE__.mutation_wes_coverage AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_sv_panel_gene_coverage ON __CLICKHOUSE_DATABASE__.sv_panel_gene_coverage AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_sv_wes_coverage ON __CLICKHOUSE_DATABASE__.sv_wes_coverage AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_top_cna_genes_in_study ON __CLICKHOUSE_DATABASE__.top_cna_genes_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_top_mutated_genes_in_cohort ON __CLICKHOUSE_DATABASE__.top_mutated_genes_in_cohort AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_top_mutated_genes_in_study ON __CLICKHOUSE_DATABASE__.top_mutated_genes_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_top_sv_genes_in_study ON __CLICKHOUSE_DATABASE__.top_sv_genes_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_treatment_counts_in_study ON __CLICKHOUSE_DATABASE__.treatment_counts_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;
CREATE ROW POLICY OR REPLACE cbioportal_mcp_view_treatment_regimens_in_study ON __CLICKHOUSE_DATABASE__.treatment_regimens_in_study AS PERMISSIVE FOR SELECT USING 1 TO cbioportal_mcp_study_restricted;

ALTER TABLE __CLICKHOUSE_DATABASE__.clinical_attribute_counts MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.cna_panel_gene_coverage MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.cna_wes_coverage MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.co_altered_genes_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_alteration_frequency_by_cancer_type MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_cna_distribution_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_mutation_frequency_by_cancer_type MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_mutation_frequency_in_studies MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_mutation_frequency_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_mutation_variants_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.gene_pair_coexpression MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.mutation_panel_gene_coverage MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.mutation_wes_coverage MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.sv_panel_gene_coverage MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.sv_wes_coverage MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.top_cna_genes_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.top_mutated_genes_in_cohort MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.top_mutated_genes_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.top_sv_genes_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.treatment_counts_in_study MODIFY SQL SECURITY INVOKER;
ALTER TABLE __CLICKHOUSE_DATABASE__.treatment_regimens_in_study MODIFY SQL SECURITY INVOKER;

-- ============================================================================
-- 5. Deliberately denied: authorities, users, data_access_tokens.
-- ============================================================================
-- cBioPortal's own auth tables get no policy, so the default-deny in section
-- 1 keeps them unreadable for the restricted role.

