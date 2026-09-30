import json

import mcp_clickhouse.mcp_server as ch_mcp_server
import pytest

from cbioportal_mcp import result_format, server


def _rows(n):
    return [{"i": i} for i in range(n)]


def _patch_rows(monkeypatch, n):
    monkeypatch.setattr(
        server, "run_select_query", lambda query, query_label=None, max_rows=None: _rows(n)
    )


@pytest.fixture
def legacy_format(monkeypatch):
    monkeypatch.setenv(result_format.RESULT_FORMAT_ENV, result_format.LEGACY)


# --- compact format (default) -------------------------------------------------


def test_select_query_under_default_limit_is_not_truncated(monkeypatch):
    _patch_rows(monkeypatch, 5)

    result = server.clickhouse_run_select_query.fn("SELECT 1")

    assert result == {"columns": ["i"], "rows": [[i] for i in range(5)], "row_count": 5}


def test_select_query_over_default_limit_is_truncated(monkeypatch):
    _patch_rows(monkeypatch, server.DEFAULT_SELECT_MAX_ROWS + 50)

    result = server.clickhouse_run_select_query.fn("SELECT * FROM huge_table")

    assert result["truncated"] is True
    assert result["row_count"] == server.DEFAULT_SELECT_MAX_ROWS
    assert len(result["rows"]) == server.DEFAULT_SELECT_MAX_ROWS
    # The exact total is unknown once ClickHouse stops at the cap; never invent it.
    assert "total_rows" not in result
    assert f"{server.DEFAULT_SELECT_MAX_ROWS} of more than" in result["note"]
    assert "aggregate" in result["note"] and "max_rows" in result["note"]


def test_select_query_uses_real_column_order_and_keeps_empty_values(monkeypatch):
    """The compact rows come from ClickHouse's positional result, not the lossy dicts."""
    monkeypatch.setattr(
        ch_mcp_server,
        "run_query",
        lambda q: json.dumps({"columns": ["a", "b", "c"], "rows": [[1, None, ""], [2, "x", "y"]]}),
    )

    result = server.clickhouse_run_select_query.fn("SELECT a, b, c FROM t")

    assert result == {
        "columns": ["a", "b", "c"],
        "rows": [[1, None, ""], [2, "x", "y"]],
        "row_count": 2,
    }


def test_select_query_all_empty_column_is_still_listed(monkeypatch):
    monkeypatch.setattr(
        ch_mcp_server,
        "run_query",
        lambda q: json.dumps({"columns": ["a", "b"], "rows": [[1, None], [2, None]]}),
    )

    result = server.clickhouse_run_select_query.fn("SELECT a, b FROM t")

    assert result["columns"] == ["a", "b"] and result["rows"] == [[1, None], [2, None]]


def test_select_query_cuts_long_cells(monkeypatch):
    monkeypatch.setenv(result_format.MAX_CELL_CHARS_ENV, "10")
    monkeypatch.setattr(
        ch_mcp_server,
        "run_query",
        lambda q: json.dumps({"columns": ["t"], "rows": [["x" * 25], ["short"]]}),
    )

    result = server.clickhouse_run_select_query.fn("SELECT t FROM t")

    assert result["rows"] == [["x" * 10 + "…[+15 chars]"], ["short"]]
    assert result["cut_cells"] == {"t": 1}
    assert "substringUTF8(t, 11, 10)" in result["cell_note"]


def test_select_query_passes_max_rows_through_to_run_select_query(monkeypatch):
    captured = {}

    def fake_run_select_query(query, query_label=None, max_rows=None):
        captured["max_rows"] = max_rows
        return _rows(5)

    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    server.clickhouse_run_select_query.fn("SELECT 1", max_rows=42)

    assert captured["max_rows"] == 42


def test_select_query_max_rows_can_be_raised(monkeypatch):
    _patch_rows(monkeypatch, server.DEFAULT_SELECT_MAX_ROWS + 50)

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.DEFAULT_SELECT_MAX_ROWS + 50
    )

    assert "truncated" not in result
    assert len(result["rows"]) == result["row_count"] == server.DEFAULT_SELECT_MAX_ROWS + 50


