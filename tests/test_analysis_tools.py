"""Tests for the analysis tools added for the STRESS questions.

- mutation_diagram domain / protein_range filters and multi-study scopes (T11, T13)
- alteration_enrichment, the genome-wide group comparison (T7)
- nucleotide_variants, codon-level detail behind a protein change (T12)
- mutation_allele_frequency + histogram_chart, a real distribution chart (T14)
"""

import pytest
from _fakedb import FakeDB

from cbioportal_mcp import server, ui

STUDY = "study_x"

EGFR_TRANSCRIPT = {
    "transcriptId": "ENST00000275493",
    "proteinLength": 1210,
    "pfamDomains": [
        {"pfamDomainId": "PF01030", "pfamDomainStart": 57, "pfamDomainEnd": 167},
        {"pfamDomainId": "PF00757", "pfamDomainStart": 185, "pfamDomainEnd": 338},
        {"pfamDomainId": "PF01030", "pfamDomainStart": 361, "pfamDomainEnd": 480},
        {"pfamDomainId": "PF07714", "pfamDomainStart": 713, "pfamDomainEnd": 965},
    ],
}
PFAM = {
    "PF01030": {"name": "Recep_L_domain", "description": "Receptor L domain"},
    "PF00757": {"name": "Furin-like", "description": "Furin-like cysteine rich region"},
    "PF07714": {"name": "Pkinase_Tyr", "description": "Protein tyrosine kinase"},
}


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    fake.add_study(STUDY)
    monkeypatch.setattr(server, "run_select_query", fake)
    return fake


@pytest.fixture
def genome_nexus(monkeypatch):
    calls = []

    def fake_get(path):
        calls.append(path)
        if path.startswith("/ensembl/canonical-transcript/hgnc/EGFR"):
            return EGFR_TRANSCRIPT
        if path.startswith("/pfam/domain/"):
            return PFAM[path.rsplit("/", 1)[1]]
        raise ValueError("Could not reach Genome Nexus")

    monkeypatch.setattr(server, "_genome_nexus_get", fake_get)
    server._GENOME_NEXUS_CACHE.clear()
    yield calls
    server._GENOME_NEXUS_CACHE.clear()


# --- mutation_diagram: regions -----------------------------------------------------


def _egfr_study(db):
    s = [db.add_sample(STUDY, f"s{i}") for i in range(1, 7)]
    db.add_mutation(s[0], "EGFR", "L858R")  # kinase domain
    db.add_mutation(s[1], "EGFR", "E746_A750del", "In_Frame_Del")  # kinase domain
    db.add_mutation(s[2], "EGFR", "A289V")  # Furin-like
    db.add_mutation(s[3], "EGFR", "L858R")
    db.add_mutation(s[4], "EGFR", "NA", "Splice_Region")  # unplaceable
    db.add_mutation(s[5], "EGFR", "V1010M")  # outside every domain
    return s


@pytest.mark.parametrize("domain", ["PF07714", "pkinase_tyr", "tyrosine kinase", "kinase domain"])
def test_domain_filter_counts_only_mutations_inside(db, genome_nexus, domain):
    _egfr_study(db)
    p = server.mutation_diagram(study_id=STUDY, gene="EGFR", domain=domain)
    assert "error" not in p, p.get("error")
    region = p["region"]
    assert region["pfam_accession"] == "PF07714" and region["ranges"] == [[713, 965]]
    assert region["transcript_id"] == "ENST00000275493"
    assert region["n_samples_in_region"] == 3
    assert region["n_samples_mutated_anywhere"] == 6
    assert region["pct_of_mutated_samples"] == 50.0
    assert region["n_protein_changes_in_region"] == 2
    assert region["n_samples_unplaceable"] == 1
    assert {m["protein_change"] for m in p["mutations"]} == {"L858R", "E746_A750del"}
    assert p["n_samples_mutated"] == 3


