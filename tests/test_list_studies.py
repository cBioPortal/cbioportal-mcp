from cbioportal_mcp import server


def test_list_studies_adds_clickable_study_urls(monkeypatch):
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: ["brca_test_2026"])
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query: [
            {
                "cancer_study_identifier": "brca_test_2026",
                "name": "Breast Test Study",
                "description": "Test study",
                "type_of_cancer_id": "brca",
                "sample_count": 42,
            }
        ],
    )

    studies = server.list_studies.fn(search="breast", limit=20)

    assert studies == [
        {
            "cancer_study_identifier": "brca_test_2026",
            "name": "Breast Test Study",
            "description": "Test study",
            "type_of_cancer_id": "brca",
            "sample_count": 42,
            "has_guide": True,
            "url": "https://www.cbioportal.org/study/summary?id=brca_test_2026",
        }
    ]


def test_prompt_and_faq_reference_study_summary_links():
    prompt = server._load_resource("system-prompt.md")
    faq = server._faq_guide_text()

    assert "When an answer lists studies" in prompt
    assert "https://www.cbioportal.org/study/summary?id=<study_id>" in prompt
    assert "https://www.cbioportal.org/study/summary?id={study_id}" in faq
