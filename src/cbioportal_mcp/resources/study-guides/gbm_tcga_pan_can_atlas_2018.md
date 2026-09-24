# Glioblastoma Multiforme (TCGA, PanCancer Atlas)

**Study ID:** `gbm_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Molecular Classification
| Attribute | Description | Values (patients) |
|-----------|-------------|--------|
| `SUBTYPE` | TCGA glioma IDH classification | `GBM_IDHwt` 114, `GBM_IDHmut-non-codel` 7, `GBM` 5, blank 459 |

`SUBTYPE` is blank for most patients and does not hold the transcriptional subtypes (Classical/Mesenchymal/Proneural/Neural).

### Not available in this study
- **IDH status**: no `IDH_STATUS` attribute. Use IDH1/IDH2 mutations from mutation data (covers all sequenced samples), or `SUBTYPE` for the 121 classified patients.
- **MGMT promoter methylation status**: not available (the methylation profiles are CpG-probe level, with no MGMT status call).
- **Karnofsky performance status and extent of resection**: not available.

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