def test_repeated_domain_uses_every_instance(db, genome_nexus):
    s = [db.add_sample(STUDY, f"s{i}") for i in range(1, 3)]
    db.add_mutation(s[0], "EGFR", "R108K")
    db.add_mutation(s[1], "EGFR", "A400T")
    p = server.mutation_diagram(study_id=STUDY, gene="EGFR", domain="PF01030")
    assert p["region"]["ranges"] == [[57, 167], [361, 480]]
    assert p["region"]["n_samples_in_region"] == 2


def test_ambiguous_and_unknown_domains_list_the_choices(db, genome_nexus):
    _egfr_study(db)
    # Every word is a stopword or matches nothing, so this is "no match", not "ambiguous".
    stopwords = server.mutation_diagram(
        study_id=STUDY, gene="EGFR", domain="domain receptor cysteine"
    )
    assert (
        "No Pfam domain of EGFR matches 'domain receptor cysteine'" in stopwords["error"]
    )
    several = server.mutation_diagram(study_id=STUDY, gene="EGFR", domain="r")
    assert "'r' matches several EGFR Pfam domains; pass one accession" in several["error"]
    none = server.mutation_diagram(study_id=STUDY, gene="EGFR", domain="SH2")
    assert "No Pfam domain of EGFR matches 'SH2'" in none["error"]
    # Whichever branch it is, the error lists every domain so the caller can pick one.
    for out in (stopwords, several, none):
        assert "PF07714 Pkinase_Tyr (Protein tyrosine kinase) codons 713-965" in out["error"]
        assert "PF01030 Recep_L_domain (Receptor L domain) codons 57-167" in out["error"]


def test_unreachable_genome_nexus_points_to_protein_range(db, monkeypatch):
    import urllib.error
    import urllib.request

    def offline(*args, **kwargs):
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    server._GENOME_NEXUS_CACHE.clear()
    _egfr_study(db)
    p = server.mutation_diagram(study_id=STUDY, gene="EGFR", domain="kinase")
    assert "Could not reach Genome Nexus" in p["error"]
    assert "protein_range=[start, end]" in p["error"]


def test_protein_range_filter_and_validation(db):
    _egfr_study(db)
    p = server.mutation_diagram(study_id=STUDY, gene="EGFR", protein_range=[712, 979])
    assert p["region"]["label"] == "codons 712-979" and p["region"]["n_samples_in_region"] == 3
    for bad in ([979, 712], [0, 5], [1], "712-979"):
        out = server.mutation_diagram(study_id=STUDY, gene="EGFR", protein_range=bad)
        assert "protein_range" in out["error"]
    both = server.mutation_diagram(study_id=STUDY, gene="EGFR", domain="x", protein_range=[1, 2])
    assert "either domain or protein_range" in both["error"]
    outside = server.mutation_diagram(study_id=STUDY, gene="EGFR", protein_range=[1100, 1200])
    assert "No EGFR mutations fall in codons 1100-1200" in outside["error"]


def test_gene_is_required(db):
    assert "gene is required" in server.mutation_diagram(study_id=STUDY)["error"]


def test_lollipop_over_a_study_set_reports_studies(db):
    db.add_preference("tcga_set", [STUDY, "second"])
    a = db.add_sample(STUDY, "a1", patient_stable_id="P1")
    b = db.add_sample("second", "b1", patient_stable_id="P1")
    db.add_mutation(a, "BAP1", "Q253*", "Nonsense_Mutation")
    db.add_mutation(b, "BAP1", "Q253*", "Nonsense_Mutation")
    p = server.mutation_diagram(preference="tcga_set", gene="BAP1")
    assert p["study_id"] is None and p["scope"]["label"] == "tcga_set (2 studies)"
    assert p["n_samples_mutated"] == 2 and p["mutations"][0]["count"] == 2
    assert {r["study_id"] for r in p["by_study"]} == {STUDY, "second"}
    assert any("occur in more than one study" in w for w in p["warnings"])


# --- alteration_enrichment ----------------------------------------------------------


