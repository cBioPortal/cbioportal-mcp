-- ============================================================================
-- cBioPortal Database Column Comments for LLM Agents
-- ============================================================================
-- This script adds/updates column comments to help LLM agents understand
-- the database schema and avoid common query mistakes.
--
-- Run this AFTER cleanup-for-llm.sql to ensure removed columns don't cause errors.
-- ============================================================================

-- ============================================================================
-- sample table
-- ============================================================================

-- NOTE: If cleanup-for-llm.sql was run, sample_type column no longer exists.
-- If you did NOT run cleanup, this comment warns agents about the column:
-- ALTER TABLE sample MODIFY COLUMN sample_type String
--   COMMENT 'DEPRECATED: Contains generic values like "Primary Solid Tumor" for ALL tumor samples. DO NOT use for primary/metastatic filtering! Use clinical_data_derived with attribute_name="SAMPLE_TYPE" instead.';

ALTER TABLE sample MODIFY COLUMN internal_id Int32
  COMMENT 'Primary key. Unique internal identifier for the sample.';

ALTER TABLE sample MODIFY COLUMN stable_id String
  COMMENT 'Stable identifier for the sample within the study. Combined with cancer_study_identifier forms sample_unique_id.';

ALTER TABLE sample MODIFY COLUMN patient_id Int32
  COMMENT 'Foreign key to patient.internal_id. Links sample to its patient.';

-- ============================================================================
-- patient table
-- ============================================================================

ALTER TABLE patient MODIFY COLUMN internal_id Int32
  COMMENT 'Primary key. Unique internal identifier for the patient.';

ALTER TABLE patient MODIFY COLUMN stable_id String
  COMMENT 'Stable patient identifier within the study. Combined with cancer_study_identifier forms patient_unique_id.';

ALTER TABLE patient MODIFY COLUMN cancer_study_id Int64
  COMMENT 'Foreign key to cancer_study.cancer_study_id. Links patient to their study.';

-- ============================================================================
-- clinical_data_derived table (key table for clinical queries)
-- ============================================================================

ALTER TABLE clinical_data_derived MODIFY COLUMN sample_unique_id String
  COMMENT 'Globally unique sample ID: cancer_study_identifier + "_" + sample.stable_id. Empty for patient-level attributes. Use this for sample filtering and joins.';

ALTER TABLE clinical_data_derived MODIFY COLUMN patient_unique_id String
  COMMENT 'Globally unique patient ID: cancer_study_identifier + "_" + patient.stable_id. Present for both sample and patient-level attributes.';

ALTER TABLE clinical_data_derived MODIFY COLUMN attribute_name LowCardinality(String)
  COMMENT 'Clinical attribute name (e.g., SAMPLE_TYPE, CANCER_TYPE, AGE, OS_MONTHS). Use with attribute_value for filtering.';

ALTER TABLE clinical_data_derived MODIFY COLUMN attribute_value String
  COMMENT 'Value of the clinical attribute. For SAMPLE_TYPE: Primary, Metastasis, Local Recurrence, Unknown. Cast to Float64 for numeric comparisons.';

ALTER TABLE clinical_data_derived MODIFY COLUMN type LowCardinality(String)
  COMMENT 'Data level: "sample" for sample-level attributes (e.g., SAMPLE_TYPE), "patient" for patient-level attributes (e.g., AGE, OS_MONTHS).';

-- ============================================================================
-- genomic_event_derived table (key table for mutation/CNA queries)
-- ============================================================================

ALTER TABLE genomic_event_derived MODIFY COLUMN variant_type String
  COMMENT 'Type of genomic event: "mutation" for SNVs/indels, "cna" for copy number alterations, "structural_variant" for SVs. Always filter by this.';

ALTER TABLE genomic_event_derived MODIFY COLUMN hugo_gene_symbol String
  COMMENT 'HUGO gene symbol (e.g., TP53, KRAS, BRAF). Use for gene-specific queries.';

ALTER TABLE genomic_event_derived MODIFY COLUMN mutation_status String
  COMMENT 'For mutations: Somatic, Germline, UNKNOWN, or UNCALLED. Filter mutation_status != "UNCALLED" to exclude uncertain calls. Include all other statuses.';

ALTER TABLE genomic_event_derived MODIFY COLUMN off_panel UInt8
  COMMENT 'Boolean: 1 = mutation outside gene panel coverage (off-panel), 0 = within panel (on-panel). Filter off_panel = 0 for reliable frequency calculations.';

ALTER TABLE genomic_event_derived MODIFY COLUMN cna_alteration Int8
  COMMENT 'Copy number alteration: -2 = deep deletion (HOMDEL), -1 = shallow deletion, 0 = diploid, 1 = gain, 2 = amplification (AMP). NULL for non-CNA events.';

ALTER TABLE genomic_event_derived MODIFY COLUMN mutation_variant String
  COMMENT 'Protein change notation (e.g., p.V600E, p.R175H). Use for specific variant queries. "NA" for non-mutation events.';

-- ============================================================================
-- cancer_study table
-- ============================================================================

ALTER TABLE cancer_study MODIFY COLUMN cancer_study_identifier String
  COMMENT 'Stable string identifier for the study (e.g., "msk_chord_2024", "brca_tcga"). Use this for filtering, not cancer_study_id.';

ALTER TABLE cancer_study MODIFY COLUMN cancer_study_id Int64
  COMMENT 'Internal numeric ID. Prefer cancer_study_identifier for queries as it is more readable and stable.';

ALTER TABLE cancer_study MODIFY COLUMN name String
  COMMENT 'Full descriptive name of the study (e.g., "MSK-CHORD (MSK, Nature 2024)").';
