import asyncio
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
        "cancer_study": [
            "cancer_study_identifier",
            "type_of_cancer_id",
            "sample_count",
            "mutation_sample_count",
            "resource_sample_counts",
        ],
        "type_of_cancer": ["main_type", "parent"],
        "sample_derived": ["sample_unique_id", "patient_unique_id", "sequenced"],
        "cancer_study_query_preferences": ["preference_name", "pan_cancer_tcga"],
        "clinical_data_derived": ["attribute_name", "attribute_value", "type"],
        "clinical_attribute_meta": ["attr_id", "patient_attribute"],
        "clinical_event_data": ["key", "value", "AGENT"],
        "genomic_event_derived": [
            "variant_type",
            "mutation_status",
            "off_panel",
            "cna_alteration",
            "driver_filter",
            "mutation_variant",
        ],
        "genetic_alteration_derived": ["profile_type", "alteration_value"],
        "genetic_profile": ["genetic_alteration_type", "datatype"],
        "sample_to_gene_panel_derived": ["gene_panel_id", "alteration_type"],
        "resource_definition": ["resource_type"],
    }
    for table, columns in expected.items():
        assert f"`{table}`" in schema, f"core table {table} missing from schema section"
        for column in columns:
            assert column in schema, f"{table}.{column} missing from schema section"


def test_system_prompt_mentions_every_shipped_sql_view():
    prompt = _prompt()
    views = sorted(
        {
            v
            for path in SQL_DIR.glob("[0-9]-*.sql")
            for v in re.findall(r"^CREATE VIEW (\w+) AS", path.read_text(), re.M)
        }
    )

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

UNCALLED_RE = r"mutation_status\s*!=\s*'UNCALLED'"
OFF_PANEL_RE = r"off_panel\s*=\s*0"
# One OR-chain branch: `({alteration:String} = 'x' AND ...)`. The multiIf()
# that maps the token to an alteration_type has no AND and is not a branch.
BRANCH_RE = re.compile(r"\(\s*\{alteration:String\}\s*=\s*'(\w+)'\s+AND\b")


def _ddl_tables() -> dict[str, dict[str, str]]:
    return {name: cols for name, (cols, _) in _ddl_statements().items()}


def _parse_columns(body: str) -> dict[str, str]:
    columns = {}
    for column, col_type in re.findall(r"^\s*`?([\w.]+)`?\s+(\w+(?:\(.*?\))?)", body, re.M):
        if column.upper() not in ("PRIMARY", "INDEX", "CONSTRAINT"):
            columns[column] = col_type
    return columns


def _ddl_statements() -> dict[str, tuple[dict[str, str], str | None]]:
    ddl = DDL_FIXTURE.read_text()
    pattern = r"^CREATE TABLE (\w+) \((.*?)\n\)(?:\nORDER BY (.*?))?;"
    return {
        name: (_parse_columns(body), order_by or None)
        for name, body, order_by in re.findall(pattern, ddl, re.M | re.S)
    }


def _sql_statements() -> list[str]:
    return [
        stmt
        for path in sorted(SQL_DIR.rglob("*.sql"))
        for stmt in re.sub(r"--[^\n]*", "", path.read_text()).split(";")
    ]


def _sql_created_tables() -> dict[str, tuple[dict[str, str], str | None]]:
    tables = {}
    for stmt in _sql_statements():
        m = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*)\)\s*ENGINE(.*)", stmt, re.S)
        if m:
            order_by = re.search(r"ORDER BY\s+(\([^)]*\)|\w+)", m.group(3))
            tables[m.group(1)] = (
                _parse_columns(m.group(2)),
                " ".join(order_by.group(1).split()) if order_by else None,
            )
    return tables


def _view_bodies() -> dict[str, str]:
    views = {}
    for path in sorted(SQL_DIR.glob("[0-9]-*.sql")):
        sql = re.sub(r"--[^\n]*", "", path.read_text())
        pattern = r"^CREATE VIEW (\w+) AS(.*?)(?=^DROP VIEW |^CREATE VIEW |\Z)"
        for name, body in re.findall(pattern, sql, re.M | re.S):
            views[name] = body
    return views


