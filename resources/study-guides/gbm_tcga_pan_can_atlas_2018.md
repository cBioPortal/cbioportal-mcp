# Glioblastoma Multiforme (TCGA, PanCancer Atlas)

**Study ID:** `gbm_tcga_pan_can_atlas_2018`
**Cancer Type:** difg
**Patients:** 607
**Samples:** 593

## Description
Glioblastoma Multiforme TCGA PanCancer data. The original data is <a href="https://gdc.cancer.gov/about-data/publications/pancanatlas">here</a>. The publications are <a href="https://www.cell.com/pb-assets/consortium/pancanceratlas/pancani3/index.html">here</a>.

## Gene Panels
- **WES**: 592 samples

## Available Clinical Attributes
- MUTATION_COUNT (592 samples)
- TBL_SCORE (592 samples)
- CANCER_TYPE (592 samples)
- SOMATIC_STATUS (592 samples)
- MSI_SENSOR_SCORE (592 samples)
- TISSUE_SOURCE_SITE (592 samples)
- TISSUE_SOURCE_SITE_CODE (592 samples)
- SAMPLE_TYPE (592 samples)
- TMB_NONSYNONYMOUS (592 samples)
- ONCOTREE_CODE (592 samples)
- TISSUE_PROSPECTIVE_COLLECTION_INDICATOR (592 samples)
- MSI_SCORE_MANTIS (592 samples)
- TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR (592 samples)
- TUMOR_TISSUE_SITE (592 samples)
- TUMOR_TYPE (592 samples)

## Top Mutated Genes
- PTEN: 133 samples
- TP53: 125 samples
- TTN: 101 samples
- EGFR: 94 samples
- MUC16: 61 samples
- FLG: 53 samples
- NF1: 46 samples
- RYR2: 43 samples
- PIK3R1: 39 samples
- RB1: 38 samples

## Sample Types
- Primary: 585 samples
- Recurrence: 7 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'gbm_tcga_pan_can_atlas_2018';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'gbm_tcga_pan_can_atlas_2018'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
