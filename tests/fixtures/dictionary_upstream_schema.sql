-- Verbatim table excerpts from cBioPortal upstream ClickHouse DDL.
-- https://raw.githubusercontent.com/cBioPortal/cbioportal/5677496c44def7693ffd5c9634d3c8ce3aa4b7c2/src/main/resources/db-scripts/clickhouse/init/schema.sql
-- Refresh from upstream when its schema changes; do not tailor to dictionary SQL.

CREATE TABLE gene (
    `entrez_gene_id` Int64,
    `hugo_gene_symbol` String,
    `genetic_entity_id` Int64,
    `type` Nullable(String)
) ENGINE = MergeTree ORDER BY (entrez_gene_id);

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

CREATE TABLE sample_list (
    `list_id` Int64,
    `stable_id` String,
    `category` String,
    `cancer_study_id` Int64,
    `name` String,
    `description` Nullable(String)
) ENGINE = MergeTree ORDER BY (list_id);

CREATE TABLE sample_list_list (
    `list_id` Int64,
    `sample_id` Int64
) ENGINE = MergeTree ORDER BY (list_id, sample_id);
