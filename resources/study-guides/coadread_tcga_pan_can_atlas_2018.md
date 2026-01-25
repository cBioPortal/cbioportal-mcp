# Colorectal Adenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `coadread_tcga_pan_can_atlas_2018`
**Cancer Type:** coadread
**Patients:** 596
**Samples:** 595

## Description
Colorectal Adenocarcinoma TCGA PanCancer data. The original data is <a href="https://gdc.cancer.gov/about-data/publications/pancanatlas">here</a>. The publications are <a href="https://www.cell.com/pb-assets/consortium/pancanceratlas/pancani3/index.html">here</a>.

## Gene Panels
- **WES**: 594 samples

## Available Clinical Attributes
- MUTATION_COUNT (594 samples)
- TBL_SCORE (594 samples)
- CANCER_TYPE (594 samples)
- SOMATIC_STATUS (594 samples)
- MSI_SENSOR_SCORE (594 samples)
- TISSUE_SOURCE_SITE (594 samples)
- TISSUE_SOURCE_SITE_CODE (594 samples)
- SAMPLE_TYPE (594 samples)
- TMB_NONSYNONYMOUS (594 samples)
- ONCOTREE_CODE (594 samples)
- TISSUE_PROSPECTIVE_COLLECTION_INDICATOR (594 samples)
- MSI_SCORE_MANTIS (594 samples)
- TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR (594 samples)
- TUMOR_TISSUE_SITE (594 samples)
- TUMOR_TYPE (594 samples)

## Top Mutated Genes
- APC: 387 samples
- TP53: 314 samples
- TTN: 257 samples
- KRAS: 218 samples
- PIK3CA: 147 samples
- MUC16: 146 samples
- SYNE1: 144 samples
- FAT4: 124 samples
- RYR2: 105 samples
- OBSCN: 99 samples

## Sample Types
- Primary: 594 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'coadread_tcga_pan_can_atlas_2018';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'coadread_tcga_pan_can_atlas_2018'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
