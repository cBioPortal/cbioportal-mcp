"""Exercise the gate through real MCP dispatch, ahead of validation and caches."""

import json
import os
import subprocess
import sys

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from cbioportal_mcp import identity, rollout, server
from cbioportal_mcp.study_authz import availability, startup_check


@pytest.fixture
def degraded(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "on")
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "enforce")
    monkeypatch.setenv(rollout.PUBLIC_DB_CHECK_ENV, "enforce")
    monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", "test-bridge")
    monkeypatch.setenv(startup_check.PUBLIC_DATABASES_ENV, "")
    rollout.initialize("on", local_dev=False)

    def fail(*args, **kwargs):
        raise startup_check.PolicyCoverageError("policy coverage failed")

    monkeypatch.setattr(startup_check, "run_startup_check", fail)
    token = identity._CURRENT.set(
        identity.Decision(identity.Identity("test", "tester@example.org", "bridge"), True, True)
    )
    yield
    identity._CURRENT.reset(token)


@pytest.mark.asyncio
async def test_every_registered_tool_and_resource_is_gated(degraded, monkeypatch, caplog):
    tools = await server.mcp.get_tools()
    # Require an explicit classification when a tool is added, as well as
    # default-deny admission in product code.
    study_tools = {
        "clickhouse_run_select_query",
        "list_guides",
        "read_guide",
        "get_general_guide",
        "get_study_guide",
        "list_studies",
        "list_study_guides",
        "get_alteration_frequency",
        "get_top_altered_genes",
        "get_gene_frequency_by_cancer_type",
        "get_profiled_counts",
    }
    assert set(tools) == study_tools | {
        "clickhouse_list_tables",
        "clickhouse_list_table_columns",
        "search_oncotree",
    }
    resources = await server.mcp.get_resources()
    templates = await server.mcp.get_resource_templates()
    assert resources
    assert not templates  # Adding templates requires concrete URI coverage here.

    def touched(*args, **kwargs):
        pytest.fail("handler/cache/SQL ran before gate")

    # Even static files and warm caches must not be consulted.
    monkeypatch.setattr(server, "_load_resource", touched)
    monkeypatch.setattr(server, "_cache_get", touched)
    monkeypatch.setattr(server, "run_select_query", touched)
    async with Client(server.mcp) as client:
        arguments = {
            "clickhouse_run_select_query": {"query": "SELECT * FROM cancer_study"},
            "read_guide": {"uri": "cbioportal://mutation-frequency-guide"},
            "get_general_guide": {"name": "deployment"},
            "get_study_guide": {"study_id": "public_study"},
            "get_alteration_frequency": {"gene": "TP53", "study_id": "public_study"},
            "get_top_altered_genes": {"study_id": "public_study"},
            "get_gene_frequency_by_cancer_type": {"gene": "TP53"},
            "get_profiled_counts": {"study_id": "public_study"},
        }
        for name in sorted(study_tools):
            result = await client.call_tool(name, arguments.get(name, {}), raise_on_error=False)
            assert result.is_error, name
            assert result.structured_content is None, name
            assert "Study data unavailable: filtering_check_failed" in result.content[0].text
        for uri in resources:
            with pytest.raises(McpError, match="Study data unavailable: filtering_check_failed"):
                await client.read_resource(uri)

    refusals = [r.getMessage() for r in caplog.records if r.name == availability.logger.name]
    assert len(refusals) == len(study_tools) + len(resources)
    assert all("\n" not in line and "@" not in line for line in refusals)


@pytest.mark.asyncio
async def test_safe_tools_work_and_login_is_still_enforced(degraded, monkeypatch):
    import mcp_clickhouse.mcp_server as upstream

    server._clear_schema_cache()
    monkeypatch.setattr(
        upstream,
        "run_query",
        lambda query: json.dumps({"rows": [["cancer_study"]], "columns": ["name"]}),
    )
    async with Client(server.mcp) as client:
        for name, args in [
            ("clickhouse_list_tables", {}),
            ("clickhouse_list_table_columns", {"table": "cancer_study"}),
            ("search_oncotree", {"search_term": "breast"}),
        ]:
            result = await client.call_tool(name, args, raise_on_error=False)
            assert not result.is_error, name
        monkeypatch.setattr(
            identity, "request_decision", lambda: identity.Decision(None, False, True)
        )
        result = await client.call_tool(
            "search_oncotree", {"search_term": "breast"}, raise_on_error=False
        )
        assert result.is_error
        assert "Verified login required" in result.content[0].text


