# Breast Invasive Carcinoma (TCGA, PanCancer Atlas)

**Study ID:** `brca_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Molecular Subtypes
| Attribute | Description | Values (patients) |
|-----------|-------------|--------|
| `SUBTYPE` | PAM50 molecular subtype | `BRCA_LumA` 499, `BRCA_LumB` 197, `BRCA_Basal` 171, `BRCA_Her2` 78, `BRCA_Normal` 36, blank 103 |

### Histology
| Attribute | Description | Values (samples) |
|-----------|-------------|--------|
| `CANCER_TYPE_DETAILED` | Histological type | Breast Invasive Ductal Carcinoma 780, Breast Invasive Lobular Carcinoma 201, Breast Invasive Carcinoma (NOS) 77, Breast Invasive Mixed Mucinous Carcinoma 17, Metaplastic Breast Cancer 8, Invasive Breast Carcinoma 1 |

### Not available in this study
- **ER / PR / HER2 receptor status and triple-negative status**: no clinical attribute; say it is not available here. Do not infer receptor status from `SUBTYPE`: PAM50 is an expression-based classification, not IHC/FISH. If the user accepts an expression-based proxy, `BRCA_Basal` (≈ triple-negative) or `BRCA_Her2` can be offered, labelled as PAM50. ERBB2 amplification is available from CNA data.
- `GRADE` is blank for all samples.

## Key Genes
- **TP53**: Most frequently mutated (~30%)
- **PIK3CA**: Common in ER+ tumors (~35%)
- **CDH1**: Enriched in lobular carcinoma
- **GATA3**: Luminal marker
- **ERBB2**: HER2 amplification target

## Notes
- Molecular subtypes (PAM50) correlate strongly with clinical behavior
- Lobular vs ductal distinction has different mutation profiles
