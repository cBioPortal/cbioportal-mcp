"""Print the MCP contract tier's fixture data as ClickHouse INSERT statements.

    python tests/contract/fixtures/seed.py > seed.sql

Loaded on top of cbioportal_schema.sql (the vendored, empty cBioPortal schema)
and BEFORE sql/*.sql, exactly like the daily clone job: base + derived tables
first, then the LLM-prep SQL reshapes them. Deterministic and tiny (four
studies, ~140 samples) — just enough that every tool and every sql/ view has
real rows to chew on:

  * two TCGA PanCancer studies (WES, >= 50 samples of one cancer type each, so
    sql/3's pan_cancer_tcga cohort and the >= 50-profiled-sample rule of the
    by-cancer-type views both produce rows);
  * msk_chord_2024 on a targeted panel (IMPACT468) with treatment timeline
    events, so the panel-coverage, treatment and portal-specific preference
    paths (sql/portal-specific/public-portal) are exercised;
  * a small study with no static study guide, so get_study_guide's
    database-backed path runs.

No randomness: values come from simple modular patterns so reruns are stable.
"""

from __future__ import annotations

GENES = {  # symbol -> (entrez, genetic_entity_id)
    "TP53": (7157, 1),
    "KRAS": (3845, 2),
    "EGFR": (1956, 3),
    "PIK3CA": (5290, 4),
    "BRAF": (673, 5),
    "MYC": (4609, 6),
    "ERBB2": (2064, 7),
    "ALK": (238, 8),
}
ALIASES = {"ERBB2": ["HER2", "NEU"], "MYC": ["c-Myc"]}
IMPACT468 = ["TP53", "KRAS", "EGFR", "PIK3CA", "BRAF", "ERBB2"]  # MYC, ALK off-panel

# cancer_study_id, identifier, type_of_cancer_id, name, cancer type, n samples, panel
STUDIES = [
    (
        1,
        "brca_tcga_pan_can_atlas_2018",
        "brca",
        "Breast Invasive Carcinoma (TCGA, PanCancer Atlas)",
        "Breast Cancer",
        60,
        "WES",
    ),
    (
        2,
        "luad_tcga_pan_can_atlas_2018",
        "luad",
        "Lung Adenocarcinoma (TCGA, PanCancer Atlas)",
        "Non-Small Cell Lung Cancer",
        55,
        "WES",
    ),
    (
        3,
        "msk_chord_2024",
        "mixed",
        "MSK-CHORD (MSK, Nature 2024)",
        "Non-Small Cell Lung Cancer",
        20,
        "IMPACT468",
    ),
    # No static guide in resources/study-guides/, so get_study_guide builds one
    # from the database (the path that actually queries ClickHouse).
    (
        4,
        "brca_contract_fixture_2025",
        "brca",
        "Breast Cancer (Contract Fixture, 2025)",
        "Breast Cancer",
        6,
        "WES",
    ),
]
TYPES_OF_CANCER = [
    ("brca", "Invasive Breast Carcinoma", "HotPink", "BRCA", "breast"),
    ("breast", "Breast", "HotPink", "Breast", "tissue"),
    ("luad", "Lung Adenocarcinoma", "Gainsboro", "LUAD", "nsclc"),
    ("nsclc", "Non-Small Cell Lung Cancer", "Gainsboro", "NSCLC", "lung"),
    ("lung", "Lung", "Gainsboro", "Lung", "tissue"),
    ("mixed", "Mixed Cancer Types", "Black", "MIXED", "tissue"),
    ("tissue", "Tissue", "White", "Tissue", None),
]
MUT_TYPES = ["Missense_Mutation", "Nonsense_Mutation", "Frame_Shift_Del"]
VARIANTS = {
    "TP53": "R175H",
    "KRAS": "G12D",
    "EGFR": "L858R",
    "PIK3CA": "E545K",
    "BRAF": "V600E",
    "MYC": "P57S",
    "ERBB2": "S310F",
    "ALK": "F1174L",
}


def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(q(x) for x in v) + "]"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def insert(table: str, columns: list[str], rows: list[tuple]) -> str:
    if not rows:
        return ""
    values = ",\n".join("(" + ", ".join(q(v) for v in row) + ")" for row in rows)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n{values};\n"


