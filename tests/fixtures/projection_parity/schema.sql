-- Schema for tests/test_projection_parity_live.py.
--
-- Copied verbatim from cBioPortal's ClickHouse DDL
-- (src/main/resources/db-scripts/clickhouse/clickhouse.sql for the *_derived
-- tables, init/schema.sql for the base tables the sql/4 views join), so the
-- parity test exercises the real sort keys the projections interact with.
-- cancer_study_query_preferences is created by sql/3 in the real pipeline;
-- its DDL is repeated here without the pattern-detected INSERTs.

CREATE TABLE sample_to_gene_panel_derived
(
    sample_unique_id String,
    alteration_type LowCardinality(String),
    gene_panel_id LowCardinality(String),
    cancer_study_identifier LowCardinality(String),
    genetic_profile_id LowCardinality(String)
) ENGINE = MergeTree()
ORDER BY (gene_panel_id, alteration_type, genetic_profile_id, sample_unique_id);

CREATE TABLE sample_derived
(
    sample_unique_id            String,
    sample_unique_id_base64     String,
    sample_stable_id            String,
    patient_unique_id           String,
    patient_unique_id_base64    String,
    patient_stable_id           String,
    cancer_study_identifier     LowCardinality(String),
    internal_id                 Int,
    -- fields below are needed for the SUMMARY projection
    patient_internal_id         Int,
    sample_type                 String,
    -- fields below are needed for the DETAILED projection
    sequenced                   Int,
    copy_number_segment_present Int
)
    ENGINE = MergeTree
        ORDER BY (cancer_study_identifier, sample_unique_id);

CREATE TABLE genomic_event_derived
(
    sample_unique_id          String,
    hugo_gene_symbol          String,
    entrez_gene_id            Int32,
    gene_panel_stable_id      LowCardinality(String),
    cancer_study_identifier   LowCardinality(String),
    genetic_profile_stable_id LowCardinality(String),
    variant_type              LowCardinality(String),
    mutation_variant          String,
    mutation_type             LowCardinality(String),
    mutation_status           LowCardinality(String),
    driver_filter             LowCardinality(String),
    driver_filter_annotation  String,
    driver_tiers_filter       LowCardinality(String),
    driver_tiers_filter_annotation String,
    cna_alteration            Nullable(Int8),
    cna_cytoband              String,
    sv_event_info             String,
    patient_unique_id         String,
    off_panel                 Boolean DEFAULT FALSE
) ENGINE = MergeTree
      ORDER BY (genetic_profile_stable_id, cancer_study_identifier, variant_type, entrez_gene_id, hugo_gene_symbol, sample_unique_id);

CREATE TABLE clinical_data_derived
(
    internal_id Int,
    sample_unique_id String,
    patient_unique_id String,
    attribute_name LowCardinality(String),
    attribute_value String,
    cancer_study_identifier LowCardinality(String),
    type LowCardinality(String)
)
    ENGINE=MergeTree
        ORDER BY (cancer_study_identifier, type, attribute_name, sample_unique_id);

CREATE TABLE genetic_alteration_derived
(
    sample_unique_id String,
    cancer_study_identifier LowCardinality(String),
    hugo_gene_symbol String,
    profile_type LowCardinality(String),
    alteration_value Nullable(String)
    )
    ENGINE = MergeTree()
    ORDER BY (cancer_study_identifier, hugo_gene_symbol, profile_type, sample_unique_id);

CREATE TABLE cancer_study (
    `cancer_study_id` Int64,
    `cancer_study_identifier` Nullable(String),
    `type_of_cancer_id` String,
    `name` String,
    `description` String,
    `public` Int32,
    `pmid` Nullable(String),
    `citation` Nullable(String),
    `groups` Nullable(String),
    `status` Nullable(Int64),
    `import_date` Nullable(DateTime64(6)),
    `reference_genome_id` Nullable(Int64)
) ENGINE = MergeTree ORDER BY (cancer_study_id);

CREATE TABLE genetic_profile (
    `genetic_profile_id` Int64,
    `stable_id` String,
    `cancer_study_id` Int64,
    `genetic_alteration_type` String,
    `generic_assay_type` Nullable(String),
    `datatype` String,
    `name` String,
    `description` Nullable(String),
    `show_profile_in_analysis_tab` Int32,
    `pivot_threshold` Nullable(Float64),
    `sort_order` Nullable(String),
    `patient_level` Nullable(Int32)
) ENGINE = MergeTree ORDER BY (genetic_profile_id);

CREATE TABLE gene (
    `entrez_gene_id` Int64,
    `hugo_gene_symbol` String,
    `genetic_entity_id` Int64,
    `type` Nullable(String)
) ENGINE = MergeTree ORDER BY (entrez_gene_id);

CREATE TABLE gene_panel (
    `internal_id` Int64,
    `stable_id` String,
    `description` Nullable(String)
) ENGINE = MergeTree ORDER BY (internal_id);

CREATE TABLE gene_panel_list (
    `internal_id` Int64,
    `gene_id` Int64
) ENGINE = MergeTree ORDER BY (internal_id, gene_id);

CREATE TABLE cancer_study_query_preferences (
    preference_name LowCardinality(String),
    cancer_study_identifier String,
    notes String
)
ENGINE = MergeTree
ORDER BY (preference_name, cancer_study_identifier);
