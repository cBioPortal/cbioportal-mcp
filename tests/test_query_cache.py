"""Exercise the query cache pilot through the SELECT funnel without a database."""

import logging
import os
import threading
import time
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
# Canonical shapes, as mutation-frequency-guide.md teaches them: parameterized
# views called with named parameters.
VIEW_QUERIES = [
    "SELECT * FROM gene_mutation_frequency_by_cancer_type(preference='public', gene='TP53')",
    "SELECT * FROM top_mutated_genes_in_cohort(preference = 'public') LIMIT 5",
    "select * from TOP_MUTATED_GENES_IN_STUDY (study='msk_chord_2024', top_n = 20)",
    "SELECT hugo_gene_symbol, freq FROM gene_mutation_frequency_in_study(\n"
    "    study = 'brca_tcga_pan_can_atlas_2018',\n    gene = 'KRAS'\n) ORDER BY freq DESC",
    "SELECT a.hugo_gene_symbol FROM `top_mutated_genes_in_study`(study='x', top_n=5) AS a"
    " JOIN top_cna_genes_in_study(study='x', top_n=5) b USING hugo_gene_symbol -- system.x",
    "SELECT * FROM (SELECT * FROM top_sv_genes_in_study(study='x', top_n=5)) WHERE n > 1",
    # Bare view names (no parameter list) are still allowlisted sources.
    "SELECT * FROM top_mutated_genes_in_study",
    "SELECT a.* FROM top_mutated_genes_in_study AS a JOIN top_cna_genes_in_study b USING n",
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
        # Bypasses of a raw-text view match: view name only in a comment ...
        (LLM, "SELECT * FROM genomic_event_derived -- top_mutated_genes_in_cohort(x)"),
        (LLM, "SELECT * FROM genomic_event_derived /* top_mutated_genes_in_study( */"),
        (LLM, "SELECT * FROM genomic_event_derived # gene_mutation_frequency_in_study("),
        # ... or only in a string literal ...
        (LLM, "SELECT 'top_mutated_genes_in_cohort(' AS s FROM genomic_event_derived"),
        # ... quoted system database ...
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_cohort(preference='public')"
            " WHERE 1 IN (SELECT 1 FROM `system`.tables)",
        ),
        (LLM, "SELECT * FROM top_mutated_genes_in_cohort(preference='p') JOIN \"system\" . parts"),
        # ... or a standard view joined to an arbitrary table.
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_cohort(preference='public') t"
            " JOIN genomic_event_derived g ON t.hugo_gene_symbol = g.hugo_gene_symbol",
        ),
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_cohort(preference='public') v,"
            " clinical_data_derived c",
        ),
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_cohort(preference='public')"
            " WHERE hugo_gene_symbol IN (SELECT hugo_gene_symbol FROM gene)",
        ),
        (LLM, "SELECT * FROM top_mutated_genes_in_cohort(preference='public') WHERE x IN gene"),
        (LLM, "SELECT * FROM other_db.top_mutated_genes_in_cohort(preference='public')"),
        (LLM, "SELECT dictGet('d', 'v', 1) FROM top_mutated_genes_in_cohort(preference='p')"),
        (LLM, "SELECT * FROM remote('host', system.tables)"),
        (LLM, "SELECT * FROM top_mutated_genes_in_cohort(preference='public') WHERE x = 'open"),
        # Bare view names still require every other source to be allowlisted.
        (LLM, "SELECT * FROM top_mutated_genes_in_study t JOIN genomic_event_derived USING n"),
        (LLM, "SELECT * FROM top_mutated_genes_in_cohort, clinical_data_derived"),
        (LLM, "SELECT * FROM other_db.top_mutated_genes_in_study"),
        # CTE names are never sources: ClickHouse scopes WITH to its own
        # subquery, so an inner CTE must not vouch for an outer table.
        (
            LLM,
            "SELECT s.x, t.hugo_gene_symbol, t.altered FROM top_mutated_genes_in_study"
            " JOIN (WITH genomic_event_derived AS (SELECT 1 AS x) SELECT 1 AS x) s ON 1=1"
            " JOIN genomic_event_derived AS t ON 1=1",
        ),
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_study(study='x', top_n=5)"
            " JOIN (WITH c AS (SELECT 1 AS x) SELECT x FROM c) s ON 1=1 JOIN c ON 1=1",
        ),
        (
            LLM,
            "SELECT * FROM top_mutated_genes_in_study(study='x', top_n=5)"
            " JOIN (SELECT * FROM (WITH clinical_data_derived AS (SELECT 1 AS x)"
            " SELECT x FROM clinical_data_derived)) s ON 1=1"
            " JOIN clinical_data_derived ON 1=1",
        ),
        # ... and a view wrapped in a CTE is simply not cached.
        (
            LLM,
            "WITH t AS (SELECT * FROM gene_mutation_frequency_in_study(study='x', gene='KRAS'))"
            " SELECT * FROM t",
        ),
        (
            LLM,
            "WITH t AS (SELECT * FROM top_mutated_genes_in_study(study='x', top_n=5)),"
            " u AS (SELECT * FROM top_cna_genes_in_study(study='x', top_n=5))"
            " SELECT * FROM t JOIN u USING hugo_gene_symbol",
        ),
        (
            LLM,
            "SELECT max(n), genomic_event_derived AS (1)"
            " FROM top_mutated_genes_in_cohort(preference='p')"
            " JOIN genomic_event_derived USING hugo_gene_symbol",
        ),
        # ARRAY JOIN after a view is intentionally uncached.
        (LLM, "SELECT * FROM top_mutated_genes_in_study(study='x', top_n=5) ARRAY JOIN arr"),
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
    result, cached = query_cache.run_query(query, settings=settings)
    assert result is payload and cached is False
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
    enabled, request_context, database, metrics, caplog, message
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
    metrics.increment.assert_any_call("db_query.cache_disabled", {})
    calls = [c.args for c in metrics.increment.call_args_list if c.args[0] == "db_query.calls"]
    # The retried call is tagged as a fallback; later calls run with the pilot off.
    assert [tags.get("query_cache") for _, tags in calls] == ["fallback", None, None]