def test_select_query_max_rows_is_clamped_to_hard_cap(monkeypatch):
    _patch_rows(monkeypatch, server.MAX_SELECT_MAX_ROWS + 500)

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.MAX_SELECT_MAX_ROWS + 5000
    )

    assert result["truncated"] is True
    assert result["row_count"] == server.MAX_SELECT_MAX_ROWS


def test_default_max_rows_is_env_configurable(monkeypatch):
    monkeypatch.setenv(result_format.MAX_RESULT_ROWS_ENV, "25")
    assert result_format.default_max_rows() == 25
    monkeypatch.setenv(result_format.MAX_RESULT_ROWS_ENV, "0")
    assert result_format.default_max_rows() == result_format.DEFAULT_MAX_RESULT_ROWS


def test_tool_description_documents_the_compact_shape():
    description = server.clickhouse_run_select_query.description
    assert '"columns"' in description and '"row_count"' in description
    assert "aggregate or filter instead of paging" in description


# --- legacy format (CBIOPORTAL_MCP_RESULT_FORMAT=rows) --------------------------


def test_legacy_select_query_under_default_limit_is_not_truncated(monkeypatch, legacy_format):
    _patch_rows(monkeypatch, 5)

    result = server.clickhouse_run_select_query.fn("SELECT 1")

    assert result == {"rows": _rows(5)}
    assert "truncated" not in result


def test_legacy_select_query_over_default_limit_is_truncated(monkeypatch, legacy_format):
    _patch_rows(monkeypatch, server.DEFAULT_SELECT_MAX_ROWS + 50)

    result = server.clickhouse_run_select_query.fn("SELECT * FROM huge_table")

    assert result["truncated"] is True
    assert result["returned_rows"] == server.DEFAULT_SELECT_MAX_ROWS
    assert "total_rows" not in result
    assert len(result["rows"]) == server.DEFAULT_SELECT_MAX_ROWS
    assert "max_rows" in result["note"]


def test_legacy_select_query_max_rows_is_clamped_to_hard_cap(monkeypatch, legacy_format):
    _patch_rows(monkeypatch, server.MAX_SELECT_MAX_ROWS + 500)

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.MAX_SELECT_MAX_ROWS + 5000
    )

    assert result["truncated"] is True
    assert result["returned_rows"] == server.MAX_SELECT_MAX_ROWS


def test_legacy_schema_tools_keep_list_of_dicts(monkeypatch, legacy_format):
    server._clear_schema_cache()
    monkeypatch.setattr(
        ch_mcp_server,
        "run_query",
        lambda q: json.dumps(
            {"columns": ["name", "type", "default_type", "default_expression", "comment"],
             "rows": [["sample_id", "String", "", "", "Sample id"]]}
        ),
    )
    try:
        assert server.clickhouse_list_tables.fn() == {"tables": [{"name": "sample_id"}]}
        assert server.clickhouse_list_table_columns.fn("sample") == {
            "columns": [{"name": "sample_id", "type": "String", "comment": "Sample id"}]
        }
    finally:
        server._clear_schema_cache()


# --- run_select_query (unchanged by the result format) ---------------------------


def _fake_run_query(calls, rows):
    def fake_run_query(query):
        calls.append(query)
        return json.dumps({"columns": ["i"], "rows": [[i] for i in range(rows)]})

    return fake_run_query


def test_run_select_query_with_max_rows_limits_rows_in_clickhouse(monkeypatch):
    calls = []
    monkeypatch.setattr(ch_mcp_server, "run_query", _fake_run_query(calls, rows=3))

    result = server.run_select_query("SELECT 1;", query_label="test", max_rows=50)

    assert result == [{"i": i} for i in range(3)]
    assert calls == ["SELECT * FROM (SELECT 1) LIMIT 51"]


def test_run_select_query_without_max_rows_runs_query_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(ch_mcp_server, "run_query", _fake_run_query(calls, rows=2))

    result = server.run_select_query("SELECT 1", query_label="test")

    assert calls == ["SELECT 1"]
    assert result == [{"i": 0}, {"i": 1}]
    assert result.columns == ["i"] and result.raw_rows == [[0], [1]]