def _alteration_branches(body: str) -> dict[str, str]:
    """`({alteration:String} = 'x' AND ...)` groups of a view, in SQL order, keyed by x.

    Each value is the group's text up to its matching close paren.
    """
    branches = {}
    for m in BRANCH_RE.finditer(body):
        depth, i = 0, m.start()
        while True:
            depth += {"(": 1, ")": -1}.get(body[i], 0)
            if depth == 0:
                break
            i += 1
        assert m.group(1) not in branches, f"branch {m.group(1)!r} appears twice"
        branches[m.group(1)] = body[m.start() : i + 1]
    return branches


def _annotation(selected: list[str], branches: list[str]) -> str | None:
    """Prompt wording for a filter that applies to `selected` of a view's branches."""
    if not branches:
        return None
    if selected == branches:
        return "all branches"
    names = [f"`alteration='{b}'`" for b in selected]
    if len(names) == 1:
        return f"{names[0]} branch only"
    return f"{', '.join(names[:-1])} and {names[-1]} branches"


def _filter_scope(body: str, predicate: str) -> tuple[list[str], list[str]]:
    """(branches with the predicate, all branches); a predicate outside the branch
    groups applies to every branch."""
    branches = _alteration_branches(body)
    outside = body
    for text in branches.values():
        outside = outside.replace(text, "")
    if re.search(predicate, outside):
        return list(branches), list(branches)
    return [b for b, text in branches.items() if re.search(predicate, text)], list(branches)


def _expected_with(views: dict[str, str], predicate: str) -> dict[str, str | None]:
    expected = {}
    for view, body in views.items():
        selected, branches = _filter_scope(body, predicate)
        if (branches and selected) or (not branches and re.search(predicate, body)):
            expected[view] = _annotation(selected, branches)
    return expected


def _expected_without(views: dict[str, str], predicate: str, universe) -> dict[str, str | None]:
    expected = {}
    for view in universe:
        selected, branches = _filter_scope(views[view], predicate)
        missing = [b for b in branches if b not in selected]
        if branches and missing:
            expected[view] = _annotation(missing, branches)
        elif not branches and not re.search(predicate, views[view]):
            expected[view] = None
    return expected


def _prompt_line(prefix: str) -> str:
    lines = [ln for ln in _prompt().splitlines() if ln.startswith(prefix)]
    assert len(lines) == 1, f"expected one prompt line starting with {prefix!r}, got {len(lines)}"
    return lines[0]


def _prompt_view_list(prefix: str, views) -> dict[str, str | None]:
    """Parse "`view` (annotation), `view`, ..." after `prefix`; fail on unknown names."""
    listed = {}
    for name, note in re.findall(r"`(\w+)`(?: \(([^)]*)\))?", _prompt_line(prefix)[len(prefix) :]):
        assert name in views, f"{name!r} in {prefix!r} is not a view in sql/"
        assert name not in listed, f"{name!r} listed twice in {prefix!r}"
        listed[name] = note or None
    return listed


def _backticked(text: str, allowed: set[str]) -> set[str]:
    """Backticked identifiers in `text`; fail on any not in `allowed`."""
    names = set(re.findall(r"`(\w+)`", text))
    unknown = names - allowed
    assert not unknown, f"unknown backticked names: {sorted(unknown)}"
    return names


def _all_tables() -> dict[str, tuple[dict[str, str], str | None]]:
    return {**_ddl_statements(), **_sql_created_tables()}


def _documented_tables() -> set[str]:
    """Tables the Core Schema introduces: **`t`** — ... or - `t` — ... / - `t` has **no** ..."""
    schema = _section(_prompt(), "## Core Schema")
    pattern = r"^(?:\*\*`(\w+)`\*\* —|- `(\w+)` (?:—|has \*\*no\*\*))"
    names = {a or b for a, b in re.findall(pattern, schema, re.M)}
    unknown = names - set(_all_tables())
    assert not unknown, f"Core Schema introduces tables that don't exist: {sorted(unknown)}"
    return names


