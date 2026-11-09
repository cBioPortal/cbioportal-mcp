"""P7 request settings, thread isolation and fail-closed user surfaces."""

import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from clickhouse_connect import common

from cbioportal_mcp import domain_tools, identity, rollout, server
from cbioportal_mcp.study_authz import request


@contextmanager
def caller(email):
    token = identity._CURRENT.set(
        identity.Decision(identity.Identity("id", email, "oauth"), True, True)
    )
    try:
        yield
    finally:
        identity._CURRENT.reset(token)


@pytest.fixture
def filtering(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "on")
    previous = common.get_setting("invalid_setting_action")
    yield
    common.set_setting("invalid_setting_action", previous)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT getSetting('x')",
        "SELECT GETSETTINGORDEFAULT('x', '')",
        "SELECT `SQL_cbio_user`",
        'SELECT "SQL_cbio_user"',
        r"SELECT 'SQL_\x63bio_user'",
        r"SELECT `SQL_cbio_\user`",
        "SELECT 1 SETTINGS `SQL_other`='x'",
        "SELECT 1 SETTINGS SQL_other='x'",
        "SELECT 'SQL_''cbio_user'",
        "SELECT name, value FROM system.settings WHERE name LIKE 'S%cbio%'",
        "SELECT * FROM system.settings WHERE is_custom",
        "SELECT * FROM system.settings_changes",
        "SELECT * FROM `system`.`settings`",
        'SELECT * FROM "system" . "settings_changes"',
        "SELECT * FROM system /* comment */ . /* comment */ settings",
        "SELECT * FROM system -- comment\n . settings_changes",
        "SELECT * FROM system # comment\n . settings",
        "SELECT * FROM system /* nested /* comment */ end */ . settings",
        r"SELECT * FROM `system`.`s\x65ttings`",
        'SELECT * FROM "system.settings"',
        "SELECT 1 SETTINGS /* comment */ `SQL_custom` = 'x'",
        "SELECT 1 SETTINGS max_threads = 1, SQL_custom = 'x'",
        r"SELECT 1 SETTINGS `SQL_\x63ustom` = 'x'",
        "SELECT 1 SETTINGS 'SQL_custom' = 'x'",
    ],
)
def test_guard(filtering, monkeypatch, sql):
    import mcp_clickhouse.mcp_server as upstream

    monkeypatch.setattr(upstream, "run_query", lambda _: pytest.fail("executed guarded SQL"))
    with caller("alice@example.org"):
        assert (
            "identity settings"
            in server.clickhouse_run_select_query.fn.__wrapped__(sql)["error_message"]
        )


def test_no_identity_and_off(monkeypatch, filtering):
    import mcp_clickhouse.mcp_server as upstream

    monkeypatch.setattr(
        upstream, "run_query", lambda _: json.dumps({"columns": ["n"], "rows": [[1]]})
    )
    assert (
        "verified user identity"
        in server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1")["error_message"]
    )
    assert "verified user identity" in server.list_studies.fn.__wrapped__()[0]["error"]
    with pytest.raises(RuntimeError, match="verified user identity"):
        server._study_guide_text("private")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    assert server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1") == {
        "columns": ["n"],
        "rows": [[1]],
        "row_count": 1,
    }


@pytest.mark.asyncio
async def test_concurrent_requests_real_pool(filtering, monkeypatch):
    import mcp_clickhouse.mcp_server as upstream

    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")

    barrier = threading.Barrier(2)
    seen = []

    class Client:
        server_settings = {}

        def query(self, query, settings):
            if "user_study" not in query:
                barrier.wait(timeout=10)
            assert common.get_setting("invalid_setting_action") == "send"
            assert settings["readonly"] == 1
            seen.append(settings["SQL_cbio_user"])
            return SimpleNamespace(
                column_names=["email"], result_rows=[[settings["SQL_cbio_user"]]]
            )

    entry = SimpleNamespace(client=Client())
    monkeypatch.setattr(upstream, "_acquire_clickhouse_client", lambda _: entry)
    monkeypatch.setattr(upstream, "_release_client_entry", lambda _: None)
    monkeypatch.setattr(upstream, "_resolve_client_config", lambda: {})

    async def run(email):
        with caller(email):
            middleware = identity.IdentityMiddleware(SimpleNamespace(startup_transport="http"))
            return await middleware.on_message(
                None, lambda _: server.clickhouse_run_select_query.fn("SELECT 1")
            )

    results = await asyncio.gather(run(" Alice@Example.org "), run("bob@example.org"))
    assert [r["rows"][0][0] for r in results] == ["alice@example.org", "bob@example.org"]
    assert sorted(seen) == ["alice@example.org"] * 2 + ["bob@example.org"] * 2
    assert request._USER_QUERY.get() is None
    assert upstream.QUERY_EXECUTOR.submit(request._USER_QUERY.get).result() is None


