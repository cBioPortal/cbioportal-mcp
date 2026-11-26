import pytest

from cbioportal_mcp import query_errors, server

COLUMNS = {
    "cancer_study": ["cancer_study_identifier", "name", "type_of_cancer_id", "sample_count"],
    "genomic_event_derived": [
        "sample_unique_id",
        "hugo_gene_symbol",
        "cancer_study_identifier",
        "variant_type",
    ],
}
TABLES = ["cancer_study", "genomic_event_derived", "clinical_data_derived"]


def describe(table):
    return COLUMNS.get(table)


def tables():
    return TABLES


def reply(message, query="SELECT 1"):
    return query_errors.select_query_error(RuntimeError(message), query, describe, tables)


def test_tables_in_query_maps_aliases_and_skips_keywords():
    query = "SELECT * FROM cancer_study s JOIN db.genomic_event_derived AS g ON s.x = g.x WHERE 1 LEFT JOIN cancer_study"
    assert query_errors.tables_in_query(query) == {
        "s": "cancer_study",
        "cancer_study": "cancer_study",
        "g": "genomic_event_derived",
        "genomic_event_derived": "genomic_event_derived",
    }


def test_unknown_qualified_column_lists_the_aliased_tables_columns_and_close_matches():
    out = reply(
        "Code: 47. DB::Exception: Identifier 's.cancer_study_id' cannot be resolved from table with name s.",
        "SELECT s.cancer_study_id FROM cancer_study s",
    )
    assert "Columns of `cancer_study`: cancer_study_identifier, name" in out["hint"]
    assert (
        "Closest to `cancer_study_id` in `cancer_study`: `cancer_study_identifier`" in out["hint"]
    )
    assert (
        "clickhouse_list_table_columns" in out["hint"]
    ), "the general hint is kept after the details"


def test_unknown_column_that_exists_in_another_joined_table_is_pointed_out():
    out = reply(
        "Code: 47. DB::Exception: Unknown expression identifier `hugo_gene_symbol` in scope",
        "SELECT hugo_gene_symbol FROM cancer_study c JOIN genomic_event_derived g ON 1",
    )
    assert "`hugo_gene_symbol` is a column of `genomic_event_derived`" in out["hint"]


def test_unknown_table_suggests_the_closest_names():
    out = reply(
        "Code: 60. DB::Exception: Unknown table expression identifier 'cancer_studies' in scope",
        "SELECT 1 FROM cancer_studies",
    )
    assert "Closest tables: `cancer_study`" in out["hint"]
    assert "Tables: cancer_study, genomic_event_derived, clinical_data_derived." in out["hint"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Query execution failed: unrecognized data found in stream: `7436342929`",
            "tupleElement(test_expression, 2)",
        ),
        (
            "Code: 1. DB::Exception: Resolved identifier 'patient_unique_id' in parent scope to expression",
            "Correlated subqueries",
        ),
        (
            "Query execution failed: Error ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))",
            "safe to run again",
        ),
    ],
)
def test_errors_the_code_table_misses_get_their_fix(message, expected):
    assert expected in reply(message)["hint"]


def test_a_failing_schema_lookup_only_drops_its_own_part():
    def boom(table):
        raise RuntimeError("down")

    out = query_errors.select_query_error(
        RuntimeError(
            "Code: 47. DB::Exception: Identifier 's.nope' cannot be resolved from table with name s."
        ),
        "SELECT s.nope FROM cancer_study s",
        boom,
        tables,
    )
    assert "Columns of" not in out["hint"] and "clickhouse_list_table_columns" in out["hint"]


def test_without_a_query_the_reply_is_unchanged():
    out = query_errors.select_query_error(
        RuntimeError("Code: 47. DB::Exception: Unknown expression identifier `x`")
    )
    assert "Columns of" not in out["hint"]


def test_a_timeout_is_not_a_transient_connection_error():
    assert not query_errors.is_transient_connection_error("Query timed out after 30 seconds")
    assert query_errors.is_transient_connection_error("Connection reset by peer")


def test_select_tool_retries_once_after_a_dropped_connection(monkeypatch):
    calls = []

    def flaky(query, query_label=None, max_rows=None):
        calls.append(query)
        if len(calls) == 1:
            raise RuntimeError(
                "Error ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
            )
        return [{"n": 1}]

    monkeypatch.setattr(server, "run_select_query", flaky)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    assert server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1")["rows"] == [[1]]
    assert len(calls) == 2


def test_select_tool_does_not_retry_a_timeout(monkeypatch):
    calls = []

    def slow(query, query_label=None, max_rows=None):
        calls.append(query)
        raise RuntimeError("Query timed out after 30 seconds")

    monkeypatch.setattr(server, "run_select_query", slow)
    result = server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1")
    assert len(calls) == 1 and result["error_message"].startswith("Query timed out")


def test_select_tool_unknown_column_error_carries_the_real_columns(monkeypatch):
    def bad(query, query_label=None, max_rows=None):
        raise RuntimeError(
            "Code: 47. DB::Exception: Identifier 's.cancer_study_id' cannot be resolved from table with name s."
        )

    monkeypatch.setattr(server, "run_select_query", bad)
    monkeypatch.setattr(server, "_column_names", describe)
    monkeypatch.setattr(server, "_table_names", tables)
    result = server.clickhouse_run_select_query.fn.__wrapped__(
        "SELECT s.cancer_study_id FROM cancer_study s"
    )
    assert "Columns of `cancer_study`" in result["hint"]