# --- Every identifier the prompt names must exist -----------------------------
# Each backticked token and each FROM/JOIN target must resolve to a relation,
# column, view parameter, ClickHouse word, server tool (and its parameters),
# guide URI, or one of the short allowlists below.


def _relation_columns() -> dict[str, set[str]]:
    """Columns per table (DDL fixture, replayed through sql/ CREATE, ADD COLUMN and
    EXCHANGE TABLES) and per view (its output aliases and parameters).

    Dropped columns are kept: the prompt may name them to say they are gone.
    """
    tables = {name: set(cols) for name, (cols, _) in _ddl_statements().items()}
    for stmt in _sql_statements():
        copy = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) AS (\w+)\s*$", stmt.strip())
        if copy:
            tables[copy.group(1)] = set(tables[copy.group(2)])
        altered = re.search(r"ALTER TABLE (\w+)", stmt)
        if altered:
            added = re.findall(r"ADD COLUMN (?:IF NOT EXISTS )?(\w+)", stmt)
            tables.setdefault(altered.group(1), set()).update(added)
        exchanged = re.search(r"EXCHANGE TABLES (\w+) AND (\w+)", stmt)
        if exchanged:
            a, b = exchanged.groups()
            tables[a], tables[b] = tables[b], tables[a]
    for name, (cols, _) in _sql_created_tables().items():
        tables.setdefault(name, set()).update(cols)
    for name, params in _view_params().items():
        tables[name] = set(re.findall(r"\bAS\s+(\w+)", _view_bodies()[name], re.I)) | params
    return tables


def _view_params() -> dict[str, set[str]]:
    return {name: set(re.findall(r"\{(\w+):", body)) for name, body in _view_bodies().items()}


def _server_tools() -> dict[str, set[str]]:
    tools = asyncio.run(server.mcp.get_tools())
    return {name: set(tool.parameters.get("properties", {})) for name, tool in tools.items()}


def _guide_uri_exists(uri: str) -> bool:
    text = server.read_guide.fn(uri)
    return not text.startswith(("Resource not found:", "No pitfall numbered"))


def _sql_literals() -> set[str]:
    """String literals the shipped SQL uses (attribute names, enum values, preferences)."""
    return {lit for stmt in _sql_statements() for lit in re.findall(r"'([^'\n]*)'", stmt)}


def _oncotree_codes() -> set[str]:
    return {entry["code"] for entry in server._load_oncotree_data()}


# ClickHouse keywords, functions and types the prompt uses (matched case-insensitively).
CLICKHOUSE_WORDS = {
    *"SELECT DISTINCT FROM WHERE AND OR NOT IN IS NULL AS ON JOIN LEFT UNION ALL".split(),
    *"GROUP BY ORDER DESC ASC WITH LIKE CAST DESCRIBE TABLE true".split(),
    *"count countIf sum avg min max maxIf quantile round nullIf lower upper".split(),
    *"startsWith toFloat64OrNull Nullable String Int Array".split(),
}

# Names the prompt cites to say they do NOT exist; test_negative_examples_do_not_exist
# keeps them honest.
NEGATIVE_EXAMPLES = {
    "oncokb_annotations",
    "tumor_grade",
    "patient_count",
    "cancer_study.patient_count",
}

# External analysis tools the prompt hands off to (not part of this database).
EXTERNAL_NAMES = {"lifelines", "survival::survfit", "fisher.test", "scipy.stats.fisher_exact"}

# Output fields of server tools; test_allowlisted_tool_fields_exist checks server.py.
TOOL_OUTPUT_FIELDS = {"has_guide"}

