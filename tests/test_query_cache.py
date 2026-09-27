"""Exercise the query cache pilot through the SELECT funnel without a database."""

import logging
import os
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from clickhouse_connect.driver.exceptions import ProgrammingError
from fastmcp.server.context import Context, _current_context
from mcp_clickhouse import mcp_server as upstream

from cbioportal_mcp import query_cache, server, telemetry

CACHE_SETTINGS = {
    "use_query_cache": 1,
    "query_cache_ttl": 120,
    "query_cache_nondeterministic_function_handling": "ignore",
}
LLM = "clickhouse_run_select_query"
VIEW_QUERIES = [
    "SELECT * FROM gene_mutation_frequency_by_cancer_type(preference='public', gene='TP53')",
    "SELECT * FROM top_mutated_genes_in_cohort(preference = 'public') LIMIT 5",
    "select * from TOP_MUTATED_GENES_IN_STUDY (study='msk_chord_2024')",
    "WITH t AS (SELECT * FROM gene_mutation_frequency_in_study(study='x', gene='KRAS'))"
    " SELECT * FROM t",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (query_cache.ENABLED_ENV, query_cache.TTL_ENV):
        monkeypatch.delenv(name, raising=False)
    query_cache.reset_for_tests()
    config = Mock()
    config.get_client_config.side_effect = lambda: {"host": "test", "username": "reader"}
    config.allow_write_access = False
    monkeypatch.setattr(upstream, "get_config", lambda: config)
    yield
    query_cache.reset_for_tests()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(query_cache.ENABLED_ENV, "1")
    monkeypatch.setenv(query_cache.TTL_ENV, "120")


@pytest.fixture
def request_context():
    ctx = Context(fastmcp=server.mcp)
    token = _current_context.set(ctx)
    yield ctx
    _current_context.reset(token)


@pytest.fixture
def database(monkeypatch):
    client = Mock()
    client.query.return_value = SimpleNamespace(column_names=["n"], result_rows=[[42]])
    configs = []

    def acquire(config):
        configs.append(dict(config))
        return upstream._ClientCacheEntry(client=client, last_used=0, active_users=1)

    monkeypatch.setattr(upstream, "_acquire_clickhouse_client", acquire)
    monkeypatch.setattr(upstream, "_release_client_entry", Mock())
    monkeypatch.setattr(upstream, "get_readonly_setting", lambda client: "1")
    return SimpleNamespace(client=client, configs=configs)


@pytest.fixture
def metrics(monkeypatch):
    client = Mock()
    monkeypatch.setattr(telemetry, "_get_dogstatsd_client", lambda: client)
    return client


def _client_settings(database):
    return database.configs[-1].get("settings", {})


@pytest.mark.parametrize("flag", [None, "0", "true", "yes", "1"])
@pytest.mark.parametrize("query", VIEW_QUERIES)
def test_only_flag_1_caches_standard_views(
    monkeypatch, request_context, database, metrics, flag, query
):
    if flag is not None:
        monkeypatch.setenv(query_cache.ENABLED_ENV, flag)
    monkeypatch.setenv(query_cache.TTL_ENV, "120")

    assert server.run_select_query(query, query_label=LLM) == [{"n": 42}]
    if flag == "1":
        assert _client_settings(database) == CACHE_SETTINGS
    else:
        assert "use_query_cache" not in _client_settings(database)
    assert database.client.query.call_args.args == (query,)
    assert database.client.query.call_args.kwargs["settings"]["readonly"] == "1"
    tags = {"query_label": LLM, "success": "true"}
    if flag == "1":
        tags["query_cache"] = "on"
    metrics.increment.assert_called_once_with("db_query.calls", tags)
    assert request_context.get_state(upstream.CLIENT_CONFIG_OVERRIDES_KEY) is None


def test_allowlisted_label_is_cached(enabled, request_context, database):
    server.run_select_query("SELECT 1", query_label="study_guide.top_genes")
    assert _client_settings(database) == CACHE_SETTINGS


@pytest.mark.parametrize(
    "query_label, query",
    [
        ("list_studies.all_studies", "SELECT * FROM cancer_study"),
        ("study_guide.counts", "SELECT COUNT(*) FROM clinical_data_derived"),
        ("similar_study_identifiers", "SELECT cancer_study_identifier FROM cancer_study"),
        # Agent SQL: system tables, even alongside a standard view.
        (LLM, "SELECT * FROM system.query_cache"),
        (LLM, "SELECT * FROM System . tables"),
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_cohort(preference='public')"
            " WHERE hugo_gene_symbol IN (SELECT name FROM system.tables)",
        ),
        # Agent SQL: no standard view call.
        (LLM, "SELECT hugo_gene_symbol FROM genomic_event_derived LIMIT 5"),
        (LLM, "WITH x AS (SELECT 42 AS n) SELECT n FROM x"),
        (LLM, "SELECT 'top_mutated_genes_in_cohort' AS name"),
    ],
)
def test_not_cached_when_flag_on(enabled, request_context, database, metrics, query_label, query):
    assert query_cache.query_cache_settings(query_label, query) is None
    server.run_select_query(query, query_label=query_label)
    assert "use_query_cache" not in _client_settings(database)
    metrics.increment.assert_called_once_with(
        "db_query.calls", {"query_label": query_label, "success": "true"}
    )


