"""Synthetic cBioPortal derived-table fixture for the precomputed-aggregate tests.

Builds the handful of tables sql/4 and sql/8 read, in the same shape as
cbioportal's db-scripts/clickhouse/clickhouse.sql, with the edge cases the
frequency recipes exist to get right:

  * WES samples (profiled for every gene) next to named panels that list
    only some genes, plus a panel id that is not in gene_panel at all;
  * samples with TWO profiles of one type: WES + panel, and panel + panel
    (the case where summing per-block counts double-counts);
  * off_panel calls, UNCALLED mutations, CNA +-1 (not counted), multiple
    events per sample-gene;
  * samples with no CANCER_TYPE, and a study outside every preference;
  * continuous (log2) CNA profiles next to DISCRETE ones, including samples
    that only have log2 data, and UNCALLED structural variants — the two
    places where the single-study and cross-study recipes disagree.

Also provides a pure-Python oracle that computes the expected counts
straight from the generated rows, independent of any SQL.
"""

import random
from collections import defaultdict

GENES = {
    "TP53": 7157,
    "KRAS": 3845,
    "EGFR": 1956,
    "MYC": 4609,
    "ALK": 238,
    "PTEN": 5728,
    "BRAF": 673,
    "C1orf112": 55732,
}
PANELS = {  # internal_id, stable_id -> genes
    1: ("PANEL_A", ["TP53", "KRAS", "EGFR"]),
    2: ("PANEL_B", ["TP53", "MYC", "ALK", "EGFR"]),
    3: ("PANEL_C", ["PTEN", "KRAS", "BRAF"]),
}
PANEL_GENES = {stable: set(genes) for stable, genes in PANELS.values()}
CANCER_TYPES = ["Breast Cancer", "Non-Small Cell Lung Cancer", "Colorectal Cancer"]
STUDIES = {1: "study_wes", 2: "study_panel", 3: "study_mixed", 4: "study_tiny"}
PREFERENCES = {
    "pref_all": ["study_wes", "study_panel", "study_mixed"],
    "pref_panel": ["study_panel"],
    "pref_mixed": ["study_mixed"],
    "pan_cancer_tcga": ["study_wes", "study_panel"],
}
ALTERATION_PROFILE = {
    "mutation": "MUTATION_EXTENDED",
    "amplification": "COPY_NUMBER_ALTERATION",
    "deep_deletion": "COPY_NUMBER_ALTERATION",
    "structural_variant": "STRUCTURAL_VARIANT",
}
GENOMIC_TYPES = ("MUTATION_EXTENDED", "COPY_NUMBER_ALTERATION", "STRUCTURAL_VARIANT")
# genetic_profile.datatype by the profile-id suffix used in _profiles_for().
PROFILE_DATATYPES = {
    "mut": "MAF",
    "mut_panel": "MAF",
    "cna": "DISCRETE",
    "cna_log2": "LOG2-VALUE",
    "sv": "SV",
    "mrna": "CONTINUOUS",
}