@pytest.mark.asyncio
async def test_pending_and_health_contract(degraded):
    # Before lifespan/checks, no data is admitted.
    assert availability.reason() == "checks_pending"
    with pytest.raises(ToolError, match="checks_pending"):
        availability.require()
    response = await server.study_data_health(None)
    assert response.status_code == 503
    assert json.loads(response.body) == {"study_data_available": False, "reason": "checks_pending"}
    server._validate_server_startup()
    response = await server.study_data_health(None)
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "study_data_available": False,
        "reason": "filtering_check_failed",
    }


def test_checks_only_run_once_and_preload_is_gated(degraded, monkeypatch, caplog):
    server._validate_server_startup()
    monkeypatch.setattr(startup_check, "run_startup_check", lambda *a: pytest.fail("checks reran"))
    server._validate_server_startup()
    server._clear_studies_cache()
    with pytest.raises(ToolError, match="filtering_check_failed"):
        server._refresh_studies_cache_once()
    assert server._studies_cache_rows is None
    lines = [r.getMessage() for r in caplog.records if "Study data unavailable" in r.getMessage()]
    assert len(lines) == 1 and "\n" not in lines[0]


@pytest.mark.asyncio
async def test_health_http_route_and_future_registrations(degraded):
    import httpx

    app = server.ConfiguredFastMCP(name="future")

    @app.tool()
    def new_study_tool() -> str:
        pytest.fail("new tool bypassed gate")

    @app.resource("cbioportal://future/{study}")
    def new_study_resource(study: str) -> str:
        pytest.fail("new resource template bypassed gate")

    asgi = app.http_app()
    async with asgi.router.lifespan_context(asgi):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi), base_url="http://test"
        ) as http:
            response = await http.get("/health")
            assert response.status_code == 200
            assert response.json() == {
                "study_data_available": False,
                "reason": "filtering_check_failed",
            }
        async with Client(app) as client:
            result = await client.call_tool("new_study_tool", {}, raise_on_error=False)
            assert result.is_error and result.structured_content is None
            with pytest.raises(McpError, match="Study data unavailable"):
                await client.read_resource("cbioportal://future/public-study")


def test_gate_remains_closed_during_check_and_off_bypasses(degraded, monkeypatch):
    def check(*args, **kwargs):
        with pytest.raises(ToolError, match="checks_pending"):
            availability.require()
        return startup_check.CheckResult("user", "role", False, 1, 0)

    monkeypatch.setattr(startup_check, "run_startup_check", check)
    server._validate_server_startup()
    assert availability.reason() == "ready"
    availability.require()
    rollout._reset_for_tests()
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    rollout.initialize("off", local_dev=False)
    availability.complete("filtering_check_failed")
    assert availability.reason() == "filtering_off"
    availability.require()


def test_safe_tool_allowlist_is_pinned():
    assert availability.SAFE_TOOLS == {
        "clickhouse_list_tables",
        "clickhouse_list_table_columns",
        "search_oncotree",
    }


