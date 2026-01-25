# Lung Adenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `luad_tcga_pan_can_atlas_2018`
**Cancer Type:** luad
**Patients:** 580
**Samples:** 567

## Description
Lung Adenocarcinoma TCGA PanCancer data. The original data is <a href="https://gdc.cancer.gov/about-data/publications/pancanatlas">here</a>. The publications are <a href="https://www.cell.com/pb-assets/consortium/pancanceratlas/pancani3/index.html">here</a>.

## Gene Panels
- **WES**: 566 samples

## Available Clinical Attributes
- MUTATION_COUNT (566 samples)
- TBL_SCORE (566 samples)
- CANCER_TYPE (566 samples)
- SOMATIC_STATUS (566 samples)
- MSI_SENSOR_SCORE (566 samples)
- TISSUE_SOURCE_SITE (566 samples)
- TISSUE_SOURCE_SITE_CODE (566 samples)
- SAMPLE_TYPE (566 samples)
- TMB_NONSYNONYMOUS (566 samples)
- ONCOTREE_CODE (566 samples)
- TISSUE_PROSPECTIVE_COLLECTION_INDICATOR (566 samples)
- MSI_SCORE_MANTIS (566 samples)
- TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR (566 samples)
- TUMOR_TISSUE_SITE (566 samples)
- TUMOR_TYPE (566 samples)

## Top Mutated Genes
- TP53: 295 samples
- TTN: 272 samples
- MUC16: 242 samples
- CSMD3: 226 samples
- RYR2: 217 samples
- LRP1B: 201 samples
- ZFHX4: 185 samples
- USH2A: 177 samples
- KRAS: 168 samples
- XIRP2: 150 samples

## Sample Types
- Primary: 566 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'luad_tcga_pan_can_atlas_2018';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'luad_tcga_pan_can_atlas_2018'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
