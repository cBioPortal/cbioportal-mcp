"""P7 request settings, thread isolation and fail-closed user surfaces."""

import asyncio
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from clickhouse_connect import common

from cbioportal_mcp import identity, server
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
