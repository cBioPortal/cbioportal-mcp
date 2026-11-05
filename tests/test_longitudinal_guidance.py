"""Guidance for longitudinal / multi-sample questions (issue #101).

The agent once called the NCI PDMR study "longitudinal" because patients had
many samples, then silently switched studies. These tests pin the guidance that
prevents that: check *why* patients have several samples, and retract
explicitly. (Link behaviour is covered in test_example_links.py.)
"""
import re

from cbioportal_mcp import server


def _longitudinal_section() -> str:
    guide = server._clinical_data_guide_text()
    start = guide.index("## Longitudinal and Multi-Sample Patients")
    end = guide.index("\n## ", start + 1)
    return guide[start:end]


def test_system_prompt_routes_longitudinal_questions_to_guide_section():
    prompt = server._load_resource("system-prompt.md")

    assert "Longitudinal and Multi-Sample Patients" in prompt
    assert "NOT proof of longitudinal sampling" in prompt


def test_system_prompt_requires_explicit_retraction():
    prompt = server._load_resource("system-prompt.md")

    assert "retract explicitly" in prompt
    assert "never silently pivot" in prompt
    assert "cbioportal://common-pitfalls#23" in prompt


def test_guide_section_separates_multi_sample_from_longitudinal():
    section = _longitudinal_section()

    assert "does not mean samples taken over time" in section
    assert "PASSAGE" in section and "MODEL_ID" in section
    assert "SAMPLE_COLLECTION_TIMEPOINT" in section
    assert "clinical-timeline" in section


def test_guide_section_lists_verified_and_rejected_studies():
    section = _longitudinal_section()

    for study_id in ["difg_glass", "breast_alpelisib_2020", "brca_aurora_2023"]:
        assert f"`{study_id}`" in section

    # PDMR is the study the agent got wrong; it must be called out as NOT longitudinal.
    pdmr_row = next(line for line in section.splitlines() if "`pancan_pdmr_2025`" in line)
    assert "Not longitudinal" in pdmr_row

    # MSK-CHORD has ~one sample per patient (see its study guide): timeline sense only.
    chord_row = next(line for line in section.splitlines() if "`msk_chord_2024`" in line)
    assert "clinical-timeline" in chord_row
    assert "Nearly one sample per patient" in server._load_resource("study-guides/msk_chord_2024.md")


def test_guide_section_has_no_dynamic_counts():
    """AGENTS.md: static resources describe semantics, not data that changes."""
    rows = [line for line in _longitudinal_section().splitlines() if line.startswith("| `")]

    assert rows
    for row in rows:
        description = re.sub(r"`[^`]*`", "", row)  # study IDs contain years, e.g. _2020
        assert not re.search(r"\d{2,}", description), f"count-like number in study row: {row}"


def test_guide_section_strips_the_study_prefix_in_sql():
    assert "substring(patient_unique_id, length(cancer_study_identifier) + 2)" in _longitudinal_section()


def test_guide_does_not_link_to_a_patient_tab_that_does_not_exist():
    # The portal has no "clinicalTimeline" patient tab; the timeline is in Summary.
    assert "clinicalTimeline" not in _longitudinal_section()


def test_pitfall_23_is_reachable_as_a_fragment():
    fragment = server.read_guide.fn.__wrapped__("cbioportal://common-pitfalls#23")

    assert "DECLARING SUCCESS BEFORE VERIFYING" in fragment
    assert "Correction:" in fragment
    assert "P-899932" in fragment
    assert "CRITICAL MUTATION FREQUENCY ERRORS" not in fragment
