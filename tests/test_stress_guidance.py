"""The guidance layer must route the STRESS-test shapes to the tools that now handle them,
and must keep the rule behind the documented T6 incident: never claim an adjustment,
exclusion or merge the payload does not show."""

from cbioportal_mcp import server


def test_system_prompt_lists_what_the_data_apps_can_express():
    prompt = server._load_resource("system-prompt.md")
    assert "## Data Apps — What They Can Express" in prompt
    for fragment in (
        'preference="pan_cancer_tcga"',
        "survival_curve(groups=",
        "group_by_expression",
        'stratify_by="CANCER_TYPE"',
        "oncoprint(oql=...)",
        'mutation_classes="collapsed"',
        "alteration_cooccurrence(tracks=",
        "alteration_enrichment(",
        'mutation_diagram(domain="tyrosine kinase")',
        "nucleotide_variants(",
        "mutation_allele_frequency(",
        "cbioportal://oql-guide",
    ):
        assert fragment in prompt, fragment
    assert "Still not available" in prompt and "whole-genome ploidy" in prompt


def test_system_prompt_forbids_claiming_steps_the_payload_does_not_show():
    prompt = server._load_resource("system-prompt.md")
    assert "never claim an analysis step the payload does not show" in prompt
    assert "`stratification` (null means NOT adjusted)" in prompt
    assert "Never re-describe an earlier unadjusted result as adjusted" in prompt


def test_statistical_guide_routes_stratified_and_group_comparisons():
    guide = server._load_resource("statistical-tests-guide.md")
    assert "### Confounding: pooled cancer types" in guide
    assert "**Group-vs-group** alteration enrichment" in guide
    assert "**Stratified** log-rank test" in guide
    assert "When asked whether results were adjusted for tumour type" in guide
    assert 'Never answer "yes, I normalised for tumour type"' in guide
    assert "has `stratification: null` — nothing was adjusted" in guide


def test_pitfalls_document_the_documented_incident():
    pitfalls = server._load_resource("common-pitfalls.md")
    assert "CLAIMING AN ADJUSTMENT, FILTER OR MERGE THE TOOL DID NOT APPLY" in pitfalls
    assert "PASSING A STUDY SET AS A STUDY ID" in pitfalls
    assert "`exclusions[]` with `events_removed`" in pitfalls
    assert "Histograms with mean / median lines" in pitfalls


def test_oql_guide_is_registered_and_lists_refused_constructs():
    assert "cbioportal://oql-guide" in {uri for uri, _ in server.GUIDES}
    guide = server.read_guide("cbioportal://oql-guide")
    assert "## Refused" in guide and "EXP" in guide and "DRIVER" in guide
    assert "EGFR: MUT != T790M MUT != L858R" in guide
    assert len(guide.split()) < 600