class ScriptedEnrichmentDB:
    """Answers the enrichment queries from per-gene count tables."""

    def __init__(self, sizes, altered, profiled, studies=(STUDY,)):
        self.sizes, self.altered, self.profiled = sizes, altered, profiled
        self.studies = studies
        self.executed = []

    def __call__(self, q):
        self.executed.append(q)
        if "FROM cancer_study" in q and "WITH groups" not in q:
            return [{"cancer_study_identifier": s} for s in self.studies]
        if "AS n\n        FROM groups g" in q:
            return self.sizes
        if "uniqExactIf(e.sample_unique_id, g.grp = 'A')" in q:
            return self.altered
        if "SELECT gene, grp, stratum, n FROM (" in q:
            return self.profiled
        if "n_cancer_types" in q:
            return [{"n_samples": 100, "n_cancer_types": 3}]
        if "countIf(driver_filter != '')" in q:
            return [{"cancer_study_identifier": STUDY, "annotated": 0}]
        raise AssertionError(f"unexpected query: {q[:200]}")


def _enrichment_db(monkeypatch, stratified=False):
    strata = ["Lung", "Breast"] if stratified else [""]
    sizes = [{"grp": g, "stratum": st, "n": 50} for g in ("A", "B") for st in strata]
    altered = []
    for st in strata:
        altered += [
            {"gene": "TP53", "stratum": st, "a": 50, "c": 0},  # defines the groups
            {"gene": "CDKN2A", "stratum": st, "a": 20, "c": 2},  # enriched in A
            {"gene": "PTEN", "stratum": st, "a": 1, "c": 15},  # enriched in B
            {"gene": "RARE1", "stratum": st, "a": 1, "c": 0},  # below min_altered
            {"gene": "KRAS", "stratum": st, "a": 5, "c": 5},  # no difference
        ]
    profiled = [
        {"gene": "*", "grp": g, "stratum": st, "n": 50} for g in ("A", "B") for st in strata
    ]
    fake = ScriptedEnrichmentDB(sizes, altered, profiled)
    monkeypatch.setattr(server, "run_select_query", fake)
    return fake


def test_enrichment_tests_every_gene_and_excludes_the_defining_one(monkeypatch):
    fake = _enrichment_db(monkeypatch)
    p = server.alteration_enrichment("TP53: MUT", study_id=STUDY)

    assert "error" not in p, p.get("error")
    assert p["groups"]["A"] == {"definition": "TP53: MUT", "n_samples": 50}
    assert p["groups"]["B"]["definition"] == "profiled for TP53 and NOT (TP53: MUT)"
    assert p["excluded_genes"] == ["TP53"]
    genes = {g["gene"]: g for g in p["genes"]}
    assert set(genes) == {"CDKN2A", "PTEN", "KRAS"}  # RARE1 is below min_altered
    assert p["n_genes_tested"] == 3
    assert genes["CDKN2A"]["enriched_in"] == "A" and genes["CDKN2A"]["pct_a"] == 40.0
    assert genes["PTEN"]["enriched_in"] == "B"
    assert genes["KRAS"]["q_value"] > 0.5
    assert p["n_significant"] == 2
    assert p["n_significant_enriched_in_a"] == 1 and p["n_significant_enriched_in_b"] == 1
    assert p["stratification"] is None
    assert any("NOT adjusted" in w for w in p["warnings"])
    assert "not by itself show synthetic lethality" in p["notes"]
    groups_sql = next(q for q in fake.executed if "uniqExactIf(e.sample_unique_id" in q)
    assert "hugo_gene_symbol = 'TP53'" in groups_sql
    assert "HAVING uniqExact(hugo_gene_symbol) = 1" in groups_sql


def test_enrichment_direction_filter(monkeypatch):
    _enrichment_db(monkeypatch)
    p = server.alteration_enrichment("TP53: MUT", study_id=STUDY, direction="B")
    # KRAS (5 vs 5) has an odds ratio of 1 and ties are reported as B; it is kept because
    # direction filters on the direction, not on significance.
    assert [g["gene"] for g in p["genes"]] == ["PTEN", "KRAS"]
    assert [g["q_value"] < 0.05 for g in p["genes"]] == [True, False]
    assert all(g["enriched_in"] == "B" for g in p["genes"])
    assert p["n_significant"] == 2  # still counts every tested gene
    bad = server.alteration_enrichment("TP53: MUT", study_id=STUDY, direction="up")
    assert "direction must be" in bad["error"]


