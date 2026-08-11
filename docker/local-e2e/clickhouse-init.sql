CREATE DATABASE IF NOT EXISTS cbioportal_authz_e2e;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.cancer_study
(
    cancer_study_identifier String,
    name String,
    description String,
    type_of_cancer_id String
)
ENGINE = MergeTree
ORDER BY cancer_study_identifier;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_data_derived
(
    cancer_study_identifier String,
    sample_unique_id String,
    patient_unique_id String,
    attribute_name String,
    attribute_value String
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, sample_unique_id, attribute_name);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample
(
    internal_id UInt64,
    cancer_study_identifier String,
    sample_unique_id String
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.mutation
(
    mutation_event_id UInt64,
    sample_id UInt64,
    hugo_gene_symbol String,
    mutation_variant String
)
ENGINE = MergeTree
ORDER BY mutation_event_id;

-- `patient` carries a direct study reference, same shape as `sample`.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.patient
(
    internal_id UInt64,
    cancer_study_identifier String,
    patient_unique_id String
)
ENGINE = MergeTree
ORDER BY internal_id;

-- `clinical_event` has no study or patient-identifier column of its own -
-- study membership is one join away, through `patient_id`. This mirrors the
-- real cBioPortal schema, where clinical events are only ever linked to a
-- patient, never directly to a study.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_event
(
    clinical_event_id UInt64,
    patient_id UInt64,
    event_type String
)
ENGINE = MergeTree
ORDER BY clinical_event_id;

-- `clinical_event_data` is *two* joins removed from `cancer_study`
-- (clinical_event_data -> clinical_event -> patient -> cancer_study). This is
-- the shape the row-policy pattern has to generalize to: not every
-- study-derived table is a single join away from a study column.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_event_data
(
    clinical_event_id UInt64,
    attr_key String,
    attr_value String
)
ENGINE = MergeTree
ORDER BY (clinical_event_id, attr_key);

INSERT INTO cbioportal_authz_e2e.cancer_study VALUES
    ('study_alpha', 'Allowed Alpha Study', 'Visible to alpha researchers', 'BRCA'),
    ('study_beta', 'Restricted Beta Study', 'Visible to beta researchers', 'LUAD');

INSERT INTO cbioportal_authz_e2e.clinical_data_derived VALUES
    ('study_alpha', 'study_alpha_S1', 'study_alpha_P1', 'CANCER_TYPE', 'Breast Cancer'),
    ('study_alpha', 'study_alpha_S2', 'study_alpha_P2', 'CANCER_TYPE', 'Breast Cancer'),
    ('study_beta', 'study_beta_S1', 'study_beta_P1', 'CANCER_TYPE', 'Lung Adenocarcinoma');

INSERT INTO cbioportal_authz_e2e.sample VALUES
    (1, 'study_alpha', 'study_alpha_S1'),
    (2, 'study_alpha', 'study_alpha_S2'),
    (3, 'study_beta', 'study_beta_S1');

INSERT INTO cbioportal_authz_e2e.mutation VALUES
    (101, 1, 'TP53', 'p.R175H'),
    (102, 3, 'KRAS', 'p.G12D');

INSERT INTO cbioportal_authz_e2e.patient VALUES
    (1, 'study_alpha', 'study_alpha_P1'),
    (2, 'study_alpha', 'study_alpha_P2'),
    (3, 'study_beta', 'study_beta_P1');

INSERT INTO cbioportal_authz_e2e.clinical_event VALUES
    (1001, 1, 'Diagnosis'),
    (1002, 3, 'Treatment');

INSERT INTO cbioportal_authz_e2e.clinical_event_data VALUES
    (1001, 'DIAGNOSIS_TYPE', 'Primary'),
    (1002, 'AGENT', 'Cisplatin');

CREATE USER IF NOT EXISTS mcp_authz IDENTIFIED WITH plaintext_password BY 'mcp_authz_pw';
CREATE ROLE IF NOT EXISTS cbioportal_mcp_study_restricted;
GRANT cbioportal_mcp_study_restricted TO mcp_authz;
SET DEFAULT ROLE cbioportal_mcp_study_restricted TO mcp_authz;

-- Stand-in for a real deployment's CLICKHOUSE_ADMIN_USER (see
-- scripts/apply_sql.sh), used only by scripts/verify_row_policy_coverage.py.
-- Deliberately NOT granted SELECT on cbioportal_authz_e2e's actual row
-- data - only metadata visibility (SHOW TABLES) plus system.row_policies,
-- which is enough to enumerate tables and their policy coverage without
-- this identity being able to read a single row of study data itself.
CREATE USER IF NOT EXISTS local_e2e_admin IDENTIFIED WITH plaintext_password BY 'local_e2e_admin_pw';
GRANT SHOW TABLES ON cbioportal_authz_e2e.* TO local_e2e_admin;
GRANT SELECT ON system.row_policies TO local_e2e_admin;

