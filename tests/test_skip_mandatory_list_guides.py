from cbioportal_mcp import server


def test_system_prompt_no_longer_mandates_list_guides_before_read_guide():
    prompt = server._load_resource("system-prompt.md")

    assert "Call `read_guide(uri)` directly" in prompt
    assert "no need to call `list_guides()` first" in prompt


def test_system_prompt_still_falls_back_to_list_guides_for_unmapped_questions():
    prompt = server._load_resource("system-prompt.md")

    assert "Only call `list_guides()` first if the question doesn't fit" in prompt


def test_system_prompt_still_routes_every_guide_uri():
    prompt = server._load_resource("system-prompt.md")

    for uri in [
        "cbioportal://mutation-frequency-guide",
        "cbioportal://statistical-tests-guide",
        "cbioportal://clinical-data-guide",
        "cbioportal://sample-filtering-guide",
        "cbioportal://study-resolution-guide",
        "cbioportal://treatment-guide",
        "cbioportal://gene-expression-guide",
        "cbioportal://gene-resolution-guide",
        "cbioportal://external-resources-guide",
        "cbioportal://faq-guide",
        "cbioportal://common-pitfalls",
    ]:
        assert uri in prompt
