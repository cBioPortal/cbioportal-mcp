# Sample and Study Filtering Guide

## Overview
Proper filtering is essential for meaningful cBioPortal analysis. This guide covers filtering by studies, sample types, and other criteria.

## Study-Level Filtering

### 1. Always Filter by Study
Every query should specify a study to ensure consistent results:

```sql
-- Always include study filtering
SELECT *
FROM your_table
WHERE
    cancer_study_identifier = 'your_study_id'
    -- Additional filters...
```

### 2. Find Available Studies

```sql
-- Discover available studies
SELECT
    cancer_study_identifier,
    name,
    description,
    type_of_cancer_id
FROM cancer_study
ORDER BY cancer_study_identifier;
```

### 3. Study Information

```sql
-- Get detailed study information
SELECT
    cs.cancer_study_identifier,
    cs.name as study_name,
    cs.description,
    cs.sample_count,
    COUNT(DISTINCT p.internal_id) as patient_count
FROM cancer_study cs
LEFT JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
WHERE
    cs.cancer_study_identifier = 'your_study_id'
GROUP BY cs.cancer_study_identifier, cs.name, cs.description, cs.sample_count;
```

### 4. Find Studies by Available Data Types

Use this when the user asks *"which studies have mutation and copy-number data for X"*, *"studies with expression for Y"*, *"is there any imaging data"*, or any *"studies with Z data"* question. `cancer_study` has one precomputed sample count per data type — filter on them. No join and no schema exploration needed. These are the same counts as the portal's study list and its "Data type" filter.

**Canonical query — studies with ALL requested data types for a cancer type:**

```sql
SELECT
    cancer_study_identifier,
    name,
    sample_count,
    mutation_sample_count,
    cna_sample_count
FROM cancer_study
WHERE type_of_cancer_id = 'luad'          -- ← OncoTree code from search_oncotree
  AND mutation_sample_count > 0           -- ← one condition per required data type
  AND cna_sample_count > 0
ORDER BY sample_count DESC;
```

