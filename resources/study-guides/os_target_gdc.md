# Osteosarcoma (TARGET GDC, 2025)

**Study ID:** `os_target_gdc`
**Cancer Type:** os
**Patients:** 383
**Samples:** 160

## Description
TARGET Osteosarcoma. Source data from <A HREF="https://gdc.cancer.gov">NCI GDC</A> and generated in Aug 2025 using <A HREF="https://cda.readthedocs.io/en/latest/">Cancer Data Aggregator</A>.

## Gene Panels
- **WES**: 159 samples

## Available Clinical Attributes
- CANCER_TYPE_DETAILED (159 samples)
- MUTATION_COUNT (159 samples)
- DISEASE_TYPE (159 samples)
- TMB_NONSYNONYMOUS (159 samples)
- ONCOTREE_CODE (159 samples)
- CANCER_TYPE (159 samples)
- SPECIMEN_TYPE (159 samples)
- OS_MONTHS (1 samples)
- RACE (1 samples)
- PROJECT_ID (1 samples)
- DAYS_TO_DEATH (1 samples)
- PRIMARY_SITE_PATIENT (1 samples)
- ETHNICITY (1 samples)
- AGE (1 samples)
- DAYS_TO_BIRTH (1 samples)

## Top Mutated Genes
- TP53: 32 samples
- MUC16: 16 samples
- TTN: 16 samples
- ATRX: 11 samples
- DNAH9: 10 samples
- RB1: 8 samples
- MUC4: 7 samples
- PCLO: 7 samples
- DMD: 7 samples
- CNTNAP5: 7 samples

## Sample Types

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'os_target_gdc';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'os_target_gdc'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
