# Ovarian Serous Cystadenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `ov_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Histology
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `HISTOLOGICAL_GRADE` | Tumor grade | High-grade serous predominant |

### Treatment Response
| Attribute | Description |
|-----------|-------------|
| `PLATINUM_STATUS` | Platinum sensitivity | Sensitive, Resistant, Refractory |
| `RESIDUAL_DISEASE` | Post-surgical residual | Optimal (<1cm), Suboptimal |

### Molecular Features
| Attribute | Description |
|-----------|-------------|
| `BRCA_STATUS` | BRCA1/2 mutation status |
| `HRD_STATUS` | Homologous recombination deficiency |

## Key Genes
| Gene | Frequency | Notes |
|------|-----------|-------|
| TP53 | ~96% | Near-universal in high-grade serous |
| BRCA1 | ~10% germline | PARP inhibitor sensitivity |
| BRCA2 | ~6% germline | PARP inhibitor sensitivity |
| NF1 | ~4% | RAS pathway |
| RB1 | ~2% | Cell cycle |
| CDK12 | ~3% | DNA repair |

## Notes
- High-grade serous ovarian cancer (HGSOC) is genomically distinct from other ovarian subtypes
- TP53 is mutated in virtually all HGSOC
- BRCA1/2 mutations (germline or somatic) predict PARP inhibitor response
- HRD score predicts platinum and PARP sensitivity even without BRCA mutation
- Copy number alterations more prominent than mutations (except TP53)
