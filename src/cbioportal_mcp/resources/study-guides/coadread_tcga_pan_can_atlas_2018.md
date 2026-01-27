# Colorectal Adenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `coadread_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Microsatellite Status
| Attribute | Description | Values |
|-----------|-------------|--------|
| `MSI_STATUS` | Microsatellite instability | MSI-H (high), MSI-L (low), MSS (stable) |

### Molecular Classification
| Attribute | Description | Values |
|-----------|-------------|--------|
| `HYPERMUTATED` | Hypermutation status | Yes, No |
| `CMS_SUBTYPE` | Consensus Molecular Subtype | CMS1, CMS2, CMS3, CMS4 |

### Anatomic Location
| Attribute | Description |
|-----------|-------------|
| `TUMOR_LOCATION` | Colon vs rectum, right vs left |

## Key Genes
| Gene | Frequency | Clinical Relevance |
|------|-----------|-------------------|
| APC | ~80% | Initiating event in most CRC |
| TP53 | ~55% | Progression marker |
| KRAS | ~40% | Predicts anti-EGFR resistance |
| PIK3CA | ~15% | May predict aspirin benefit |
| BRAF | ~10% | V600E poor prognosis (MSS context) |
| SMAD4 | ~10% | TGF-β pathway |

## Molecular Subtypes (CMS)
- **CMS1 (MSI-immune)**: MSI-H, hypermutated, strong immune infiltration
- **CMS2 (Canonical)**: WNT/MYC activation, epithelial
- **CMS3 (Metabolic)**: KRAS mutations, metabolic dysregulation
- **CMS4 (Mesenchymal)**: Stromal infiltration, poor prognosis

## Notes
- MSI-H tumors respond well to immunotherapy
- KRAS/NRAS mutations contraindicate anti-EGFR therapy
- BRAF V600E has different prognosis in MSI-H vs MSS context
- Left vs right-sided tumors have different biology and outcomes
