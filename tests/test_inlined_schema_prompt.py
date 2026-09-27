import re
from pathlib import Path

from cbioportal_mcp import server

REPO = Path(__file__).resolve().parent.parent
SQL_DIR = REPO / "sql"

# Baseline system-prompt.md before the schema/recipes were inlined was ~20k
# characters (~5-6k tokens). The inlined schema, views and recipes may add at
# most ~12k tokens on top; 3.5 chars/token is a conservative estimate for
# markdown with SQL.
MAX_PROMPT_CHARS = 20_000 + 12_000 * 3.5


def _prompt() -> str:
    return server._load_resource("system-prompt.md")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def test_system_prompt_stays_within_token_budget():
    prompt = _prompt()

    assert len(prompt) <= MAX_PROMPT_CHARS, (
        f"system-prompt.md is {len(prompt)} chars (~{len(prompt) / 3.5:.0f} tokens); "
        f"budget is {MAX_PROMPT_CHARS:.0f} chars. Move detail into a guide instead."
    )


def test_system_prompt_lists_core_tables_with_key_columns():
    schema = _section(_prompt(), "## Core Schema")

    expected = {
        "cancer_study": ["cancer_study_identifier", "type_of_cancer_id", "sample_count",
                         "mutation_sample_count", "resource_sample_counts"],
        "type_of_cancer": ["main_type", "parent"],
        "sample_derived": ["sample_unique_id", "patient_unique_id", "sequenced"],
        "cancer_study_query_preferences": ["preference_name", "pan_cancer_tcga"],
        "clinical_data_derived": ["attribute_name", "attribute_value", "type"],
        "clinical_attribute_meta": ["attr_id", "patient_attribute"],
        "clinical_event_data": ["key", "value", "AGENT"],
        "genomic_event_derived": ["variant_type", "mutation_status", "off_panel",
                                  "cna_alteration", "driver_filter", "mutation_variant"],
        "genetic_alteration_derived": ["profile_type", "alteration_value"],
        "genetic_profile": ["genetic_alteration_type", "datatype"],
        "sample_to_gene_panel_derived": ["gene_panel_id", "alteration_type"],
        "resource_definition": ["resource_type"],
    }
    for table, columns in expected.items():
        assert f"`{table}`" in schema, f"core table {table} missing from schema section"
        for column in columns:
            assert column in schema, f"{table}.{column} missing from schema section"


def test_system_prompt_documents_sort_keys_for_fact_tables():
    schema = _section(_prompt(), "## Core Schema")

    assert "(cancer_study_identifier, type, attribute_name, sample_unique_id)" in schema
    assert (
        "(genetic_profile_stable_id, cancer_study_identifier, variant_type, "
        "entrez_gene_id, hugo_gene_symbol, sample_unique_id)" in schema
    )
    assert "(cancer_study_identifier, hugo_gene_symbol, profile_type, sample_unique_id)" in schema


def test_system_prompt_mentions_every_shipped_sql_view():
    prompt = _prompt()
    views = sorted({
        v
        for path in SQL_DIR.glob("[0-9]-*.sql")
        for v in re.findall(r"^CREATE VIEW (\w+) AS", path.read_text(), re.M)
    })

    assert views, "no views found under sql/"
    missing = [v for v in views if f"`{v}" not in prompt]
    assert not missing, f"views missing from system-prompt.md: {missing}"


def test_system_prompt_treats_schema_as_authoritative():
    prompt = _prompt()

    assert "The schema below is authoritative" in prompt
    assert "only for a table that is **not** listed in the Core Schema" in prompt
    # The old mandatory per-query validation step forced a metadata round per table.
    assert "verify table existence with `clickhouse_list_tables`" not in prompt
    assert "Explore tables with `clickhouse_list_tables`" not in prompt


def test_system_prompt_does_not_mandate_guide_reads_before_every_answer():
    prompt = _prompt()

    assert "BEFORE ANSWERING ANY QUESTION, you MUST" not in prompt
    assert "Guides are for depth, not a mandatory first step" in prompt


def test_system_prompt_forbids_narration_between_tool_calls():
    prompt = _prompt()

    assert "Emit tool calls directly — no narration between tool calls" in prompt
    assert "Batch independent calls in one turn" in prompt


def test_system_prompt_places_static_reference_before_behavioral_sections():
    prompt = _prompt()

    order = [
        "## How to Work",
        "## Core Schema",
        "## Precomputed Views",
        "## Core Recipes",
        "## Guide Index",
        "## Source Boundaries",
        "## Rules",
    ]
    positions = [prompt.index(h) for h in order]
    assert positions == sorted(positions)


def test_system_prompt_has_no_dynamic_content():
    # Instructions are cached as a prompt prefix; anything that varies per
    # request or per build would bust the cache.
    prompt = _prompt()

    assert "{" not in re.sub(r"```.*?```", "", prompt, flags=re.S).replace("{{", "")
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", prompt)


def test_pitfall_15_no_longer_mandates_schema_checks_for_listed_tables():
    fragment = server.read_guide.fn("cbioportal://common-pitfalls#15")

    assert "HALLUCINATED TABLES OR COLUMNS" in fragment
    assert "Always check with `clickhouse_list_tables`" not in fragment
    assert "only for tables outside it" in fragment


def _code_blocks(text: str) -> list[str]:
    return re.findall(r"```sql\n(.*?)```", text, re.S)


def test_survival_sql_never_aggregates_os_months_into_a_median():
    sources = {
        "clinical-data-guide.md": server._clinical_data_guide_text(),
        "system-prompt.md": _prompt(),
    }
    forbidden = re.compile(r"\b(median|quantile\(0\.5\)|avg)\s*\(", re.I)

    for name, text in sources.items():
        for block in _code_blocks(text):
            if "OS_MONTHS" in block:
                assert not forbidden.search(block), f"{name} aggregates OS_MONTHS:\n{block}"


def test_clinical_guide_survival_section_hands_off_to_kaplan_meier():
    survival = _section(server._clinical_data_guide_text(), "## Survival Analysis Queries")

    assert "Kaplan-Meier" in survival
    assert "not reached" in survival
    assert "median_os" not in survival

