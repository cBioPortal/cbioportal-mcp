"""Per-request settings ride on mcp-clickhouse's own run_query path."""

import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp_clickhouse import mcp_server

from cbioportal_mcp import clickhouse_query_settings as cqs

SETTING = "SQL_cbiomcp_allowed_studies"


class _FakeClient:
    server_settings: dict = {}

    def __init__(self, barrier: threading.Barrier | None = None, sleep: float = 0.0):
        self.barrier = barrier
        self.sleep = sleep
        self.calls: list[tuple[str, dict, str]] = []
        self.lock = threading.Lock()

    def query(self, query, settings=None):
        with self.lock:
            self.calls.append((query, dict(settings or {}), threading.current_thread().name))
        if self.barrier is not None:
            # Both callers must be inside a query at the same time; a global
            # lock around execution would make this time out.
            self.barrier.wait(timeout=5)
        if self.sleep:
            time.sleep(self.sleep)
        return SimpleNamespace(column_names=["v"], result_rows=[[settings.get(SETTING, "")]])

    def command(self, *args, **kwargs):
        return None

    def close(self):
        pass


@pytest.fixture
def fake_pool(monkeypatch):
    # get_readonly_setting reads mcp-clickhouse's env config; no connection is made.
    for name, value in {
        "CLICKHOUSE_HOST": "fake",
        "CLICKHOUSE_USER": "fake",
        "CLICKHOUSE_PASSWORD": "fake",
    }.items():
        monkeypatch.setenv(name, value)

    def use(client):
        entry = mcp_server._ClientCacheEntry(
            client=client, last_used=time.time(), active_users=1, retired=False
        )
        monkeypatch.setattr(mcp_server, "_resolve_client_config", lambda *a, **k: {})
        monkeypatch.setattr(mcp_server, "_acquire_clickhouse_client", lambda config: entry)
        monkeypatch.setattr(mcp_server, "_release_client_entry", lambda entry: None)
        return client

    return use


