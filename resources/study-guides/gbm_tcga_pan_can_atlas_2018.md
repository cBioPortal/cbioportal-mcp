# Glioblastoma Multiforme (TCGA, PanCancer Atlas)

**Study ID:** `gbm_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Molecular Classification
| Attribute | Description | Values |
|-----------|-------------|--------|
| `IDH_STATUS` | IDH mutation status | Mutant, Wild-type |
| `MGMT_STATUS` | MGMT promoter methylation | Methylated, Unmethylated |
| `SUBTYPE` | Molecular subtype | Classical, Mesenchymal, Proneural, Neural |

### Clinical Context
| Attribute | Description |
|-----------|-------------|
| `KPS` | Karnofsky Performance Status |
| `EXTENT_OF_RESECTION` | Surgical resection extent |

## Key Genes & Pathways
- **TP53**: Frequently mutated
- **PTEN**: Common deletions
- **EGFR**: Amplified in ~40%, often with EGFRvIII variant
- **IDH1**: R132H mutation defines IDH-mutant subtype (better prognosis)
- **RB pathway**: CDKN2A deletion, CDK4 amplification, RB1 mutation (often mutually exclusive)

## Notes
- IDH-mutant GBMs have significantly better prognosis
- MGMT methylation predicts temozolomide response
- EGFR amplification and EGFRvIII are GBM-specific
- Molecular subtypes have distinct transcriptional programs
