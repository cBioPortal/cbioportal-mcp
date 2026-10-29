import json

import mcp_clickhouse.mcp_server as ch_mcp_server

from cbioportal_mcp import server


def _rows(n):
    return [{"i": i} for i in range(n)]


def test_select_query_under_default_limit_is_not_truncated(monkeypatch):
    monkeypatch.setattr(
        server, "run_select_query", lambda query, query_label=None, max_rows=None: _rows(5)
    )

    result = server.clickhouse_run_select_query.fn("SELECT 1")

    assert result == {"rows": _rows(5)}
    assert "truncated" not in result


def test_select_query_over_default_limit_is_truncated(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query, query_label=None, max_rows=None: _rows(server.DEFAULT_SELECT_MAX_ROWS + 50),
    )

    result = server.clickhouse_run_select_query.fn("SELECT * FROM huge_table")

    assert result["truncated"] is True
    assert result["returned_rows"] == server.DEFAULT_SELECT_MAX_ROWS
    assert "total_rows" not in result
    assert len(result["rows"]) == server.DEFAULT_SELECT_MAX_ROWS
    assert "max_rows" in result["note"]


def test_select_query_passes_max_rows_through_to_run_select_query(monkeypatch):
    captured = {}

    def fake_run_select_query(query, query_label=None, max_rows=None):
        captured["max_rows"] = max_rows
        return _rows(5)

    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    server.clickhouse_run_select_query.fn("SELECT 1", max_rows=42)

    assert captured["max_rows"] == 42


def test_select_query_max_rows_can_be_raised(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query, query_label=None, max_rows=None: _rows(server.DEFAULT_SELECT_MAX_ROWS + 50),
    )

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.DEFAULT_SELECT_MAX_ROWS + 50
    )

    assert "truncated" not in result
    assert len(result["rows"]) == server.DEFAULT_SELECT_MAX_ROWS + 50


def test_select_query_max_rows_is_clamped_to_hard_cap(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query, query_label=None, max_rows=None: _rows(server.MAX_SELECT_MAX_ROWS + 500),
    )

    result = server.clickhouse_run_select_query.fn(
        "SELECT * FROM huge_table", max_rows=server.MAX_SELECT_MAX_ROWS + 5000
    )

    assert result["truncated"] is True
    assert result["returned_rows"] == server.MAX_SELECT_MAX_ROWS


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