def test_enrichment_stratified(monkeypatch):
    _enrichment_db(monkeypatch, stratified=True)
    p = server.alteration_enrichment("TP53: MUT", study_id=STUDY, stratify_by="CANCER_TYPE")
    genes = {g["gene"]: g for g in p["genes"]}
    assert p["stratification"]["by"] == "CANCER_TYPE" and p["stratification"]["n_strata"] == 2
    assert genes["CDKN2A"]["test"] in ("cmh_chi_square", "exact_conditional")
    assert "crude_p_value" in genes["CDKN2A"]
    assert genes["CDKN2A"]["altered_a"] == 40 and genes["CDKN2A"]["profiled_a"] == 100
    assert not any("NOT adjusted" in w for w in p["warnings"])


def test_enrichment_restricted_to_named_genes(monkeypatch):
    fake = _enrichment_db(monkeypatch)
    p = server.alteration_enrichment(
        {"SAMPLE_TYPE": ["Metastasis"]},
        study_id=STUDY,
        group_b={"SAMPLE_TYPE": ["Primary"]},
        genes=["PTEN", "RARE1", "NOTALTERED"],
    )
    assert p["genes_requested"] == ["PTEN", "RARE1", "NOTALTERED"]
    assert {g["gene"] for g in p["genes"]} == {"PTEN", "RARE1", "NOTALTERED"}
    never = next(g for g in p["genes"] if g["gene"] == "NOTALTERED")
    assert never["altered_a"] == 0 and never["p_value"] == 1.0
    assert "the 3 requested genes" in p["notes"]
    sql = next(q for q in fake.executed if "uniqExactIf(e.sample_unique_id" in q)
    assert "e.hugo_gene_symbol IN ('PTEN', 'RARE1', 'NOTALTERED')" in sql
    assert "upper(attribute_value) IN ('METASTASIS')" in sql


def test_enrichment_burden_warning(monkeypatch):
    sizes = [{"grp": "A", "stratum": "", "n": 10}, {"grp": "B", "stratum": "", "n": 10}]
    altered = [{"gene": f"G{i}", "stratum": "", "a": 6, "c": 2} for i in range(30)]
    profiled = [{"gene": "*", "grp": g, "stratum": "", "n": 10} for g in ("A", "B")]
    monkeypatch.setattr(server, "run_select_query", ScriptedEnrichmentDB(sizes, altered, profiled))
    p = server.alteration_enrichment("TP53: MUT", study_id=STUDY)
    assert p["alteration_burden"]["ratio"] == 3.0
    assert any("more altered genes per sample" in w for w in p["warnings"])


@pytest.mark.parametrize(
    "kwargs, fragment",
    [
        ({"group_a": 42}, "group_a must be an OQL alteration"),
        ({"group_a": "TP53: MUT", "min_altered": 0}, "min_altered must be at least 1"),
        ({"group_a": "TP53: MUT", "alteration": "fusion"}, "Invalid alteration_type"),
        ({"group_a": "TP53: MUT", "study_id": "bad id!"}, "Invalid study_id"),
        ({"group_a": "TP53 KRAS"}, "describes 2 tracks"),
    ],
)
def test_enrichment_validation(monkeypatch, kwargs, fragment):
    _enrichment_db(monkeypatch)
    kwargs.setdefault("study_id", STUDY)
    assert fragment in server.alteration_enrichment(**kwargs)["error"]


def test_enrichment_empty_group_errors(monkeypatch):
    fake = ScriptedEnrichmentDB(
        [{"grp": "A", "stratum": "", "n": 5}], [], [], studies=(STUDY, "other")
    )
    monkeypatch.setattr(server, "run_select_query", fake)
    p = server.alteration_enrichment("TP53: MUT", studies=[STUDY, "other"])
    assert "group B has 0 samples" in p["error"]


# --- nucleotide_variants --------------------------------------------------------------


