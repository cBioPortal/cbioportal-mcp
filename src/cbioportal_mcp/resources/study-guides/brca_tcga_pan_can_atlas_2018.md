# Breast Invasive Carcinoma (TCGA, PanCancer Atlas)

**Study ID:** `brca_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Molecular Subtypes
| Attribute | Description | Values |
|-----------|-------------|--------|
| `SUBTYPE` | PAM50 molecular subtype | Luminal A, Luminal B, HER2-enriched, Basal-like, Normal-like |

### Receptor Status
| Attribute | Description | Notes |
|-----------|-------------|-------|
| `ER_STATUS` | Estrogen receptor status | Positive, Negative |
| `PR_STATUS` | Progesterone receptor status | Positive, Negative |
| `HER2_STATUS` | HER2 receptor status | Positive, Negative, Equivocal |
| `TRIPLE_NEGATIVE` | Triple negative status | Derived from ER/PR/HER2 |

### Histology
| Attribute | Description |
|-----------|-------------|
| `HISTOLOGICAL_TYPE` | Ductal, Lobular, Mixed, etc. |

## Key Genes
- **TP53**: Most frequently mutated (~30%)
- **PIK3CA**: Common in ER+ tumors (~35%)
- **CDH1**: Enriched in lobular carcinoma
- **GATA3**: Luminal marker
- **ERBB2**: HER2 amplification target

## Notes
- Molecular subtypes (PAM50) correlate strongly with clinical behavior
- ER/PR/HER2 status drives treatment decisions
- Lobular vs ductal distinction has different mutation profiles
