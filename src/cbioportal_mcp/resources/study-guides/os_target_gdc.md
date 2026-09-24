# Osteosarcoma (TARGET GDC, 2025)

**Study ID:** `os_target_gdc`

## Overview
Pediatric osteosarcoma study from the TARGET (Therapeutically Applicable Research to Generate Effective Treatments) initiative. Whole exome sequencing data.

## Gene Panel
- **WES** (Whole Exome Sequencing): All coding genes profiled
- For mutation frequency calculations, you can use study-wide sample counts as the denominator (all genes equally covered)

## Clinical Attributes - Semantic Guide

### Patient Demographics
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `AGE` | Age at diagnosis, **floored at 18** | Every patient younger than 18 is recorded as 18 (241 of 293). **Don't use it for age statistics** — use `DAYS_TO_BIRTH` |
| `DAYS_TO_BIRTH` | Days from birth to diagnosis, negative | Age at diagnosis in years = `-DAYS_TO_BIRTH / 365.25`. 293 patients have a value; 90 are empty |
| `SEX` | Patient sex | Male, Female |
| `RACE` | Patient race | Per NIH categories |
| `ETHNICITY` | Patient ethnicity | Hispanic/Latino status |

### Disease Characteristics
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `DISEASE` | Disease type | Should be "Osteosarcoma" |
| `TUMOR_SITE` | Primary tumor location | e.g., Femur, Tibia |
| `HISTOLOGY` | Histological subtype | Osteoblastic, Chondroblastic, etc. |

### Clinical Outcomes
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `OS_MONTHS` | Overall survival in months | Time from diagnosis |
| `OS_STATUS` | Overall survival status | 0:LIVING, 1:DECEASED |
| `EFS_MONTHS` | Event-free survival in months | Time to first event |
| `EFS_STATUS` | Event-free survival status | 0:No event, 1:Event |

### Treatment Response
| Attribute | Description |
|-----------|-------------|
| `PERCENT_NECROSIS` | Tumor necrosis percentage post-chemotherapy |
| `NECROSIS_GROUP` | Grouped necrosis response |

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
