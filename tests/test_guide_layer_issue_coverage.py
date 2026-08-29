import inspect

from cbioportal_mcp import server


def test_new_targeted_guides_are_registered_and_readable():
    server_source = inspect.getsource(server)

    assert "cbioportal://external-resources-guide" in server_source
    assert "cbioportal://gene-resolution-guide" in server_source
    assert "cbioportal://study-resolution-guide" in server_source

    assert "resource_definition" in server._external_resources_guide_text()
    assert "CD3D" in server._gene_resolution_guide_text()
    assert "These examples are not exhaustive" in server._gene_resolution_guide_text()
    assert "pedcbioportal.kidsfirstdrc.org" in server._study_resolution_guide_text()


def test_new_targeted_guides_stay_concise():
    targeted_guides = [
        server._external_resources_guide_text(),
        server._gene_resolution_guide_text(),
        server._study_resolution_guide_text(),
    ]

    for guide in targeted_guides:
        assert len(guide.split()) < 500


def test_system_prompt_routes_to_targeted_guides():
    prompt = server._load_resource("system-prompt.md")

    assert "cbioportal://study-resolution-guide" in prompt
    assert "cbioportal://gene-resolution-guide" in prompt
    assert "cbioportal://external-resources-guide" in prompt
    assert "PBTA" in prompt
    assert "CD3" in prompt
    assert "Minerva" in prompt


def test_existing_guides_cover_open_issue_patterns():
    clinical = server._clinical_data_guide_text()
    mutation = server._mutation_frequency_guide_text()
    faq = server._faq_guide_text()
    pitfalls = server._common_pitfalls_guide_text()

    assert "Case-Insensitive Matching for Attribute Values" in clinical
    assert "Query the Requested Attribute, Not a Proxy" in clinical
    assert "HER2" in clinical

    assert "Promoter and Non-Coding Mutation Questions" in mutation
    assert "C228T" in mutation
    assert "all mutations in the gene" in mutation
    assert "Do not report all `TERT` mutation records as promoter mutations" in mutation

    assert "Clinical Actionability and OncoKB" in faq
    assert "polygenic risk scores" in faq
    assert "should not promise" in faq

    assert "FLAWED PREMISE OR NONEXISTENT DATA FIELD" in pitfalls
    assert "OUT-OF-SCOPE DRIFT AFTER USER PUSHBACK" in pitfalls
    assert "MISLEADING OUTPUT PROMISES" in pitfalls


def test_frequency_guide_covers_study_view_reconciliation():
    """Issue #53: agent frequencies diverge from the cBioPortal UI.

    The pre-existing STOP rule only fires above 100%; #53 is the silently-plausible
    case. These assertions pin the reconciliation guidance and the two field-level
    traps that cause it.
    """
    guide = server._mutation_frequency_guide_text()

    assert "When the user says your numbers don't match the study view" in guide
    assert "numberOfAlteredCases / numberOfProfiledCases" in guide
    assert "/api/mutated-genes/fetch" in guide

    # totalCount is mutation events, not samples - the most common numerator bug.
    assert "mutation **events**, not samples" in guide
    assert "never a numerator" in guide

    # The denominator is per-gene, evidenced with real numbers.
    assert "The denominator is per-gene, and the numerator is per-sample" in guide
    assert "268 of 485 genes have more events than altered samples" in guide

    # A five-step procedure, not just a warning.
    assert "Reconciliation procedure" in guide
    for required in ("COUNT(DISTINCT sample_unique_id)", "mutation_wes_coverage",
                     "mutation_status != 'UNCALLED'", "off_panel = 0",
                     "same sample set"):
        assert required in guide, required


def test_frequency_guide_warns_the_error_runs_both_ways():
    """A number below the UI is not evidence of a conservative estimate."""
    guide = server._mutation_frequency_guide_text()
    assert "errors run in both directions" in guide