DDL = [
    "CREATE TABLE gene (entrez_gene_id Int64, hugo_gene_symbol String) "
    "ENGINE = MergeTree ORDER BY entrez_gene_id",
    "CREATE TABLE gene_panel (internal_id Int64, stable_id String) "
    "ENGINE = MergeTree ORDER BY internal_id",
    "CREATE TABLE gene_panel_list (internal_id Int64, gene_id Int64) "
    "ENGINE = MergeTree ORDER BY internal_id",
    "CREATE TABLE cancer_study (cancer_study_id Int64, cancer_study_identifier String, "
    "name String) ENGINE = MergeTree ORDER BY cancer_study_id",
    "CREATE TABLE genetic_profile (genetic_profile_id Int64, stable_id String, "
    "cancer_study_id Int64, genetic_alteration_type String, datatype String) "
    "ENGINE = MergeTree ORDER BY genetic_profile_id",
    "CREATE TABLE genetic_alteration_derived (sample_unique_id String, "
    "cancer_study_identifier LowCardinality(String), hugo_gene_symbol String, "
    "profile_type LowCardinality(String), alteration_value Nullable(String)) "
    "ENGINE = MergeTree ORDER BY (cancer_study_identifier, hugo_gene_symbol, profile_type, "
    "sample_unique_id)",
    "CREATE TABLE cancer_study_query_preferences (preference_name LowCardinality(String), "
    "cancer_study_identifier String, notes String) ENGINE = MergeTree "
    "ORDER BY (preference_name, cancer_study_identifier)",
    "CREATE TABLE sample_to_gene_panel_derived (sample_unique_id String, "
    "alteration_type LowCardinality(String), gene_panel_id LowCardinality(String), "
    "cancer_study_identifier LowCardinality(String), genetic_profile_id LowCardinality(String)) "
    "ENGINE = MergeTree ORDER BY (gene_panel_id, alteration_type, genetic_profile_id, "
    "sample_unique_id)",
    "CREATE TABLE genomic_event_derived (sample_unique_id String, hugo_gene_symbol String, "
    "entrez_gene_id Int32, gene_panel_stable_id LowCardinality(String), "
    "cancer_study_identifier LowCardinality(String), "
    "genetic_profile_stable_id LowCardinality(String), variant_type LowCardinality(String), "
    "mutation_status LowCardinality(String), cna_alteration Nullable(Int8), "
    "patient_unique_id String, off_panel Boolean DEFAULT FALSE, "
    "mutation_variant String DEFAULT '', mutation_type LowCardinality(String) DEFAULT '', "
    "cna_cytoband String DEFAULT '') ENGINE = MergeTree "
    "ORDER BY (genetic_profile_stable_id, cancer_study_identifier, variant_type, "
    "entrez_gene_id, hugo_gene_symbol, sample_unique_id)",
    "CREATE TABLE clinical_data_derived (internal_id Int, sample_unique_id String, "
    "patient_unique_id String, attribute_name LowCardinality(String), attribute_value String, "
    "cancer_study_identifier LowCardinality(String), type LowCardinality(String)) "
    "ENGINE = MergeTree ORDER BY (type, attribute_name, sample_unique_id)",
    "CREATE TABLE sample_derived (sample_unique_id String, patient_unique_id String, "
    "cancer_study_identifier LowCardinality(String)) ENGINE = MergeTree "
    "ORDER BY (cancer_study_identifier, sample_unique_id)",
]


def _profiles_for(study: str, i: int, rng: random.Random) -> list[tuple[str, str, str]]:
    """(alteration_type, gene_panel_id, genetic_profile_id) rows for sample i."""
    m, c, s = "MUTATION_EXTENDED", "COPY_NUMBER_ALTERATION", "STRUCTURAL_VARIANT"
    rows = []
    if study == "study_wes":
        rows += [(m, "WES", "mut"), (c, "WES", "cna")]
        if rng.random() < 0.7:
            rows.append((s, "WES", "sv"))
    elif study == "study_panel":
        rows.append((m, "PANEL_A" if i % 2 else "PANEL_B", "mut"))
        if i % 6:
            rows.append((c, "PANEL_A", "cna"))
        rows.append((c, "PANEL_A", "cna_log2"))
        if i % 2 == 0:
            rows.append((s, "PANEL_B", "sv"))
    elif study == "study_mixed":
        bucket = i % 14
        if bucket < 4:
            rows.append((m, "WES", "mut"))
        elif bucket < 8:
            rows.append((m, "PANEL_C", "mut"))
        elif bucket < 10:  # WES for one mutation profile, panel for another
            rows += [(m, "WES", "mut"), (m, "PANEL_A", "mut_panel")]
        elif bucket < 12:  # two named panels
            rows += [(m, "PANEL_A", "mut"), (m, "PANEL_C", "mut_panel")]
        elif bucket == 12:  # panel id missing from gene_panel -> profiled for nothing
            rows.append((m, "ORPHAN", "mut"))
        # bucket 13: no mutation profile at all
        if i % 9:  # every 9th sample has only the log2 CNA profile
            rows.append((c, "WES" if i % 2 else "PANEL_B", "cna"))
        rows.append((c, "WES", "cna_log2"))
        if i % 7 == 0:
            rows.append((s, "PANEL_A", "sv"))
        if i % 5 == 0:
            rows.append(("MRNA_EXPRESSION", "WES", "mrna"))
    else:  # study_tiny
        rows += [(m, "WES", "mut"), (c, "WES", "cna")]
    return [(t, p, f"{study}_{prof}") for t, p, prof in rows]


