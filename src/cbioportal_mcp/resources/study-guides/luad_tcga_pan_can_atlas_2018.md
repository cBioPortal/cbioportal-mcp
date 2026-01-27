# Lung Adenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `luad_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Smoking History
| Attribute | Description | Values |
|-----------|-------------|--------|
| `SMOKING_HISTORY` | Tobacco use history | Never, Former, Current |
| `PACK_YEARS` | Pack-years of smoking | Numeric |

### Molecular Features
| Attribute | Description |
|-----------|-------------|
| `EGFR_MUTATION_STATUS` | EGFR mutation presence |
| `ALK_FUSION_STATUS` | ALK rearrangement status |
| `KRAS_MUTATION_STATUS` | KRAS mutation presence |

## Key Genes - Driver Mutations
| Gene | Frequency | Notes |
|------|-----------|-------|
| KRAS | ~30% | G12C targetable with sotorasib/adagrasib |
| EGFR | ~15% | L858R, exon 19 del targetable with TKIs |
| ALK | ~5% | Fusions targetable with crizotinib, etc. |
| BRAF | ~5% | V600E targetable |
| ROS1 | ~2% | Fusions targetable |
| RET | ~2% | Fusions targetable |
| MET | ~3% | Exon 14 skipping, amplification |

## Notes
- Driver mutations are largely mutually exclusive
- Never-smokers enriched for EGFR mutations
- Smokers enriched for KRAS mutations
- STK11 and KEAP1 mutations associated with poor immunotherapy response
