# TCGA Pan-Cancer Atlas Studies - Common Reference

This document describes clinical attributes and data structures common to all TCGA Pan-Cancer Atlas 2018 studies.

## Gene Panel
All TCGA Pan-Cancer studies use **WES** (Whole Exome Sequencing) for mutations.
- All coding genes are profiled equally
- No gene panel adjustment needed for frequency calculations

## Common Clinical Attributes

### Patient Demographics
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `AGE` | Age at diagnosis | In years |
| `SEX` | Patient sex | Male, Female |
| `RACE` | Patient race | Per NIH categories |
| `ETHNICITY` | Patient ethnicity | Hispanic/Latino status |

### Survival Data
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `OS_MONTHS` | Overall survival months | From diagnosis to death/last follow-up |
| `OS_STATUS` | Overall survival status | 0:LIVING, 1:DECEASED |
| `DFS_MONTHS` | Disease-free survival months | |
| `DFS_STATUS` | Disease-free survival status | |
| `PFS_MONTHS` | Progression-free survival months | |
| `PFS_STATUS` | Progression-free survival status | |

### Tumor Characteristics
| Attribute | Description |
|-----------|-------------|
| `CANCER_TYPE` | Broad cancer category |
| `CANCER_TYPE_DETAILED` | Specific histological subtype |
| `TUMOR_STAGE` | Clinical/pathological stage |
| `TUMOR_GRADE` | Tumor grade |
| `SAMPLE_TYPE` | Primary, Metastatic, Recurrence |

### Genomic Features
| Attribute | Description |
|-----------|-------------|
| `FRACTION_GENOME_ALTERED` | Fraction of genome with CNA |
| `MUTATION_COUNT` | Total mutation count |
| `ANEUPLOIDY_SCORE` | Chromosomal instability measure |

### Molecular Subtypes
Many TCGA studies have study-specific molecular subtype classifications. Check the specific study guide for details.

## Available Data Types
TCGA Pan-Cancer studies typically include:
- **Mutations** (WES)
- **Copy Number Alterations** (SNP arrays)
- **mRNA Expression** (RNA-seq)
- **Protein Expression** (RPPA)
- **DNA Methylation** (450K arrays)

## Notes
- TCGA data is publicly available
- Molecular subtype definitions vary by cancer type
- Expression data uses RSEM normalized values
