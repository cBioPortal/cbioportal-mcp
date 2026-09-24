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


def test_system_prompt_calibrates_response_depth_for_researcher_queries():
    prompt = server._load_resource("system-prompt.md")

    assert "## Response Depth Calibration" in prompt
    assert "researcher-grade detail" in prompt
    assert "specific gene symbols" in prompt
    assert "raw counts and denominators" in prompt
    assert "selected cohort/study and counting unit" in prompt
    assert "Avoid pop-science summaries" in prompt