# Values stored in the data (not the DDL) that the shipped SQL never spells out,
# keyed by the column that holds them; test_column_values_are_keyed_by_real_columns
# checks each key.
COLUMN_VALUES = {
    "attribute_name": {
        *"AGE CANCER_TYPE_DETAILED DAYS_TO_BIRTH MUTATION_COUNT ONCOTREE_CODE SEX".split(),
        *"OS_MONTHS OS_STATUS SAMPLE_TYPE TMB_NONSYNONYMOUS TUMOR_PURITY DFS_* PFS_*".split(),
    },
    "datatype": {"NUMBER", "STRING", "BOOLEAN", "MAF", "CONTINUOUS", "Z-SCORE"},
    "genetic_alteration_type": {"PROTEIN_LEVEL", "METHYLATION", "GENERIC_ASSAY"},
    "profile_type": {
        *"rna_seq_v2_mrna rna_seq_v2_mrna_median_all_sample_Zscores methylation_hm450".split(),
        "mrna",
        "rppa",
    },
    "mutation_type": {"Missense_Mutation", "Nonsense_Mutation", "Frame_Shift_Del", "Splice_Site"},
    "mutation_status": {"UNKNOWN"},
    "mutation_variant": {"V600E"},
    "hugo_gene_symbol": {"TP53"},
    "event_type": {"Status", "Sample acquisition"},
}

# Free-text terms in prose: clinical markers and cBioPortal OQL keywords.
PROSE_TERMS = {"ER-positive", "HER2-negative", "PR", "PD-L1", "DRIVER", "MUT_DRIVER"}

# Whole spans that are formulas in prose, not SQL.
PROSE_SPANS = {"altered/profiled × 100"}

# Call-syntax examples: placeholders allowed ONLY inside that exact snippet.
SYNTAX_EXAMPLES = {"SELECT * FROM view_name(param='…', …)": {"view_name", "param"}}

IDENT_RE = re.compile(r"(?<![\w.:#-])[A-Za-z_][\w]*(?:(?:\.|::)[A-Za-z_]\w*)*(?:-[A-Za-z0-9]\w*)*")
PATTERN_RE = re.compile(r"[\w.<>*]*(?:<[\w.]+>|\w\*|\*\w)[\w.<>*]*")
CALL_RE = re.compile(r"\b(\w+)\(([^()]*)\)")


def _prompt_snippets() -> list[tuple[int, str]]:
    """(line, text) for each inline code span and each line of each fenced block."""
    text = _prompt()
    snippets = []
    for m in re.finditer(r"```\w*\n(.*?)```", text, re.S):
        snippets.append((text.count("\n", 0, m.start(1)) + 1, m.group(1)))
    prose = re.sub(r"```.*?```", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.S)
    for m in re.finditer(r"`([^`\n]+)`", prose):
        snippets.append((text.count("\n", 0, m.start()) + 1, m.group(1)))
    return snippets


def _unquote(sql: str) -> str:
    """Drop string literals and unwrap `quoted` / "quoted" identifiers."""
    sql = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)
    return re.sub(r"[`\"]([\w.]+)[`\"]", r"\1", sql)


def _local_names(sql: str) -> set[str]:
    """CTEs, column aliases and table aliases a snippet defines."""
    stop = "WHERE|ON|JOIN|LEFT|INNER|GROUP|ORDER|UNION|LIMIT|AS|USING|FINAL"
    return set(re.findall(r"\b(\w+)\s+AS\s*\(", sql, re.I)) | {
        *re.findall(r"\bAS\s+(\w+)", sql, re.I),
        *re.findall(rf"\b(?:FROM|JOIN)\s+[\w.]+\s+(?!(?:{stop})\b)(\w+)", sql, re.I),
    }


def _relations_read(sql: str) -> set[str]:
    """FROM/JOIN targets, with quotes removed and database qualifiers kept."""
    return set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", _unquote(sql), re.I))


def _blank(m: re.Match) -> str:
    """Blank out a match but keep its newlines, so offsets still map to prompt lines."""
    return re.sub(r"[^\n]", " ", m.group())


