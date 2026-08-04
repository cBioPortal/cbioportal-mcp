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

CREATE USER IF NOT EXISTS mcp_authz IDENTIFIED WITH plaintext_password BY 'mcp_authz_pw';
CREATE ROLE IF NOT EXISTS cbioportal_mcp_study_restricted;
GRANT SELECT ON cbioportal_authz_e2e.* TO cbioportal_mcp_study_restricted;
GRANT cbioportal_mcp_study_restricted TO mcp_authz;
SET DEFAULT ROLE cbioportal_mcp_study_restricted TO mcp_authz;

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
