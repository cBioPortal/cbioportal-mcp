# Ovarian Serous Cystadenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `ov_tcga_pan_can_atlas_2018`
**Cancer Type:** hgsoc
**Patients:** 601
**Samples:** 586

## Description
Ovarian Serous Cystadenocarcinoma TCGA PanCancer data. The original data is <a href="https://gdc.cancer.gov/about-data/publications/pancanatlas">here</a>. The publications are <a href="https://www.cell.com/pb-assets/consortium/pancanceratlas/pancani3/index.html">here</a>.

## Gene Panels
- **WES**: 585 samples

## Available Clinical Attributes
- MUTATION_COUNT (585 samples)
- TBL_SCORE (585 samples)
- CANCER_TYPE (585 samples)
- SOMATIC_STATUS (585 samples)
- MSI_SENSOR_SCORE (585 samples)
- TISSUE_SOURCE_SITE (585 samples)
- TISSUE_SOURCE_SITE_CODE (585 samples)
- SAMPLE_TYPE (585 samples)
- TMB_NONSYNONYMOUS (585 samples)
- ONCOTREE_CODE (585 samples)
- TISSUE_PROSPECTIVE_COLLECTION_INDICATOR (585 samples)
- MSI_SCORE_MANTIS (585 samples)
- TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR (585 samples)
- TUMOR_TISSUE_SITE (585 samples)
- TUMOR_TYPE (585 samples)

## Top Mutated Genes
- TP53: 373 samples
- TTN: 110 samples
- FLG2: 41 samples
- MUC16: 41 samples
- CSMD3: 38 samples
- HMCN1: 34 samples
- USH2A: 32 samples
- AHNAK2: 30 samples
- NF1: 30 samples
- FLG: 30 samples

## Sample Types
- Primary: 583 samples
- Recurrence: 2 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'ov_tcga_pan_can_atlas_2018';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'ov_tcga_pan_can_atlas_2018'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
