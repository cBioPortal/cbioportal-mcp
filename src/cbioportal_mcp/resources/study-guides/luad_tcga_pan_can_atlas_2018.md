# Lung Adenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `luad_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Not available in this study
- **Smoking history / pack-years**: no smoking attribute. Say smoking status is not available here rather than inferring it.
- **EGFR / KRAS / ALK status**: no clinical status attributes. Derive EGFR and KRAS status from mutation data, and ALK fusions from structural-variant data (5 samples with an ALK SV in `genomic_event_derived`).
- `SUBTYPE` is `LUAD` (502) or blank (64) and carries no molecular subtype. `GRADE` is blank for all samples.

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
- STK11 and KEAP1 mutations associated with poor immunotherapy response