def test_nucleotide_breakdown_of_a_protein_change(db):
    s = [db.add_sample(STUDY, f"s{i}") for i in range(1, 5)]
    for sid in s[:3]:
        db.add_maf_row(sid, "BRAF", "V600E", codon="gTg/gAg", start=140453136)
    db.add_maf_row(s[3], "BRAF", "V600E", codon="gTG/gAA", start=140453135, ref_allele="CA")
    p = server.nucleotide_variants("BRAF", protein_change="p.Val600Glu", study_id=STUDY)

    assert "error" not in p, p.get("error")
    assert p["filters"] == {"protein_change": "V600E"}
    assert p["n_samples"] == 4 and p["n_variants"] == 2
    assert p["codon_changes"] == [
        {"codon_change": "GTG/GAG", "n_samples": 3, "pct_of_samples": 75.0},
        {"codon_change": "GTG/GAA", "n_samples": 1, "pct_of_samples": 25.0},
    ]
    top = p["variants"][0]
    assert top["start_position"] == 140453136 and top["n_samples"] == 3
    assert top["genome_build"] == "GRCh37"
    sql = next(q for q in db.executed if "FROM mutation m" in q)
    assert "codon_change" in sql and "upper(replaceRegexpOne" in sql


def test_synonymous_codon_request_explains_the_zero(db):
    db.add_sample(STUDY, "s1")
    p = server.nucleotide_variants("EGFR", codon_change="GAG>GAA", study_id=STUDY)
    assert "synonymous (silent)" in p["error"]
    assert p["filters"]["codon_amino_acids"] == "E>E"
    assert p["silent_mutations_in_scope"] == 0


def test_codon_change_validation(db):
    db.add_sample(STUDY, "s1")
    for bad in ("GAG-GAA", "XYZ>GAA", ">"):
        assert (
            "codon_change must look like"
            in server.nucleotide_variants("EGFR", codon_change=bad, study_id=STUDY)["error"]
        )


# --- mutation_allele_frequency + histogram_chart ----------------------------------------


def _vaf_study(db):
    db.add_profile(STUDY, "gistic", "COPY_NUMBER_ALTERATION", "DISCRETE")
    s = [
        db.add_sample(STUDY, f"s{i}", cancer_type=("Lung" if i % 2 else "Breast"))
        for i in range(1, 8)
    ]
    db.add_maf_row(s[0], "TP53", "R175H", alt=20, ref=80, copy_number=0)  # 0.2
    db.add_maf_row(s[1], "TP53", "R248Q", alt=50, ref=50, copy_number=0)  # 0.5
    db.add_maf_row(s[2], "TP53", "R273H", alt=90, ref=10, copy_number=0)  # 0.9
    db.add_maf_row(s[3], "TP53", "R273C", alt=40, ref=10, copy_number=-1)  # other CN
    db.add_maf_row(s[4], "TP53", "G245S", alt=None, ref=None, copy_number=0)  # no reads
    db.add_maf_row(s[5], "TP53", "Y220C", alt=0, ref=0, copy_number=0)  # zero depth
    db.add_maf_row(s[6], "TP53", "R249S", alt=30, ref=70, copy_number=None)  # no CN call
    return s


def test_allele_frequency_histogram_in_diploid_samples(db):
    _vaf_study(db)
    p = server.mutation_allele_frequency(
        "TP53: MISSENSE", study_id=STUDY, copy_number="diploid", bins=10
    )
    assert "error" not in p, p.get("error")
    assert p["kind"] == "histogram"
    assert p["counts"] == {
        "n_mutations_matched": 7,
        "n_excluded_no_read_counts": 2,
        "n_excluded_no_copy_number_call": 1,
        "n_excluded_other_copy_number": 1,
        "n_mutations": 3,
        "n_samples": 3,
    }
    assert p["stats"]["mean"] == pytest.approx(0.5333, abs=1e-4)
    assert p["stats"]["median"] == pytest.approx(0.5)
    assert p["reference_lines"] == [
        {"label": "mean", "value": pytest.approx(0.5333, abs=1e-4)},
        {"label": "median", "value": 0.5},
    ]
    assert len(p["bins"]) == 10 and sum(b["count"] for b in p["bins"]) == 3
    assert p["bins"][0]["start"] == 0.0 and p["bins"][-1]["end"] == 1.0
    cn = p["filters"]["copy_number"]
    assert cn["gistic_value"] == 0 and "not whole-genome ploidy" in cn["definition"]
    assert cn["profiles"] == "gistic"
    assert "diploid (GISTIC 0)" in p["subtitle"]
    sql = next(q for q in db.executed if "tumor_alt_count" in q)
    assert "profile_type) IN (('study_x', 'gistic'))" in sql
    assert "mutation_type IN ('Missense_Mutation')" in sql


