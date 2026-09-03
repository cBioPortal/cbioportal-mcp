import mcp_clickhouse.mcp_server as ch_mcp_server

from cbioportal_mcp import server


def _rows(n):
    return [{"i": i} for i in range(n)]


class _FakeClickHouseResult:
    def __init__(self, column_names, result_rows):
        self.column_names = column_names
        self.result_rows = result_rows


class _FakeClickHouseClient:
    def __init__(self, result_rows):
        self._result_rows = result_rows
        self.queries = []

    def query(self, query, settings=None):
        self.queries.append({"query": query, "settings": settings})
        return _FakeClickHouseResult(["i"], [(i,) for i in range(self._result_rows)])


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


def test_run_select_query_with_max_rows_passes_clickhouse_settings(monkeypatch):
    fake_client = _FakeClickHouseClient(result_rows=3)
    monkeypatch.setattr(ch_mcp_server, "create_clickhouse_client", lambda: fake_client)
    monkeypatch.setattr(ch_mcp_server, "get_readonly_setting", lambda client: "1")

    result = server.run_select_query("SELECT 1", query_label="test", max_rows=50)

    assert result == [{"i": i} for i in range(3)]
    assert len(fake_client.queries) == 1
    assert fake_client.queries[0]["settings"] == {
        "readonly": "1",
        "max_result_rows": 50,
        "result_overflow_mode": "break",
    }


def test_run_select_query_without_max_rows_uses_vendored_wrapper(monkeypatch):
    calls = []

    def fake_ch_run_select_query(query):
        calls.append(query)
        return {"columns": ["i"], "rows": [(1,), (2,)]}

    monkeypatch.setattr(ch_mcp_server, "run_select_query", fake_ch_run_select_query)

    result = server.run_select_query("SELECT 1", query_label="test")

    assert calls == ["SELECT 1"]
    assert result == [{"i": 1}, {"i": 2}]
