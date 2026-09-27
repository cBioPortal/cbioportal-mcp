"""TCGA PanCan study guides may only name clinical attributes that exist in those studies."""

import re
from pathlib import Path

GUIDES = Path(__file__).parent.parent / "src" / "cbioportal_mcp" / "resources" / "study-guides"

# SELECT DISTINCT attribute_name FROM clinical_data_derived
# WHERE cancer_study_identifier LIKE '%_tcga_pan_can_atlas_2018' (identical set across studies,
# minus the hypoxia scores / GENETIC_ANCESTRY_LABEL in a few)
TCGA_PANCAN_ATTRIBUTES = {
    "AGE", "AJCC_PATHOLOGIC_TUMOR_STAGE", "AJCC_STAGING_EDITION", "ANEUPLOIDY_SCORE",
    "BUFFA_HYPOXIA_SCORE", "CANCER_TYPE", "CANCER_TYPE_ACRONYM", "CANCER_TYPE_DETAILED",
    "DAYS_LAST_FOLLOWUP", "DAYS_TO_BIRTH", "DAYS_TO_INITIAL_PATHOLOGIC_DIAGNOSIS", "DFS_MONTHS",
    "DFS_STATUS", "DSS_MONTHS", "DSS_STATUS", "ETHNICITY", "FORM_COMPLETION_DATE",
    "FRACTION_GENOME_ALTERED", "GENETIC_ANCESTRY_LABEL", "GRADE", "HISTORY_NEOADJUVANT_TRTYN",
    "ICD_10", "ICD_O_3_HISTOLOGY", "ICD_O_3_SITE", "INFORMED_CONSENT_VERIFIED",
    "IN_PANCANPATHWAYS_FREEZE", "MSI_SCORE_MANTIS", "MSI_SENSOR_SCORE", "MUTATION_COUNT",
    "NEW_TUMOR_EVENT_AFTER_INITIAL_TREATMENT", "ONCOTREE_CODE", "OS_MONTHS", "OS_STATUS",
    "OTHER_PATIENT_ID", "PATH_M_STAGE", "PATH_N_STAGE", "PATH_T_STAGE",
    "PERSON_NEOPLASM_CANCER_STATUS", "PFS_MONTHS", "PFS_STATUS",
    "PRIMARY_LYMPH_NODE_PRESENTATION_ASSESSMENT", "PRIOR_DX", "RACE", "RADIATION_THERAPY",
    "RAGNUM_HYPOXIA_SCORE", "SAMPLE_COUNT", "SAMPLE_TYPE", "SEX", "SOMATIC_STATUS", "SUBTYPE",
    "TBL_SCORE", "TISSUE_PROSPECTIVE_COLLECTION_INDICATOR",
    "TISSUE_RETROSPECTIVE_COLLECTION_INDICATOR", "TISSUE_SOURCE_SITE", "TISSUE_SOURCE_SITE_CODE",
    "TMB_NONSYNONYMOUS", "TUMOR_TISSUE_SITE", "TUMOR_TYPE", "WEIGHT", "WINTER_HYPOXIA_SCORE",
}

ROW_ATTRS = re.compile(r"^\|\s*(`[A-Z0-9_]+`(?:\s*/\s*`[A-Z0-9_]+`)*)\s*\|", re.M)


def test_tcga_pancan_guide_tables_name_only_real_attributes():
    guides = [GUIDES / "_tcga_pancan_template.md", *GUIDES.glob("*_tcga_pan_can_atlas_2018.md")]
    assert len(guides) > 1
    unknown = {}
    for guide in guides:
        names = {n for cell in ROW_ATTRS.findall(guide.read_text()) for n in re.findall(r"`([A-Z0-9_]+)`", cell)}
        bad = names - TCGA_PANCAN_ATTRIBUTES
        if bad:
            unknown[guide.name] = sorted(bad)
    assert not unknown, f"attributes not in TCGA PanCan studies: {unknown}"
