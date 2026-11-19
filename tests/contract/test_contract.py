"""MCP contract tier: the published image, as chat.cbioportal.org's LibreChat sees it.

Opt-in. Skipped unless CBIOPORTAL_MCP_CONTRACT_URL points at a running server,
so a plain `pytest` stays hermetic. scripts/contract/run.sh builds the image,
starts ClickHouse with the fixture schema/data and sql/ applied, runs the
image with prod's chat env, and runs this module; see docs/contract-tests.md.

Each test guards a way a promotion has broken (or could break) the chat:

  * the container exits at startup (verify_query_pool / ensure_db_permissions
    sys.exit(2));
  * the endpoint moves or redirects (FastMCP's trailing-slash 307 -> 406 -> 404);
  * a tool is renamed/removed or its input schema changes under LibreChat;
  * a tool starts returning error_message, or a different result shape;
  * a sql/ view is not queryable by the read-only app_user (SQL SECURITY);
  * the guide resources stop loading.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest

from .mcp_http import LIBRECHAT_HEADERS, McpHttpError, McpSession, tool_payload

URL = os.getenv("CBIOPORTAL_MCP_CONTRACT_URL")
CONTAINER = os.getenv("CBIOPORTAL_MCP_CONTRACT_CONTAINER")
pytestmark = pytest.mark.skipif(not URL, reason="CBIOPORTAL_MCP_CONTRACT_URL not set")

REPO = Path(__file__).resolve().parents[2]
SQL_DIR = REPO / "sql"
SNAPSHOT = Path(__file__).parent / "fixtures" / "tools_snapshot.json"
STARTUP_GRACE_SECONDS = 30

# Fixture identifiers (tests/contract/fixtures/seed.py).
WES_STUDY = "brca_tcga_pan_can_atlas_2018"
PANEL_STUDY = "msk_chord_2024"
NO_GUIDE_STUDY = "brca_contract_fixture_2025"  # get_study_guide queries the DB for it

# One valid call per tool, and the top-level keys its result must have in the
# default (compact) result format. A tool missing here fails
# test_every_tool_has_a_contract_call, so new tools get a contract too.
#   dict -> required keys of the returned object
#   list -> the tool returns a non-empty list (of dicts with these keys)
#   str  -> the tool returns text containing this substring
TOOL_CALLS: dict[str, tuple[dict, object]] = {
    "clickhouse_run_select_query": (
        {
            "query": "SELECT count() AS n FROM sample_derived "
            f"WHERE cancer_study_identifier = '{WES_STUDY}'"
        },
        {"columns", "rows", "row_count"},
    ),
    "clickhouse_list_tables": ({}, {"tables"}),
    "clickhouse_list_table_columns": ({"table": "genomic_event_derived"}, {"fields", "columns"}),
    "list_guides": ({}, ["uri", "description"]),
    "read_guide": ({"uri": "cbioportal://mutation-frequency-guide"}, "Mutation"),
    "get_general_guide": ({"name": "contract-nonexistent"}, "not found"),
    "get_study_guide": ({"study_id": NO_GUIDE_STUDY}, "## Cohort Statistics"),
    "list_studies": (
        {"search": "TCGA"},
        ["cancer_study_identifier", "name", "sample_count", "url"],
    ),
    "list_study_guides": ({}, []),
    "search_oncotree": ({"search_term": "LUAD"}, ["code", "name", "score"]),
    "get_alteration_frequency": (
        {"gene": "TP53", "study_id": WES_STUDY},
        {"gene", "study_id", "columns", "rows", "provenance", "source"},
    ),
    "get_top_altered_genes": (
        {"study_id": PANEL_STUDY, "alteration_type": "mutation", "top_n": 5},
        {"study_id", "alteration_type", "columns", "rows", "provenance", "source"},
    ),
    "get_gene_frequency_by_cancer_type": (
        {"gene": "TP53", "alteration_type": "mutation", "preference": "pan_cancer_tcga"},
        {"gene", "alteration_type", "preference", "columns", "rows", "provenance", "source"},
    ),
    "get_profiled_counts": ({"study_id": PANEL_STUDY}, {"study_id", "columns", "rows"}),
}

# Values for every {name:Type} parameter used by a sql/ view.
VIEW_PARAMS = {
    "study": f"'{WES_STUDY}'",
    "studies": f"['{WES_STUDY}', '{PANEL_STUDY}']",
    "gene": "'TP53'",
    "gene_a": "'TP53'",
    "gene_b": "'KRAS'",
    "preference": "'pan_cancer_tcga'",
    "alteration": "'mutation'",
    "top_n": "5",
    "profile_type": "'rna_seq_v2_mrna'",
    "attribute": "'SAMPLE_TYPE'",
}
# Views whose fixture rows only exist in the panel/treatment study.
VIEW_STUDY_OVERRIDE = {
    "treatment_counts_in_study": PANEL_STUDY,
    "treatment_regimens_in_study": PANEL_STUDY,
}

GUIDE_RESOURCES = {
    "cbioportal://mutation-frequency-guide",
    "cbioportal://clinical-data-guide",
    "cbioportal://sample-filtering-guide",
    "cbioportal://common-pitfalls",
    "cbioportal://treatment-guide",
    "cbioportal://faq-guide",
    "cbioportal://statistical-tests-guide",
    "cbioportal://gene-expression-guide",
    "cbioportal://external-resources-guide",
    "cbioportal://gene-resolution-guide",
    "cbioportal://study-resolution-guide",
    "cbioportal://germline-guide",
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session():
    s = McpSession(URL)
    try:
        s.initialize()
        yield s
    finally:
        s.close()


@pytest.fixture(scope="module")
def tools(session):
    return session.list_all("tools/list", "tools")


def _docker_logs() -> str:
    if not CONTAINER:
        return ""
    done = subprocess.run(
        ["docker", "logs", "--tail", "60", CONTAINER], capture_output=True, text=True
    )
    return done.stdout + done.stderr


# ---------------------------------------------------------------------------
# 1. the container stays up
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CONTAINER, reason="CBIOPORTAL_MCP_CONTRACT_CONTAINER not set")
def test_container_still_running_after_startup():
    """Startup gates (verify_query_pool, ensure_db_permissions) sys.exit(2) on
    a bad config; Kubernetes then crash-loops the chat MCP pod."""
    started = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", CONTAINER],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    started_at = datetime.fromisoformat(
        re.sub(r"(\.\d{6})\d*", r"\1", started.replace("Z", "+00:00"))
    )
    remaining = STARTUP_GRACE_SECONDS - (datetime.now(timezone.utc) - started_at).total_seconds()
    if remaining > 0:
        time.sleep(remaining)
    state = json.loads(
        subprocess.run(
            ["docker", "inspect", "-f", "{{json .State}}", CONTAINER],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert state["Running"] and not state["Restarting"], (
        f"MCP container exited (code {state['ExitCode']}) within "
        f"{STARTUP_GRACE_SECONDS}s of starting. Last logs:\n{_docker_logs()}"
    )


# ---------------------------------------------------------------------------
# 2. transport: exactly /db/mcp, LibreChat headers, no redirects
# ---------------------------------------------------------------------------


def test_initialize_and_tools_list_at_exact_path_without_redirects():
    s = McpSession(URL)
    try:
        info = s.initialize()
        assert info["serverInfo"]["name"] == "cBioPortal MCP Server"
        assert "tools" in info["capabilities"]
        assert s.session_id, "no mcp-session-id header on initialize"
        assert s.list_all("tools/list", "tools")
    except McpHttpError as e:
        pytest.fail(f"{e}\n\nServer logs:\n{_docker_logs()}")
    finally:
        s.close()
    statuses = [(x.method, x.status, x.location) for x in s.exchanges]
    assert all(200 <= st < 300 for _, st, _ in statuses), statuses
    assert all(loc is None for _, _, loc in statuses), statuses


def test_trailing_slash_path_is_not_a_redirect_loop():
    """LibreChat is configured with .../db/mcp (no slash). /db/mcp/ must not be
    where the server actually lives: answering it with a 3xx back to itself (or
    from /db/mcp to /db/mcp/) is the 307 -> 406 -> 404 loop that broke chat.
    Documented behaviour: /db/mcp/ answers with a single 307 to exactly
    /db/mcp (a 404 or 200 would also be acceptable); it must never redirect
    anywhere else, in particular not to /db/mcp/ itself."""
    import httpx

    slash = URL.rstrip("/") + "/"
    resp = httpx.post(
        slash,
        headers={
            **LIBRECHAT_HEADERS,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "contract", "version": "1"},
            },
        },
        follow_redirects=False,
        timeout=30,
    )
    if 300 <= resp.status_code < 400:
        # Current FastMCP behaviour: 307 to exactly the configured path.
        location = urlparse(resp.headers.get("location", ""))
        assert (
            location.path == urlparse(URL).path
        ), f"{slash} redirects to {resp.headers.get('location')!r}, not to {urlparse(URL).path}"
    else:
        assert resp.status_code in (200, 404), (resp.status_code, resp.text[:300])


# ---------------------------------------------------------------------------
# 3. tools/list matches the committed snapshot
# ---------------------------------------------------------------------------


def _snapshot_of(tools: list[dict]) -> dict:
    return {
        t["name"]: {"inputSchema": t["inputSchema"]} for t in sorted(tools, key=lambda t: t["name"])
    }


def test_tools_match_snapshot(tools):
    current = _snapshot_of(tools)
    if os.getenv("UPDATE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"snapshot rewritten: {SNAPSHOT.relative_to(REPO)}")
    expected = json.loads(SNAPSHOT.read_text())
    if current == expected:
        return
    added = sorted(set(current) - set(expected))
    removed = sorted(set(expected) - set(current))
    changed = sorted(n for n in set(current) & set(expected) if current[n] != expected[n])
    lines = ["tools/list no longer matches tests/contract/fixtures/tools_snapshot.json."]
    if added:
        lines.append(f"  added tools:   {added}")
    if removed:
        lines.append(
            f"  REMOVED tools: {removed}  (LibreChat agents configured with these will break)"
        )
    for name in changed:
        lines.append(f"  changed input schema: {name}")
        lines.append(f"    was: {json.dumps(expected[name], sort_keys=True)}")
        lines.append(f"    now: {json.dumps(current[name], sort_keys=True)}")
    lines.append(
        "If the change is intended, regenerate the snapshot and commit it:\n"
        "  UPDATE_SNAPSHOT=1 scripts/contract/run.sh -k snapshot"
    )
    pytest.fail("\n".join(lines))


# ---------------------------------------------------------------------------
# 4. every tool answers a valid call without error_message
# ---------------------------------------------------------------------------


def test_every_tool_has_a_contract_call(tools):
    names = {t["name"] for t in tools}
    assert names == set(TOOL_CALLS), (
        f"tools without a contract call: {sorted(names - set(TOOL_CALLS))}; "
        f"contract calls for tools that no longer exist: {sorted(set(TOOL_CALLS) - names)}. "
        "Update TOOL_CALLS in tests/contract/test_contract.py."
    )


def _assert_no_error(name, payload):
    if isinstance(payload, dict):
        assert "error_message" not in payload, f"{name}: {payload['error_message']}"
        assert "error" not in payload, f"{name}: {payload['error']}"
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                assert "error" not in item and "error_message" not in item, f"{name}: {item}"
    if isinstance(payload, str):
        assert not payload.startswith("Error"), f"{name}: {payload[:300]}"


@pytest.mark.parametrize("name", sorted(TOOL_CALLS))
def test_tool_call_succeeds_with_expected_shape(session, name):
    args, expected = TOOL_CALLS[name]
    result = session.call_tool(name, args)
    payload = tool_payload(result)
    assert not result.get("isError"), f"{name} isError: {result.get('content')}"
    _assert_no_error(name, payload)
    if isinstance(expected, set):
        assert isinstance(
            payload, dict
        ), f"{name} returned {type(payload).__name__}: {payload!r:.300}"
        missing = expected - set(payload)
        assert not missing, f"{name} result lacks {sorted(missing)}; got keys {sorted(payload)}"
    elif isinstance(expected, list):
        assert isinstance(
            payload, list
        ), f"{name} returned {type(payload).__name__}: {payload!r:.300}"
        if expected:
            assert payload, f"{name} returned an empty list"
            missing = set(expected) - set(payload[0])
            assert not missing, f"{name} items lack {sorted(missing)}; got {sorted(payload[0])}"
    else:
        assert isinstance(payload, str), f"{name} returned {type(payload).__name__}"
        assert expected in payload, f"{name}: {expected!r} not in {payload[:300]!r}"


def test_static_study_guide_loads(session):
    payload = tool_payload(session.call_tool("get_study_guide", {"study_id": PANEL_STUDY}))
    assert isinstance(payload, str) and len(payload) > 500, payload[:300]
    assert not payload.startswith("Error"), payload[:300]


def test_frequency_tools_return_fixture_rows(session):
    """Guards against a silently empty result (e.g. a view or permission
    regression that turns every count into zero rows)."""
    for name in (
        "get_alteration_frequency",
        "get_top_altered_genes",
        "get_gene_frequency_by_cancer_type",
    ):
        payload = tool_payload(session.call_tool(name, TOOL_CALLS[name][0]))
        assert payload["rows"], f"{name} returned no rows on the fixture: {payload}"


# ---------------------------------------------------------------------------
# 5. every sql/ view is queryable by the read-only app_user
# ---------------------------------------------------------------------------

_VIEW_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)(.*?);", re.IGNORECASE | re.DOTALL)
_PARAM_RE = re.compile(r"\{(\w+):[^}]+\}")


def _sql_views() -> dict[str, list[str]]:
    views = {}
    for path in sorted(SQL_DIR.rglob("*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        for name, body in _VIEW_RE.findall(text):
            views[name] = sorted(set(_PARAM_RE.findall(body)))
    return views


def test_sql_dir_defines_views():
    assert len(_sql_views()) >= 10, _sql_views()


@pytest.mark.parametrize("view", sorted(_sql_views()))
def test_sql_view_queryable_as_app_user(session, view):
    params = _sql_views()[view]
    unknown = [p for p in params if p not in VIEW_PARAMS]
    assert not unknown, f"add fixture values for {unknown} to VIEW_PARAMS"
    values = {p: VIEW_PARAMS[p] for p in params}
    if view in VIEW_STUDY_OVERRIDE:
        values["study"] = f"'{VIEW_STUDY_OVERRIDE[view]}'"
    source = f"{view}({', '.join(f'{k}={v}' for k, v in values.items())})" if params else view
    query = f"SELECT * FROM {source} LIMIT 5"
    payload = tool_payload(session.call_tool("clickhouse_run_select_query", {"query": query}))
    assert "error_message" not in payload, f"{query}\n-> {payload['error_message']}"
    assert payload["rows"], f"{query} returned no rows on the fixture"


# ---------------------------------------------------------------------------
# 6. resources
# ---------------------------------------------------------------------------


def test_resources_list_and_guides_load(session):
    resources = {r["uri"] for r in session.list_all("resources/list", "resources")}
    missing = GUIDE_RESOURCES - resources
    assert not missing, f"resources/list lacks {sorted(missing)}"
    for uri in sorted(GUIDE_RESOURCES):
        contents = session.request("resources/read", {"uri": uri})["contents"]
        text = "".join(c.get("text", "") for c in contents)
        assert len(text) > 200, f"{uri} loaded {len(text)} chars"
