"""P10 startup and availability contract."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from mcp_clickhouse import mcp_server

from cbioportal_mcp import rollout, server
from cbioportal_mcp.study_authz import availability, startup_check

ALLOWLIST = "CBIOPORTAL_MCP_PUBLIC_DB_ALLOWED_GROUPS"


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv(rollout.PUBLIC_DB_CHECK_ENV, "enforce")
    monkeypatch.setenv(ALLOWLIST, "Example,Another")


@pytest.mark.parametrize("value", [None, "example,,another"])
def test_allowlist_is_strict(enforced, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ALLOWLIST)
    else:
        monkeypatch.setenv(ALLOWLIST, value)
    with pytest.raises(ValueError, match=ALLOWLIST):
        server.ConfiguredFastMCP(name="p10-config")


ERR = RuntimeError("sensitive connection details")
# system.tables (name, engine, total_rows), system.columns tables, then
# count() as the check user for each storage table. Views are skipped.
TABLES = [("cancer_study", "MergeTree", 2), ("summary", "View", None)]
COLUMNS = [("cancer_study",), ("summary",)]
SAME = [TABLES, COLUMNS, 2]
MANY = [
    ("cancer_study", "MergeTree", 2),
    ("mv", "MaterializedView", 3),
    (".inner_id.0f0e-1", "MergeTree", 3),
    ("dict", "Dictionary", None),
    ("sample", "ReplacingMergeTree", 5),
    ("mutation", "MergeTree", 7),
]
MANY_COLUMNS = [(row[0],) for row in MANY]


@pytest.mark.parametrize(
    "responses, rule",
    [
        ([0, *SAME, 0, 0, 0], None),
        ([0, *SAME, 0, 0, 1, 0], None),
        ([0, MANY, MANY_COLUMNS, 2, 3, 5, 7, 0, 0, 0], None),
        ([1], "row policies exist"),
        ([ERR], "row policies: could not verify"),
        # Policies hidden from a filtered system.row_policies still hide rows,
        # on any table in the database.
        ([0, TABLES, COLUMNS, 1], "rows hidden from the check user (cancer_study)"),
        ([0, MANY, MANY_COLUMNS, 2, 3, 4], "rows hidden from the check user (sample)"),
        ([0, MANY, MANY_COLUMNS, 2, 3, 5, 6], "rows hidden from the check user (mutation)"),
        ([0, [("bad\nname", "Log", 1)], [("bad\nname",)], 0], "check user (<table>)"),
        (
            [0, [("sample", "MergeTree", None)], [("sample",)]],
            "could not verify row counts (sample)",
        ),
        # A policy on system.tables hides a table from that listing only.
        ([0, TABLES, [*COLUMNS, ("sample",)]], "row counts: table listings disagree"),
        ([0, ERR], "could not verify row counts"),
        ([0, TABLES, ERR], "could not verify row counts"),
        ([0, TABLES, COLUMNS, TimeoutError()], "could not verify row counts (cancer_study)"),
        ([0, TABLES, COLUMNS, ERR], "could not verify row counts (cancer_study)"),
        ([0, *SAME, 1], "cancer_study.public"),
        ([0, *SAME, 0, 1], "cancer_study.groups"),
        ([0, *SAME, 0, 0, 1, 1], "authorities contains rows"),
        ([0, *SAME, 0, ERR], "cancer_study.groups"),
        ([0, *SAME, 0, 0, ERR], "authorities"),
        (None, "connection"),
    ],
)
@pytest.mark.asyncio
async def test_startup_check(enforced, monkeypatch, caplog, responses, rule):
    db = Mock()
    connect = Mock(return_value=db)
    config = Mock()
    config.get_client_config.return_value = {}
    monkeypatch.setattr(mcp_server, "get_config", lambda: config)
    monkeypatch.setattr(mcp_server.clickhouse_connect, "get_client", connect)
    if responses is None:
        connect.side_effect = RuntimeError("sensitive connection details")
    else:
        db.query.side_effect = [
            value
            if isinstance(value, Exception)
            else SimpleNamespace(result_rows=value if isinstance(value, list) else [(value,)])
            for value in responses
        ]
    server.validate_startup()
    with pytest.raises(ToolError, match="checks_pending"):
        availability.require()
    server._validate_server_startup()
    server._validate_server_startup()
    connect.assert_called_once()
    if responses is not None:
        assert db.query.call_count == len(responses)
        assert "system.row_policies" in db.query.call_args_list[0].args[0]
        assert connect.call_args.kwargs["send_receive_timeout"] == 60
        db.close.assert_called_once()
    response = await server.study_data_health(None)
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "study_data_available": rule is None,
        "reason": "ready" if rule is None else "public_db_check_failed",
    }
    critical = [
        record.getMessage() for record in caplog.records if record.levelno == logging.CRITICAL
    ]
    if rule is None:
        assert critical == []
    else:
        assert len(critical) == 1
        assert "Study data unavailable" in critical[0]
        assert rule in critical[0]
        assert "sensitive connection details" not in caplog.text


def test_allowlist_preserves_group_spelling(enforced, monkeypatch):
    monkeypatch.setenv(ALLOWLIST, "Example, example")
    assert server.validate_startup()[0] == "off"
    assert rollout.get().public_db_allowed_groups == {"Example", " example"}


@pytest.mark.parametrize("filtering, mode", [("off", "off"), ("on", "enforce")])
def test_public_check_skipped(monkeypatch, filtering, mode):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", filtering)
    monkeypatch.setenv(rollout.PUBLIC_DB_CHECK_ENV, mode)
    monkeypatch.setenv(ALLOWLIST, ",")
    monkeypatch.setenv(rollout.AUTH_ENFORCEMENT_ENV, "enforce" if filtering == "on" else "off")
    if filtering == "on":
        monkeypatch.setenv("CBIOPORTAL_MCP_BRIDGE_SECRET", "test-bridge")
    connect = Mock()
    monkeypatch.setattr(mcp_server.clickhouse_connect, "get_client", connect)
    monkeypatch.setattr(
        startup_check,
        "run_startup_check",
        lambda database: startup_check.CheckResult("test", "test", True, 0, 0),
    )
    server._validate_server_startup()
    connect.assert_not_called()
    assert availability.reason() == ("filtering_off" if filtering == "off" else "ready")


def test_row_count_budget_fails_closed(monkeypatch):
    from cbioportal_mcp.study_authz import public_db_check

    db = Mock()
    db.query.side_effect = [SimpleNamespace(result_rows=rows) for rows in ([(0,)], TABLES, COLUMNS)]
    config = Mock()
    config.get_client_config.return_value = {"send_receive_timeout": 30}
    monkeypatch.setattr(mcp_server, "get_config", lambda: config)
    connect = Mock(return_value=db)
    monkeypatch.setattr(mcp_server.clickhouse_connect, "get_client", connect)
    monkeypatch.setattr(public_db_check, "ROW_COUNT_BUDGET_SECONDS", -1)
    with pytest.raises(public_db_check.PublicDatabaseError, match="could not verify row counts"):
        public_db_check.check(frozenset())
    assert connect.call_args.kwargs["send_receive_timeout"] == 30
    db.close.assert_called_once()