@pytest.mark.parametrize(
    "message",
    [
        "Code: 115. DB::Exception: Unknown setting 'max_replication_threads'. (UNKNOWN_SETTING)",
        "Code: 452. DB::Exception: Setting readonly shouldn't be greater than 0."
        " (SETTING_CONSTRAINT_VIOLATION)",
        "Code: 452. DB::Exception: Setting max_block_size shouldn't be greater than 100",
        "Setting foo_bar is unknown or readonly",
        "Code: 164. DB::Exception: Cannot modify 'readonly' setting in readonly mode",
        # A cache setting named elsewhere in the message, not as the refused one.
        "Unknown setting 'max_threadz' in SELECT 1 SETTINGS use_query_cache_x = 1",
    ],
)
def test_other_setting_errors_do_not_disable_pilot(
    enabled, request_context, database, metrics, message
):
    database.client.query.side_effect = ProgrammingError(message)
    with pytest.raises(upstream.ToolError):
        server.run_select_query(VIEW_QUERIES[0], query_label=LLM)
    assert len(database.configs) == 1  # no uncached retry
    assert query_cache.query_cache_settings(LLM, VIEW_QUERIES[0]) == CACHE_SETTINGS
    assert all(c.args[0] != "db_query.cache_disabled" for c in metrics.increment.call_args_list)


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
        "SELECT 1 SETTINGS `use_query_cache` = 1",
    ],
)
def test_agent_query_cache_settings_rejected_when_on(
    enabled, request_context, database, metrics, query
):
    result = server.clickhouse_run_select_query.fn(query)
    assert "query_cache settings are managed by the server" in result["error_message"]
    assert database.configs == []
    metrics.increment.assert_any_call("db_query.errors", {"query_label": LLM, "success": "false"})


def test_query_cache_text_in_comment_or_literal_is_not_an_assignment(enabled, request_context):
    query = VIEW_QUERIES[0] + " -- SETTINGS use_query_cache = 0\n AND 'query_cache_ttl = 1' = ''"
    assert query_cache.query_cache_settings(LLM, query) == CACHE_SETTINGS


def test_agent_query_cache_settings_untouched_when_off(request_context, database):
    # Pilot off means pre-pilot behavior: the model's SETTINGS pass through.
    query = "SELECT 1 SETTINGS use_query_cache = 1"
    assert server.clickhouse_run_select_query.fn(query) == {"rows": [{"n": 42}]}


def test_enabled_preserves_row_cap(enabled, request_context, database):
    server.run_select_query(VIEW_QUERIES[1] + ";", query_label=LLM, max_rows=10)
    assert database.client.query.call_args.args == (f"SELECT * FROM ({VIEW_QUERIES[1]}) LIMIT 11",)
    assert _client_settings(database) == CACHE_SETTINGS