def test_caches_bypassed_and_scope(filtering, monkeypatch):
    seen = []

    def query(sql, **kwargs):
        seen.append((request.email(), sql))
        if "user_study" in sql:
            return []
        if "SELECT 1" in sql:
            return []  # revoked, including a static guide
        return [{"cancer_study_identifier": request.email(), "name": "test"}]

    monkeypatch.setattr(server, "run_select_query", query)
    monkeypatch.setattr(server, "_all_studies_query", lambda: pytest.fail("global cache"))
    monkeypatch.setattr(server, "_load_study_guide", lambda _: pytest.fail("revoked static guide"))
    monkeypatch.setattr(server, "_fetch_and_store_studies", lambda: pytest.fail("startup warming"))
    for email in ("alice@example.org", "bob@example.org", "alice@example.org"):
        with caller(email):
            rows = server.list_studies.fn.__wrapped__(search="test")
            assert rows[0]["cancer_study_identifier"] == email
            assert rows[-1] == {"note": request.PUBLIC_ONLY_NOTE}
            assert "unavailable" in server._study_guide_text("brca_tcga_pan_can_atlas_2018")
    from cbioportal_mcp.study_authz import availability

    availability.complete("ready")
    server._refresh_studies_cache_once()
    assert len(seen) == 9
    assert "WHERE" in seen[0][1] and "LIMIT 20" in seen[0][1]


def test_logs_redact_email(filtering, caplog):
    with caplog.at_level("DEBUG", logger="cbioportal_mcp.server"):
        server.logger.error("Database error for %s", "alice@example.org")
        server.logger.error("Request /?SQL_cbio_user=alice%%40example.org")
        try:
            raise RuntimeError("alice@example.org")
        except RuntimeError:
            server.logger.exception("Request failed")
    assert "alice@example.org" not in caplog.text
    assert "alice%40example.org" not in caplog.text


def test_off_settings_unchanged_and_schema_exempt(filtering, monkeypatch):
    import mcp_clickhouse.mcp_server as upstream

    monkeypatch.setenv("CLICKHOUSE_HOST", "localhost")
    monkeypatch.setenv("CLICKHOUSE_USER", "test")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "test")

    client = SimpleNamespace(server_settings={})
    # Schema queries never enter the user-query scope, even with filtering on.
    assert "SQL_cbio_user" not in upstream.build_query_settings(client)
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    with caller("alice@example.org"), request.user_query():
        assert "SQL_cbio_user" not in upstream.build_query_settings(client)


