import inspect

from cbioportal_mcp import server


def test_new_targeted_guides_are_registered_and_readable():
    server_source = inspect.getsource(server)

    assert "cbioportal://external-resources-guide" in server_source
    assert "cbioportal://gene-resolution-guide" in server_source
    assert "cbioportal://study-resolution-guide" in server_source

    assert "resource_definition" in server._load_resource("external-resources-guide.md")
    assert "CD3D" in server._load_resource("gene-resolution-guide.md")
    assert "These examples are not exhaustive" in server._load_resource("gene-resolution-guide.md")
    assert "pedcbioportal.kidsfirstdrc.org" in server._load_resource("study-resolution-guide.md")


def test_new_targeted_guides_stay_concise():
    targeted_guides = [
        server._load_resource("external-resources-guide.md"),
        server._load_resource("gene-resolution-guide.md"),
        server._load_resource("study-resolution-guide.md"),
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
    clinical = server._load_resource("clinical-data-guide.md")
    mutation = server._load_resource("mutation-frequency-guide.md")
    faq = server._load_resource("faq-guide.md")
    pitfalls = server._load_resource("common-pitfalls.md")

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