def test_concurrent_callers_keep_their_own_settings_and_run_concurrently(fake_pool):
    client = fake_pool(_FakeClient(barrier=threading.Barrier(2)))
    results: dict[str, object] = {}

    def call(study):
        try:
            results[study] = cqs.run_query_with_settings(f"SELECT '{study}'", {SETTING: study})
        except Exception as e:  # surfaced by the asserts below
            results[study] = e

    threads = [threading.Thread(target=call, args=(s,)) for s in ("study_alpha", "study_beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results["study_alpha"] == {"columns": ["v"], "rows": [["study_alpha"]]}
    assert results["study_beta"] == {"columns": ["v"], "rows": [["study_beta"]]}
    by_query = {query: settings for query, settings, _thread in client.calls}
    assert by_query["SELECT 'study_alpha'"][SETTING] == "study_alpha"
    assert by_query["SELECT 'study_beta'"][SETTING] == "study_beta"
    for settings in by_query.values():
        assert settings["readonly"] == "1"
        assert "query_id" in settings  # still mcp-clickhouse's execute_query
    # Executed on mcp-clickhouse's worker pool, not the callers' threads.
    assert all(thread.startswith("ThreadPoolExecutor") for *_, thread in client.calls)


def test_queries_without_request_settings_are_untouched(fake_pool):
    client = fake_pool(_FakeClient())
    cqs.install()
    mcp_server.run_query("SHOW TABLES")
    ((_query, settings, _thread),) = client.calls
    assert SETTING not in settings


def test_readonly_is_forced_even_if_mcp_clickhouse_would_send_2(fake_pool, monkeypatch):
    client = fake_pool(_FakeClient())
    cqs.install()
    monkeypatch.setattr(mcp_server, "get_readonly_setting", lambda client: "2")
    cqs.run_query_with_settings("SELECT 1", {SETTING: "study_alpha"})
    ((_query, settings, _thread),) = client.calls
    assert settings["readonly"] == "1"


def test_timeout_still_cancels_through_mcp_clickhouse(fake_pool, monkeypatch):
    fake_pool(_FakeClient(sleep=2))
    killed = []
    monkeypatch.setattr(mcp_server, "_cancel_query_with_bounded_wait", killed.append)
    monkeypatch.setattr(mcp_server, "get_mcp_config", lambda: SimpleNamespace(query_timeout=0.2))
    with pytest.raises(Exception, match="timed out"):
        cqs.run_query_with_settings("SELECT sleep(2)", {SETTING: "study_alpha"})
    assert len(killed) == 1


@pytest.mark.parametrize(
    ("probe_rows", "override_error", "ok"),
    [
        ([["__cbiomcp_lockdown_probe__"]], "Code: 164. Cannot modify ... in readonly mode", True),
        ([["__cbiomcp_lockdown_probe__"]], None, False),  # override succeeded
        ([["__cbiomcp_lockdown_probe__"]], "Connection refused", False),  # unconfirmed
        ([[""]], "Code: 164", False),  # app setting didn't arrive
    ],
)
def test_verify_allowlist_lockdown(probe_rows, override_error, ok):
    def fake_run(query, settings):
        if "getSetting" in query:
            return {"columns": ["v"], "rows": probe_rows}
        if override_error:
            raise RuntimeError(override_error)
        return {"columns": ["1"], "rows": [[1]]}

    with patch.object(cqs, "run_query_with_settings", fake_run):
        if ok:
            cqs.verify_allowlist_lockdown(SETTING)
        else:
            with pytest.raises(cqs.AllowlistLockdownError):
                cqs.verify_allowlist_lockdown(SETTING)


# Exactly what mcp-clickhouse raises for a 497 (observed live on 25.8).
DENIED = (
    "Query execution failed: HTTPDriver for http://127.0.0.1:8123 received ClickHouse "
    "error code 497\n Code: 497. DB::Exception: mcp_authz: Not enough privileges."
)


def _fake_surface(objects, grants=("GRANT SELECT ON cbio.* TO mcp",), probe_errors=None):
    """objects: [(name, engine, create_query)]; probe_errors: {probe: error or None}."""
    probe_errors = probe_errors or {}

    def fake_run(query, settings):
        if "FROM system.tables" in query:
            return {"columns": ["name", "engine", "q"], "rows": [list(o) for o in objects]}
        if query == "SELECT currentDatabase()":
            return {"columns": ["db"], "rows": [["cbio"]]}
        if query == "SHOW GRANTS FINAL":
            return {"columns": ["g"], "rows": [[g] for g in grants]}
        for name, probe in cqs.RESTRICTED_ROLE_PROBES.items():
            if query == probe:
                error = probe_errors.get(name, DENIED)
                if error is None:
                    return {"columns": [], "rows": []}
                raise RuntimeError(error)
        raise AssertionError(query)

    return fake_run


def _surface(*args, **kwargs):
    return patch.object(cqs, "run_query_with_settings", _fake_surface(*args, **kwargs))


INVOKER_VIEW = ("v", "View", "CREATE VIEW cbio.v SQL SECURITY INVOKER AS SELECT 1")


def test_restricted_surface_ok_for_invoker_views_and_select_only_role():
    with _surface([INVOKER_VIEW]):
        cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize(
    "create_query",
    [
        "CREATE VIEW cbio.bad SQL SECURITY NONE AS SELECT 1",
        "CREATE VIEW cbio.bad DEFINER = admin SQL SECURITY DEFINER AS SELECT 1",
        "CREATE VIEW cbio.bad AS SELECT 1",
        # The reviewer's repro: a quoted column named like the clause.
        "CREATE VIEW cbio.bad (`SQL SECURITY INVOKER` String) SQL SECURITY NONE AS SELECT 1",
        "CREATE VIEW cbio.bad (x String COMMENT 'SQL SECURITY INVOKER') SQL SECURITY NONE "
        "AS SELECT 'SQL SECURITY INVOKER'",
        "CREATE VIEW cbio.bad (`SQL SECURITY INVOKER` String) AS SELECT 1",
    ],
)
def test_restricted_surface_refuses_non_invoker_views(create_query):
    with _surface([INVOKER_VIEW, ("bad", "View", create_query)]):
        with pytest.raises(cqs.AllowlistLockdownError, match="bad"):
            cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize("engine", ["MaterializedView", "Dictionary"])
def test_restricted_surface_refuses_materialized_views_and_dictionaries(engine):
    with _surface([("thing", engine, "")]):
        with pytest.raises(cqs.AllowlistLockdownError, match=f"thing \\({engine}\\)"):
            cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT REMOTE ON *.* TO mcp",
        "GRANT SELECT ON system.query_log TO mcp",
        "GRANT SELECT ON other_db.* TO mcp",
        "GRANT SELECT ON cbio.* TO mcp WITH GRANT OPTION",
        "GRANT dictGet ON *.* TO mcp",
    ],
)
def test_restricted_surface_refuses_extra_grants(grant):
    with _surface([], grants=("GRANT SELECT ON cbio.* TO mcp", grant)):
        with pytest.raises(cqs.AllowlistLockdownError, match=re.escape(grant)):
            cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize("probe", ["system.processes", "remote()", "url()"])