def test_send_configured_before_client_creation():
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from clickhouse_connect import common; "
                "common.set_setting('invalid_setting_action', 'drop'); "
                "import cbioportal_mcp; "
                "assert common.get_setting('invalid_setting_action') == 'send'"
            ),
        ],
        env={**os.environ, "CBIOPORTAL_MCP_STUDY_FILTERING": "on"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_cached_guide_is_rechecked_on_revocation(filtering, monkeypatch):
    visible = True
    monkeypatch.setattr(server, "_load_study_guide", lambda _: "curated guide")
    monkeypatch.setattr(server, "run_select_query", lambda *a, **kw: [{"n": 1}] if visible else [])
    server._study_guide_cache.put("private_study", "stale dynamic guide")
    try:
        with caller("alice@example.org"):
            assert server._study_guide_text("private_study") == "curated guide"
            visible = False
            assert "unavailable" in server._study_guide_text("private_study")
        with caller("bob@example.org"):
            assert "unavailable" in server._study_guide_text("private_study")
    finally:
        server._clear_study_guide_cache()


@pytest.mark.parametrize(
    "sql",
    [
        "WITH 'mutation_settings' AS a SELECT 'sql_x'",
        "SELECT 'SETTINGS SQL_custom = 1'",
        "SELECT '/* SETTINGS */', 'sql_x'",
        "SELECT 'it\\'s SETTINGS', 'sql_x'",
        "SELECT 'it''s SETTINGS', 'sql_x'",
        "SELECT sql_x FROM mutation_settings",
        "SELECT * FROM system.columns",
        "SELECT 1 SETTINGS max_threads = 1",
        "SELECT 1 /* SETTINGS SQL_custom = 1 */",
    ],
)
def test_guard_allows_harmless_sql(sql):
    request.guard_sql(sql)


@pytest.mark.parametrize("filtering_mode", ["off", "on"])
def test_filtering_follows_immutable_startup_pin(monkeypatch, filtering_mode):
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_ENFORCEMENT", "enforce")
    monkeypatch.setenv("CBIOPORTAL_MCP_PUBLIC_DB_CHECK", "enforce")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", filtering_mode)
    settings = rollout.initialize(filtering_mode, local_dev=True)
    pin = rollout._PIN
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off" if filtering_mode == "on" else "on")
    assert request.enabled() == (filtering_mode == "on")
    assert rollout.get() is settings and rollout._PIN is pin


@pytest.mark.parametrize("format_name", ["compact", "rows"])
@pytest.mark.parametrize("max_rows", [1, 3])
def test_scope_check_failure_keeps_query_and_retries(
    filtering, monkeypatch, caplog, format_name, max_rows
):
    monkeypatch.setenv("CBIOPORTAL_MCP_RESULT_FORMAT", format_name)
    scope_checks = iter([RuntimeError("database exception alice@example.org"), [], [{"n": 1}]])

    def query(sql, **kwargs):
        if kwargs["query_label"] == "study_scope.private_grants":
            outcome = next(scope_checks)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return [{"n": 1}, {"n": 2}]

    monkeypatch.setattr(server, "run_select_query", query)
    tool = server.clickhouse_run_select_query.fn.__wrapped__
    with caller("alice@example.org"):
        first = tool("SELECT 1", max_rows=max_rows)
        expected = [[1], [2]] if format_name == "compact" else [{"n": 1}, {"n": 2}]
        assert first["rows"] == expected[:max_rows]
        assert "could not be determined" in first["study_scope_note"]
        assert tool("SELECT 1")["study_scope_note"] == request.PUBLIC_ONLY_NOTE
        assert "study_scope_note" not in tool("SELECT 1")
    assert "Could not determine public-only study status" in caplog.text
    assert "alice@example.org" not in caplog.text


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("get_alteration_frequency", ("TP53", "study_a")),
        ("get_top_altered_genes", ("study_a",)),
        ("get_gene_frequency_by_cancer_type", ("TP53",)),
        ("get_profiled_counts", ("study_a",)),
    ],
)
@pytest.mark.parametrize("format_name", ["compact", "rows"])
@pytest.mark.parametrize("scope", ["public", "private", "failed", "off"])
def test_domain_scope_metadata(monkeypatch, caplog, tool_name, args, format_name, scope):
    monkeypatch.setenv("CBIOPORTAL_MCP_RESULT_FORMAT", format_name)
    checks = []

    def query(sql, **kwargs):
        if kwargs["query_label"] == "study_scope.private_grants":
            checks.append(True)
            if scope == "failed":
                raise RuntimeError("alice@example.org: scope lookup failed")
            return [{"n": 1}] if scope == "private" else []
        return [
            {
                "matched_studies": ["study_a"],
                "matched_genes": ["TP53"],
                "altered_any": 2,
                "profiled_ANY": 10,
                "hugo_gene_symbol": "TP53",
                "altered_samples": 2,
                "profiled_samples": 10,
                "profile_type": "ALL_SAMPLES",
                "samples": 10,
                "patients": 10,
            }
        ]

    monkeypatch.setattr(server, "run_select_query", query)
    tool = getattr(domain_tools, tool_name).fn.__wrapped__
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    baseline = tool(*args)
    assert not checks
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off" if scope == "off" else "on")
    result = tool(*args)
    if scope == "public":
        assert result.pop("study_scope_note") == request.PUBLIC_ONLY_NOTE
    elif scope == "failed":
        assert "could not be determined" in result.pop("study_scope_note")
        assert "alice@example.org" not in caplog.text
    assert json.dumps(result) == json.dumps(baseline)
    assert len(checks) == (0 if scope == "off" else 1)


def test_scope_check_failure_keeps_study_list(filtering, monkeypatch):
    def query(sql, **kwargs):
        if kwargs["query_label"] == "study_scope.private_grants":
            raise RuntimeError("scope lookup failed")
        return [{"cancer_study_identifier": "study_a", "name": "Study A"}]

    monkeypatch.setattr(server, "run_select_query", query)
    with caller("alice@example.org"):
        result = server.list_studies.fn.__wrapped__()
    assert result[0]["cancer_study_identifier"] == "study_a"
    assert "could not be determined" in result[-1]["note"]