def test_cached_query_still_times_out(enabled, request_context, database, monkeypatch):
    monkeypatch.setattr(upstream, "get_mcp_config", lambda: SimpleNamespace(query_timeout=0.2))
    monkeypatch.setattr(upstream, "_cancel_query_with_bounded_wait", Mock())

    def slow_query(sql, settings):
        time.sleep(1)
        return SimpleNamespace(column_names=["n"], result_rows=[[42]])

    database.client.query.side_effect = slow_query
    with pytest.raises(upstream.ToolError, match="timed out after 0.2 seconds"):
        server.run_select_query(VIEW_QUERIES[0], query_label=LLM)
    assert _client_settings(database) == CACHE_SETTINGS
    assert request_context.get_state(upstream.CLIENT_CONFIG_OVERRIDES_KEY) is None


def test_concurrent_queries_sharing_a_request_do_not_leak_settings(
    enabled, request_context, database
):
    """Threads given the same request context (as get_study_guide does) must not
    see each other's temporary cache overrides."""
    release = threading.Event()
    started = threading.Event()

    def query(sql, settings):
        if "top_mutated" in sql:
            started.set()
            release.wait(5)
        return SimpleNamespace(column_names=["n"], result_rows=[[42]])

    database.client.query.side_effect = query
    import contextvars

    cached = threading.Thread(
        target=contextvars.copy_context().run,
        args=(server.run_select_query, VIEW_QUERIES[1]),
        kwargs={"query_label": LLM},
    )
    cached.start()
    assert started.wait(5)
    uncached = threading.Thread(
        target=contextvars.copy_context().run,
        args=(server.run_select_query, "SELECT 1"),
        kwargs={"query_label": "study_guide.counts"},
    )
    uncached.start()
    time.sleep(0.1)
    release.set()
    cached.join(5)
    uncached.join(5)
    assert [c.get("settings", {}) for c in database.configs] == [CACHE_SETTINGS, {}]


def test_study_guide_caches_only_top_genes(enabled, request_context, monkeypatch):
    seen = {}

    def fake_run_query(query, *, settings):
        overrides = query_cache._request_context()
        state = overrides and overrides.get_state(upstream.CLIENT_CONFIG_OVERRIDES_KEY)
        seen[query] = (settings, state)
        return '{"columns": ["cancer_study_identifier"], "rows": [["x"]]}', False

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
    reason=(
        "set CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL=http://user:pass@host:port (the MCP user, "
        "with the recommended readonly=2 profile) and optionally "
        "CBIOPORTAL_MCP_TEST_CLICKHOUSE_ADMIN_URL (defaults to the same URL) for query_log"
    ),
)
def test_real_clickhouse_second_query_hits_cache(monkeypatch, enabled, request_context):
    """Runs through mcp-clickhouse's real readonly handling (get_readonly_setting
    on a live client), so it fails if the profile/readonly combination refuses
    the cache settings: the pilot would then self-disable and log no hit."""
    from urllib.parse import urlparse

    import clickhouse_connect
    from mcp_clickhouse import mcp_env

    url = urlparse(os.environ["CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL"])
    admin_url = urlparse(
        os.getenv("CBIOPORTAL_MCP_TEST_CLICKHOUSE_ADMIN_URL")
        or os.environ["CBIOPORTAL_MCP_TEST_CLICKHOUSE_URL"]
    )
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
        host=admin_url.hostname,
        port=admin_url.port or 8123,
        username=admin_url.username or "default",
        password=admin_url.password or "",
        secure=admin_url.scheme == "https",
    )
    try:
        admin.command("SYSTEM FLUSH LOGS")
        rows = admin.query(
            "SELECT query_cache_usage, ProfileEvents['QueryCacheHits'], Settings['readonly'],"
            " Settings['use_query_cache'], user FROM system.query_log"
            " WHERE type = 'QueryFinish' AND query LIKE {pattern:String}"
            " AND query NOT LIKE '%system.query_log%' ORDER BY event_time_microseconds",
            parameters={"pattern": f"%{marker}%"},
        ).result_rows
    finally:
        admin.close()
    assert len(rows) == 2
    for _, _, readonly, use_query_cache, user in rows:
        # Upstream's readonly enforcement is in effect on the cached queries.
        assert readonly in ("1", "2") and use_query_cache == "1" and user == env["CLICKHOUSE_USER"]
    assert rows[0][0] == "Write"
    assert rows[1][0] == "Read" and rows[1][1] > 0