-- Per-table grants (no wildcard) would mechanically fail-close a forgotten
-- table too - ClickHouse just denies an ungranted table outright. But two
-- things rule that out here regardless of whether it would work: (1) the
-- MCP server's own startup check (ensure_db_permissions) does
-- `CHECK GRANT SELECT ON <db>.*`, which per-table grants do not satisfy
-- even when every existing table is individually granted (verified against
-- ClickHouse 24.12); (2) SHOW TABLES / schema discovery is grant-scoped, so
-- per-table grants would make an ungranted table invisible to
-- clickhouse_list_tables(), not just inaccessible. Keeping the grant
-- wildcard means it says yes to every table, forgotten or not, by
-- construction - so fail-closed protection has to come from the
-- default-deny wildcard ROW POLICY below instead, since the grant layer no
-- longer has room to say no to any one table.
GRANT SELECT ON cbioportal_authz_e2e.* TO cbioportal_mcp_study_restricted;

-- Default-deny fallback: applies to every table in the database, including
-- ones created after this statement runs (verified: a `CREATE ... ON db.*`
-- row policy auto-covers new tables, the same way a `GRANT ... ON db.*`
-- does). Multiple PERMISSIVE row policies on the same table combine with
-- OR, so a table with no policy of its own effectively gets `USING 0`
-- (nothing visible), while a table with its own policy below gets
-- `0 OR <real condition>` = `<real condition>`. This is what makes an
-- unclassified/forgotten table fail closed instead of leaking every row.
CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_default_deny
ON cbioportal_authz_e2e.*
USING 0
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_cancer_study
ON cbioportal_authz_e2e.cancer_study
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_clinical_data
ON cbioportal_authz_e2e.clinical_data_derived
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_sample
ON cbioportal_authz_e2e.sample
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_mutation
ON cbioportal_authz_e2e.mutation
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR sample_id IN (
        SELECT internal_id
        FROM cbioportal_authz_e2e.sample
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_patient
ON cbioportal_authz_e2e.patient
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
TO cbioportal_mcp_study_restricted;

-- One join removed from a study column: resolve through `patient`, the same
-- shape as the `mutation` policy above but keyed on `patient_id` instead of
-- `sample_id`. Any table whose only provenance path is "belongs to a
-- patient" (clinical events, treatments, etc.) needs this shape.
CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_clinical_event
ON cbioportal_authz_e2e.clinical_event
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR patient_id IN (
        SELECT internal_id
        FROM cbioportal_authz_e2e.patient
        WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
    )
TO cbioportal_mcp_study_restricted;

-- Two joins removed from a study column: resolve through `clinical_event`,
-- then through `patient`. Proves the pattern isn't limited to a single hop -
-- any chain of foreign keys back to a study-scoped table can be expressed as
-- a nested IN (...) subquery in the row policy.
CREATE ROW POLICY IF NOT EXISTS cbioportal_mcp_study_policy_clinical_event_data
ON cbioportal_authz_e2e.clinical_event_data
USING getSetting('SQL_cbiomcp_allowed_studies') = '*'
    OR clinical_event_id IN (
        SELECT clinical_event_id
        FROM cbioportal_authz_e2e.clinical_event
        WHERE patient_id IN (
            SELECT internal_id
            FROM cbioportal_authz_e2e.patient
            WHERE has(splitByChar(',', getSetting('SQL_cbiomcp_allowed_studies')), cancer_study_identifier)
        )
    )
TO cbioportal_mcp_study_restricted;
