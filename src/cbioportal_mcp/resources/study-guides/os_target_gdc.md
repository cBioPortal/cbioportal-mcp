# Osteosarcoma (TARGET GDC, 2025)

**Study ID:** `os_target_gdc`

## Overview
Pediatric osteosarcoma study from the TARGET (Therapeutically Applicable Research to Generate Effective Treatments) initiative. Whole exome sequencing data.

## Gene Panel
- **WES** (Whole Exome Sequencing): all coding genes profiled
- **143 of the 160 samples are profiled for mutations.** Use 143 as the mutation-frequency denominator (`sample_to_gene_panel_derived`, `alteration_type = 'MUTATION_EXTENDED'`), not the study's sample count — e.g. TP53 is mutated in 32/143 = 22.4%.

## Patients vs Samples
383 patients have clinical data, but only 153 of them have a sample (159 samples). Patient-level questions (age, sex, survival) use all patients with a value; genomic questions use the 143 mutation-profiled samples.

## Clinical Attributes - Semantic Guide

### Patient Demographics
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `AGE` | Age at diagnosis, **floored at 18** | Every patient younger than 18 is recorded as 18 (241 of 293). **Don't use it for age statistics** — use `DAYS_TO_BIRTH` |
| `DAYS_TO_BIRTH` | Days from birth to diagnosis, negative | Age at diagnosis in years = `-DAYS_TO_BIRTH / 365.25`. 293 patients have a value; 90 are empty |
| `SEX` | Patient sex | Male 172, Female 133, 78 empty |
| `RACE`, `ETHNICITY` | Race, ethnicity | |

### Disease Characteristics
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `CANCER_TYPE_DETAILED` | Cancer type | Osteosarcoma for every sample |
| `PRIMARY_SITE_PATIENT` | Primary site | "Appendicular Skeleton" for every patient with a value — no finer location (femur, tibia) |

No histological subtype, tumor necrosis / chemotherapy response, or event-free survival attributes exist in this study.

### Clinical Outcomes
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `OS_MONTHS` | Overall survival in months | Time from diagnosis |
| `OS_STATUS` | Overall survival status | 0:LIVING 171, 1:DECEASED 105, 107 empty |

## Age at Diagnosis

Compute age from `DAYS_TO_BIRTH`, not `AGE`. A median from `AGE` comes out as 18 because every child is recorded as 18; the real median is about 15 years.

```sql
SELECT
    count() AS patients,
    round(median(-toFloat64OrNull(attribute_value) / 365.25), 1) AS median_age_years,
    round(min(-toFloat64OrNull(attribute_value) / 365.25), 1) AS min_age_years,
    round(max(-toFloat64OrNull(attribute_value) / 365.25), 1) AS max_age_years
FROM clinical_data_derived
WHERE cancer_study_identifier = 'os_target_gdc'
  AND attribute_name = 'DAYS_TO_BIRTH'
  AND toFloat64OrNull(attribute_value) IS NOT NULL;
-- 293 patients, median 15.2, range 3.6-87.1
```

When reporting, say the age comes from `DAYS_TO_BIRTH` and that `AGE` is floored at 18.

## Notes & Caveats
- This is a pediatric cancer cohort; age distribution is younger than adult studies
- WES coverage means no gene panel filtering needed for frequency calculations
- Part of TARGET consortium; integrated with other pediatric cancer data
