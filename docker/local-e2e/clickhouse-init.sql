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

INSERT INTO cbioportal_authz_e2e.cancer_study VALUES
    ('study_alpha', 'Allowed Alpha Study', 'Visible to alpha researchers', 'BRCA'),
    ('study_beta', 'Restricted Beta Study', 'Visible to beta researchers', 'LUAD');

INSERT INTO cbioportal_authz_e2e.clinical_data_derived VALUES
    ('study_alpha', 'study_alpha_S1', 'study_alpha_P1', 'CANCER_TYPE', 'Breast Cancer'),
    ('study_alpha', 'study_alpha_S2', 'study_alpha_P2', 'CANCER_TYPE', 'Breast Cancer'),
    ('study_beta', 'study_beta_S1', 'study_beta_P1', 'CANCER_TYPE', 'Lung Adenocarcinoma');

CREATE USER IF NOT EXISTS mcp_authz IDENTIFIED WITH plaintext_password BY 'mcp_authz_pw';
GRANT SELECT ON cbioportal_authz_e2e.* TO mcp_authz;
