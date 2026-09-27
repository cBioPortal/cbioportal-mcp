import importlib.util
import os
import re
from pathlib import Path

import pytest

from cbioportal_mcp import server

REPO = Path(__file__).resolve().parent.parent
SQL_DIR = REPO / "sql"
DDL_FIXTURE = REPO / "tests" / "fixtures" / "cbioportal_clickhouse_ddl.sql"

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



# --- Cross-checks against the DDL and view SQL ------------------------------
# The prompt tells the model its schema is authoritative, so each claim below
# is derived from the SQL rather than restated by hand.


def _ddl_tables() -> dict[str, dict[str, str]]:
    tables: dict[str, dict[str, str]] = {}
    ddl = DDL_FIXTURE.read_text()
    for name, body in re.findall(r"^CREATE TABLE (\w+) \((.*?)\n\);", ddl, re.M | re.S):
        for column, col_type in re.findall(r"^\s*`?([\w.]+)`?\s+(\w+(?:\(.*?\))?)", body, re.M):
            if column.upper() not in ("PRIMARY", "INDEX", "CONSTRAINT"):
                tables.setdefault(name, {})[column] = col_type
    return tables


def _view_bodies() -> dict[str, str]:
    views = {}
    for path in sorted(SQL_DIR.glob("[0-9]-*.sql")):
        sql = re.sub(r"--[^\n]*", "", path.read_text())
        pattern = r"^CREATE VIEW (\w+) AS(.*?)(?=^DROP VIEW |^CREATE VIEW |\Z)"
        for name, body in re.findall(pattern, sql, re.M | re.S):
            views[name] = body
    return views


def _prompt_line(prefix: str) -> str:
    lines = [ln for ln in _prompt().splitlines() if ln.startswith(prefix)]
    assert len(lines) == 1, f"expected one prompt line starting with {prefix!r}, got {len(lines)}"
    return lines[0]


def _named(line: str, names) -> list[str]:
    return [n for n in re.findall(r"`(\w+)`", line) if n in names]


def _documented_tables() -> set[str]:
    # A table is documented where the Core Schema introduces it: **`t`** or a `- `t`` bullet.
    schema = _section(_prompt(), "## Core Schema")
    return {a or b for a, b in re.findall(r"^(?:\*\*`(\w+)`\*\*|- `(\w+)`)", schema, re.M)}


def _sql_created_tables() -> set[str]:
    return {
        t for path in SQL_DIR.rglob("*.sql")
        for t in re.findall(r"^CREATE TABLE (\w+) \(", path.read_text(), re.M)
    }


def test_documented_tables_exist_in_ddl():
    ddl = _ddl_tables()
    documented = _documented_tables() & (set(ddl) | _sql_created_tables())

    assert len(documented) >= 20, sorted(documented)
    for table in ["cancer_study", "clinical_data_derived", "genomic_event_derived",
                  "resource_sample", "clinical_event_derived", "cancer_study_query_preferences"]:
        assert table in documented, table


def test_nullable_string_list_equals_ddl_for_documented_tables():
    ddl = _ddl_tables()
    expected = {
        (table, column)
        for table in _documented_tables() & set(ddl)
        for column, col_type in ddl[table].items()
        if "Nullable(String)" in col_type
    }
    line = _prompt_line("- Most String columns hold `''` (not NULL)")

    assert "are `Nullable(String)` instead" in line
    assert set(re.findall(r"`(\w+)\.(\w+)`", line)) == expected
    assert "This list covers only the documented tables" in line
    assert "DESCRIBE TABLE" in line
    # sql/ migrations must not add Nullable(String) columns the DDL doesn't show.
    for path in SQL_DIR.rglob("*.sql"):
        assert not re.search(r"ADD COLUMN[^\n]*Nullable\(String\)", path.read_text()), path


