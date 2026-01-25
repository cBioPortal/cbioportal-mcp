# MSK-CHORD (MSK, Nature 2024)

**Study ID:** `msk_chord_2024`
**Cancer Type:** mixed
**Patients:** 24950
**Samples:** 25041

## Description
Targeted sequencing of 25040 tumors from 24950 patients and their matched normals via MSK-IMPACT, along with clinical annotations, some of which are derived from natural language processing (denoted NLP). This data is available under the <a href="https://creativecommons.org/licenses/by-nc-nd/4.0/deed.en">Creative Commons BY-NC-ND 4.0 license</a>. For commercial use, please contact <a href="mailto: datarequests@mskcc.org">datarequests@mskcc.org</a>

## Gene Panels
- **IMPACT468**: 12891 samples
- **IMPACT505**: 7155 samples
- **IMPACT410**: 3973 samples
- **IMPACT341**: 1019 samples
- **IMPACT-HEME-400**: 2 samples

## Available Clinical Attributes
- TMB_NONSYNONYMOUS (25040 samples)
- PATHOLOGICAL_GROUP (25040 samples)
- CANCER_TYPE (25040 samples)
- CANCER_TYPE_DETAILED (25040 samples)
- SAMPLE_TYPE (25040 samples)
- CLINICAL_GROUP (25040 samples)
- ONCOTREE_CODE (25040 samples)
- CLINICAL_SUMMARY (25040 samples)
- GLEASON_SAMPLE_LEVEL (25040 samples)
- MSI_COMMENT (25040 samples)
- MSI_TYPE (25040 samples)
- METASTATIC_SITE (25040 samples)
- SAMPLE_CLASS (25040 samples)
- MUTATION_COUNT (25040 samples)
- PDL1_POSITIVE (25040 samples)

## Top Mutated Genes
- TP53: 13124 samples
- KRAS: 7128 samples
- APC: 4777 samples
- PIK3CA: 3708 samples
- EGFR: 2159 samples
- ARID1A: 1843 samples
- SMAD4: 1796 samples
- KMT2D: 1783 samples
- KMT2C: 1629 samples
- ATM: 1388 samples

## Sample Types
- Primary: 15928 samples
- Metastasis: 8878 samples
- Unknown: 136 samples
- Local Recurrence: 98 samples

## Query Examples

```sql
-- Get all samples
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024';

-- Get mutations for a gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'msk_chord_2024'
  AND hugo_gene_symbol = 'TP53'
  AND variant_type = 'mutation';
```

## Notes
<!-- Add study-specific notes, caveats, or tips here -->
