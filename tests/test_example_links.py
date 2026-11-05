"""get_example_links builds cBioPortal URLs for studies, patients and samples (issue #101)."""
import asyncio

from cbioportal_mcp import server


def _links(*args, **kwargs):
    return server.get_example_links.fn(*args, **kwargs)


def test_tool_is_registered_with_a_description_that_says_when_to_call_it():
    tool = asyncio.run(server.mcp.get_tools())["get_example_links"]

    assert "shown an example" in tool.description
    assert "does not check that the study" in tool.description


def test_study_only_returns_only_the_study_link():
    result = _links("difg_glass")

    assert result == {"links": {"study": "https://www.cbioportal.org/study/summary?id=difg_glass"}}


def test_patient_and_sample_links_use_the_portal_url_formats():
    result = _links("difg_glass", patient_id="GLSS-SF-0017", sample_id="GLSS-SF-0017-TP")

    assert result["links"]["patient"] == (
        "https://www.cbioportal.org/patient?studyId=difg_glass&caseId=GLSS-SF-0017"
    )
    assert result["links"]["sample"] == (
        "https://www.cbioportal.org/patient?studyId=difg_glass&sampleId=GLSS-SF-0017-TP"
    )
    assert "note" not in result


def test_unique_ids_from_clinical_data_derived_lose_the_study_prefix_and_say_so():
    result = _links("difg_glass", patient_id="difg_glass_GLSS-SF-0017")

    assert result["links"]["patient"].endswith("studyId=difg_glass&caseId=GLSS-SF-0017")
    assert "Removed the 'difg_glass_' prefix from patient_id" in result["note"]


def test_a_bare_id_equal_to_the_prefix_is_not_emptied():
    result = _links("s1", patient_id="s1_")

    assert result["links"]["patient"].endswith("caseId=s1_")
    assert "note" not in result


def test_ids_are_url_encoded_so_they_cannot_add_parameters():
    result = _links("difg_glass", patient_id="P1&studyId=evil#x")

    assert result["links"]["patient"].endswith("caseId=P1%26studyId%3Devil%23x")
    assert result["links"]["patient"].count("studyId=") == 1


def test_bad_study_id_returns_an_error_message():
    for bad in ["", "a b", "x&y=1", "../etc"]:
        assert "error_message" in _links(bad), bad


def test_empty_or_oversized_ids_return_an_error_message():
    assert "error_message" in _links("difg_glass", patient_id="")
    assert "error_message" in _links("difg_glass", patient_id="   ")
    assert "error_message" in _links("difg_glass", sample_id="x" * 129)
    assert "error_message" in _links("difg_glass", patient_id="bad\nid")


def test_system_prompt_and_guides_point_to_the_tool():
    prompt = server._load_resource("system-prompt.md")

    assert "get_example_links(study_id, patient_id)" in prompt
    assert "get_example_links" in server._clinical_data_guide_text()
    assert "get_example_links" in server._common_pitfalls_guide_text()
