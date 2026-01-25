# Clinical Data Query Guide

## Overview
Clinical data in cBioPortal is stored at both patient and sample levels. Understanding the distinction is crucial for accurate analysis.

## Data Organization

### Patient-Level vs Sample-Level Data
- **Patient-level**: Demographics, overall survival, disease stage (stored once per patient)
- **Sample-level**: Sample type, sequencing platform, purity (can have multiple per patient)

### Key Tables
- `clinical_patient`: Patient-level clinical attributes
- `clinical_sample`: Sample-level clinical attributes
- `clinical_data_derived`: Pre-joined view combining both levels
- `clinical_attribute_meta`: Metadata about available clinical attributes

## Recommended Approach: Use clinical_data_derived

The `clinical_data_derived` table is pre-joined and optimized for most queries:

```sql
-- Get clinical data for specific attributes
SELECT
    sample_unique_id,
    patient_unique_id,
    attribute_name,
    attribute_value
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name IN ('CANCER_TYPE', 'SAMPLE_TYPE', 'TMB_NONSYNONYMOUS');
```

## Clinical Attribute Discovery

### Use clinical_attribute_meta for Discovering Available Attributes
**Always start here** to see what clinical attributes are available for a specific study:

```sql
-- Discover available clinical attributes for a study
SELECT
    attr_id,
    description,
    patient_attribute,
    cancer_study_id
FROM clinical_attribute_meta
WHERE
    cancer_study_id = (
        SELECT cancer_study_id
        FROM cancer_study
        WHERE cancer_study_identifier = 'msk_chord_2024'
    )
ORDER BY patient_attribute, attr_id;
```

**Key Fields:**
- **attr_id**: matches attr_id in clinical_sample/clinical_patient tables
- **description**: human-readable description of the attribute
- **patient_attribute**: true = patient attribute, false = sample attribute
- **cancer_study_id**: links to cancer_study table (filter by study)

## Common Clinical Attributes

### Sample-Level Attributes:
- `SAMPLE_TYPE`: Primary, Metastasis, Recurrence, etc.
- `SEQUENCING_CENTER`: Where sequencing was performed
- `TUMOR_PURITY`: Estimated tumor cell percentage
- `PLATFORM`: Sequencing platform used
- `TMB_NONSYNONYMOUS`: Tumor mutational burden
- `MUTATION_COUNT`: Total mutation count per sample

### Patient-Level Attributes:
- `CANCER_TYPE`: Broad cancer category
- `CANCER_TYPE_DETAILED`: Specific cancer subtype
- `SEX`: Patient gender
- `AGE`: Age at diagnosis
- `OS_MONTHS`: Overall survival time in months
- `OS_STATUS`: Overall survival status (0:LIVING, 1:DECEASED or similar)

## Survival Analysis Queries

Survival data is stored as clinical attributes. Common patterns:

```sql
-- Get survival data for a study
SELECT 
    patient_unique_id,
    MAX(CASE WHEN attribute_name = 'OS_MONTHS' THEN toFloat64OrNull(attribute_value) END) as os_months,
    MAX(CASE WHEN attribute_name = 'OS_STATUS' THEN attribute_value END) as os_status
FROM clinical_data_derived
WHERE cancer_study_identifier = 'your_study'
GROUP BY patient_unique_id;
```

```sql
-- Compare survival between groups (e.g., mutated vs wild-type)
WITH patient_mutation AS (
    SELECT DISTINCT patient_unique_id, 1 as is_mutated
    FROM genomic_event_derived
    WHERE hugo_gene_symbol = 'TP53' AND variant_type = 'mutation'
        AND cancer_study_identifier = 'your_study'
)
SELECT 
    CASE WHEN m.is_mutated = 1 THEN 'Mutated' ELSE 'Wild-type' END as group_name,
    median(toFloat64OrNull(c.attribute_value)) as median_os_months
FROM clinical_data_derived c
LEFT JOIN patient_mutation m ON c.patient_unique_id = m.patient_unique_id
WHERE c.cancer_study_identifier = 'your_study'
    AND c.attribute_name = 'OS_MONTHS'
GROUP BY group_name;
```

### Cancer Type Selection Guidance:
**CANCER_TYPE vs CANCER_TYPE_DETAILED**: Choose based on question specificity
- **CANCER_TYPE**: broader categories like 'Non-Small Cell Lung Cancer', 'Breast Cancer'
- **CANCER_TYPE_DETAILED**: specific subtypes like 'Spindle Cell Carcinoma of the Lung', 'Invasive Ductal Carcinoma'
- **Decision**: Match the attribute to the level of detail requested in the question
- **When unsure**: start with CANCER_TYPE for broader matching

