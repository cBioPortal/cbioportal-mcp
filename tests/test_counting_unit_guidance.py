from cbioportal_mcp import server


def test_mutation_frequency_guide_surfaces_counting_unit_rules():
    guide = server._mutation_frequency_guide_text()

    assert "## Counting Unit: Samples vs Patients" in guide
    assert "Patient-level" in guide
    assert '"prevalence", "rate", "fraction of patients"' in guide
    assert "Sample-level" in guide
    assert "| User wording | Counting unit |" in guide
    assert "COUNT(DISTINCT patient_unique_id)" in guide
    assert "COUNT(DISTINCT sample_unique_id)" in guide
    assert "Cross-study sample-count caveat" in guide
    assert "overlapping cohorts can count the same patient/sample more than once" in guide


def test_system_prompt_requires_counting_unit_and_cross_study_caveat():
    prompt = server._load_resource("system-prompt.md")

    assert "Choose and state the counting unit" in prompt
    assert "default to patient-level" in prompt
    assert "prevalence/rate/fraction-of-patients questions" in prompt
    assert "multi-study answer reports sample counts" in prompt
    assert "overlapping cohorts can inflate counts" in prompt
