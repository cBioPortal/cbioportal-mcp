# Uterine Corpus Endometrial Carcinoma (TCGA, PanCancer Atlas)

**Study ID:** `ucec_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Molecular Classification (TCGA)
| Attribute | Description | Values |
|-----------|-------------|--------|
| `SUBTYPE` | TCGA molecular subtype | POLE (ultramutated), MSI (hypermutated), CN-low, CN-high |

### Histology
| Attribute | Description | Values |
|-----------|-------------|--------|
| `HISTOLOGICAL_TYPE` | Histological subtype | Endometrioid, Serous, Mixed |

## Molecular Subtypes
| Subtype | Characteristics | Prognosis |
|---------|-----------------|-----------|
| **POLE** | Ultra-hypermutated (>100 mut/Mb), POLE exonuclease mutations | Excellent |
| **MSI** | Hypermutated, microsatellite instability, MLH1 silencing | Intermediate |
| **CN-low** | Microsatellite stable, few copy number alterations | Intermediate |
| **CN-high** | Serous-like, TP53 mutations, extensive CNA | Poor |

## Key Genes
| Gene | Frequency | Subtype Association |
|------|-----------|---------------------|
| PTEN | ~65% | CN-low, MSI |
| PIK3CA | ~50% | All subtypes |
| PIK3R1 | ~30% | CN-low |
| ARID1A | ~35% | MSI |
| TP53 | ~25% | CN-high (>90% in this subtype) |
| KRAS | ~20% | Various |
| CTNNB1 | ~20% | CN-low |
| POLE | ~7% | Defines POLE subtype |

## Notes
- TCGA molecular classification has prognostic value and guides treatment
- POLE and MSI subtypes are hypermutated but have different mechanisms
- TP53 mutations with CN-high pattern indicate serous-like behavior regardless of histology
- POLE mutations should be in exonuclease domain (proofreading) to be driver
- MSI-H tumors respond well to immunotherapy
