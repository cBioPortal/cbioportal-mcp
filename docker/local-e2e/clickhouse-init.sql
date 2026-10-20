-- Production-shaped mock of a cloned cBioPortal ClickHouse database.
--
-- Table and column names follow the real schema (cBioPortal's ClickHouse
-- schema: legacy base tables as cloned into the MCP database, plus the
-- *_derived tables from schema 3.1.0), trimmed to the columns that matter for
-- study linkage and for the repo's sql/*.sql files. In particular, `sample`
-- and `patient` have NO study column: sample.patient_id -> patient.internal_id
-- -> patient.cancer_study_id -> cancer_study.cancer_study_id. The row
-- policies are NOT defined here: init-apply-repo-sql.sh applies the real
-- sql/*.sql files and sql/study-authz/0-row-policies.sql on top of this, so
-- the e2e exercises exactly what a deployment would run.
--
-- Two studies, study_alpha (cancer_study_id 1) and study_beta (2). Every
-- study-scoped table holds rows for both, keyed so that
-- scripts/local_e2e_keycloak_authz.py can check exactly which rows each
-- user sees. Internal ids: alpha patients 1-2 / samples 1-2, beta patient 3
-- / sample 3; alpha genetic profiles 1x, beta 2x.

CREATE DATABASE IF NOT EXISTS cbioportal_authz_e2e;

-- ---------------------------------------------------------------------------
-- Study, patient, sample
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.cancer_study
(
    cancer_study_id Int32,
    cancer_study_identifier String,
    type_of_cancer_id String,
    name String,
    description String,
    public UInt8,
    pmid Nullable(String),
    citation Nullable(String),
    groups Nullable(String),
    status Nullable(Int32),
    import_date Nullable(DateTime),
    reference_genome_id Int32
)
ENGINE = MergeTree
ORDER BY cancer_study_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.cancer_study_tags
(
    cancer_study_id Int32,
    tags String
)
ENGINE = MergeTree
ORDER BY cancer_study_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.patient
(
    internal_id Int32,
    stable_id String,
    cancer_study_id Int32
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample
(
    internal_id Int32,
    stable_id String,
    sample_type String,
    patient_id Int32
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample_list
(
    list_id Int32,
    stable_id String,
    category String,
    cancer_study_id Int32,
    name String,
    description Nullable(String)
)
ENGINE = MergeTree
ORDER BY list_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample_list_list
(
    list_id Int32,
    sample_id Int32
)
ENGINE = MergeTree
ORDER BY (list_id, sample_id);

-- ---------------------------------------------------------------------------
-- Clinical data and events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_patient
(
    internal_id Int32,
    attr_id Nullable(String),
    attr_value Nullable(String)
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_sample
(
    internal_id Int32,
    attr_id Nullable(String),
    attr_value Nullable(String)
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_attribute_meta
(
    attr_id String,
    display_name Nullable(String),
    description Nullable(String),
    datatype Nullable(String),
    patient_attribute Nullable(UInt8),
    priority Nullable(String),
    cancer_study_id Nullable(Int32)
)
ENGINE = MergeTree
ORDER BY attr_id;

-- No study column: one join to patient.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_event
(
    clinical_event_id Int64,
    patient_id Int32,
    start_date Int32,
    stop_date Int32,
    event_type String
)
ENGINE = MergeTree
ORDER BY clinical_event_id;

-- No study or patient column: two joins (clinical_event -> patient).
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_event_data
(
    clinical_event_id Int64,
    key String,
    value String
)
ENGINE = MergeTree
ORDER BY clinical_event_id;

-- ---------------------------------------------------------------------------
-- Molecular profiles and per-profile data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genetic_profile
(
    genetic_profile_id Int32,
    stable_id String,
    cancer_study_id Int32,
    genetic_alteration_type String,
    generic_assay_type Nullable(String),
    datatype String,
    name String,
    description String,
    show_profile_in_analysis_tab UInt8,
    pivot_threshold Float32,
    sort_order String,
    patient_level Nullable(UInt8)
)
ENGINE = MergeTree
ORDER BY genetic_profile_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genetic_profile_link
(
    referring_genetic_profile_id Int32,
    referred_genetic_profile_id Int32,
    reference_type String
)
ENGINE = MergeTree
ORDER BY referring_genetic_profile_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genetic_profile_samples
(
    genetic_profile_id Int32,
    ordered_sample_list String
)
ENGINE = MergeTree
ORDER BY genetic_profile_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genetic_alteration
(
    genetic_profile_id Int32,
    genetic_entity_id Int32,
    values String
)
ENGINE = MergeTree
ORDER BY (genetic_profile_id, genetic_entity_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample_profile
(
    sample_id Int32,
    genetic_profile_id Int32,
    panel_id Nullable(Int32)
)
ENGINE = MergeTree
ORDER BY (sample_id, genetic_profile_id);

-- Shared across studies: one row per distinct mutation.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.mutation_event
(
    mutation_event_id Int32,
    entrez_gene_id Int32,
    chr Nullable(String),
    start_position Nullable(Int64),
    end_position Nullable(Int64),
    reference_allele Nullable(String),
    tumor_seq_allele Nullable(String),
    protein_change String,
    mutation_type String,
    variant_type Nullable(String),
    keyword Nullable(String)
)
ENGINE = MergeTree
ORDER BY mutation_event_id;

-- No study or sample-identifier column: genetic_profile_id / sample_id only.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.mutation
(
    mutation_event_id Int32,
    genetic_profile_id Int32,
    sample_id Int32,
    entrez_gene_id Int32,
    mutation_status String,
    validation_status String,
    tumor_alt_count Int32,
    tumor_ref_count Int32,
    amino_acid_change String
)
ENGINE = MergeTree
ORDER BY (genetic_profile_id, entrez_gene_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.mutation_count_by_keyword
(
    genetic_profile_id Int32,
    keyword Nullable(String),
    entrez_gene_id Int32,
    keyword_count Int32,
    gene_count Int32
)
ENGINE = MergeTree
ORDER BY (genetic_profile_id, entrez_gene_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.alteration_driver_annotation
(
    alteration_event_id Int32,
    genetic_profile_id Int32,
    sample_id Int32,
    driver_filter Nullable(String),
    driver_filter_annotation Nullable(String),
    driver_tiers_filter Nullable(String),
    driver_tiers_filter_annotation Nullable(String)
)
ENGINE = MergeTree
ORDER BY (alteration_event_id, genetic_profile_id, sample_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.allele_specific_copy_number
(
    mutation_event_id Int32,
    genetic_profile_id Int32,
    sample_id Int32,
    ascn_integer_copy_number Nullable(Int32),
    ascn_method String,
    clonal Nullable(String),
    total_copy_number Nullable(Int32)
)
ENGINE = MergeTree
ORDER BY (mutation_event_id, genetic_profile_id, sample_id);

-- Shared across studies: one row per distinct gene-level CNA.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.cna_event
(
    cna_event_id Int32,
    entrez_gene_id Nullable(Int32),
    alteration Int32
)
ENGINE = MergeTree
ORDER BY cna_event_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample_cna_event
(
    cna_event_id Int32,
    sample_id Int32,
    genetic_profile_id Int32,
    annotation_json String
)
ENGINE = MergeTree
ORDER BY (genetic_profile_id, cna_event_id, sample_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.structural_variant
(
    internal_id Int32,
    genetic_profile_id Int32,
    sample_id Int32,
    site1_entrez_gene_id Nullable(Int32),
    site2_entrez_gene_id Nullable(Int32),
    event_info Nullable(String),
    sv_status String
)
ENGINE = MergeTree
ORDER BY (sample_id, genetic_profile_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.copy_number_seg
(
    seg_id Int64,
    cancer_study_id Int32,
    sample_id Int32,
    chr String,
    start Int32,
    end Int32,
    num_probes Int32,
    segment_mean Nullable(Float64)
)
ENGINE = MergeTree
ORDER BY seg_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.copy_number_seg_file
(
    seg_file_id Int32,
    cancer_study_id Int32,
    reference_genome_id String,
    description String,
    filename String
)
ENGINE = MergeTree
ORDER BY seg_file_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gistic
(
    gistic_roi_id Int64,
    cancer_study_id Int32,
    chromosome Int32,
    cytoband String,
    wide_peak_start Int32,
    wide_peak_end Int32,
    q_value Float64,
    amp UInt8
)
ENGINE = MergeTree
ORDER BY gistic_roi_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gistic_to_gene
(
    gistic_roi_id Int64,
    entrez_gene_id Nullable(Int32)
)
ENGINE = MergeTree
ORDER BY gistic_roi_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.mut_sig
(
    cancer_study_id Int32,
    entrez_gene_id Int32,
    rank Int32,
    NumBasesCovered Int32,
    NumMutations Int32,
    p_value Float32,
    q_value Float32
)
ENGINE = MergeTree
ORDER BY (cancer_study_id, entrez_gene_id);

-- ---------------------------------------------------------------------------
-- Resources (legacy schema: one table per scope)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.resource_definition
(
    resource_id String,
    display_name String,
    description String,
    resource_type String,
    open_by_default UInt8,
    priority Int32,
    cancer_study_id Int32,
    custom_metadata String
)
ENGINE = MergeTree
ORDER BY (resource_id, cancer_study_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.resource_sample
(
    internal_id Int32,
    resource_id String,
    url String
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.resource_patient
(
    internal_id Int32,
    resource_id String,
    url String
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.resource_study
(
    internal_id Int32,
    resource_id String,
    url String
)
ENGINE = MergeTree
ORDER BY internal_id;

-- 3.1.0 replacement for resource_sample/_patient/_study (both shapes are
-- covered by the row policies; real deployments have one or the other).
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.resource
(
    internal_id Int64,
    resource_id String,
    cancer_study_id Int64,
    resource_scope String,
    patient_id Nullable(String),
    sample_id Nullable(String),
    url String,
    display_name Nullable(String),
    metadata Nullable(String)
)
ENGINE = MergeTree
ORDER BY (cancer_study_id, resource_id, internal_id);

-- ---------------------------------------------------------------------------
-- Derived tables (cBioPortal schema 3.1.0)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_data_derived
(
    internal_id Int32,
    sample_unique_id String,
    patient_unique_id String,
    attribute_name LowCardinality(String),
    attribute_value String,
    cancer_study_identifier LowCardinality(String),
    type LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, type, attribute_name, sample_unique_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_event_derived
(
    clinical_event_id Int64,
    patient_id Nullable(Int64),
    patient_stable_id String,
    start_date Nullable(Int64),
    stop_date Nullable(Int64),
    event_type LowCardinality(String),
    cancer_study_identifier LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, event_type, clinical_event_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.clinical_event_data_derived
(
    patient_unique_id String,
    key String,
    value String,
    start_date Int32,
    stop_date Int32 DEFAULT 0,
    event_type LowCardinality(String),
    cancer_study_identifier LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, event_type, patient_unique_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genetic_alteration_derived
(
    sample_unique_id String,
    cancer_study_identifier LowCardinality(String),
    hugo_gene_symbol String,
    profile_type LowCardinality(String),
    alteration_value Nullable(String)
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, hugo_gene_symbol, profile_type, sample_unique_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genomic_event_derived
(
    sample_unique_id String,
    hugo_gene_symbol String,
    entrez_gene_id Int32,
    gene_panel_stable_id LowCardinality(String),
    cancer_study_identifier LowCardinality(String),
    genetic_profile_stable_id LowCardinality(String),
    variant_type LowCardinality(String),
    mutation_variant String,
    mutation_type LowCardinality(String),
    mutation_status LowCardinality(String),
    driver_filter LowCardinality(String),
    driver_filter_annotation String,
    driver_tiers_filter LowCardinality(String),
    driver_tiers_filter_annotation String,
    cna_alteration Nullable(Int8),
    cna_cytoband String,
    sv_event_info String,
    patient_unique_id String,
    off_panel Boolean DEFAULT FALSE
)
ENGINE = MergeTree
ORDER BY (genetic_profile_stable_id, cancer_study_identifier, variant_type, entrez_gene_id, hugo_gene_symbol, sample_unique_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample_derived
(
    sample_unique_id String,
    sample_unique_id_base64 String,
    sample_stable_id String,
    patient_unique_id String,
    patient_unique_id_base64 String,
    patient_stable_id String,
    cancer_study_identifier LowCardinality(String),
    internal_id Int32,
    patient_internal_id Int32,
    sample_type String,
    sequenced Int32,
    copy_number_segment_present Int32
)
ENGINE = MergeTree
ORDER BY (cancer_study_identifier, sample_unique_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.sample_to_gene_panel_derived
(
    sample_unique_id String,
    alteration_type LowCardinality(String),
    gene_panel_id LowCardinality(String),
    cancer_study_identifier LowCardinality(String),
    genetic_profile_id LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (gene_panel_id, alteration_type, genetic_profile_id, sample_unique_id);

-- Trimmed: the real table carries ~45 camelCase API columns.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.mutation_derived
(
    molecularProfileId String,
    sampleId String,
    sampleInternalId Int64,
    patientId String,
    entrezGeneId Int64,
    studyId String,
    proteinChange Nullable(String)
)
ENGINE = MergeTree
ORDER BY (molecularProfileId, sampleId, entrezGeneId);

-- No study column: profile_stable_id -> genetic_profile.stable_id.
CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.generic_assay_data_derived
(
    sample_unique_id String,
    patient_unique_id String,
    genetic_entity_id String,
    value String,
    generic_assay_type String,
    profile_stable_id String,
    entity_stable_id String,
    datatype String,
    patient_level Decimal(10, 0),
    profile_type String
)
ENGINE = MergeTree
ORDER BY (profile_type, entity_stable_id, patient_unique_id, sample_unique_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.generic_assay_profile_entity_derived
(
    profile_stable_id LowCardinality(String),
    entity_stable_id String
)
ENGINE = MergeTree
ORDER BY (profile_stable_id, entity_stable_id);

-- ---------------------------------------------------------------------------
-- Study-agnostic reference data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.type_of_cancer
(
    type_of_cancer_id String,
    name String,
    dedicated_color String,
    short_name Nullable(String),
    parent Nullable(String)
)
ENGINE = MergeTree
ORDER BY type_of_cancer_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.reference_genome
(
    reference_genome_id Int32,
    species String,
    name String,
    build_name String,
    genome_size Nullable(Int64),
    url String,
    release_date Nullable(DateTime)
)
ENGINE = MergeTree
ORDER BY reference_genome_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.genetic_entity
(
    id Int32,
    entity_type String,
    stable_id Nullable(String)
)
ENGINE = MergeTree
ORDER BY id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gene
(
    entrez_gene_id Int32,
    hugo_gene_symbol String,
    genetic_entity_id Int32,
    type Nullable(String)
)
ENGINE = MergeTree
ORDER BY entrez_gene_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gene_alias
(
    entrez_gene_id Int32,
    gene_alias String
)
ENGINE = MergeTree
ORDER BY (entrez_gene_id, gene_alias);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gene_panel
(
    internal_id Int32,
    stable_id String,
    description Nullable(String)
)
ENGINE = MergeTree
ORDER BY internal_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gene_panel_list
(
    internal_id Int32,
    gene_id Int32
)
ENGINE = MergeTree
ORDER BY (internal_id, gene_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.gene_panel_to_gene_derived
(
    gene_panel_id LowCardinality(String),
    gene String
)
ENGINE = MergeTree
ORDER BY gene_panel_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.reference_genome_gene
(
    entrez_gene_id Int32,
    reference_genome_id Int32,
    chr String,
    cytoband String,
    start Int64,
    end Int64
)
ENGINE = MergeTree
ORDER BY (entrez_gene_id, reference_genome_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.info
(
    db_schema_version String,
    geneset_version String,
    derived_table_schema_version String
)
ENGINE = MergeTree
ORDER BY tuple();

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.cosmic_mutation
(
    cosmic_mutation_id String,
    entrez_gene_id Int32,
    protein_change String,
    count Int32
)
ENGINE = MergeTree
ORDER BY cosmic_mutation_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.geneset
(
    id Int32,
    genetic_entity_id Int32,
    external_id String,
    name String,
    description Nullable(String),
    ref_link String
)
ENGINE = MergeTree
ORDER BY id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.geneset_gene
(
    geneset_id Int32,
    entrez_gene_id Int32
)
ENGINE = MergeTree
ORDER BY (geneset_id, entrez_gene_id);

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.geneset_hierarchy_node
(
    node_id Int64,
    node_name Nullable(String),
    parent_id Nullable(Int64)
)
ENGINE = MergeTree
ORDER BY node_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.geneset_hierarchy_leaf
(
    node_id Int64,
    geneset_id Nullable(Int32)
)
ENGINE = MergeTree
ORDER BY node_id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.generic_entity_properties
(
    id Int32,
    genetic_entity_id Int32,
    name String,
    value String
)
ENGINE = MergeTree
ORDER BY id;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.generic_assay_meta_derived
(
    entity_stable_id String,
    entity_type LowCardinality(String),
    properties Map(String, String)
)
ENGINE = MergeTree
ORDER BY entity_stable_id;

-- ---------------------------------------------------------------------------
-- cBioPortal auth tables: deliberately denied to the restricted role
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.users
(
    email String,
    name String,
    enabled UInt8
)
ENGINE = MergeTree
ORDER BY email;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.data_access_tokens
(
    token String,
    username String,
    expiration DateTime,
    creation DateTime
)
ENGINE = MergeTree
ORDER BY token;

CREATE TABLE IF NOT EXISTS cbioportal_authz_e2e.authorities
(
    email String,
    authority String
)
ENGINE = MergeTree
ORDER BY email;

-- ---------------------------------------------------------------------------
-- Data
-- ---------------------------------------------------------------------------

INSERT INTO cbioportal_authz_e2e.cancer_study VALUES
    (1, 'study_alpha', 'brca', 'Allowed Alpha Study', 'Visible to alpha researchers', 0, NULL, NULL, NULL, 0, NULL, 1),
    (2, 'study_beta', 'luad', 'Restricted Beta Study', 'Visible to beta researchers', 0, NULL, NULL, NULL, 0, NULL, 1);

INSERT INTO cbioportal_authz_e2e.cancer_study_tags VALUES
    (1, '{"tag": "alpha"}'),
    (2, '{"tag": "beta"}');

INSERT INTO cbioportal_authz_e2e.patient VALUES
    (1, 'P1', 1),
    (2, 'P2', 1),
    (3, 'P3', 2);

INSERT INTO cbioportal_authz_e2e.sample VALUES
    (1, 'S1', 'Primary Solid Tumor', 1),
    (2, 'S2', 'Primary Solid Tumor', 2),
    (3, 'S3', 'Primary Solid Tumor', 3);

INSERT INTO cbioportal_authz_e2e.sample_list VALUES
    (1, 'study_alpha_all', 'all_cases_in_study', 1, 'All samples', NULL),
    (2, 'study_beta_all', 'all_cases_in_study', 2, 'All samples', NULL);

INSERT INTO cbioportal_authz_e2e.sample_list_list VALUES
    (1, 1),
    (1, 2),
    (2, 3);

INSERT INTO cbioportal_authz_e2e.clinical_patient VALUES
    (1, 'OS_STATUS', '0:LIVING'),
    (2, 'OS_STATUS', '1:DECEASED'),
    (3, 'OS_STATUS', '0:LIVING');

INSERT INTO cbioportal_authz_e2e.clinical_sample VALUES
    (1, 'CANCER_TYPE', 'Breast Cancer'),
    (2, 'CANCER_TYPE', 'Breast Cancer'),
    (3, 'CANCER_TYPE', 'Non-Small Cell Lung Cancer');

INSERT INTO cbioportal_authz_e2e.clinical_attribute_meta VALUES
    ('CANCER_TYPE', 'Cancer Type', 'Cancer type', 'STRING', 0, '1', 1),
    ('CANCER_TYPE', 'Cancer Type', 'Cancer type', 'STRING', 0, '1', 2);

INSERT INTO cbioportal_authz_e2e.clinical_event VALUES
    (1001, 1, 10, 100, 'Treatment'),
    (1002, 3, 20, 200, 'Treatment');

INSERT INTO cbioportal_authz_e2e.clinical_event_data VALUES
    (1001, 'AGENT', 'Tamoxifen'),
    (1002, 'AGENT', 'Cisplatin');

INSERT INTO cbioportal_authz_e2e.genetic_profile VALUES
    (11, 'study_alpha_mutations', 1, 'MUTATION_EXTENDED', NULL, 'MAF', 'Mutations', '', 1, 0, '', NULL),
    (12, 'study_alpha_gistic', 1, 'COPY_NUMBER_ALTERATION', NULL, 'DISCRETE', 'CNA', '', 1, 0, '', NULL),
    (13, 'study_alpha_mrna', 1, 'MRNA_EXPRESSION', NULL, 'Z-SCORE', 'mRNA', '', 1, 0, '', NULL),
    (14, 'study_alpha_structural_variants', 1, 'STRUCTURAL_VARIANT', NULL, 'SV', 'SV', '', 1, 0, '', NULL),
    (15, 'study_alpha_treatment_ic50', 1, 'GENERIC_ASSAY', 'TREATMENT_RESPONSE', 'LIMIT-VALUE', 'IC50', '', 1, 0, '', 0),
    (21, 'study_beta_mutations', 2, 'MUTATION_EXTENDED', NULL, 'MAF', 'Mutations', '', 1, 0, '', NULL),
    (22, 'study_beta_gistic', 2, 'COPY_NUMBER_ALTERATION', NULL, 'DISCRETE', 'CNA', '', 1, 0, '', NULL),
    (23, 'study_beta_mrna', 2, 'MRNA_EXPRESSION', NULL, 'Z-SCORE', 'mRNA', '', 1, 0, '', NULL),
    (24, 'study_beta_structural_variants', 2, 'STRUCTURAL_VARIANT', NULL, 'SV', 'SV', '', 1, 0, '', NULL),
    (25, 'study_beta_treatment_ic50', 2, 'GENERIC_ASSAY', 'TREATMENT_RESPONSE', 'LIMIT-VALUE', 'IC50', '', 1, 0, '', 0);

INSERT INTO cbioportal_authz_e2e.genetic_profile_link VALUES
    (11, 12, 'AGGREGATION'),
    (21, 22, 'AGGREGATION');

INSERT INTO cbioportal_authz_e2e.genetic_profile_samples VALUES
    (13, '1,2,'),
    (23, '3,');

INSERT INTO cbioportal_authz_e2e.genetic_alteration VALUES
    (13, 1, '1.5,2.5,'),
    (23, 1, '-0.5,');

INSERT INTO cbioportal_authz_e2e.sample_profile VALUES
    (1, 11, 1),
    (2, 11, 1),
    (3, 21, 1);

-- mutation_event 103 and cna_event 203 are observed in both studies, so
-- either study's users can see them.
INSERT INTO cbioportal_authz_e2e.mutation_event VALUES
    (101, 7157, '17', 7675088, 7675088, 'C', 'T', 'R175H', 'Missense_Mutation', 'SNP', 'TP53 R175 missense'),
    (102, 3845, '12', 25245350, 25245350, 'C', 'T', 'G12D', 'Missense_Mutation', 'SNP', 'KRAS G12 missense'),
    (103, 1956, '7', 55181378, 55181378, 'C', 'T', 'T790M', 'Missense_Mutation', 'SNP', 'EGFR T790 missense');

INSERT INTO cbioportal_authz_e2e.mutation VALUES
    (101, 11, 1, 7157, 'SOMATIC', 'Unknown', 10, 20, 'R175H'),
    (102, 21, 3, 3845, 'SOMATIC', 'Unknown', 11, 21, 'G12D'),
    (103, 11, 2, 1956, 'SOMATIC', 'Unknown', 5, 30, 'T790M'),
    (103, 21, 3, 1956, 'SOMATIC', 'Unknown', 6, 31, 'T790M');

INSERT INTO cbioportal_authz_e2e.mutation_count_by_keyword VALUES
    (11, 'TP53 R175 missense', 7157, 1, 1),
    (21, 'KRAS G12 missense', 3845, 1, 1);

INSERT INTO cbioportal_authz_e2e.alteration_driver_annotation VALUES
    (101, 11, 1, 'Putative_Driver', '', NULL, NULL),
    (102, 21, 3, 'Putative_Driver', '', NULL, NULL);

INSERT INTO cbioportal_authz_e2e.allele_specific_copy_number VALUES
    (101, 11, 1, 2, 'FACETS', 'CLONAL', 2),
    (102, 21, 3, 3, 'FACETS', 'CLONAL', 3);

INSERT INTO cbioportal_authz_e2e.cna_event VALUES
    (201, 1956, 2),
    (202, 3845, -2),
    (203, 7157, -2);

INSERT INTO cbioportal_authz_e2e.sample_cna_event VALUES
    (201, 1, 12, ''),
    (202, 3, 22, ''),
    (203, 2, 12, ''),
    (203, 3, 22, '');

INSERT INTO cbioportal_authz_e2e.structural_variant VALUES
    (301, 14, 1, 1956, 7157, 'EGFR-TP53 fusion', 'SOMATIC'),
    (302, 24, 3, 3845, 1956, 'KRAS-EGFR fusion', 'SOMATIC');

INSERT INTO cbioportal_authz_e2e.copy_number_seg VALUES
    (401, 1, 1, '17', 1, 1000, 10, 0.5),
    (402, 2, 3, '12', 1, 1000, 10, -0.5);

INSERT INTO cbioportal_authz_e2e.copy_number_seg_file VALUES
    (501, 1, 'hg19', 'alpha segments', 'alpha.seg'),
    (502, 2, 'hg19', 'beta segments', 'beta.seg');

INSERT INTO cbioportal_authz_e2e.gistic VALUES
    (601, 1, 7, '7p11.2', 1, 2, 0.01, 1),
    (602, 2, 12, '12p12.1', 1, 2, 0.01, 0);

INSERT INTO cbioportal_authz_e2e.gistic_to_gene VALUES
    (601, 1956),
    (602, 3845);

INSERT INTO cbioportal_authz_e2e.mut_sig VALUES
    (1, 7157, 1, 1000, 1, 0.001, 0.01),
    (2, 3845, 1, 1000, 1, 0.001, 0.01);

INSERT INTO cbioportal_authz_e2e.resource_definition VALUES
    ('PATHOLOGY_SLIDE', 'Slide Microscopy', '', 'SAMPLE', 0, 1, 1, ''),
    ('PATIENT_NOTES', 'Patient Notes', '', 'PATIENT', 0, 1, 1, ''),
    ('STUDY_SITE', 'Study Site', '', 'STUDY', 0, 1, 1, ''),
    ('PATHOLOGY_SLIDE', 'Slide Microscopy', '', 'SAMPLE', 0, 1, 2, ''),
    ('PATIENT_NOTES', 'Patient Notes', '', 'PATIENT', 0, 1, 2, ''),
    ('STUDY_SITE', 'Study Site', '', 'STUDY', 0, 1, 2, '');

INSERT INTO cbioportal_authz_e2e.resource_sample VALUES
    (1, 'PATHOLOGY_SLIDE', 'https://example.org/alpha/S1'),
    (3, 'PATHOLOGY_SLIDE', 'https://example.org/beta/S3');

INSERT INTO cbioportal_authz_e2e.resource_patient VALUES
    (1, 'PATIENT_NOTES', 'https://example.org/alpha/P1'),
    (3, 'PATIENT_NOTES', 'https://example.org/beta/P3');

INSERT INTO cbioportal_authz_e2e.resource_study VALUES
    (1, 'STUDY_SITE', 'https://example.org/alpha'),
    (2, 'STUDY_SITE', 'https://example.org/beta');

INSERT INTO cbioportal_authz_e2e.clinical_data_derived VALUES
    (1, 'study_alpha_S1', 'study_alpha_P1', 'CANCER_TYPE', 'Breast Cancer', 'study_alpha', 'sample'),
    (2, 'study_alpha_S2', 'study_alpha_P2', 'CANCER_TYPE', 'Breast Cancer', 'study_alpha', 'sample'),
    (3, 'study_beta_S3', 'study_beta_P3', 'CANCER_TYPE', 'Non-Small Cell Lung Cancer', 'study_beta', 'sample');

INSERT INTO cbioportal_authz_e2e.clinical_event_derived VALUES
    (1001, 1, 'P1', 10, 100, 'Treatment', 'study_alpha'),
    (1002, 3, 'P3', 20, 200, 'Treatment', 'study_beta');

INSERT INTO cbioportal_authz_e2e.clinical_event_data_derived VALUES
    ('study_alpha_P1', 'AGENT', 'Tamoxifen', 10, 100, 'Treatment', 'study_alpha'),
    ('study_beta_P3', 'AGENT', 'Cisplatin', 20, 200, 'Treatment', 'study_beta');

INSERT INTO cbioportal_authz_e2e.genetic_alteration_derived VALUES
    ('study_alpha_S1', 'study_alpha', 'TP53', 'mrna', '1.5'),
    ('study_alpha_S2', 'study_alpha', 'TP53', 'mrna', '2.5'),
    ('study_alpha_S1', 'study_alpha', 'EGFR', 'mrna', '0.5'),
    ('study_alpha_S2', 'study_alpha', 'EGFR', 'mrna', '1.0'),
    ('study_beta_S3', 'study_beta', 'TP53', 'mrna', '-0.5'),
    ('study_beta_S3', 'study_beta', 'EGFR', 'mrna', '-1.0'),
    ('study_beta_B1', 'study_beta', 'TP53', 'mrna', '0.5'),
    ('study_beta_B1', 'study_beta', 'EGFR', 'mrna', '1.5'),
    ('study_beta_B2', 'study_beta', 'TP53', 'mrna', '1.0'),
    ('study_beta_B2', 'study_beta', 'EGFR', 'mrna', '-0.2');

INSERT INTO cbioportal_authz_e2e.genomic_event_derived
    (sample_unique_id, hugo_gene_symbol, entrez_gene_id, gene_panel_stable_id, cancer_study_identifier,
     genetic_profile_stable_id, variant_type, mutation_variant, mutation_type, mutation_status,
     driver_filter, driver_filter_annotation, driver_tiers_filter, driver_tiers_filter_annotation,
     cna_alteration, cna_cytoband, sv_event_info, patient_unique_id, off_panel)
VALUES
    ('study_alpha_S1', 'TP53', 7157, 'IMPACT468', 'study_alpha', 'study_alpha_mutations', 'mutation',
     'R175H', 'Missense_Mutation', 'SOMATIC', '', '', '', '', NULL, '', '', 'study_alpha_P1', FALSE),
    ('study_beta_S3', 'KRAS', 3845, 'IMPACT468', 'study_beta', 'study_beta_mutations', 'mutation',
     'G12D', 'Missense_Mutation', 'SOMATIC', '', '', '', '', NULL, '', '', 'study_beta_P3', FALSE);

INSERT INTO cbioportal_authz_e2e.sample_derived VALUES
    ('study_alpha_S1', '', 'S1', 'study_alpha_P1', '', 'P1', 'study_alpha', 1, 1, 'Primary Solid Tumor', 1, 0),
    ('study_alpha_S2', '', 'S2', 'study_alpha_P2', '', 'P2', 'study_alpha', 2, 2, 'Primary Solid Tumor', 1, 0),
    ('study_beta_S3', '', 'S3', 'study_beta_P3', '', 'P3', 'study_beta', 3, 3, 'Primary Solid Tumor', 1, 0);

INSERT INTO cbioportal_authz_e2e.sample_to_gene_panel_derived VALUES
    ('study_alpha_S1', 'MUTATION_EXTENDED', 'IMPACT468', 'study_alpha', 'study_alpha_mutations'),
    ('study_alpha_S2', 'MUTATION_EXTENDED', 'IMPACT468', 'study_alpha', 'study_alpha_mutations'),
    ('study_beta_S3', 'MUTATION_EXTENDED', 'IMPACT468', 'study_beta', 'study_beta_mutations');

INSERT INTO cbioportal_authz_e2e.mutation_derived VALUES
    ('study_alpha_mutations', 'S1', 1, 'P1', 7157, 'study_alpha', 'R175H'),
    ('study_beta_mutations', 'S3', 3, 'P3', 3845, 'study_beta', 'G12D');

INSERT INTO cbioportal_authz_e2e.generic_assay_data_derived VALUES
    ('study_alpha_S1', 'study_alpha_P1', '9001', '0.25', 'TREATMENT_RESPONSE', 'study_alpha_treatment_ic50',
     'Tamoxifen', 'LIMIT-VALUE', 0, 'treatment_ic50'),
    ('study_beta_S3', 'study_beta_P3', '9001', '0.75', 'TREATMENT_RESPONSE', 'study_beta_treatment_ic50',
     'Tamoxifen', 'LIMIT-VALUE', 0, 'treatment_ic50');

INSERT INTO cbioportal_authz_e2e.generic_assay_profile_entity_derived VALUES
    ('study_alpha_treatment_ic50', 'Tamoxifen'),
    ('study_beta_treatment_ic50', 'Tamoxifen');

INSERT INTO cbioportal_authz_e2e.type_of_cancer VALUES
    ('brca', 'Invasive Breast Carcinoma', 'HotPink', 'BRCA', 'breast'),
    ('luad', 'Lung Adenocarcinoma', 'Gainsboro', 'LUAD', 'lung');

INSERT INTO cbioportal_authz_e2e.reference_genome VALUES
    (1, 'human', 'hg19', 'GRCh37', 3000000000, 'http://example.org/hg19', NULL);

INSERT INTO cbioportal_authz_e2e.genetic_entity VALUES
    (1, 'GENE', NULL),
    (2, 'GENE', NULL),
    (3, 'GENE', NULL),
    (9001, 'GENERIC_ASSAY', 'Tamoxifen');

INSERT INTO cbioportal_authz_e2e.gene VALUES
    (7157, 'TP53', 1, 'protein-coding'),
    (3845, 'KRAS', 2, 'protein-coding'),
    (1956, 'EGFR', 3, 'protein-coding');

INSERT INTO cbioportal_authz_e2e.gene_alias VALUES
    (7157, 'P53');

INSERT INTO cbioportal_authz_e2e.gene_panel VALUES
    (1, 'IMPACT468', 'MSK-IMPACT 468');

INSERT INTO cbioportal_authz_e2e.gene_panel_list VALUES
    (1, 7157),
    (1, 3845),
    (1, 1956);

INSERT INTO cbioportal_authz_e2e.gene_panel_to_gene_derived VALUES
    ('IMPACT468', 'TP53'),
    ('IMPACT468', 'KRAS'),
    ('IMPACT468', 'EGFR');

INSERT INTO cbioportal_authz_e2e.reference_genome_gene VALUES
    (7157, 1, '17', '17p13.1', 7565097, 7590856);

INSERT INTO cbioportal_authz_e2e.info VALUES
    ('2.13.1', 'msigdb_v7.5.1', '1.0.0');

INSERT INTO cbioportal_authz_e2e.users VALUES
    ('alice@example.org', 'Alice', 1);

INSERT INTO cbioportal_authz_e2e.authorities VALUES
    ('alice@example.org', 'cbioportal:study_alpha');

INSERT INTO cbioportal_authz_e2e.resource VALUES
    (1, 'STUDY_SITE', 1, 'STUDY', NULL, NULL, 'https://example.org/alpha', 'Study Site', NULL),
    (2, 'STUDY_SITE', 2, 'STUDY', NULL, NULL, 'https://example.org/beta', 'Study Site', NULL);

INSERT INTO cbioportal_authz_e2e.cosmic_mutation VALUES ('COSM10648', 7157, 'R175H', 1000);
INSERT INTO cbioportal_authz_e2e.geneset VALUES (1, 1, 'HALLMARK_P53_PATHWAY', 'p53 pathway', NULL, '');
INSERT INTO cbioportal_authz_e2e.geneset_gene VALUES (1, 7157);
INSERT INTO cbioportal_authz_e2e.geneset_hierarchy_node VALUES (1, 'Hallmark', NULL);
INSERT INTO cbioportal_authz_e2e.geneset_hierarchy_leaf VALUES (1, 1);
INSERT INTO cbioportal_authz_e2e.generic_entity_properties VALUES (1, 9001, 'NAME', 'Tamoxifen');
INSERT INTO cbioportal_authz_e2e.generic_assay_meta_derived VALUES ('Tamoxifen', 'GENERIC_ASSAY', map('NAME', 'Tamoxifen'));
INSERT INTO cbioportal_authz_e2e.data_access_tokens VALUES ('secret-token', 'alice@example.org', now(), now());

-- Bulk cohort: 60 synthetic samples per study in the derived tables, so the
-- sql/4 frequency views clear their profiled_samples >= 50 threshold and
-- co_altered_genes_in_study its >= 10. Half are on IMPACT468, half WES; TP53
-- is mutated in 40, KRAS in 20 (10 with TP53, 10 without), EGFR amplified
-- in 15 and in an SV in 12. Each study has its own CANCER_TYPE, so a leaked
-- beta row is recognisable in aggregate view output.
INSERT INTO cbioportal_authz_e2e.clinical_data_derived
SELECT
    toInt32(s.3 + number),
    concat(s.1, '_B', toString(number + 1)),
    concat(s.1, '_BP', toString(number + 1)),
    'CANCER_TYPE',
    s.2,
    s.1,
    'sample'
FROM numbers(60) AS n
CROSS JOIN (
    SELECT arrayJoin([('study_alpha', 'Breast Cancer', 1000), ('study_beta', 'Non-Small Cell Lung Cancer', 2000)]) AS s
) AS ss;

INSERT INTO cbioportal_authz_e2e.sample_to_gene_panel_derived
SELECT
    concat(s, '_B', toString(number + 1)),
    a.1,
    if(number < 30, 'IMPACT468', 'WES'),
    s,
    concat(s, a.2)
FROM numbers(60) AS n
CROSS JOIN (SELECT arrayJoin(['study_alpha', 'study_beta']) AS s) AS ss
CROSS JOIN (
    SELECT arrayJoin([
        ('MUTATION_EXTENDED', '_mutations'),
        ('COPY_NUMBER_ALTERATION', '_gistic'),
        ('STRUCTURAL_VARIANT', '_structural_variants')
    ]) AS a
) AS aa;

INSERT INTO cbioportal_authz_e2e.genomic_event_derived
    (sample_unique_id, hugo_gene_symbol, entrez_gene_id, gene_panel_stable_id, cancer_study_identifier,
     genetic_profile_stable_id, variant_type, mutation_variant, mutation_type, mutation_status,
     driver_filter, driver_filter_annotation, driver_tiers_filter, driver_tiers_filter_annotation,
     cna_alteration, cna_cytoband, sv_event_info, patient_unique_id, off_panel)
SELECT
    concat(s, '_B', toString(number + 1)),
    e.1,
    e.2,
    if(number < 30, 'IMPACT468', 'WES'),
    s,
    concat(s, e.4),
    e.3,
    e.5,
    'Missense_Mutation',
    'SOMATIC',
    '', '', '', '',
    e.6,
    '',
    '',
    concat(s, '_BP', toString(number + 1)),
    FALSE
FROM numbers(60) AS n
CROSS JOIN (SELECT arrayJoin(['study_alpha', 'study_beta']) AS s) AS ss
CROSS JOIN (
    SELECT arrayJoin([
        ('TP53', 7157, 'mutation', '_mutations', 'R175H', CAST(NULL AS Nullable(Int8)), 40, 0),
        ('KRAS', 3845, 'mutation', '_mutations', 'G12D', CAST(NULL AS Nullable(Int8)), 20, 30),
        ('EGFR', 1956, 'cna', '_gistic', '', CAST(2 AS Nullable(Int8)), 15, 0),
        ('EGFR', 1956, 'structural_variant', '_structural_variants', 'EGFR-TP53', CAST(NULL AS Nullable(Int8)), 12, 0)
    ]) AS e
) AS ee
WHERE number >= e.8 AND number < e.8 + e.7;

INSERT INTO cbioportal_authz_e2e.genetic_alteration_derived
SELECT concat(s, '_B', toString(number + 1)), s, 'EGFR', 'gistic', if(number < 15, '2', '0')
FROM numbers(60) AS n
CROSS JOIN (SELECT arrayJoin(['study_alpha', 'study_beta']) AS s) AS ss;

-- ---------------------------------------------------------------------------
-- Identities
-- ---------------------------------------------------------------------------

-- The MCP's runtime user. The role, its database-wide SELECT grant and every
-- row policy come from sql/study-authz/0-row-policies.sql (applied by
-- init-apply-repo-sql.sh); the role is created here only so it can be
-- granted before that file runs.
CREATE USER IF NOT EXISTS mcp_authz IDENTIFIED WITH plaintext_password BY 'mcp_authz_pw';
CREATE ROLE IF NOT EXISTS cbioportal_mcp_study_restricted;
GRANT cbioportal_mcp_study_restricted TO mcp_authz;
SET DEFAULT ROLE cbioportal_mcp_study_restricted TO mcp_authz;

-- Stand-in for a real deployment's CLICKHOUSE_ADMIN_USER (see
-- scripts/apply_sql.sh), used only by scripts/verify_row_policy_coverage.py.
-- Deliberately NOT granted SELECT on cbioportal_authz_e2e's row data - only
-- metadata visibility (SHOW TABLES / SHOW DICTIONARIES) plus
-- system.row_policies / system.dictionaries, which is enough to enumerate tables, views and their
-- policy coverage without this identity being able to read a row of study
-- data itself.
CREATE USER IF NOT EXISTS local_e2e_admin IDENTIFIED WITH plaintext_password BY 'local_e2e_admin_pw';
GRANT SHOW TABLES, SHOW DICTIONARIES ON cbioportal_authz_e2e.* TO local_e2e_admin;
GRANT SELECT ON system.row_policies TO local_e2e_admin;
GRANT SELECT ON system.dictionaries TO local_e2e_admin;