**Data type → column** (a count of 0 means the study doesn't have that data):

| Data type in the user's question | Column (portal "Data type" filter label) |
|---|---|
| samples in the study | `sample_count` |
| mutation / mutations | `mutation_sample_count` ("Mutations") |
| copy-number / CNA / amplification / deletion | `cna_sample_count` ("CNA") |
| structural variant / fusion | `structural_variant_sample_count` |
| RNA / mRNA / gene expression (any platform) | `mrna_expression_sample_count` — use this for "which studies have expression data" |
| specifically the portal's "RNA-Seq" filter | `rna_seq_sample_count` ("RNA-Seq") — counts only the `<study>_rna_seq_v2_mrna` list; 0 for many GDC and iAtlas studies that do have RNA-Seq |
| specifically the portal's "RNA (microarray)" filter | `mrna_microarray_sample_count` ("RNA (microarray)") — counts only the `<study>_mrna` list |
| microRNA | `mirna_sample_count` ("miRNA") |
| protein / RPPA | `rppa_sample_count` ("RPPA") |
| mass-spectrometry proteomics | `mass_spectrometry_sample_count` ("Protein Mass-Spectrometry") |
| treatment | `treatment_patient_count` ("Treatment") — counts **patients**, not samples |
| imaging / pathology slides / other linked resources | `resource_sample_counts['<display name>'] > 0`, e.g. `'Slide Microscopy'`, `'Computed Tomography'`, `'Magnetic Resonance'`, `'H&E Slide'`; list what exists with `SELECT DISTINCT arrayJoin(mapKeys(resource_sample_counts)) FROM cancer_study` |

**Rules:**

- **Resolve the cancer type first** via `search_oncotree(term)`; use the returned `type_of_cancer_id` (lowercase) in the WHERE clause. Don't `ILIKE '%lung%'`.
- **ALL vs ANY:** AND the conditions for "study has all of these types"; OR them for "at least one".
- Report the counts, not just the study names: "TCGA LUAD PanCancer: 566 samples with mutations, 511 with CNA".
- Do NOT `clickhouse_list_tables` or `clickhouse_list_table_columns` first — this query is the schema you need.
- Do NOT run per-study probing queries. One query returns the whole list.

**Worked example — "Which cBioPortal studies include lung adenocarcinoma samples with mutation and copy-number data?"**

1. `search_oncotree("lung adenocarcinoma")` → `LUAD`
2. Run the canonical query above with `type_of_cancer_id = 'luad'`
3. Return the list of studies with their sample counts, and use `list_studies(search="<id>")` (or the navigator) only if the user then wants details on a specific one

## Sample Type Filtering

### 1. Common Sample Types
- **Primary**: Primary tumor samples
- **Metastasis**: Metastatic tumor samples
- **Recurrence**: Recurrent tumor samples
- **Blood**: Blood/liquid biopsy samples
- **Normal**: Normal tissue samples

### 2. Filter by Sample Type

```sql
-- Filter samples by type using clinical_data_derived
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name = 'SAMPLE_TYPE'
    AND attribute_value = 'Primary';
```

### 3. Sample Type Distribution

```sql
-- See sample type distribution in a study
SELECT
    attribute_value as sample_type,
    COUNT(DISTINCT sample_unique_id) as sample_count,
    COUNT(DISTINCT patient_unique_id) as patient_count
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name = 'SAMPLE_TYPE'
GROUP BY attribute_value
ORDER BY sample_count DESC;
```

## Cancer Type Filtering

### 1. Broad vs Detailed Cancer Types
- **CANCER_TYPE**: Broad categories (e.g., "Breast Cancer", "Lung Cancer")
- **CANCER_TYPE_DETAILED**: Specific subtypes (e.g., "Invasive Ductal Carcinoma", "Adenocarcinoma")

### 2. Filter by Cancer Type

```sql
-- Filter by broad cancer type
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name = 'CANCER_TYPE'
    AND attribute_value = 'Breast Cancer';
```

### 2b. A Subtype Inside a Multi-Subtype Study

When the user names a subtype and the study mixes several, restrict to that subtype's samples by `ONCOTREE_CODE` (resolve the code with `search_oncotree`) instead of using the whole study. Example: adenoid cystic carcinoma in `acc_2019`, which also holds other salivary and gland tumors:

```sql
SELECT attribute_value AS oncotree_code, uniqExact(sample_unique_id) AS samples
FROM clinical_data_derived
WHERE cancer_study_identifier = 'acc_2019' AND attribute_name = 'ONCOTREE_CODE'
GROUP BY oncotree_code ORDER BY samples DESC;
-- ACYC 935, LUACC 76, ACBC 38
```

Use the matching samples as both numerator and denominator (e.g. `sample_unique_id IN (... attribute_value = 'ACYC')`), and say how many of the study's samples that is.

### 3. Cancer Type Hierarchy

```sql
-- See cancer type breakdown
SELECT
    broad.attribute_value as broad_cancer_type,
    detailed.attribute_value as detailed_cancer_type,
    COUNT(DISTINCT broad.sample_unique_id) as sample_count
FROM clinical_data_derived broad
JOIN clinical_data_derived detailed
    ON broad.sample_unique_id = detailed.sample_unique_id
WHERE
    broad.cancer_study_identifier = 'your_study_id'
    AND broad.attribute_name = 'CANCER_TYPE'
    AND detailed.attribute_name = 'CANCER_TYPE_DETAILED'
GROUP BY broad.attribute_value, detailed.attribute_value
ORDER BY broad.attribute_value, sample_count DESC;
```

## Multi-Criteria Filtering

### 1. Combine Multiple Filters

```sql
-- Filter by multiple criteria
WITH filtered_samples AS (
    SELECT DISTINCT sample_unique_id, patient_unique_id
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'your_study_id'
        AND (
            (attribute_name = 'SAMPLE_TYPE' AND attribute_value = 'Primary')
            OR (attribute_name = 'CANCER_TYPE' AND attribute_value = 'Breast Cancer')
        )
    GROUP BY sample_unique_id, patient_unique_id
    HAVING COUNT(DISTINCT attribute_name) = 2  -- Must match both criteria
)
SELECT COUNT(*) as filtered_sample_count
FROM filtered_samples;
```

### 2. Age and Gender Filtering

```sql
-- Filter by patient demographics
WITH patient_filters AS (
    SELECT DISTINCT
        patient_unique_id,
        MAX(CASE WHEN attribute_name = 'SEX' THEN attribute_value END) as sex,
        MAX(CASE WHEN attribute_name = 'AGE' THEN CAST(attribute_value AS Float64) END) as age
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'your_study_id'
        AND attribute_name IN ('SEX', 'AGE')
    GROUP BY patient_unique_id
)
SELECT
    patient_unique_id,
    sex,
    age
FROM patient_filters
WHERE
    sex = 'Female'
    AND age BETWEEN 40 AND 70;
```

## Quality Filtering

### 1. Sequencing Quality Filters

```sql
-- Filter by sequencing platform or quality metrics
SELECT DISTINCT
    sample_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name = 'PLATFORM'
    AND attribute_value LIKE '%Illumina%';
```

### 2. Tumor Purity Filtering

```sql
-- Filter by tumor purity
SELECT DISTINCT
    sample_unique_id,
    CAST(attribute_value AS Float64) as tumor_purity
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name = 'TUMOR_PURITY'
    AND CAST(attribute_value AS Float64) >= 0.3;  -- >= 30% purity
```

## Advanced Filtering Patterns

### 1. Exclude Certain Samples

```sql
-- Exclude specific sample types
SELECT DISTINCT sample_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND sample_unique_id NOT IN (
        SELECT sample_unique_id
        FROM clinical_data_derived
        WHERE
            cancer_study_identifier = 'your_study_id'
            AND attribute_name = 'SAMPLE_TYPE'
            AND attribute_value IN ('Normal', 'Blood')
    );
```

### 2. Filter by Data Availability

```sql
-- Only include samples with mutation data
SELECT DISTINCT
    cd.sample_unique_id,
    cd.patient_unique_id
FROM clinical_data_derived cd
WHERE
    cd.cancer_study_identifier = 'your_study_id'
    AND EXISTS (
        SELECT 1
        FROM genomic_event_derived ged
        WHERE
            ged.sample_unique_id = cd.sample_unique_id
            AND ged.variant_type = 'mutation'
    );
```

## Best Practices

1. **Always specify study identifier** - Never query across all studies unless intended
2. **Check available values first** - Use DISTINCT queries to see available filter options
3. **Use clinical_data_derived for filtering** - It's optimized and easier than joining raw tables
4. **Handle NULL values** - Clinical data may have missing values
5. **Document your filters** - Complex filters should be well-commented
6. **Test filter logic** - Verify your filters return expected sample counts
7. **Consider sample vs patient level** - Know whether you're filtering samples or patients