@pytest.mark.parametrize("raw", [None, "off", "on"])
def test_fresh_import_is_blocked_before_and_after_pinning(raw):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLICKHOUSE_", "CBIOPORTAL_MCP_"))
    }
    if raw is not None:
        env["CBIOPORTAL_MCP_STUDY_FILTERING"] = raw
    env.update(
        {
            rollout.AUTH_ENFORCEMENT_ENV: "enforce",
            rollout.PUBLIC_DB_CHECK_ENV: "enforce",
            rollout.AUTH_TEST_USERS_ENV: "",
        }
    )
    script = """
from fastmcp.exceptions import ToolError
from cbioportal_mcp import rollout
from cbioportal_mcp.study_authz import availability
assert rollout._PIN is None
for pinned in (False, True):
    if pinned:
        rollout.initialize('on', local_dev=False)
    assert availability.reason() == 'checks_pending'
    assert not availability.available()
    try:
        availability.require()
    except ToolError as error:
        assert 'checks_pending' in str(error)
    else:
        raise AssertionError('fresh import served study data')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("raw", [None, "off", "on"])
def test_unpinned_filtering_never_reports_available(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("CBIOPORTAL_MCP_STUDY_FILTERING", raising=False)
    else:
        monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", raw)
    assert rollout._PIN is None
    availability.complete("ready")
    assert availability.reason() == "checks_pending"
    with pytest.raises(ToolError, match="checks_pending"):
        availability.require()


@pytest.mark.asyncio
async def test_preload_waits_for_startup_checks(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    app = server.ConfiguredFastMCP(name="preload-order")
    app.preload_studies = True
    events = []
    monkeypatch.setattr(server, "ensure_db_permissions", lambda config: events.append("checks"))

    def fetch():
        assert events == ["checks"]
        events.append("fetch")

    class InlineThread:
        def __init__(self, *, target, daemon):
            assert daemon
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    monkeypatch.setattr(server, "_fetch_and_store_studies", fetch)
    monkeypatch.setattr(server, "_refresh_studies_cache_forever", lambda: events.append("loop"))
    async with server._startup_lifespan(app):
        events.append("serving")
    assert events == ["checks", "fetch", "loop", "serving"]


def test_main_requests_preload(monkeypatch):
    monkeypatch.setattr(server.mcp, "preload_studies", False, raising=False)
    monkeypatch.setattr(server, "configure_telemetry", lambda: None)
    monkeypatch.setattr(server, "dogstatsd_metrics_configured", lambda: False)
    calls = []

    def run(**kwargs):
        assert server.mcp.preload_studies is True
        calls.append(kwargs)

    monkeypatch.setattr(server.mcp, "run", run)
    server.main()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_login_precedes_study_gate(degraded, monkeypatch, caplog):
    assert isinstance(server.mcp.middleware[0], identity.IdentityMiddleware)
    assert isinstance(server.mcp.middleware[1], availability.StudyDataMiddleware)
    async with Client(server.mcp) as client:
        caplog.clear()
        monkeypatch.setattr(
            identity, "request_decision", lambda: identity.Decision(None, False, True)
        )
        result = await client.call_tool("list_studies", {}, raise_on_error=False)
        assert result.is_error and result.structured_content is None
        assert "Verified login required" in result.content[0].text
        assert not [r for r in caplog.records if r.name == availability.logger.name]


@pytest.mark.asyncio
async def test_none_check_result_keeps_server_degraded(degraded, monkeypatch):
    monkeypatch.setattr(startup_check, "run_startup_check", lambda *args: None)
    async with Client(server.mcp) as client:
        assert availability.reason() == "filtering_check_failed"
        result = await client.call_tool("list_studies", {}, raise_on_error=False)
        assert result.is_error and result.structured_content is None
        assert "Study data unavailable" in result.content[0].text


@pytest.mark.asyncio
async def test_prompts_require_explicit_classification():
    assert await server.mcp.get_prompts() == {}


def test_custom_http_routes_require_explicit_classification():
    routes = server.mcp._get_additional_http_routes()
    assert [(route.path, route.methods, route.endpoint) for route in routes] == [
        ("/health", {"GET", "HEAD"}, server.study_data_health)
    ]


@pytest.mark.asyncio
async def test_future_prompt_is_gated(degraded):
    app = server.ConfiguredFastMCP(name="future-prompt")

    @app.prompt()
    def new_study_prompt() -> str:
        return "study data must not be served"

    async with Client(app) as client:
        with pytest.raises(McpError, match="Study data unavailable: filtering_check_failed"):
            await client.get_prompt("new_study_prompt")