def test_restricted_surface_refuses_allowed_probe(probe):
    with _surface([], probe_errors={probe: None}):
        with pytest.raises(cqs.AllowlistLockdownError, match=re.escape(f"can use {probe}")):
            cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize(
    "error",
    [
        "Code: 701. DB::Exception: Requested cluster 'default' not found",
        "Code: 210. Connection refused",
        "Code: 62. Syntax error",
        "Code: 60. Unknown table expression identifier 'system.processes'",
    ],
)
def test_any_error_other_than_497_is_inconclusive(error):
    with _surface([], probe_errors={"system.processes": error}):
        with pytest.raises(cqs.AllowlistLockdownError, match="could not confirm system.processes"):
            cqs.verify_restricted_surface(SETTING)


def test_unreadable_grants_are_inconclusive():
    def fake(query, settings):
        if query == "SHOW GRANTS FINAL":
            raise RuntimeError("Code: 62. Syntax error")
        return _fake_surface([])(query, settings)

    with patch.object(cqs, "run_query_with_settings", fake):
        with pytest.raises(cqs.AllowlistLockdownError, match="effective grants"):
            cqs.verify_restricted_surface(SETTING)


@pytest.fixture
def monitor_state(monkeypatch):
    """Isolate the module-level monitor state between tests."""
    for name in ("_surface_problem", "_monitor_thread", "_monitor_interval", "_last_check_at"):
        monkeypatch.setattr(cqs, name, None)
    return cqs


def _started(monkeypatch, interval=3600.0):
    """Monitor state as if start_surface_monitor ran, with a live thread."""
    alive = threading.Thread(target=threading.Event().wait, daemon=True)
    alive.start()
    monkeypatch.setattr(cqs, "_monitor_thread", alive)
    monkeypatch.setattr(cqs, "_monitor_interval", interval)
    monkeypatch.setattr(cqs, "_last_check_at", time.monotonic())


def test_failed_recheck_disables_row_policy_queries_until_fixed(monkeypatch, monitor_state):
    from cbioportal_mcp import server

    monkeypatch.setattr(server, "run_query_with_settings", lambda q, s: {"columns": [], "rows": []})
    monkeypatch.setattr(server, "clickhouse_study_access_settings", lambda **kw: {SETTING: "*"})
    _started(monkeypatch)

    bad = [("v", "View", "CREATE VIEW cbio.v SQL SECURITY NONE AS SELECT 1")]
    with _surface(bad):
        cqs.recheck_surface(SETTING)
    assert "v (SQL SECURITY NONE)" in cqs.current_surface_problem()
    with pytest.raises(RuntimeError, match="disabled until"):
        server._run_select_query_with_clickhouse_settings("SELECT 1")

    with _surface([INVOKER_VIEW]):
        cqs.recheck_surface(SETTING)
    assert cqs.current_surface_problem() is None
    assert server._run_select_query_with_clickhouse_settings("SELECT 1") == {
        "columns": [],
        "rows": [],
    }


def test_recheck_that_cannot_run_fails_closed(monkeypatch, monitor_state):
    _started(monkeypatch)

    def boom(query, settings):
        raise RuntimeError("Code: 210. Connection refused")

    with patch.object(cqs, "run_query_with_settings", boom):
        cqs.recheck_surface(SETTING)
    assert "re-check could not run" in cqs.current_surface_problem()
    with _surface([INVOKER_VIEW]):
        cqs.recheck_surface(SETTING)
    assert cqs.current_surface_problem() is None


def test_gate_refuses_when_monitor_never_started(monitor_state):
    assert "not been started" in cqs.current_surface_problem()


def test_gate_refuses_when_monitor_thread_died(monkeypatch, monitor_state):
    _started(monkeypatch)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(cqs, "_monitor_thread", dead)
    assert "stopped" in cqs.current_surface_problem()


def test_gate_refuses_when_last_check_is_stale(monkeypatch, monitor_state):
    _started(monkeypatch, interval=10.0)
    monkeypatch.setattr(cqs, "_last_check_at", time.monotonic() - 31)
    assert "stale" in cqs.current_surface_problem()
    monkeypatch.setattr(cqs, "_last_check_at", time.monotonic() - 29)
    assert cqs.current_surface_problem() is None


def test_exception_in_monitor_cycle_marks_unhealthy_and_thread_survives(monkeypatch, monitor_state):
    calls = threading.Event()

    def crash(setting_name):
        calls.set()
        raise RuntimeError("boom")

    monkeypatch.setattr(cqs, "recheck_surface", crash)
    monkeypatch.setattr(cqs, "MIN_RECHECK_SECONDS", 1.0)
    thread = cqs.start_surface_monitor(SETTING, 1.0)
    assert calls.wait(5)
    deadline = time.monotonic() + 5
    while cqs.current_surface_problem() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "crashed" in cqs.current_surface_problem()
    assert thread.is_alive()


