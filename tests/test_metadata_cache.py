"""Schema and dynamic-guide cache behavior through the actual tools."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import mcp_clickhouse.mcp_server
import pytest

from cbioportal_mcp import metadata_cache, server


def _patch_run_query(monkeypatch, payload):
    """Stub mcp-clickhouse's run_query (returns JSON text) and count calls."""
    query = Mock(side_effect=lambda _sql: json.dumps(payload))
    monkeypatch.setattr(mcp_clickhouse.mcp_server, "run_query", query)
    return query


@pytest.fixture(autouse=True)
def cache_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(metadata_cache, "_now", lambda: now[0])
    monkeypatch.setattr(metadata_cache, "METADATA_CACHE_TTL_SECONDS", 3600)
    server._clear_schema_cache()
    server._clear_study_guide_cache()
    yield now
    server._clear_schema_cache()
    server._clear_study_guide_cache()


@pytest.mark.parametrize("table", [None, "sample"])
def test_schema_hit_expiry_errors_and_clear(monkeypatch, cache_clock, table):
    query = _patch_run_query(
        monkeypatch, {"columns": ["name", "type"], "rows": [["sample", "String"]]}
    )
    call = (
        (lambda: server.clickhouse_list_tables.fn.__wrapped__())
        if table is None
        else (lambda: server.clickhouse_list_table_columns.fn.__wrapped__(table))
    )
    first = call()
    assert call() == first
    assert query.call_count == 1
    # Returned mutable objects cannot poison the stored response.
    first[next(iter(first))].clear()
    assert call()[next(iter(first))]
    cache_clock[0] += 3600
    call()
    assert query.call_count == 2
    server._clear_schema_cache()
    ok = query.side_effect
    query.side_effect = RuntimeError("offline")
    assert "error_message" in call()
    query.side_effect = ok
    assert "error_message" not in call()
    assert query.call_count == 4


def test_columns_keyed_by_table(monkeypatch):
    def describe(sql):
        table = sql.split()[-1]
        return json.dumps({"columns": ["name", "type"], "rows": [[f"{table}_id", "String"]]})

    query = Mock(side_effect=describe)
    monkeypatch.setattr(mcp_clickhouse.mcp_server, "run_query", query)
    list_columns = server.clickhouse_list_table_columns.fn.__wrapped__
    results = [list_columns(t) for t in ["sample", "patient", "sample"]]
    assert query.call_count == 2
    fields = ["name", "type", "comment"]
    assert results[0] == results[2] == {"fields": fields, "columns": [["sample_id", "String", ""]]}
    assert results[1] == {"fields": fields, "columns": [["patient_id", "String", ""]]}


def test_dynamic_guide_hit_expiry_keys_and_clear(monkeypatch, cache_clock):
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)

    def result(query, *, query_label):
        return [{"name": "Test"}] if query_label == "study_guide.study_info" else []

    query = Mock(side_effect=result)
    monkeypatch.setattr(server, "run_select_query", query)
    first = server.get_study_guide.fn.__wrapped__("alpha")
    assert server.get_study_guide.fn.__wrapped__("ALPHA") == first
    assert query.call_count == 7
    server.get_study_guide.fn.__wrapped__("beta")
    assert query.call_count == 14
    cache_clock[0] += 3600
    server.get_study_guide.fn.__wrapped__("alpha")
    assert query.call_count == 21
    server._clear_study_guide_cache()
    server.get_study_guide.fn.__wrapped__("alpha")
    assert query.call_count == 28


@pytest.mark.parametrize("failing_label", ["study_guide.study_info", "study_guide.panels"])
def test_dynamic_guide_errors_not_cached(monkeypatch, failing_label):
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    fail = [True]

    def result(query, *, query_label):
        if fail[0] and query_label == failing_label:
            raise RuntimeError("offline")
        return [{"name": "Test"}] if query_label == "study_guide.study_info" else []

    monkeypatch.setattr(server, "run_select_query", result)
    assert server.get_study_guide.fn.__wrapped__("alpha").startswith("Error generating")
    fail[0] = False
    assert server.get_study_guide.fn.__wrapped__("alpha").startswith("# Study Guide")