## Query Patterns

### 1. Filter Samples by Clinical Criteria

```sql
-- Get samples with specific clinical characteristics
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND (
        (attribute_name = 'CANCER_TYPE' AND attribute_value = 'Breast Cancer')
        OR (attribute_name = 'SAMPLE_TYPE' AND attribute_value = 'Primary')
    );
```

### 2. Aggregate Clinical Data

```sql
-- Count samples by cancer type
SELECT
    attribute_value as cancer_type,
    COUNT(DISTINCT sample_unique_id) as sample_count,
    COUNT(DISTINCT patient_unique_id) as patient_count
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'CANCER_TYPE'
GROUP BY attribute_value
ORDER BY sample_count DESC;
```

### 3. Patient Demographics Analysis

```sql
-- Get patient demographics summary
WITH patient_data AS (
    SELECT DISTINCT
        patient_unique_id,
        CASE WHEN attribute_name = 'SEX' THEN attribute_value END as sex,
        CASE WHEN attribute_name = 'AGE' THEN CAST(attribute_value AS Float64) END as age
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'msk_chord_2024'
        AND attribute_name IN ('SEX', 'AGE')
)
SELECT
    sex,
    COUNT(*) as patient_count,
    AVG(age) as avg_age,
    MIN(age) as min_age,
    MAX(age) as max_age
FROM patient_data
WHERE sex IS NOT NULL AND age IS NOT NULL
GROUP BY sex;
```

## Raw Table Queries (Advanced)

If you need to use raw clinical tables instead of the derived view:

### Patient-Level Query:
```sql
-- Query patient-level data directly
SELECT
    cp.patient_id,
    cp.attr_id,
    cp.attr_value
FROM cancer_study cs
JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
JOIN clinical_patient cp ON p.internal_id = cp.patient_id
WHERE
    cs.cancer_study_identifier = 'msk_chord_2024'
    AND cp.attr_id = 'CANCER_TYPE';
```

### Sample-Level Query:
```sql
-- Query sample-level data directly
SELECT
    cs_sample.internal_id as sample_id,
    cs_sample.attr_id,
    cs_sample.attr_value
FROM cancer_study cs
JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
JOIN sample s ON p.internal_id = s.patient_id
JOIN clinical_sample cs_sample ON s.internal_id = cs_sample.internal_id
WHERE
    cs.cancer_study_identifier = 'msk_chord_2024'
    AND cs_sample.attr_id = 'SAMPLE_TYPE';
```

## Treatment and Clinical Events Data

Treatment data is stored separately from clinical attributes, in the clinical events tables:

### Key Tables for Treatment Data
- `clinical_event`: Contains event records (Treatment, Diagnosis, Surgery, etc.)
- `clinical_event_data`: Contains key-value data for each event

### Query Treatment Information

```sql
-- Find most common treatments in a study
SELECT 
    ced.value as treatment_agent,
    COUNT(DISTINCT ce.patient_id) as patient_count
FROM clinical_event ce
JOIN clinical_event_data ced ON ce.clinical_event_id = ced.clinical_event_id
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'msk_chord_2024'
    AND ce.event_type IN ('Treatment', 'TREATMENT')
    AND ced.key = 'AGENT'
GROUP BY ced.value
ORDER BY patient_count DESC
LIMIT 10;
```

### Available Event Types
Common event types include:
- `Treatment` / `TREATMENT`: Drug/therapy administration
- `Diagnosis`: Diagnosis events
- `SURGERY`: Surgical procedures
- `LAB_TEST`: Laboratory test results
- `Sequencing`: Sequencing events
- `Sample acquisition`: Sample collection events
- `PATHOLOGY` / `Pathology`: Pathology reports

### Discover Event Types in a Study

```sql
-- See what event types are available
SELECT DISTINCT ce.event_type, COUNT(*) as event_count
FROM clinical_event ce
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'msk_chord_2024'
GROUP BY ce.event_type
ORDER BY event_count DESC;
```

## Best Practices

1. **Use clinical_data_derived when possible** - it's pre-optimized and easier to work with
2. **Check attribute availability first** - use clinical_attribute_meta to see what's available
3. **Handle missing values** - clinical data can have NULL or empty values
4. **Distinguish patient vs sample level** - know whether you need patient or sample-level aggregation
5. **Filter by study** - always specify cancer_study_identifier for consistent results
6. **Use clinical_event for treatment data** - treatment info is in clinical events, not clinical attributes