def _problem(line: int, sql: str, offset: int, token: str, why: str) -> str:
    return f"line {line + sql.count(chr(10), 0, offset)}: {token!r} {why}"


def _is_relation(name: str, relations, local) -> bool:
    parts = name.split(".")
    return (len(parts) <= 2 and parts[-1] in relations) or (len(parts) == 1 and name in local)


def _unresolved_tokens() -> list[str]:
    columns = _relation_columns()
    relations = set(columns)
    documented = _documented_tables()
    views = _view_params()
    tools = _server_tools()
    bare = (
        relations
        | set().union(*(columns[t] for t in documented | set(views)))
        | CLICKHOUSE_WORDS
        | set(tools)
        | EXTERNAL_NAMES
        | TOOL_OUTPUT_FIELDS
        | set().union(*COLUMN_VALUES.values())
        | PROSE_TERMS
        | NEGATIVE_EXAMPLES
        | _sql_literals()
        | _oncotree_codes()
    )
    keywords = {w.lower() for w in CLICKHOUSE_WORDS}

    def resolves(token: str, local: set[str]) -> bool:
        if token in bare or token in local or token.lower() in keywords:
            return True
        if token.startswith("_"):  # column-name suffix, e.g. driver_filter + `_annotation`
            return any(n.endswith(token) for n in bare)
        parts = token.split(".")
        if len(parts) == 1:
            return False
        if parts[0] in local:  # alias.column
            return True
        if parts[0] not in relations and parts[1] in relations:  # db.table[.column]
            parts = parts[1:]
        if parts[0] not in relations or len(parts) > 2:
            return False
        return len(parts) == 1 or parts[1] in columns[parts[0]]

    problems = []
    for line, raw in _prompt_snippets():
        if raw in PROSE_SPANS or raw in bare:
            continue
        allowed = SYNTAX_EXAMPLES.get(raw, set())
        sql = _unquote(raw)
        local = _local_names(sql) | allowed

        def fail(offset, token, why, line=line, sql=sql):
            problems.append(_problem(line, sql, offset, token, why))

        for target in _relations_read(raw):
            if not _is_relation(target, relations, local):
                fail(sql.find(target), target, "is read FROM/JOIN but is not a table or view")

        for m in re.finditer(r"cbioportal://[\w/#-]+|(?<![\w/])#(\w+)", sql):
            uri = m.group() if m.group(1) is None else f"cbioportal://common-pitfalls{m.group()}"
            if uri != "cbioportal://common-pitfalls#N" and not _guide_uri_exists(uri):
                fail(m.start(), m.group(), "is not a guide URI read_guide() serves")
        sql = re.sub(r"\[[^\]]*\]\([^)]*\)", _blank, sql)  # markdown link template
        sql = re.sub(r"cbioportal://[\w/#-]+|(?<![\w/])#\w+|https?://\S+", _blank, sql)
        sql = re.sub(  # repo paths
            r"[\w.-]+(?:/[\w.-]+)+/?",
            lambda m: _blank(m) if (REPO / m.group()).exists() else m.group(),
            sql,
        )

        for m in CALL_RE.finditer(sql):
            params = tools.get(m.group(1), views.get(m.group(1)))
            if params is None:
                continue
            for arg in (a.strip() for a in m.group(2).split(",")):
                name = arg.split("=")[0].strip()
                if re.fullmatch(r"\w+", name) and name not in params | allowed:
                    fail(m.start(), f"{m.group(1)}({name})", "is not a parameter of that call")
        sql = CALL_RE.sub(  # tool/view arguments are checked above
            lambda m: m.group(1) + "(" + re.sub(r"[^\n]", " ", m.group(2)) + ")"
            if m.group(1) in tools or m.group(1) in views
            else m.group(),
            sql,
        )

        for m in PATTERN_RE.finditer(sql):
            for inner in re.findall(r"<([\w.]+)>", m.group()):
                if not resolves(inner, local):
                    fail(m.start(), inner, "is not a known identifier")
            literal = re.sub(r"<[\w.]+>", "", m.group())
            if "*" in m.group() and m.group() not in bare:
                regex = re.compile(re.escape(literal).replace(r"\*", r"\w*"))
                if not any(regex.fullmatch(n) for n in bare):
                    fail(m.start(), m.group(), "matches no known identifier")
        sql = PATTERN_RE.sub(_blank, sql)

        for m in IDENT_RE.finditer(sql):
            token = m.group()
            if not resolves(token, local):
                fail(m.start(), token, "is not a known table, column, function, tool or value")
    return problems


