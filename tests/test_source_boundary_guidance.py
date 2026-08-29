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


def test_system_prompt_forbids_implied_literature_review():
    prompt = server._load_resource("system-prompt.md")
    pitfalls = server._common_pitfalls_guide_text()

    assert "Never claim or imply that you reviewed literature" in prompt
    assert "Avoid phrases such as" in prompt
    assert "the literature shows" in prompt
    assert "For rare-variant questions" in prompt
    assert "cBioPortal occurrence/absence" in prompt

    assert "IMPLIED LITERATURE REVIEW FOR RARE VARIANTS" in pitfalls
    assert "PIK3CA p.*1069Wext*3" in pitfalls
    assert "cBioPortal occurrence data does not establish biological significance" in pitfalls
