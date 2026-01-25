# MSK-CHORD (MSK, Nature 2024)

**Study ID:** `msk_chord_2024`

## Overview
Targeted sequencing via MSK-IMPACT panels. Clinical annotations include some derived from natural language processing (denoted NLP).

## Gene Panels
This study uses multiple MSK-IMPACT panel versions:
- **IMPACT341**: Earlier version, 341 genes
- **IMPACT410**: 410 genes
- **IMPACT468**: 468 genes  
- **IMPACT505**: Latest version, 505 genes

**Important:** Different samples may have different gene coverage. Always use gene-specific denominators when calculating mutation frequencies.

## Clinical Attributes - Semantic Guide

### Cancer Classification
| Attribute | Description | Values |
|-----------|-------------|--------|
| `CANCER_TYPE` | Broad cancer category | e.g., "Non-Small Cell Lung Cancer", "Breast Cancer" |
| `CANCER_TYPE_DETAILED` | Specific subtype | e.g., "Lung Adenocarcinoma", "Invasive Ductal Carcinoma" |
| `ONCOTREE_CODE` | OncoTree classification code | Standardized cancer type codes |

### Sample Information
| Attribute | Description | Values |
|-----------|-------------|--------|
| `SAMPLE_TYPE` | Sample origin | Primary, Metastasis, Local Recurrence, Unknown |
| `SAMPLE_CLASS` | Sample classification | Tumor, Normal |
| `PRIMARY_SITE` | Anatomical primary site | e.g., Lung, Breast, Colon |
| `METASTATIC_SITE` | Site of metastasis (if applicable) | e.g., Liver, Bone, Brain |

### Genomic Features
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `TMB_NONSYNONYMOUS` | Tumor mutational burden | Nonsynonymous mutations per Mb |
| `MUTATION_COUNT` | Total mutation count | Raw count of mutations in sample |
| `MSI_SCORE` | Microsatellite instability score | Numeric score |
| `MSI_TYPE` | MSI classification | Stable, Instable, Indeterminate |

### Clinical Groupings
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `CLINICAL_GROUP` | Clinical grouping | Study-specific grouping |
| `PATHOLOGICAL_GROUP` | Pathological grouping | Study-specific grouping |
| `CLINICAL_SUMMARY` | NLP-derived clinical summary | May contain extracted clinical info |

### Prostate-Specific
| Attribute | Description |
|-----------|-------------|
| `GLEASON_SAMPLE_LEVEL` | Gleason score at sample level |
| `GLEASON_FIRST_REPORTED` | First reported Gleason score |
| `GLEASON_HIGHEST_REPORTED` | Highest reported Gleason score |

### Biomarkers
| Attribute | Description |
|-----------|-------------|
| `PDL1_POSITIVE` | PD-L1 expression status |
| `HISTORY_OF_PDL1` | History of PD-L1 testing |
| `HER2` | HER2 status (breast cancer) |
| `HR` | Hormone receptor status |

## Treatment Data
Treatment information is stored in **clinical_event** tables, not clinical attributes.

```sql
-- Get treatment agents for this study
SELECT ced.value as agent, COUNT(DISTINCT ce.patient_id) as patients
FROM clinical_event ce
JOIN clinical_event_data ced ON ce.clinical_event_id = ced.clinical_event_id
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'msk_chord_2024'
  AND ce.event_type IN ('Treatment', 'TREATMENT')
  AND ced.key = 'AGENT'
GROUP BY ced.value
ORDER BY patients DESC;
```

## Notes & Caveats
- Some clinical annotations are NLP-derived and may have extraction errors
- Multiple IMPACT panel versions mean gene coverage varies by sample
- Data is under CC BY-NC-ND 4.0 license; contact datarequests@mskcc.org for commercial use