def generate(seed: int = 7) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    tables: dict[str, list[tuple]] = defaultdict(list)
    for sym, entrez in GENES.items():
        tables["gene"].append((entrez, sym))
    for internal_id, (stable, genes) in PANELS.items():
        tables["gene_panel"].append((internal_id, stable))
        for g in genes:
            tables["gene_panel_list"].append((internal_id, GENES[g]))
    study_ids = {ident: sid for sid, ident in STUDIES.items()}
    for sid, ident in STUDIES.items():
        tables["cancer_study"].append((sid, ident, ident.replace("_", " ")))
    profiles_seen = {}
    for pref, studies in PREFERENCES.items():
        for ident in studies:
            tables["cancer_study_query_preferences"].append((pref, ident, "fixture"))

    sizes = {"study_wes": 130, "study_panel": 160, "study_mixed": 140, "study_tiny": 10}
    internal_id = 0
    for study, n in sizes.items():
        for i in range(n):
            sample = f"{study}_S{i:04d}"
            patient = f"{study}_P{i // 2:04d}"  # two samples per patient
            tables["sample_derived"].append((sample, patient, study))
            internal_id += 1
            if rng.random() > 0.05:
                cancer_type = rng.choices(CANCER_TYPES, weights=[5, 3, 2])[0]
                tables["clinical_data_derived"].append(
                    (internal_id, sample, patient, "CANCER_TYPE", cancer_type, study, "sample")
                )
            tables["clinical_data_derived"].append(
                (internal_id, sample, patient, "SAMPLE_TYPE", "Primary", study, "sample")
            )
            profiles = _profiles_for(study, i, rng)
            for alt_type, panel, profile_id in profiles:
                tables["sample_to_gene_panel_derived"].append(
                    (sample, alt_type, panel, study, profile_id)
                )
                profiles_seen[profile_id] = (study, alt_type)
            for alt_type, panel, profile_id in profiles:
                if (
                    alt_type not in GENOMIC_TYPES
                    or _datatype(profile_id) != _EVENT_DATATYPE[alt_type]
                ):
                    continue  # log2 CNA profiles carry no AMP / HOMDEL events
                covered = set(GENES) if panel == "WES" else PANEL_GENES.get(panel, set())
                for gene, entrez in GENES.items():
                    on_panel = gene in covered
                    base = 0.3 if on_panel else 0.06  # some off-panel calls to exclude
                    if rng.random() >= base:
                        continue
                    for _ in range(rng.choice([1, 1, 1, 2])):
                        if alt_type == "MUTATION_EXTENDED":
                            status = "UNCALLED" if rng.random() < 0.15 else "SOMATIC"
                            row = ("mutation", status, None)
                        elif alt_type == "COPY_NUMBER_ALTERATION":
                            row = ("cna", "NA", rng.choice([-2, -1, 1, 2, 2]))
                        else:
                            status = "UNCALLED" if rng.random() < 0.2 else "SOMATIC"
                            row = ("structural_variant", status, None)
                        tables["genomic_event_derived"].append(
                            (
                                sample,
                                gene,
                                entrez,
                                panel,
                                study,
                                profile_id,
                                row[0],
                                row[1],
                                row[2],
                                patient,
                                not on_panel,
                            )
                        )
    for gp_id, (profile_id, (study, alt_type)) in enumerate(sorted(profiles_seen.items()), 1):
        tables["genetic_profile"].append(
            (gp_id, profile_id, study_ids[study], alt_type, _datatype(profile_id))
        )
    return dict(tables)


