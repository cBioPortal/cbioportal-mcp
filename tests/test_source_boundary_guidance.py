from cbioportal_mcp import server


def test_system_prompt_softly_redirects_general_biology_questions():
    prompt = server._load_resource("system-prompt.md")

    assert "## Source Boundaries" in prompt
    assert "pure biology/mechanism question" in prompt
    assert "what do IDH1 mutations do?" in prompt
    assert "not something cBioPortal data directly answers" in prompt
    assert "answer from general biomedical knowledge with that caveat" in prompt
    assert "look up cBioPortal-specific data" in prompt


def test_system_prompt_labels_general_knowledge_even_after_tool_calls():
    prompt = server._load_resource("system-prompt.md")

    assert "state in natural prose" in prompt
    assert "general biomedical knowledge and not from cBioPortal data" in prompt
    assert "Do not use a bracketed pre-hook or tag" in prompt
    assert "If a response mixes cBioPortal data and general knowledge" in prompt
    assert "separate paragraphs or sections" in prompt
    assert "explicitly state which portion is not from cBioPortal data" in prompt
    assert "The label depends on the source of the claim" in prompt
    assert "not merely whether a tool was called" in prompt


def test_user_facing_code_samples_default_to_public_rest_api():
    prompt = server._load_resource("system-prompt.md")
    faq = server._faq_guide_text()

    assert "## User-Facing Code Samples" in prompt
    assert "default to public cBioPortal interfaces" in prompt
    assert "https://www.cbioportal.org/api" in prompt
    assert "Do not write ClickHouse-driver code" in prompt
    assert "unless the user explicitly says they administer" in prompt

    assert "default to the REST API" in faq
    assert "Do not provide ClickHouse connection code" in faq