def test_view_off_panel_and_uncalled_lists_match_view_sql():
    views = _view_bodies()
    off_panel = {v for v, b in views.items() if re.search(r"off_panel\s*=\s*0", b)}
    uncalled = {v for v, b in views.items() if re.search(r"mutation_status\s*!=\s*'UNCALLED'", b)}
    branched = {v for v, b in views.items() if "{alteration:String} = 'mutation'" in b}
    uncalled_in_branch = (
        r"\(\{alteration:String\} = 'mutation'[^()]*mutation_status != 'UNCALLED'\)"
    )
    uncalled_branch_only = {v for v in branched if re.search(uncalled_in_branch, views[v])}
    off_panel_in_branch_re = r"\(\{alteration:String\} = '\w+'[^()]*off_panel"
    off_panel_in_branch = {v for v in branched if re.search(off_panel_in_branch_re, views[v])}

    off_panel_line = _prompt_line("- `off_panel = 0`:")
    uncalled_line = _prompt_line("- `mutation_status != 'UNCALLED'`:")
    no_status_line = _prompt_line("- No `mutation_status` filter:")

    assert set(_named(off_panel_line, views)) == off_panel
    assert set(_named(uncalled_line, views)) == uncalled
    for v in branched & off_panel:
        assert (f"`{v}` (all branches)" in off_panel_line) == (v not in off_panel_in_branch), v
    for v in uncalled:
        assert (f"`{v}` (`alteration='mutation'` branch only)" in uncalled_line) == (
            v in uncalled_branch_only
        ), v

    listed_no_status = set(_named(no_status_line, views))
    assert off_panel - uncalled <= listed_no_status
    assert not listed_no_status & (uncalled - uncalled_branch_only)
    for v in uncalled_branch_only:
        assert f"CNA branches of `{v}`" in no_status_line, v


def test_profiled_denominator_claims_match_view_sql():
    views = _view_bodies()
    denominator_line = _prompt_line("- Profiled denominator = samples profiled for the gene")
    assert "every view in the `off_panel = 0` list" in denominator_line
    for v in (v for v, b in views.items() if re.search(r"off_panel\s*=\s*0", b)):
        body = views[v]
        assert re.search(r"\w+_wes_coverage|gene_panel_id = 'WES'", body), v
        assert re.search(r"\w+_panel_gene_coverage|JOIN gene_panel_list", body), v

    cna = views["gene_cna_distribution_in_study"]
    assert "FROM genetic_alteration_derived" in cna
    assert "alteration_value NOT IN ('', 'NA')" in cna
    assert "sum(samples) AS profiled_samples" in cna
    assert "_coverage" not in cna and "'WES'" not in cna and "off_panel" not in cna

    exception = _prompt_line("- **Exception:** `gene_cna_distribution_in_study`")
    assert "from `genetic_alteration_derived`" in exception
    assert "no `off_panel` filter" in exception
    assert "`profiled_samples` = samples with a non-empty, non-NA value for the gene" in exception
    assert "all samples" not in exception and "study samples" not in exception


def test_public_portal_preference_wording_matches_sql():
    public_dir = SQL_DIR / "portal-specific" / "public-portal"
    public_sql = "".join(p.read_text() for p in public_dir.glob("*.sql"))
    public = set(re.findall(r"SELECT\s+'(\w+)'", public_sql))
    shared_sql = (SQL_DIR / "3-add-cancer-study-query-preferences.sql").read_text()
    everywhere = set(re.findall(r"SELECT '(\w+)'", shared_sql))
    assert public and "pan_cancer_tcga" in everywhere and not public & everywhere

    line = _prompt_line("**`cancer_study_query_preferences`**")
    marker = (
        "Defined in the public-portal SQL (`sql/portal-specific/public-portal/`); "
        "present only where that directory is applied:"
    )
    before, _, after = line.partition(marker)
    assert after, "public-portal wording missing"
    assert set(_named(after, public | everywhere)) == public
    assert set(_named(before, public | everywhere)) == everywhere


def test_clinical_event_derived_columns_match_ddl():
    ddl = _ddl_tables()["clinical_event_derived"]
    line = _prompt_line("- `clinical_event_derived` has **no** `key`/`value` columns")

    assert "key" not in ddl and "value" not in ddl
    listed = re.search(r"columns \((.*?)\)\.", line).group(1)
    assert set(re.findall(r"`(\w+)`", listed)) == set(ddl)


def test_ddl_fixture_matches_cbioportal_checkout():
    root = os.environ.get("CBIOPORTAL_REPO")
    if not root:
        pytest.skip("set CBIOPORTAL_REPO to a cbioportal checkout to check the DDL fixture")
    script = REPO / "scripts" / "extract_clickhouse_ddl.py"
    spec = importlib.util.spec_from_file_location("extract_clickhouse_ddl", script)
    extract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extract)

    assert extract.extract(root) == DDL_FIXTURE.read_text()