_EVENT_DATATYPE = {
    "MUTATION_EXTENDED": "MAF",
    "COPY_NUMBER_ALTERATION": "DISCRETE",
    "STRUCTURAL_VARIANT": "SV",
}


def _datatype(profile_id: str) -> str:
    for suffix in sorted(PROFILE_DATATYPES, key=len, reverse=True):
        if profile_id.endswith("_" + suffix):
            return PROFILE_DATATYPES[suffix]
    raise ValueError(profile_id)


INSERT_COLUMNS = {
    "gene": "entrez_gene_id, hugo_gene_symbol",
    "gene_panel": "internal_id, stable_id",
    "gene_panel_list": "internal_id, gene_id",
    "cancer_study": "cancer_study_id, cancer_study_identifier, name",
    "cancer_study_query_preferences": "preference_name, cancer_study_identifier, notes",
    "genetic_profile": "genetic_profile_id, stable_id, cancer_study_id, genetic_alteration_type, "
    "datatype",
    "sample_to_gene_panel_derived": (
        "sample_unique_id, alteration_type, gene_panel_id, cancer_study_identifier, "
        "genetic_profile_id"
    ),
    "genomic_event_derived": (
        "sample_unique_id, hugo_gene_symbol, entrez_gene_id, gene_panel_stable_id, "
        "cancer_study_identifier, genetic_profile_stable_id, variant_type, mutation_status, "
        "cna_alteration, patient_unique_id, off_panel"
    ),
    "clinical_data_derived": (
        "internal_id, sample_unique_id, patient_unique_id, attribute_name, attribute_value, "
        "cancer_study_identifier, type"
    ),
    "sample_derived": "sample_unique_id, patient_unique_id, cancer_study_identifier",
}


# ---------------------------------------------------------------------------
# Pure-Python oracle
# ---------------------------------------------------------------------------


def _event_alteration(variant_type, status, cna, *, single_study):
    if variant_type == "mutation" and status != "UNCALLED":
        return "mutation"
    if variant_type == "cna" and cna == 2:
        return "amplification"
    if variant_type == "cna" and cna == -2:
        return "deep_deletion"
    if variant_type == "structural_variant" and not (single_study and status == "UNCALLED"):
        return "structural_variant"
    return None


def _counts_as_profiled(alt_type, profile_id, *, single_study):
    if alt_type not in GENOMIC_TYPES:
        return False
    if single_study and alt_type == "COPY_NUMBER_ALTERATION":
        return _datatype(profile_id) == "DISCRETE"
    return True


def _profiled_sets(tables, *, single_study):
    """(study, profile_type, gene) -> set(samples), profile_type incl. 'ANY'."""
    panel_rows = defaultdict(set)  # (study, profile_type, sample) -> panels
    for sample, alt_type, panel, study, profile_id in tables["sample_to_gene_panel_derived"]:
        if _counts_as_profiled(alt_type, profile_id, single_study=single_study):
            panel_rows[(study, alt_type, sample)].add(panel)
            panel_rows[(study, "ANY", sample)].add(panel)
    profiled = defaultdict(set)
    for (study, ptype, sample), panels in panel_rows.items():
        for gene in GENES:
            if "WES" in panels or any(gene in PANEL_GENES.get(p, ()) for p in panels):
                profiled[(study, ptype, gene)].add(sample)
    return profiled