def test_no_request_context_runs_uncached(enabled, database, metrics):
    server.run_select_query(VIEW_QUERIES[0], query_label=LLM)
    assert "use_query_cache" not in _client_settings(database)
    metrics.increment.assert_called_once_with(
        "db_query.calls", {"query_label": LLM, "success": "true"}
    )


def test_disabled_delegates_unchanged(monkeypatch, request_context):
    monkeypatch.setenv(query_cache.TTL_ENV, "invalid")
    payload = '{"columns": ["n"], "rows": [[42]]}'
    delegate = Mock(return_value=payload)
    monkeypatch.setattr(upstream, "run_query", delegate)
    query = " SELECT * FROM top_mutated_genes_in_cohort(preference='public'); -- x\n"
    settings = query_cache.query_cache_settings(LLM, query)
    assert settings is None
    assert query_cache.run_query(query, settings=settings) is payload
    delegate.assert_called_once_with(query)


def test_request_overrides_merged_and_restored(enabled, request_context, database):
    key = upstream.CLIENT_CONFIG_OVERRIDES_KEY
    original = {"username": "alice", "settings": {"role": "reader", "max_threads": 2}}
    request_context.set_state(key, original)
    server.run_select_query(VIEW_QUERIES[0], query_label=LLM)
    config = database.configs[-1]
    assert config["username"] == "alice"
    assert config["settings"] == {"role": "reader", "max_threads": 2, **CACHE_SETTINGS}
    assert request_context.get_state(key) is original
    assert original == {"username": "alice", "settings": {"role": "reader", "max_threads": 2}}

    # A later non-cacheable query in the same request sees only the originals.
    server.run_select_query("SELECT 1", query_label="study_guide.counts")
    assert _client_settings(database) == {"role": "reader", "max_threads": 2}


def test_overrides_restored_on_failure(enabled, request_context, database, metrics):
    database.client.query.side_effect = ValueError("denied")
    result = server.clickhouse_run_select_query.fn(VIEW_QUERIES[0])
    assert "denied" in result["error_message"]
    assert request_context.get_state(upstream.CLIENT_CONFIG_OVERRIDES_KEY) is None
    metrics.increment.assert_any_call(
        "db_query.errors", {"query_label": LLM, "success": "false", "query_cache": "on"}
    )


@pytest.mark.parametrize(
    "message",
    [
        "Setting use_query_cache is unknown or readonly",
        "Code: 115. DB::Exception: Unknown setting 'use_query_cache'. (UNKNOWN_SETTING)",
        "Code: 164. DB::Exception: Cannot modify 'use_query_cache' setting in readonly mode",
        "Code: 452. DB::Exception: Setting query_cache_ttl shouldn't be greater than 300."
        " (SETTING_CONSTRAINT_VIOLATION)",
    ],
)
def test_server_rejection_falls_back_uncached_once(
    enabled, request_context, database, caplog, message
):
    def query(sql, settings):
        if database.configs[-1].get("settings", {}).get("use_query_cache"):
            raise ProgrammingError(message)
        return SimpleNamespace(column_names=["n"], result_rows=[[42]])

    database.client.query.side_effect = query
    with caplog.at_level(logging.WARNING, logger=query_cache.__name__):
        for _ in range(3):
            assert server.run_select_query(VIEW_QUERIES[0], query_label=LLM) == [{"n": 42}]
    # One rejected attempt, then uncached for the retry and every later query.
    assert [bool(c.get("settings")) for c in database.configs] == [True, False, False, False]
    assert len([r for r in caplog.records if "rejected" in r.getMessage()]) == 1
    assert query_cache.query_cache_settings(LLM, VIEW_QUERIES[0]) is None


def test_other_errors_do_not_disable_pilot(enabled, request_context, database):
    database.client.query.side_effect = ValueError("Unknown identifier: foo")
    with pytest.raises(upstream.ToolError, match="Unknown identifier"):
        server.run_select_query(VIEW_QUERIES[0], query_label=LLM)
    assert len(database.configs) == 1
    assert query_cache.query_cache_settings(LLM, VIEW_QUERIES[0]) == CACHE_SETTINGS


@pytest.mark.parametrize(
    "query",
    [
        VIEW_QUERIES[0] + " SETTINGS use_query_cache = 1, query_cache_ttl = 86400",
        "SELECT 1 SETTINGS query_cache_share_between_users = 1",
        "SELECT 1 SETTINGS Enable_Reads_From_Query_Cache=0",
    ],
)
def test_agent_query_cache_settings_rejected_when_on(enabled, request_context, database, query):
    result = server.clickhouse_run_select_query.fn(query)
    assert "query_cache settings are managed by the server" in result["error_message"]
    assert database.configs == []


def test_agent_query_cache_settings_untouched_when_off(request_context, database):
    query = "SELECT 1 SETTINGS use_query_cache = 1"
    assert server.clickhouse_run_select_query.fn(query) == {"rows": [{"n": 42}]}


