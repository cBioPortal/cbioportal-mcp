# Breast Invasive Carcinoma (TCGA, PanCancer Atlas)

**Study ID:** `brca_tcga_pan_can_atlas_2018`
**Cancer Type:** brca
**Patients:** 1098
**Samples:** 1085

## Description
Breast Invasive Carcinoma TCGA PanCancer data. The original data is <a href="https://gdc.cancer.gov/about-data/publications/pancanatlas">here</a>. The publications are <a href="https://www.cell.com/pb-assets/consortium/pancanceratlas/pancani3/index.html">here</a>.

## Gene Panels
- **WES**: 1084 samples

## Available Clinical Attributes
- SOMATIC_STATUS (1084 samples)
- TISSUE_SOURCE_SITE (1084 samples)
- TISSUE_SOURCE_SITE_CODE (1084 samples)
- SAMPLE_TYPE (1084 samples)
- MUTATION_COUNT (1084 samples)
- ONCOTREE_CODE (1084 samples)
- MSI_SCORE_MANTIS (1084 samples)
- MSI_SENSOR_SCORE (1084 samples)
- TISSUE_PROSPECTIVE_COLLECTION_INDICATOR (1084 samples)
- FRACTION_GENOME_ALTERED (1084 samples)
- TMB_NONSYNONYMOUS (1084 samples)
- GRADE (1084 samples)
- TBL_SCORE (1084 samples)
- TUMOR_TISSUE_SITE (1084 samples)
- TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR (1084 samples)

## Top Mutated Genes
- PIK3CA: 347 samples
- TP53: 347 samples
- TTN: 187 samples
- CDH1: 130 samples
- GATA3: 127 samples
- MUC16: 109 samples
- KMT2C: 99 samples
- MAP3K1: 89 samples
- RYR2: 66 samples
- FLG: 66 samples

## Sample Types
- Primary: 1084 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'brca_tcga_pan_can_atlas_2018';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'brca_tcga_pan_can_atlas_2018'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
