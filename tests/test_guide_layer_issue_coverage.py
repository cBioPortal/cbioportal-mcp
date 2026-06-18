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