@pytest.mark.parametrize(
    "raw", ["nan", "NaN", "inf", "-inf", "0", "-1", "0.5", "86401", "abc", "5s", "1e400"]
)
def test_invalid_recheck_intervals_are_rejected(raw):
    with pytest.raises(ValueError):
        cqs.parse_recheck_interval(raw)


@pytest.mark.parametrize(("raw", "seconds"), [(None, 300.0), ("", 300.0), ("5", 5.0), ("1", 1.0)])
def test_valid_recheck_intervals(raw, seconds):
    assert cqs.parse_recheck_interval(raw) == seconds


def test_start_surface_monitor_rejects_invalid_interval(monitor_state):
    for bad in (float("nan"), float("inf"), 0, -1):
        with pytest.raises(ValueError):
            cqs.start_surface_monitor(SETTING, bad)
    assert cqs._monitor_thread is None


@pytest.mark.parametrize("raw", ["nan", "inf", "0", "-1", "garbage"])
def test_server_refuses_to_start_with_invalid_recheck_interval(monkeypatch, raw):
    from cbioportal_mcp import server

    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS", raw)
    monkeypatch.setattr(server, "validate_study_access_startup_config", lambda config: None)
    monkeypatch.setattr(server, "ensure_db_permissions", lambda config: None)

    def must_not_run(*args, **kwargs):
        raise AssertionError("startup continued past an invalid recheck interval")

    monkeypatch.setattr(server, "verify_allowlist_lockdown", must_not_run)
    monkeypatch.setattr(server, "start_surface_monitor", must_not_run)
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("HTTPDriver for http://h:8123 received ClickHouse error code 497\n Code: 497. x", 497),
        (
            "Query execution failed: HTTPDriver for http://h:8123 received ClickHouse "
            "error code 497",
            497,
        ),
        ("Code: 497. DB::Exception: mcp: Not enough privileges", 497),
        ("Code: 62. Syntax error, ACCESS_DENIED", 62),
        ("Code: 62. bla received ClickHouse error code 497", 62),
        ("Connection refused (ACCESS_DENIED Code: 497)", None),
        ("Query execution failed: Connection refused, ACCESS_DENIED", None),
    ],
)
def test_error_code_comes_from_the_leading_code_only(error, code):
    assert cqs.clickhouse_error_code(error) == code
    assert cqs.is_access_denied(error) is (code == 497)
    assert cqs.is_access_denied(RuntimeError(error)) is (code == 497)


def test_structured_error_code_attribute_wins():
    class Coded(Exception):
        code = 62

    assert not cqs.is_access_denied(Coded("Code: 497. looks denied"))


@pytest.mark.parametrize(
    "error",
    ["Code: 62. Syntax error, ACCESS_DENIED", "Connection refused (ACCESS_DENIED)"],
)
def test_probe_with_non_497_error_mentioning_access_denied_is_inconclusive(error):
    with _surface([], probe_errors={"system.processes": error}):
        with pytest.raises(cqs.AllowlistLockdownError, match="could not confirm system.processes"):
            cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT SELECT ON cbio.* TO mcp",
        "GRANT SELECT ON cbio.* TO `mcp reader`",
        "GRANT SELECT ON cbio.* TO `odd\\`name`",
        "GRANT SELECT ON `cbio`.* TO `mcp reader`",
        "GRANT SELECT ON cbio.* TO mcp@example.org",
    ],
)
def test_select_only_grant_with_any_principal_is_accepted(grant):
    with _surface([], grants=(grant,)):
        cqs.verify_restricted_surface(SETTING)


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT SELECT ON cbio.* TO `mcp reader` WITH GRANT OPTION",
        "GRANT SELECT ON cbio.* TO `mcp reader`, other",
        "GRANT SELECT ON other.* TO `mcp reader`",
        "GRANT SELECT, REMOTE ON cbio.* TO `mcp reader`",
        "GRANT SELECT ON *.* TO `mcp reader`",
    ],
)
def test_extra_grants_with_quoted_principal_are_still_rejected(grant):
    with _surface([], grants=("GRANT SELECT ON cbio.* TO `mcp reader`", grant)):
        with pytest.raises(cqs.AllowlistLockdownError, match="grants beyond"):
            cqs.verify_restricted_surface(SETTING)
