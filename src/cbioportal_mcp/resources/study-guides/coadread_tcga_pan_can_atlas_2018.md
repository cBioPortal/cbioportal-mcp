# Colorectal Adenocarcinoma (TCGA, PanCancer Atlas)

**Study ID:** `coadread_tcga_pan_can_atlas_2018`

See `_tcga_pancan_template.md` for common TCGA clinical attributes.

## Study-Specific Attributes

### Microsatellite Instability (MSI)
There is no `MSI_STATUS` attribute. Three attributes carry MSI (594 patients, one sample each):

| Attribute | Definition | MSI-high count |
|-----------|------------|----------------|
| `SUBTYPE` | TCGA molecular classification: `COAD_MSI` 60 + `READ_MSI` 3 | **63** |
| `MSI_SENSOR_SCORE` | MSIsensor score ≥10 (indeterminate 4–10: 10 more) | 78 of 584 scored |
| `MSI_SCORE_MANTIS` | MANTIS score >0.4 (>0.6 = MSI: 67; 0.4–0.6 indeterminate) | 89 of 557 scored |

**For "MSI-high" questions, use `SUBTYPE` IN (`COAD_MSI`, `READ_MSI`)** (the TCGA molecular classification) and state which definition you used; mention the score-based alternatives if the counts matter. Do not switch to `coadread_tcga_pub` to find MSI; this study has it.
```sql
SELECT count(DISTINCT patient_unique_id) AS msi_patients   -- 63
FROM clinical_data_derived
WHERE cancer_study_identifier = 'coadread_tcga_pan_can_atlas_2018'
  AND attribute_name = 'SUBTYPE' AND attribute_value IN ('COAD_MSI', 'READ_MSI');
```

### Molecular Classification (`SUBTYPE`, patients)
`COAD_CIN` 226, `READ_CIN` 102, `COAD_MSI` 60, `COAD_GS` 49, `READ_GS` 9, `COAD_POLE` 6, `READ_POLE` 4, `READ_MSI` 3, blank 135.
- **Hypermutated**: no `HYPERMUTATED` attribute. Use `SUBTYPE` MSI + POLE (73 patients), or `TMB_NONSYNONYMOUS` ≥10 (83 samples; all MSI and POLE tumors exceed it).
- **CMS (consensus molecular subtypes)** are not available in this study.

### Anatomic Location
There is no `TUMOR_LOCATION` attribute.
- Colon vs rectum: `TUMOR_TISSUE_SITE` — Colon 436, Rectum 152, blank 6.
- Subsite (for left vs right): `ICD_O_3_SITE` — right: C18.0 cecum 81, C18.2 ascending 100, C18.3 hepatic flexure 10; transverse C18.4 20; left: C18.5 splenic flexure 5, C18.6 descending 16, C18.7 sigmoid 106, C19.9 rectosigmoid 72, C20.9 rectum 81; C18.9 colon NOS 97 (side unknown).

## Key Genes
| Gene | Frequency | Clinical Relevance |
|------|-----------|-------------------|
| APC | ~80% | Initiating event in most CRC |
| TP53 | ~55% | Progression marker |
| KRAS | ~40% | Predicts anti-EGFR resistance |
| PIK3CA | ~15% | May predict aspirin benefit |
| BRAF | ~10% | V600E poor prognosis (MSS context) |
| SMAD4 | ~10% | TGF-β pathway |

## Notes
- MSI-H tumors respond well to immunotherapy
- KRAS/NRAS mutations contraindicate anti-EGFR therapy
- BRAF V600E has different prognosis in MSI-H vs MSS context
- Left vs right-sided tumors have different biology and outcomes
