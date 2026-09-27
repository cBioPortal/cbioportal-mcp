# MSK-CHORD (MSK, Nature 2024)

**Study ID:** `msk_chord_2024`

## Overview
Targeted sequencing via MSK-IMPACT panels. Clinical annotations include some derived from natural language processing (denoted NLP).

**Exactly five cancer types** (`CANCER_TYPE`, patients): Non-Small Cell Lung Cancer 7,809, Colorectal Cancer 5,543, Breast Cancer 5,368, Prostate Cancer 3,211, Pancreatic Cancer 3,109. There is **no melanoma** or any other cancer type; say so up front if asked, instead of substituting another type.

**No therapy-response variable.** There is no RECIST, objective response, or best-response attribute or event. For treatment-outcome questions (e.g. immunotherapy response), say this first; the only proxies are `OS_MONTHS`/`OS_STATUS`, or NLP radiology progression events (`Diagnosis` events with `SUBTYPE = 'Progression'`, key `PROGRESSION` = Y/N/Indeterminate), in patients with `Treatment` events of the relevant `SUBTYPE` (e.g. `Immuno`: 3,341 patients). Hand off the comparison to cBioPortal group comparison / survival.

**Nearly one sample per patient: 24,950 patients / 25,040 samples.** Only 90 patients have more than one sample, and all 90 have samples from two different cancer types (second primaries); only 26 have both a `Primary` and a `Metastasis` sample. There is no meaningful same-patient (paired) primary-vs-metastasis cohort. For "same patient" / paired questions, say this up front, then offer the **unpaired** comparison of all `Primary` vs `Metastasis` samples (`SAMPLE_TYPE`), labelled as unpaired.
```sql
SELECT countIf(n > 1) AS multi_sample_patients,         -- 90
       countIf(has_p AND has_m) AS primary_and_met      -- 26
FROM (SELECT patient_unique_id, count() AS n,
             has(groupArray(attribute_value), 'Primary') AS has_p,
             has(groupArray(attribute_value), 'Metastasis') AS has_m
      FROM clinical_data_derived
      WHERE cancer_study_identifier = 'msk_chord_2024' AND attribute_name = 'SAMPLE_TYPE'
      GROUP BY patient_unique_id);
```

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
| `SAMPLE_CLASS` | Sample classification | Tumor (all samples) |
| `PRIMARY_SITE` | Anatomical primary site | e.g., Lung, Breast, Colon |
| `METASTATIC_SITE` | Site of metastasis (if applicable) | e.g., Liver, Bone, Brain |

#### Sample Type Distribution (as of 2024)
| Sample Type | Count | Notes |
|-------------|-------|-------|
| **Primary** | 15,928 | Primary tumor samples |
| **Metastasis** | 8,878 | Metastatic tumor samples |
| **Unknown** | 136 | Sample type not specified |
| **Local Recurrence** | 98 | Locally recurrent tumors |
| **Total** | **25,040** | All samples in study |

**🚨 CRITICAL:** To query primary/metastatic samples, use `clinical_data_derived` with `attribute_name = 'SAMPLE_TYPE'`:
```sql
-- CORRECT way to count primary samples
SELECT COUNT(DISTINCT sample_unique_id) as primary_samples
FROM clinical_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024'
  AND attribute_name = 'SAMPLE_TYPE'
  AND attribute_value = 'Primary';
-- Returns: 15,928

-- DO NOT use sample.sample_type column - it contains "Primary Solid Tumor" for ALL samples!
```

### Genomic Features
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `TMB_NONSYNONYMOUS` | Tumor mutational burden | Nonsynonymous mutations per Mb |
| `MUTATION_COUNT` | Total mutation count | Raw count of mutations in sample |
| `MSI_SCORE` | Microsatellite instability score | Numeric score |
| `MSI_TYPE` | MSI classification | Stable 21,096, Do not report 1,825, Indeterminate 865, Instable 699 |

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

MSK-CHORD has **detailed treatment data** stored in clinical_event tables. This is one of the most comprehensive treatment datasets in cBioPortal.

### Treatment Event Keys

| Key | Description | Example Values |
|-----|-------------|----------------|
| `AGENT` | Drug/treatment name | FLUOROURACIL, PEMBROLIZUMAB, OXALIPLATIN |
| `SUBTYPE` | Treatment category | Chemo, Immuno, Targeted, Hormone, Radiation Therapy |
| `RX_INVESTIGATIVE` | Investigational drug flag | Y, N |
| `FLAG_OROTOPICAL` | Oral/topical administration | 0, 1 |
| `TREATMENT_TYPE` | Broad treatment type | Medical Therapy |
| `PRIOR_MED_TO_MSK` | Prior medications flag | Indicates meds before MSK care |
| `INFERRED_TX_PROB` | NLP inference probability | Confidence score for NLP-extracted data |

### Treatment Subtypes

| Subtype | Description |
|---------|-------------|
| `Chemo` | Cytotoxic chemotherapy |
| `Targeted` | Molecularly targeted agents (TKIs, etc.) |
| `Immuno` | Immunotherapy (checkpoint inhibitors, etc.) |
| `Hormone` | Hormonal therapy |
| `Radiation Therapy` | Radiation treatment |
| `Biologic` | Biologic agents |
| `Investigational` | Investigational/trial drugs |
| `Bone Treatment` | Bone-targeted agents (bisphosphonates, etc.) |
| `Prior Medications to MSK` | Medications before MSK treatment |

### Query Examples

```sql
-- Get treatments by subtype
SELECT 
    subtype.value as treatment_type,
    agent.value as agent,
    COUNT(DISTINCT ce.patient_id) as patients
FROM clinical_event ce
JOIN clinical_event_data agent ON ce.clinical_event_id = agent.clinical_event_id AND agent.key = 'AGENT'
JOIN clinical_event_data subtype ON ce.clinical_event_id = subtype.clinical_event_id AND subtype.key = 'SUBTYPE'
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'msk_chord_2024'
  AND ce.event_type IN ('Treatment', 'TREATMENT')
GROUP BY subtype.value, agent.value
ORDER BY treatment_type, patients DESC;
```

### Treatment Data Caveats
- Some treatment data is NLP-extracted (`INFERRED_TX_PROB` indicates confidence)
- `PRIOR_MED_TO_MSK` captures pre-MSK treatments separately
- Treatment timing (start_date, stop_date) is in days from diagnosis
- **Cannot calculate percentages**: We can't distinguish "not collected" from "not received"

### Treatment Query Patterns for MSK-CHORD

**For broad questions** ("What treatments did patients receive?"):
1. First summarize by SUBTYPE (Chemo, Immuno, Targeted, etc.)
2. Then list top agents within each category

**For specific questions** ("Most common treatment?"):
- Query by AGENT key
- Report patient count, NOT percentage
- Expected answer: Fluorouracil (~6,319 patients)

**For treatment type questions** ("Most common chemotherapy?"):
- Filter by `SUBTYPE = 'Chemo'` and report by AGENT

## Notes & Caveats
- Some clinical annotations are NLP-derived and may have extraction errors
- Multiple IMPACT panel versions mean gene coverage varies by sample
- Data is under CC BY-NC-ND 4.0 license; contact datarequests@mskcc.org for commercial use
