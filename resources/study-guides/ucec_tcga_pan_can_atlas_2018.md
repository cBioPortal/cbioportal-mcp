# Uterine Corpus Endometrial Carcinoma (TCGA, PanCancer Atlas)

**Study ID:** `ucec_tcga_pan_can_atlas_2018`
**Cancer Type:** ucec
**Patients:** 560
**Samples:** 530

## Description
Uterine Corpus Endometrial Carcinoma TCGA PanCancer data. The original data is <a href="https://gdc.cancer.gov/about-data/publications/pancanatlas">here</a>. The publications are <a href="https://www.cell.com/pb-assets/consortium/pancanceratlas/pancani3/index.html">here</a>.

## Gene Panels
- **WES**: 529 samples

## Available Clinical Attributes
- MUTATION_COUNT (529 samples)
- TBL_SCORE (529 samples)
- CANCER_TYPE (529 samples)
- SOMATIC_STATUS (529 samples)
- MSI_SENSOR_SCORE (529 samples)
- TISSUE_SOURCE_SITE (529 samples)
- TISSUE_SOURCE_SITE_CODE (529 samples)
- SAMPLE_TYPE (529 samples)
- TMB_NONSYNONYMOUS (529 samples)
- ONCOTREE_CODE (529 samples)
- TISSUE_PROSPECTIVE_COLLECTION_INDICATOR (529 samples)
- MSI_SCORE_MANTIS (529 samples)
- TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR (529 samples)
- TUMOR_TISSUE_SITE (529 samples)
- TUMOR_TYPE (529 samples)

## Top Mutated Genes
- PTEN: 337 samples
- PIK3CA: 259 samples
- ARID1A: 227 samples
- TTN: 206 samples
- TP53: 193 samples
- PIK3R1: 159 samples
- KMT2D: 143 samples
- MUC16: 142 samples
- CTNNB1: 133 samples
- CTCF: 127 samples

## Sample Types
- Primary: 529 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'ucec_tcga_pan_can_atlas_2018';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'ucec_tcga_pan_can_atlas_2018'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
