# TCGA Pan-Cancer Atlas Studies - Common Reference

This document describes clinical attributes and data structures common to all TCGA Pan-Cancer Atlas 2018 studies.

**All 32 PanCan Atlas studies share one clinical attribute set (57–60 attributes).** Only the attributes listed here exist; there are no study-specific receptor, mutation-status, smoking, treatment-response or MSI-status attributes. Gene-level status (e.g. EGFR, KRAS, BRCA1/2, IDH1 mutation; ALK fusion) comes from mutation / structural-variant data, not a clinical attribute. Before filtering on any attribute not listed here, confirm it exists:
```sql
SELECT DISTINCT attribute_name FROM clinical_data_derived
WHERE cancer_study_identifier = '<study>_tcga_pan_can_atlas_2018';
```

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
| `DFS_STATUS` | Disease-free survival status | 0:DiseaseFree, 1:Recurred/Progressed |
| `DSS_MONTHS` | Disease-specific survival months | |
| `DSS_STATUS` | Disease-specific survival status | 0:ALIVE OR DEAD TUMOR FREE, 1:DEAD WITH TUMOR |
| `PFS_MONTHS` | Progression-free survival months | |
| `PFS_STATUS` | Progression-free survival status | 0:CENSORED, 1:PROGRESSION |

### Tumor Characteristics
| Attribute | Description |
|-----------|-------------|
| `CANCER_TYPE` | Broad cancer category |
| `CANCER_TYPE_DETAILED` | Histological subtype (use this for histology questions) |
| `SUBTYPE` | TCGA molecular subtype, prefixed by study (e.g. `BRCA_LumA`, `COAD_MSI`); blank for some patients |
| `AJCC_PATHOLOGIC_TUMOR_STAGE` | Pathologic stage (`STAGE IIA`, …); blank in some studies (e.g. GBM, OV, UCEC) |
| `PATH_T_STAGE` / `PATH_N_STAGE` / `PATH_M_STAGE` | TNM components |
| `GRADE` | Histologic grade; blank in many studies (e.g. BRCA, COADREAD, GBM, LUAD) |
| `TUMOR_TISSUE_SITE` / `ICD_O_3_SITE` | Anatomic site / ICD-O-3 topography code |
| `SAMPLE_TYPE` | Primary, Metastasis, Recurrence |
| `RADIATION_THERAPY` | Yes, No |

### Genomic Features
| Attribute | Description |
|-----------|-------------|
| `FRACTION_GENOME_ALTERED` | Fraction of genome with CNA |
| `MUTATION_COUNT` | Total mutation count |
| `ANEUPLOIDY_SCORE` | Chromosomal instability measure |
| `TMB_NONSYNONYMOUS` | Tumor mutational burden (nonsynonymous mutations per Mb) |
| `MSI_SENSOR_SCORE` | MSIsensor score; MSI ≥10, indeterminate 4–10 |
| `MSI_SCORE_MANTIS` | MANTIS score; MSI >0.6, indeterminate 0.4–0.6 |

### Molecular Subtypes
Molecular subtypes are in the shared `SUBTYPE` attribute. Values differ by study; check the specific study guide or query the values.

## Available Data Types
TCGA Pan-Cancer studies typically include:
- **Mutations** (WES)
- **Structural Variants** (fusions)
- **Copy Number Alterations** (SNP arrays)
- **mRNA Expression** (RNA-seq)
- **Protein Expression** (RPPA)
- **DNA Methylation** (450K arrays)

## Notes
- TCGA data is publicly available
- Molecular subtype definitions vary by cancer type
- Expression data uses RSEM normalized values
