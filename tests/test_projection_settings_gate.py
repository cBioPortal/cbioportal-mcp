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


def _fake_run_query(*, has_projections=1, value=True, override_refused=False, tables_error=False):
    calls = []

    def run_query(query):
        calls.append(query)
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
    assert len(fake.calls) == 1


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
