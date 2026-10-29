# Ovarian Serous Cystadenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `ov_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Histology
| Attribute | Description | Values (samples) |
|-----------|-------------|--------|
| `GRADE` | Histologic grade | G3 400, G2 65, GX 7, G1 5, GB 2, G4 1, blank 105 |
| `CANCER_TYPE_DETAILED` | Histology | Serous Ovarian Cancer (all 585) |

### Not available in this study
- **Platinum sensitivity and residual disease**: no treatment-response or surgical-outcome attributes. Survival (`OS_*`, `PFS_*`, `DFS_*`, `DSS_*`) is the only outcome data.
- **BRCA / HRD status**: no clinical status attributes. Derive BRCA1/BRCA2 status from mutation (somatic calls only) and CNA data; there is no HRD score.
- `AJCC_PATHOLOGIC_TUMOR_STAGE` is blank for all samples. `SUBTYPE` is `OV` (177) or blank (408) and carries no molecular subtype.

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
