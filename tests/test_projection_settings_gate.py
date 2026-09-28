"""Unit tests for the startup gate that keeps projections from changing results.

The live behavior (a real server refusing or accepting each user profile) is
covered by tests/test_projection_parity_live.py and the PR notes; these tests
pin the gate's decision logic against a fake run_query.
"""

import json
import types

import pytest
from fastmcp.exceptions import ToolError

from cbioportal_mcp.authentication import permissions

CONFIG = types.SimpleNamespace(mcp_user="llm_user", mcp_database="db")

# current_db value that makes the fake's SELECT currentDatabase() raise.
QUERY_FAILS = object()


def _fake_run_query(
    *,
    has_projections=1,
    value=True,
    override_refused=False,
    tables_error=False,
    current_db="db",
):
    calls = []

    def run_query(query):
        calls.append(query)
        if query == "SELECT currentDatabase()":
            if current_db is QUERY_FAILS:
                raise ToolError("connection refused")
            return json.dumps({"columns": ["currentDatabase()"], "rows": [[current_db]]})
        if "system.tables" in query:
            if tables_error:
                raise ToolError("Not enough privileges")
            return json.dumps({"columns": ["count()"], "rows": [[has_projections]]})
        if "getSetting" in query and "SETTINGS" in query:
            if override_refused:
                raise ToolError("Cannot modify setting in readonly mode")
            return json.dumps({"columns": ["v"], "rows": [[True]]})
        if "getSetting" in query:
            return json.dumps({"columns": ["v"], "rows": [[value]]})
        raise AssertionError(f"unexpected query {query}")

    run_query.calls = calls
    return run_query


def test_safe_settings_disable_implicit_projections():
    assert permissions.PROJECTION_SAFE_SETTINGS == {"optimize_use_implicit_projections": "0"}


def test_no_projections_skips_settings_checks(monkeypatch):
    fake = _fake_run_query(has_projections=0)
    monkeypatch.setattr(permissions, "run_query", fake)
    permissions.ensure_projection_safe_settings(CONFIG)
    assert fake.calls == [
        "SELECT currentDatabase()",
        "SELECT count() FROM system.tables "
        "WHERE database = 'db' AND create_table_query LIKE '%PROJECTION%'",
    ]


@pytest.mark.parametrize("value", [True, 1, "1", "true"])
def test_default_implicit_projections_refuse_start(monkeypatch, value):
    monkeypatch.setattr(permissions, "run_query", _fake_run_query(value=value))
    with pytest.raises(PermissionError) as exc:
        permissions.ensure_projection_safe_settings(CONFIG)
    message = str(exc.value)
    assert "must be 0" in message
    assert "ALTER USER llm_user SETTINGS optimize_use_implicit_projections = 0 CONST" in message


def test_pinned_value_that_a_query_can_override_refuses_start(monkeypatch):
    monkeypatch.setattr(
        permissions, "run_query", _fake_run_query(value=False, override_refused=False)
    )
    with pytest.raises(PermissionError, match="can be changed by a query"):
        permissions.ensure_projection_safe_settings(CONFIG)


@pytest.mark.parametrize("value", [False, 0, "0", "false"])
def test_pinned_and_locked_settings_pass(monkeypatch, value):
    fake = _fake_run_query(value=value, override_refused=True)
    monkeypatch.setattr(permissions, "run_query", fake)
    permissions.ensure_projection_safe_settings(CONFIG)
    assert any("SETTINGS optimize_use_implicit_projections = 1" in q for q in fake.calls)


def test_unreadable_system_tables_fails_closed(monkeypatch):
    monkeypatch.setattr(permissions, "run_query", _fake_run_query(tables_error=True, value=True))
    with pytest.raises(PermissionError):
        permissions.ensure_projection_safe_settings(CONFIG)


def test_startup_gate_runs_projection_check(monkeypatch):
    monkeypatch.setattr(permissions, "_check_grant", lambda priv, scope: priv == "SELECT")
    monkeypatch.setattr(permissions, "run_query", _fake_run_query(value=True))
    with pytest.raises(PermissionError, match="projections"):
        permissions.ensure_db_permissions(CONFIG)


def test_projection_check_uses_the_connections_database(monkeypatch, caplog):
    """CLICKHOUSE_DATABASE unset: config says one DB, the connection uses another.

    Agent SQL runs on the connection's database, so that's the one inspected,
    and the mismatch is logged loudly.
    """
    fake = _fake_run_query(current_db="default", value=True)
    monkeypatch.setattr(permissions, "run_query", fake)
    with caplog.at_level("WARNING"), pytest.raises(PermissionError, match="database 'default'"):
        permissions.ensure_projection_safe_settings(CONFIG)
    tables_query = next(q for q in fake.calls if "system.tables" in q)
    assert "database = 'default'" in tables_query
    assert "'db'" not in tables_query
    assert "current database is 'default' but the MCP is configured for 'db'" in caplog.text


def test_matching_database_logs_no_mismatch(monkeypatch, caplog):
    monkeypatch.setattr(permissions, "run_query", _fake_run_query(has_projections=0))
    with caplog.at_level("WARNING"):
        permissions.ensure_projection_safe_settings(CONFIG)
    assert "configured for" not in caplog.text


@pytest.mark.parametrize(
    "current_db",
    [QUERY_FAILS, None, "", "   "],
    ids=["query-error", "null", "empty", "whitespace"],
)
def test_unknown_database_refuses_start_even_with_safe_locked_settings(monkeypatch, current_db):
    """Settings pinned to 0 and locked would pass the settings check, but with
    no known database the gate can't tell whether projections are in play, so
    it must refuse before running any other check."""
    fake = _fake_run_query(current_db=current_db, value=False, override_refused=True)
    monkeypatch.setattr(permissions, "run_query", fake)
    with pytest.raises(PermissionError, match="could not determine"):
        permissions.ensure_projection_safe_settings(CONFIG)
    assert fake.calls == ["SELECT currentDatabase()"]


def test_unknown_database_refuses_through_startup_gate(monkeypatch):
    monkeypatch.setattr(permissions, "_check_grant", lambda priv, scope: priv == "SELECT")
    fake = _fake_run_query(current_db=QUERY_FAILS, value=False, override_refused=True)
    monkeypatch.setattr(permissions, "run_query", fake)
    with pytest.raises(PermissionError, match="could not determine"):
        permissions.ensure_db_permissions(CONFIG)


def test_database_name_is_quoted(monkeypatch):
    fake = _fake_run_query(current_db="it's", has_projections=0)
    monkeypatch.setattr(permissions, "run_query", fake)
    permissions.ensure_projection_safe_settings(
        types.SimpleNamespace(mcp_user="u", mcp_database="it's")
    )
    assert "database = 'it\\'s'" in fake.calls[1]