def _altered_sets(tables, *, single_study):
    """(study, alteration_type, gene) -> set(samples), alteration_type incl. 'any'."""
    altered = defaultdict(set)
    events = defaultdict(int)
    for row in tables["genomic_event_derived"]:
        sample, gene, _, _, study, _, vtype, status, cna, _, off_panel = row
        if off_panel:
            continue
        alt = _event_alteration(vtype, status, cna, single_study=single_study)
        if alt is None:
            continue
        for key in (alt, "any"):
            altered[(study, key, gene)].add(sample)
            events[(study, key, gene)] += 1
    return altered, events


def profile_type_of(alteration_type):
    return "ANY" if alteration_type == "any" else ALTERATION_PROFILE[alteration_type]


def oracle_study_profiled(tables, study, alteration_type, gene) -> int:
    """Per-study denominator, also for genes that have no altered sample."""
    profiled = _profiled_sets(tables, single_study=True)
    return len(profiled.get((study, profile_type_of(alteration_type), gene), ()))


def oracle_study_counts(tables) -> dict[tuple, tuple]:
    """(study, gene, alteration_type) -> (altered, profiled, events) for altered > 0.

    Single-study rules: DISCRETE CNA profiles only, UNCALLED SVs excluded.
    """
    profiled = _profiled_sets(tables, single_study=True)
    altered, events = _altered_sets(tables, single_study=True)
    out = {}
    for (study, alt, gene), samples in altered.items():
        prof = profiled.get((study, profile_type_of(alt), gene), set())
        out[(study, gene, alt)] = (len(samples), len(prof), events[(study, alt, gene)])
    return out


def oracle_cancer_type_counts(tables) -> dict[tuple, tuple]:
    """(preference, cancer_type, gene, alteration_type) -> (altered, profiled), altered > 0.

    Cross-study rules of gene_alteration_frequency_by_cancer_type: every CNA
    profile, every SV.
    """
    cancer_type = {}
    for row in tables["clinical_data_derived"]:
        if row[3] == "CANCER_TYPE":
            cancer_type[row[1]] = row[4]
    profiled = _profiled_sets(tables, single_study=False)
    altered, _ = _altered_sets(tables, single_study=False)
    out = {}
    for pref, studies in PREFERENCES.items():
        buckets = defaultdict(lambda: [set(), set()])
        for (study, alt, gene), samples in altered.items():
            if study in studies:
                for s in samples:
                    if s in cancer_type:
                        buckets[(cancer_type[s], gene, alt)][0].add(s)
        for key, (alt_samples, prof_samples) in buckets.items():
            ct, gene, alt = key
            for study in studies:
                for s in profiled.get((study, profile_type_of(alt), gene), ()):
                    if cancer_type.get(s) == ct:
                        prof_samples.add(s)
            out[(pref, ct, gene, alt)] = (len(alt_samples), len(prof_samples))
    return out


def oracle_profiled_counts(tables) -> dict[tuple, tuple]:
    """(study, profile_type) -> (samples, patients, wes_samples)."""
    patient_of = {s: p for s, p, _ in tables["sample_derived"]}
    groups = defaultdict(lambda: [set(), set()])
    for sample, alt_type, panel, study, profile_id in tables["sample_to_gene_panel_derived"]:
        keys = [alt_type]
        discrete = _counts_as_profiled(alt_type, profile_id, single_study=True)
        if alt_type == "COPY_NUMBER_ALTERATION" and discrete:
            keys.append("COPY_NUMBER_ALTERATION_DISCRETE")
        if discrete:
            keys.append("ANY_MUT_CNA_SV")
        for k in keys:
            groups[(study, k)][0].add(sample)
            if panel == "WES":
                groups[(study, k)][1].add(sample)
    out = {}
    for (study, k), (samples, wes) in groups.items():
        out[(study, k)] = (len(samples), len({patient_of[s] for s in samples}), len(wes))
    all_samples = defaultdict(set)
    for s, _, study in tables["sample_derived"]:
        all_samples[study].add(s)
    for study, samples in all_samples.items():
        out[(study, "ALL_SAMPLES")] = (len(samples), len({patient_of[s] for s in samples}), 0)
    return out
