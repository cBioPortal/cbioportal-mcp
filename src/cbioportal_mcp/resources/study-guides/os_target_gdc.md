# Osteosarcoma (TARGET, 2018)

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
| `AGE` | Age at diagnosis | In years; pediatric population |
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

## Notes & Caveats
- This is a pediatric cancer cohort; age distribution is younger than adult studies
- WES coverage means no gene panel filtering needed for frequency calculations
- Part of TARGET consortium; integrated with other pediatric cancer data