def test_cache_can_be_disabled(monkeypatch):
    monkeypatch.setattr(metadata_cache, "METADATA_CACHE_TTL_SECONDS", 0)
    cache = metadata_cache.MetadataCache()
    cache.put("key", [])
    assert cache.get("key") is None


@pytest.mark.parametrize(
    "raw, expected, warns",
    [
        (None, 3600.0, False),
        ("120", 120.0, False),
        ("0", 0.0, False),
        ("bogus", 3600.0, True),
        ("nan", 3600.0, True),
        ("inf", 3600.0, True),
        ("-1", 3600.0, True),
    ],
)
def test_ttl_env_parsing(monkeypatch, caplog, raw, expected, warns):
    if raw is None:
        monkeypatch.delenv("CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS", raw)
    with caplog.at_level(logging.WARNING, logger=metadata_cache.__name__):
        assert metadata_cache._ttl_from_env() == expected
    warnings = [r for r in caplog.records if "Invalid CBIOPORTAL_MCP_METADATA" in r.getMessage()]
    assert len(warnings) == (1 if warns else 0)


class _BrokenCache:
    def get(self, key):
        raise RuntimeError("cache get broke")

    def put(self, key, value):
        raise RuntimeError("cache put broke")

    def clear(self):
        pass


def test_cache_failure_never_changes_tool_answer(monkeypatch):
    monkeypatch.setattr(server, "_schema_cache", _BrokenCache())
    monkeypatch.setattr(server, "_study_guide_cache", _BrokenCache())
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    _patch_run_query(monkeypatch, {"columns": ["name", "type"], "rows": [["sample", "String"]]})
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda q, *, query_label: (
            [{"name": "T"}] if query_label == "study_guide.study_info" else []
        ),
    )
    assert server.clickhouse_list_tables.fn.__wrapped__() == {"tables": ["sample"]}
    assert server.clickhouse_list_table_columns.fn.__wrapped__("sample") == {
        "fields": ["name", "type", "comment"],
        "columns": [["sample", "String", ""]],
    }
    assert server.get_study_guide.fn.__wrapped__("alpha").startswith("# Study Guide")


def test_cache_hit_is_logged_at_debug(monkeypatch, caplog):
    _patch_run_query(monkeypatch, {"columns": [], "rows": [["sample"]]})
    with caplog.at_level(logging.DEBUG, logger=server.logger.name):
        server.clickhouse_list_tables.fn.__wrapped__()
        assert not any("metadata cache hit" in r.getMessage() for r in caplog.records)
        server.clickhouse_list_tables.fn.__wrapped__()
    assert any("metadata cache hit" in r.getMessage() for r in caplog.records)


def test_lru_evicts_least_recently_used():
    cache = metadata_cache.MetadataCache(maxsize=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # "a" is now most recently used
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_concurrent_get_put_is_consistent():
    cache = metadata_cache.MetadataCache(maxsize=8)
    barrier = threading.Barrier(8)

    def worker(n):
        barrier.wait(timeout=5)
        for i in range(200):
            key = (n, i % 12)  # 12 keys per thread vs maxsize 8 forces eviction races
            cache.put(key, {"n": n, "i": i % 12, "rows": [n] * 10})
            value = cache.get(key)
            # A hit must be this key's value; a miss is allowed after eviction.
            assert value is None or value == {"n": n, "i": i % 12, "rows": [n] * 10}
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(worker, range(8)))
    assert len(cache._entries) <= 8


def test_disabled_debug_does_not_format_select_result(monkeypatch):
    class Unformattable(list):
        def __str__(self):
            pytest.fail("result formatted with debug disabled")

        __repr__ = __str__

    monkeypatch.setattr(server, "run_select_query", lambda *a, **kw: Unformattable())
    monkeypatch.setattr(server.logger, "level", 20)
    assert server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1") == {
        "columns": [],
        "rows": [],
        "row_count": 0,
    }