def test_allele_frequency_group_summaries(db):
    _vaf_study(db)
    p = server.mutation_allele_frequency("TP53: MUT", study_id=STUDY, group_by="CANCER_TYPE")
    names = {g["name"]: g["n"] for g in p["groups"]}
    assert sum(names.values()) == p["counts"]["n_mutations"] == 5
    assert "violin or jitter plots are not rendered" in p["notes"]


@pytest.mark.parametrize(
    "alteration, kwargs, fragment",
    [
        ("TP53: AMP", {}, "must select mutations only"),
        ("TP53: MUT", {"copy_number": "haploid"}, "copy_number must be one of"),
        ("TP53: MUT_DRIVER", {}, "DRIVER filters need driver annotations"),
    ],
)
def test_allele_frequency_validation(db, alteration, kwargs, fragment):
    _vaf_study(db)
    out = server.mutation_allele_frequency(alteration, study_id=STUDY, **kwargs)
    assert fragment in out["error"]


def test_copy_number_filter_needs_a_discrete_profile(db):
    s = db.add_sample(STUDY, "s1")
    db.add_maf_row(s, "TP53", "R175H")
    out = server.mutation_allele_frequency("TP53: MUT", study_id=STUDY, copy_number="diploid")
    assert out["error"] == (
        f"No study in {STUDY} has a discrete (GISTIC-style) copy-number profile, "
        "so the copy_number filter cannot be applied."
    )


def test_histogram_chart_computes_its_own_statistics():
    p = server.histogram_chart([1, 2, 2, 3, 3, 3, 4, 4, 5, "x"], bins=4, value_range=[1, 5])
    assert p["kind"] == "histogram"
    assert [b["count"] for b in p["bins"]] == [1, 2, 3, 3]
    assert p["stats"]["mean"] == 3.0 and p["stats"]["median"] == 3.0
    assert [r["label"] for r in p["reference_lines"]] == ["mean", "median"]
    assert any("non-numeric" in w for w in p["warnings"])
    custom = server.histogram_chart(
        [1, 2, 3], reference_lines=["q3", {"label": "cut", "value": 2.5}]
    )
    assert custom["reference_lines"] == [
        {"label": "q3", "value": 2.5},
        {"label": "cut", "value": 2.5},
    ]
    assert server.histogram_chart([1, 2], reference_lines=[])["reference_lines"] == []
    assert (
        "Unknown reference line" in server.histogram_chart([1], reference_lines=["mode"])["error"]
    )
    assert "non-empty" in server.histogram_chart([])["error"]


# --- registration and UI wiring -------------------------------------------------------


async def test_new_tools_are_registered_and_linked():
    names = {t.name for t in await server.mcp.list_tools()}
    for name in (
        "alteration_enrichment",
        "nucleotide_variants",
        "mutation_allele_frequency",
        "histogram_chart",
    ):
        assert name in names
    for name in ("mutation_allele_frequency", "histogram_chart"):
        tool = await server.mcp.get_tool(name)
        assert tool.meta["ui"]["resourceUri"] == ui.HISTOGRAM_UI_URI == "ui://cbioportal/histogram"
    resources = {str(r.uri): r for r in await server.mcp.list_resources()}
    assert resources[ui.HISTOGRAM_UI_URI].mime_type == "text/html;profile=mcp-app"