def test_every_backticked_identifier_in_prompt_resolves():
    problems = _unresolved_tokens()

    assert not problems, "prompt names identifiers that don't exist:\n" + "\n".join(problems)


def test_negative_examples_do_not_exist():
    columns = _relation_columns()
    everything = set(columns) | set().union(*columns.values())
    for name in NEGATIVE_EXAMPLES:
        table, _, column = name.rpartition(".")
        if table:
            assert column not in columns[table], name
        else:
            assert name not in everything, name
        assert re.search(rf"\bno(?:\*\*)? `{re.escape(name)}`", _prompt()), name


def test_allowlisted_tool_fields_exist():
    source = Path(server.__file__).read_text()
    for field in TOOL_OUTPUT_FIELDS:
        assert re.search(rf"\[['\"]{field}['\"]\]", source), field


def test_column_values_are_keyed_by_real_columns():
    columns = _relation_columns()
    documented = set().union(*(columns[t] for t in _documented_tables()))
    for column in COLUMN_VALUES:
        assert column in documented, column


def test_precomputed_views_table_names_only_shipped_views():
    views = set(_view_bodies())
    section = _section(_prompt(), "## Precomputed Views")
    rows = [ln for ln in section.splitlines() if ln.startswith("|")][2:]  # skip header
    named = []
    for row in rows:
        cell = row.split("|")[1]
        names = re.findall(r"`(\w+)\(", cell)
        assert names, f"Precomputed Views row names no `view(...)`: {row}"
        named += names

    assert len(named) >= 14, named
    unknown = sorted(set(named) - views)
    assert not unknown, f"Precomputed Views table lists views not created in sql/: {unknown}"


def test_recipe_sql_reads_only_existing_relations():
    relations = set(_relation_columns())
    read = set()
    for line, raw in _prompt_snippets():
        sql = _unquote(raw)
        local = _local_names(sql) | SYNTAX_EXAMPLES.get(raw, set())
        for target in _relations_read(raw):
            at = line + sql.count("\n", 0, sql.find(target))
            assert _is_relation(target, relations, local), (
                f"line {at}: prompt SQL reads from {target!r}, which is not a table in the "
                f"DDL fixture or a table/view created by sql/:\n{raw}"
            )
            read.add(target)
    assert {"genomic_event_derived", "gene_mutation_frequency_in_study"} <= read, sorted(read)


def test_documented_tables_exist_in_ddl():
    documented = _documented_tables()

    assert len(documented) >= 22, sorted(documented)
    for table in [
        "cancer_study",
        "clinical_data_derived",
        "genomic_event_derived",
        "resource_sample",
        "clinical_event_derived",
        "cancer_study_query_preferences",
    ]:
        assert table in documented, table


def test_system_prompt_sort_keys_match_ddl_order_by():
    schema = _section(_prompt(), "## Core Schema")
    tables = _all_tables()
    pattern = r"^(?:\*\*`(\w+)`\*\*|- `(\w+)`)[^\n]*?order by `(\([^`]*\))`"
    claims = re.findall(pattern, schema, re.M)
    claimed = {a or b: key for a, b, key in claims}

    assert len(claimed) == len(claims) >= 7, claims
    for table in ["clinical_data_derived", "genomic_event_derived", "genetic_alteration_derived"]:
        assert table in claimed, table
    for table, key in claimed.items():
        assert table in tables, table
        assert key == tables[table][1], f"{table}: prompt says {key}, DDL says {tables[table][1]}"