def test_enabled_preserves_row_cap(enabled, request_context, database):
    server.run_select_query(VIEW_QUERIES[1] + ";", query_label=LLM, max_rows=10)
    assert database.client.query.call_args.args == (f"SELECT * FROM ({VIEW_QUERIES[1]}) LIMIT 11",)
    assert _client_settings(database) == CACHE_SETTINGS


def test_study_guide_caches_only_top_genes(enabled, request_context, monkeypatch):
    seen = {}

    def fake_run_query(query, *, settings):
        overrides = query_cache._request_context()
        state = overrides and overrides.get_state(upstream.CLIENT_CONFIG_OVERRIDES_KEY)
        seen[query] = (settings, state)
        return '{"columns": ["cancer_study_identifier"], "rows": [["x"]]}'

    monkeypatch.setattr(query_cache, "run_query", fake_run_query)
    labels = []
    real_settings = query_cache.query_cache_settings

    def record(query_label, query):
        settings = real_settings(query_label, query)
        labels.append((query_label, settings is not None))
        return settings

    monkeypatch.setattr(query_cache, "query_cache_settings", record)
    server.get_study_guide.fn("x")
    assert ("study_guide.top_genes", True) in labels
    assert [label for label, cached in labels if cached] == ["study_guide.top_genes"]


# --- TTL configuration -------------------------------------------------------


def test_default_ttl(monkeypatch, request_context):
    monkeypatch.setenv(query_cache.ENABLED_ENV, "1")
    assert query_cache.query_cache_settings(LLM, VIEW_QUERIES[0])["query_cache_ttl"] == 3600


def test_ttl_clamped_to_cap(monkeypatch, caplog):
    monkeypatch.setenv(query_cache.ENABLED_ENV, "1")
    monkeypatch.setenv(query_cache.TTL_ENV, "86400")
    with caplog.at_level(logging.WARNING, logger=query_cache.__name__):
        assert query_cache.load_config() == query_cache.MAX_TTL_SECONDS
    assert "exceeds" in caplog.text


@pytest.mark.parametrize("value", ["0", "-1", "bad", "1.5"])
def test_invalid_ttl_fails_at_startup(monkeypatch, value):
    monkeypatch.setenv(query_cache.ENABLED_ENV, "1")
    monkeypatch.setenv(query_cache.TTL_ENV, value)
    with pytest.raises(ValueError, match=query_cache.TTL_ENV):
        query_cache.load_config()


def test_config_validated_once(monkeypatch, request_context):
    monkeypatch.setenv(query_cache.ENABLED_ENV, "1")
    monkeypatch.setenv(query_cache.TTL_ENV, "120")
    assert query_cache.load_config() == 120
    monkeypatch.setenv(query_cache.TTL_ENV, "bad")
    assert query_cache.query_cache_settings(LLM, VIEW_QUERIES[0]) == CACHE_SETTINGS


# --- Real ClickHouse ---------------------------------------------------------


@pytest.mark.skipif(
    not os.getenv("CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL"),
    reason="set CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL=http://user:pass@host:port to run",
)
def test_real_clickhouse_second_query_hits_cache(monkeypatch, enabled, request_context):
    from urllib.parse import urlparse

    import clickhouse_connect
    from mcp_clickhouse import mcp_env

    url = urlparse(os.environ["CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL"])
    env = {
        "CLICKHOUSE_HOST": url.hostname,
        "CLICKHOUSE_PORT": str(url.port or 8123),
        "CLICKHOUSE_USER": url.username or "default",
        "CLICKHOUSE_PASSWORD": url.password or "",
        "CLICKHOUSE_SECURE": str(url.scheme == "https").lower(),
        "CLICKHOUSE_VERIFY": "false",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(mcp_env, "_CONFIG_INSTANCE", None)
    monkeypatch.setattr(upstream, "get_config", mcp_env.get_config)

    marker = f"cache-test-{uuid.uuid4()}"
    query = f"SELECT number, '{marker}' AS marker FROM numbers(1000) ORDER BY number DESC LIMIT 5"
    first = server.run_select_query(query, query_label="study_guide.top_genes")
    second = server.run_select_query(query, query_label="study_guide.top_genes")
    assert first == second and len(first) == 5

    admin = clickhouse_connect.get_client(
        host=env["CLICKHOUSE_HOST"],
        port=int(env["CLICKHOUSE_PORT"]),
        username=env["CLICKHOUSE_USER"],
        password=env["CLICKHOUSE_PASSWORD"],
        secure=url.scheme == "https",
    )
    try:
        admin.command("SYSTEM FLUSH LOGS")
        rows = admin.query(
            "SELECT query_cache_usage, ProfileEvents['QueryCacheHits'] FROM system.query_log"
            " WHERE type = 'QueryFinish' AND query LIKE {pattern:String}"
            " AND query NOT LIKE '%system.query_log%' ORDER BY event_time_microseconds",
            parameters={"pattern": f"%{marker}%"},
        ).result_rows
    finally:
        admin.close()
    assert len(rows) == 2
    assert rows[0][0] == "Write"
    assert rows[1][0] == "Read" and rows[1][1] > 0