def build() -> list[str]:
    out: list[str] = []
    t: dict[str, list[tuple]] = {}

    def add(table, row):
        t.setdefault(table, []).append(row)

    add(
        "reference_genome",
        (
            1,
            "human",
            "hg19",
            "GRCh37",
            3_000_000_000,
            "http://hgdownload.cse.ucsc.edu/goldenPath/hg19/bigZips",
            None,
        ),
    )
    for row in TYPES_OF_CANCER:
        add("type_of_cancer", row)
    for sym, (entrez, ge) in GENES.items():
        add("genetic_entity", (ge, "GENE", sym))
        add("gene", (entrez, sym, ge, "protein-coding"))
        add("reference_genome_gene", (entrez, 1, "17", "17p13.1", 7_571_720, 7_590_868))
        for alias in ALIASES.get(sym, []):
            add("gene_alias", (entrez, alias))
    add("gene_panel", (1, "IMPACT468", "MSK-IMPACT 468-gene panel (fixture subset)"))
    for sym in IMPACT468:
        add("gene_panel_list", (1, GENES[sym][0]))
        add("gene_panel_to_gene_derived", ("IMPACT468", sym))

    clinical_attrs = [  # attr_id, display, datatype, patient_attribute
        ("CANCER_TYPE", "Cancer Type", "STRING", 0),
        ("SAMPLE_TYPE", "Sample Type", "STRING", 0),
        ("ONCOTREE_CODE", "Oncotree Code", "STRING", 0),
        ("AGE", "Diagnosis Age", "NUMBER", 1),
        ("SEX", "Sex", "STRING", 1),
        ("OS_MONTHS", "Overall Survival (Months)", "NUMBER", 1),
        ("OS_STATUS", "Overall Survival Status", "STRING", 1),
    ]

    patient_id = sample_id = profile_id = list_id = event_id = cd_id = 0
    for study_id, ident, toc, name, cancer_type, n, panel in STUDIES:
        add(
            "cancer_study",
            (
                study_id,
                ident,
                toc,
                name,
                f"Contract fixture study {ident}.",
                1,
                "12345678",
                "Fixture et al. 2024",
                "PUBLIC",
                0,
                None,
                1,
            ),
        )
        for attr_id, display, datatype, is_patient in clinical_attrs:
            add(
                "clinical_attribute_meta",
                (attr_id, display, display, datatype, is_patient, "1", study_id),
            )

        # genetic profiles: mutations, discrete CNA, SV, mRNA
        profiles = {}
        for suffix, alt_type, datatype, pname in [
            ("mutations", "MUTATION_EXTENDED", "MAF", "Mutations"),
            (
                "gistic",
                "COPY_NUMBER_ALTERATION",
                "DISCRETE",
                "Putative copy-number alterations from GISTIC",
            ),
            ("structural_variants", "STRUCTURAL_VARIANT", "SV", "Structural variants"),
            ("rna_seq_v2_mrna", "MRNA_EXPRESSION", "CONTINUOUS", "mRNA Expression, RSEM"),
        ]:
            profile_id += 1
            profiles[suffix] = (profile_id, f"{ident}_{suffix}", alt_type)
            add(
                "genetic_profile",
                (
                    profile_id,
                    f"{ident}_{suffix}",
                    study_id,
                    alt_type,
                    None,
                    datatype,
                    pname,
                    None,
                    1,
                    None,
                    None,
                    0,
                ),
            )

        lists = {}
        for suffix, category in [
            ("all", "all_cases_in_study"),
            ("sequenced", "all_cases_with_mutation_data"),
            ("cna", "all_cases_with_cna_data"),
            ("rna_seq_v2_mrna", "all_cases_with_mrna_rnaseq_data"),
        ]:
            list_id += 1
            lists[suffix] = list_id
            add(
                "sample_list",
                (list_id, f"{ident}_{suffix}", category, study_id, f"{suffix} samples", None),
            )

        for i in range(n):
            if i % 2 == 0:
                patient_id += 1
                p_stable = f"P-{study_id}{i // 2:04d}"
                add("patient", (patient_id, p_stable, study_id))
                pu = f"{ident}_{p_stable}"
                for attr, val in [
                    ("AGE", str(40 + i % 40)),
                    ("SEX", "Female" if i % 4 else "Male"),
                    ("OS_MONTHS", f"{12 + i % 50}.5"),
                    ("OS_STATUS", "1:DECEASED" if i % 6 == 0 else "0:LIVING"),
                ]:
                    cd_id += 1
                    add("clinical_patient", (patient_id, attr, val))
                    add("clinical_data_derived", (patient_id, "", pu, attr, val, ident, "patient"))
                if ident == "msk_chord_2024":
                    for agent, ttype, day in [
                        ("CARBOPLATIN", "Chemo", 10),
                        ("PEMETREXED", "Chemo", 10),
                        ("OSIMERTINIB", "Targeted", 200 + i),
                    ]:
                        event_id += 1
                        add("clinical_event", (event_id, patient_id, day, day + 90, "Treatment"))
                        for k, v in [
                            ("AGENT", agent),
                            ("TREATMENT_TYPE", ttype),
                            ("SUBTYPE", ttype),
                        ]:
                            add("clinical_event_data", (event_id, k, v))
                            add(
                                "clinical_event_data_derived",
                                (pu, k, v, day, day + 90, "Treatment", ident),
                            )
            sample_id += 1
            s_stable = f"S-{study_id}{i:04d}"
            su = f"{ident}_{s_stable}"
            add("sample", (sample_id, s_stable, "Primary Solid Tumor", patient_id))
            add(
                "sample_derived",
                (
                    su,
                    su,
                    s_stable,
                    pu,
                    pu,
                    p_stable,
                    ident,
                    sample_id,
                    patient_id,
                    "Primary Solid Tumor",
                    1,
                    0,
                ),
            )
            stype = "Metastasis" if i % 5 == 0 else "Primary"
            otc = toc.upper() if toc != "mixed" else "LUAD"
            for attr, val in [
                ("CANCER_TYPE", cancer_type),
                ("SAMPLE_TYPE", stype),
                ("ONCOTREE_CODE", otc),
            ]:
                add("clinical_sample", (sample_id, attr, val))
                add("clinical_data_derived", (sample_id, su, pu, attr, val, ident, "sample"))
            for suffix in ("all", "sequenced", "cna", "rna_seq_v2_mrna"):
                add("sample_list_list", (lists[suffix], sample_id))
            panel_internal = None if panel == "WES" else 1
            for suffix in ("mutations", "gistic", "structural_variants", "rna_seq_v2_mrna"):
                pid, pstable, alt_type = profiles[suffix]
                add("sample_profile", (sample_id, pid, panel_internal))
                if alt_type != "MRNA_EXPRESSION":
                    add("sample_to_gene_panel_derived", (su, alt_type, panel, ident, pstable))

            covered = set(GENES) if panel == "WES" else set(IMPACT468)
            for g_idx, (sym, (entrez, _)) in enumerate(GENES.items()):
                on_panel = sym in covered
                # mutations: TP53 in ~1/2, KRAS ~1/3, others rarer; one UNCALLED call
                if (i + g_idx) % (2 + g_idx) == 0:
                    status = "UNCALLED" if (i + g_idx) % 7 == 0 else "Somatic"
                    add(
                        "genomic_event_derived",
                        (
                            su,
                            sym,
                            entrez,
                            panel,
                            ident,
                            profiles["mutations"][1],
                            "mutation",
                            f"p.{VARIANTS[sym]}",
                            MUT_TYPES[(i + g_idx) % 3],
                            status,
                            "",
                            "",
                            "",
                            "",
                            None,
                            "",
                            "",
                            pu,
                            not on_panel,
                        ),
                    )
                # discrete CNA: amp / homdel
                if (i * 3 + g_idx) % 9 == 0:
                    cna = 2 if g_idx % 2 == 0 else -2
                    add(
                        "genomic_event_derived",
                        (
                            su,
                            sym,
                            entrez,
                            panel,
                            ident,
                            profiles["gistic"][1],
                            "cna",
                            "NA",
                            "",
                            "NA",
                            "",
                            "",
                            "",
                            "",
                            cna,
                            "17p13.1",
                            "",
                            pu,
                            not on_panel,
                        ),
                    )
                # GISTIC values (incl. shallow) and mRNA for every gene
                add(
                    "genetic_alteration_derived",
                    (su, ident, sym, "gistic", str(((i + g_idx) % 5) - 2)),
                )
                add(
                    "genetic_alteration_derived",
                    (su, ident, sym, "rna_seq_v2_mrna", f"{(i * (g_idx + 1)) % 97 + 0.5:.1f}"),
                )
            if i % 10 == 0:
                sym = "ALK" if panel == "WES" else "EGFR"
                add(
                    "genomic_event_derived",
                    (
                        su,
                        sym,
                        GENES[sym][0],
                        panel,
                        ident,
                        profiles["structural_variants"][1],
                        "structural_variant",
                        "EML4-ALK",
                        "",
                        "SOMATIC",
                        "",
                        "",
                        "",
                        "",
                        None,
                        "",
                        "EML4-ALK fusion",
                        pu,
                        False,
                    ),
                )

        add(
            "resource_definition",
            ("HE_SLIDE", "H&E Slide", "Fixture H&E slide", "SAMPLE", 1, 1, study_id, None),
        )
        add(
            "resource",
            (
                study_id,
                "HE_SLIDE",
                study_id,
                "SAMPLE",
                None,
                f"S-{study_id}0000",
                "https://example.org/slide",
                "H&E Slide",
                None,
            ),
        )

    cols = {
        "reference_genome": [
            "reference_genome_id",
            "species",
            "name",
            "build_name",
            "genome_size",
            "url",
            "release_date",
        ],
        "type_of_cancer": ["type_of_cancer_id", "name", "dedicated_color", "short_name", "parent"],
        "genetic_entity": ["id", "entity_type", "stable_id"],
        "gene": ["entrez_gene_id", "hugo_gene_symbol", "genetic_entity_id", "type"],
        "reference_genome_gene": [
            "entrez_gene_id",
            "reference_genome_id",
            "chr",
            "cytoband",
            "start",
            "end",
        ],
        "gene_alias": ["entrez_gene_id", "gene_alias"],
        "gene_panel": ["internal_id", "stable_id", "description"],
        "gene_panel_list": ["internal_id", "gene_id"],
        "gene_panel_to_gene_derived": ["gene_panel_id", "gene"],
        "cancer_study": [
            "cancer_study_id",
            "cancer_study_identifier",
            "type_of_cancer_id",
            "name",
            "description",
            "public",
            "pmid",
            "citation",
            "groups",
            "status",
            "import_date",
            "reference_genome_id",
        ],
        "clinical_attribute_meta": [
            "attr_id",
            "display_name",
            "description",
            "datatype",
            "patient_attribute",
            "priority",
            "cancer_study_id",
        ],
        "genetic_profile": [
            "genetic_profile_id",
            "stable_id",
            "cancer_study_id",
            "genetic_alteration_type",
            "generic_assay_type",
            "datatype",
            "name",
            "description",
            "show_profile_in_analysis_tab",
            "pivot_threshold",
            "sort_order",
            "patient_level",
        ],
        "sample_list": [
            "list_id",
            "stable_id",
            "category",
            "cancer_study_id",
            "name",
            "description",
        ],
        "sample_list_list": ["list_id", "sample_id"],
        "patient": ["internal_id", "stable_id", "cancer_study_id"],
        "clinical_patient": ["internal_id", "attr_id", "attr_value"],
        "clinical_sample": ["internal_id", "attr_id", "attr_value"],
        "clinical_data_derived": [
            "internal_id",
            "sample_unique_id",
            "patient_unique_id",
            "attribute_name",
            "attribute_value",
            "cancer_study_identifier",
            "type",
        ],
        "clinical_event": [
            "clinical_event_id",
            "patient_id",
            "start_date",
            "stop_date",
            "event_type",
        ],
        "clinical_event_data": ["clinical_event_id", "key", "value"],
        "clinical_event_data_derived": [
            "patient_unique_id",
            "key",
            "value",
            "start_date",
            "stop_date",
            "event_type",
            "cancer_study_identifier",
        ],
        "sample": ["internal_id", "stable_id", "sample_type", "patient_id"],
        "sample_derived": [
            "sample_unique_id",
            "sample_unique_id_base64",
            "sample_stable_id",
            "patient_unique_id",
            "patient_unique_id_base64",
            "patient_stable_id",
            "cancer_study_identifier",
            "internal_id",
            "patient_internal_id",
            "sample_type",
            "sequenced",
            "copy_number_segment_present",
        ],
        "sample_profile": ["sample_id", "genetic_profile_id", "panel_id"],
        "sample_to_gene_panel_derived": [
            "sample_unique_id",
            "alteration_type",
            "gene_panel_id",
            "cancer_study_identifier",
            "genetic_profile_id",
        ],
        "genomic_event_derived": [
            "sample_unique_id",
            "hugo_gene_symbol",
            "entrez_gene_id",
            "gene_panel_stable_id",
            "cancer_study_identifier",
            "genetic_profile_stable_id",
            "variant_type",
            "mutation_variant",
            "mutation_type",
            "mutation_status",
            "driver_filter",
            "driver_filter_annotation",
            "driver_tiers_filter",
            "driver_tiers_filter_annotation",
            "cna_alteration",
            "cna_cytoband",
            "sv_event_info",
            "patient_unique_id",
            "off_panel",
        ],
        "genetic_alteration_derived": [
            "sample_unique_id",
            "cancer_study_identifier",
            "hugo_gene_symbol",
            "profile_type",
            "alteration_value",
        ],
        "resource_definition": [
            "resource_id",
            "display_name",
            "description",
            "resource_type",
            "open_by_default",
            "priority",
            "cancer_study_id",
            "custom_metadata",
        ],
        "resource": [
            "internal_id",
            "resource_id",
            "cancer_study_id",
            "resource_scope",
            "patient_id",
            "sample_id",
            "url",
            "display_name",
            "metadata",
        ],
    }
    for table, columns in cols.items():
        out.append(insert(table, columns, t.get(table, [])))
    missing = set(t) - set(cols)
    assert not missing, f"rows without a column list: {missing}"
    return out


if __name__ == "__main__":
    print("\n".join(s for s in build() if s))