def _nullable_string_columns(table: str, tables) -> set[tuple[str, str]]:
    return {(table, c) for c, t in tables[table][0].items() if "Nullable(String)" in t}


def test_nullable_string_list_equals_ddl_for_documented_tables():
    tables = _all_tables()
    documented = _documented_tables()
    expected = set().union(*(_nullable_string_columns(t, tables) for t in documented))
    exchanged = dict(re.findall(r"EXCHANGE TABLES (\w+) AND (\w+)", " ".join(_sql_statements())))
    for stmt in _sql_statements():
        altered = re.search(r"ALTER TABLE (\w+)", stmt)
        if altered:
            table = exchanged.get(altered.group(1), altered.group(1))
            added = r"ADD COLUMN (?:IF NOT EXISTS )?(\w+) Nullable\(String\)"
            for column in re.findall(added, stmt):
                assert (
                    table in documented
                ), f"sql/ adds Nullable(String) {table}.{column}; document {table} in the prompt"
                expected.add((table, column))
    line = _prompt_line("- Most String columns hold `''` (not NULL)")

    assert "are `Nullable(String)` instead" in line
    assert set(re.findall(r"`(\w+)\.(\w+)`", line)) == expected
    # Undocumented tables (e.g. new sql/ tables) are covered by this scope statement.
    assert "This list covers only the documented tables" in line
    assert "DESCRIBE TABLE" in line


def test_view_filter_lists_match_view_sql_per_branch():
    views = _view_bodies()
    counted = {v for v, b in views.items() if "genomic_event_derived" in b} | {
        "gene_cna_distribution_in_study"
    }

    assert _prompt_view_list("- `off_panel = 0`:", views) == _expected_with(views, OFF_PANEL_RE)
    assert _prompt_view_list("- `mutation_status != 'UNCALLED'`:", views) == _expected_with(
        views, UNCALLED_RE
    )
    assert _prompt_view_list("- No `mutation_status` filter:", views) == _expected_without(
        views, UNCALLED_RE, counted
    )


def test_recipe_sql_keeps_view_filters():
    views = _view_bodies()
    reference = {
        "mutation": "gene_mutation_frequency_in_study",
        "cna": "top_cna_genes_in_study",
        "structural_variant": "top_sv_genes_in_study",
    }
    prompt = _prompt()
    snippets = _code_blocks(prompt) + [_prompt_line("- Mutation counting filter:")]
    checked = 0
    for snippet in snippets:
        for variant_type in re.findall(r"variant_type = '(\w+)'", snippet):
            if "genomic_event_derived" not in snippet and "counting filter" not in snippet:
                continue
            view = views[reference[variant_type]]
            for predicate in (UNCALLED_RE, OFF_PANEL_RE):
                assert bool(re.search(predicate, snippet)) == bool(
                    re.search(predicate, view)
                ), f"{predicate} differs between {reference[variant_type]} and:\n{snippet}"
            checked += 1
    assert checked >= 2


def test_profiled_denominator_claims_match_view_sql():
    views = _view_bodies()
    denominator_line = _prompt_line("- Profiled denominator = samples profiled for the gene")
    assert "every view in the `off_panel = 0` list" in denominator_line
    for v in (v for v, b in views.items() if re.search(OFF_PANEL_RE, b)):
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
    columns = set(_all_tables()["cancer_study_query_preferences"][0])

    line = _prompt_line("**`cancer_study_query_preferences`**")
    marker = (
        "Defined in the public-portal SQL (`sql/portal-specific/public-portal/`); "
        "present only where that directory is applied:"
    )
    before, _, after = line.partition(marker)
    assert after, "public-portal wording missing"
    # Parenthesised notes after each preference may mention other identifiers.
    after_names = re.sub(r"\([^()]*\)", "", after)
    assert _backticked(after_names, public | everywhere) == public
    named_before = _backticked(before, columns | everywhere | {"cancer_study_query_preferences"})
    assert named_before & (public | everywhere) == everywhere


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
