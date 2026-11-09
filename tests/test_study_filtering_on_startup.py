"""Filtering failures keep the server up with study data blocked."""

import logging

# Captured before the autouse fixture replaces them.
import mcp_clickhouse.mcp_server as _upstream  # noqa: E402
import pytest
from fastmcp.exceptions import ToolError

from cbioportal_mcp import rollout
from cbioportal_mcp.env import get_mcp_config
from cbioportal_mcp.server import ConfiguredFastMCP, _validate_server_startup
from cbioportal_mcp.study_authz import availability, startup_check

_REAL_CREATE_CLIENT = _upstream.create_clickhouse_client
_REAL_P6_CHECK = startup_check._check

FILTERING = "CBIOPORTAL_MCP_STUDY_FILTERING"
ENFORCEMENT = rollout.AUTH_ENFORCEMENT_ENV
DB_CHECK = rollout.PUBLIC_DB_CHECK_ENV
TESTERS = rollout.AUTH_TEST_USERS_ENV
PUBLIC_DBS = startup_check.PUBLIC_DATABASES_ENV
BRIDGE = "CBIOPORTAL_MCP_BRIDGE_SECRET"

SECRET = "s3cret-bridge-value-do-not-log"
TESTER = "tester-a@example.org"
# A ClickHouse error can echo the per-query email setting back.
LEAKY_EMAIL = "someone@example.org"


class _FakeClient:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def all_preconditions_met(monkeypatch):
    """Baseline: every precondition for filtering=on holds, P6 passes (mocked)."""
    monkeypatch.setenv(FILTERING, "on")
    monkeypatch.setenv(ENFORCEMENT, "enforce")
    monkeypatch.setenv(DB_CHECK, "enforce")
    monkeypatch.setenv(TESTERS, TESTER)
    monkeypatch.setenv(BRIDGE, SECRET)
    monkeypatch.setenv(PUBLIC_DBS, "")
    monkeypatch.delenv("CBIOPORTAL_MCP_LOCAL_DEV", raising=False)
    monkeypatch.delenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", raising=False)
    for name in (
        "CBIOPORTAL_MCP_GOOGLE_CLIENT_ID",
        "CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET",
        "CBIOPORTAL_MCP_GOOGLE_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    # Mock only the connection and the database queries: run_startup_check,
    # parse_public_databases, check() (with its redaction) and the server's
    # degraded-mode handling all run for real.
    monkeypatch.setattr("mcp_clickhouse.mcp_server.create_clickhouse_client", lambda: _FakeClient())
    p6_passes(monkeypatch)


def p6_passes(monkeypatch, seen=None):
    def fake(client, database, email, public_databases):
        if seen is not None:
            seen.append((database, public_databases))
        return startup_check.CheckResult(
            user="mcp_restricted",
            target="cbioportal_mcp_study_restricted",
            target_is_user=False,
            objects=50,
            public_studies=2,
        )

    monkeypatch.setattr(startup_check, "_check", fake)


def p6_fails(monkeypatch):
    def fake(client, database, email, public_databases):
        raise startup_check.PolicyCoverageError(
            f"{database}: cancer_study: study table has only the default-deny\n"
            f"(server said: Setting SQL_cbio_user = '{LEAKY_EMAIL}')"
        )

    monkeypatch.setattr(startup_check, "_check", fake)


def assert_clean_one_line(text: str):
    assert "\n" not in text.strip()
    for leaked in (SECRET, TESTER, LEAKY_EMAIL):
        assert leaked not in text


def critical_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.CRITICAL]


# --- positive: every precondition holds ------------------------------------


@pytest.mark.parametrize("public_dbs", ["", "cbioportal_public_blue, cbioportal_public_green"])
def test_on_starts_when_every_precondition_holds(monkeypatch, caplog, public_dbs):
    monkeypatch.setenv(PUBLIC_DBS, public_dbs)
    seen = []
    p6_passes(monkeypatch, seen)
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    database = get_mcp_config().mcp_database
    expected = frozenset(name.strip() for name in public_dbs.split(",") if name.strip())
    assert seen == [(database, expected)]
    assert "study_filtering=on transport=http" in caplog.text
    assert "auth_enforcement=enforce" in caplog.text
    assert "public_db_check=enforce" in caplog.text
    assert "is covered by the row policies on" in caplog.text
    assert not critical_lines(caplog)
    assert_clean_one_line(caplog.text.replace("\n", " "))


@pytest.mark.asyncio
async def test_on_server_construction_and_lifespan_start(monkeypatch, caplog):
    app = ConfiguredFastMCP(name="test")
    http = app.http_app()
    with caplog.at_level(logging.INFO):
        async with http.router.lifespan_context(http):
            pass
    assert "study_filtering=on transport=http" in caplog.text
    assert "is covered by the row policies on" in caplog.text


# --- negative: filtering failures degrade; malformed switches refuse startup -------------------


@pytest.mark.parametrize(
    "name, value",
    [
        (ENFORCEMENT, "off"),
        (DB_CHECK, "off"),
        (ENFORCEMENT, None),
        (DB_CHECK, None),
    ],
)
def test_on_degraded_without_both_switches_enforced(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name)
        with pytest.raises(ValueError, match=name):
            _validate_server_startup()
        return
    monkeypatch.setenv(name, value)
    _validate_server_startup()
    assert availability.reason() == "rollout_not_enforced"
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()


def test_on_constructs_without_enforced_switches(monkeypatch):
    monkeypatch.setenv(ENFORCEMENT, "off")
    ConfiguredFastMCP(name="test")
    assert not availability.available()


def test_on_degrades_without_public_databases_setting(monkeypatch, caplog):
    monkeypatch.delenv(PUBLIC_DBS)
    monkeypatch.setattr(
        "mcp_clickhouse.mcp_server.create_clickhouse_client",
        lambda: pytest.fail("must not connect before the declaration is validated"),
    )
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert PUBLIC_DBS in line and "required" in line
    assert_clean_one_line(line)
    assert "study_filtering=on transport" in caplog.text


@pytest.mark.parametrize("raw", [",", "a,,b", "bad-name", "db`x"])
def test_on_degrades_with_invalid_public_databases(monkeypatch, caplog, raw):
    monkeypatch.setenv(PUBLIC_DBS, raw)
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert_clean_one_line(line)


def test_on_degrades_when_p6_check_fails(monkeypatch, caplog):
    p6_fails(monkeypatch)
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert "study filtering startup check failed" in line
    assert "default-deny" in line
    assert "<email>" in line
    assert_clean_one_line(line)
    assert "study_filtering=on transport" in caplog.text


def test_on_degrades_when_clickhouse_unreachable(monkeypatch, caplog):
    def unreachable():
        raise ConnectionError(f"auth failed for {LEAKY_EMAIL} password={SECRET}\nretry")

    monkeypatch.setattr("mcp_clickhouse.mcp_server.create_clickhouse_client", unreachable)
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert "cannot connect to ClickHouse (ConnectionError)" in line
    assert_clean_one_line(line)
    assert SECRET not in caplog.text and LEAKY_EMAIL not in caplog.text


LEAKY_DETAILS = [
    f"Code: 516. Authentication failed: password={SECRET}",
    f"password = '{SECRET}' is incorrect",
    f"connect to https://mcp_user:{SECRET}@ch.example.org:8443/db failed",
    f"HTTP 401 Authorization: Bearer {SECRET}",
    f"bad request ?user=mcp_user&token={SECRET}&secret={SECRET}",
    f"api_key: {SECRET} rejected",
]


@pytest.mark.parametrize("detail", LEAKY_DETAILS)
def test_p6_refusal_never_logs_credentials(monkeypatch, caplog, detail):
    def fake(client, database, email, public_databases):
        raise startup_check.PolicyCoverageError(f"system.enabled_roles: {detail}\nmore")

    monkeypatch.setattr(startup_check, "_check", fake)
    with caplog.at_level(logging.DEBUG):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert "system.enabled_roles" in line
    assert_clean_one_line(line)
    assert SECRET not in caplog.text


@pytest.mark.parametrize("detail", LEAKY_DETAILS)
def test_p6_unexpected_error_logs_class_only(monkeypatch, caplog, detail):
    def fake(client, database, email, public_databases):
        raise RuntimeError(f"{detail} for {LEAKY_EMAIL}\nTraceback-ish")

    monkeypatch.setattr(startup_check, "_check", fake)
    with caplog.at_level(logging.DEBUG):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert "unexpected RuntimeError" in line
    assert_clean_one_line(line)
    assert SECRET not in caplog.text and LEAKY_EMAIL not in caplog.text


@pytest.mark.parametrize("detail", LEAKY_DETAILS)
def test_redact_strips_credentials_and_emails(detail):
    out = startup_check.redact(f"{detail} for {LEAKY_EMAIL}\nnext")
    assert SECRET not in out and LEAKY_EMAIL not in out and "\n" not in out


# --- below the upstream boundary: only clickhouse_connect is faked ----------
#
# These tests run the REAL permission gate, the REAL mcp-clickhouse client
# factory (and, for runtime queries, the REAL upstream run_query) and the REAL
# P6 checker. Only clickhouse_connect.get_client is replaced, so every log
# record the upstream stack emits is captured and checked.

REVIEW_SECRET = "review-secret-0042"

# Credential shapes a driver/server error can echo. Each must vanish.
CREDENTIAL_FORMS = [
    f"password={REVIEW_SECRET}",
    f"password = '{REVIEW_SECRET}'",
    f'{{"password": "{REVIEW_SECRET}"}}',
    f"{{'password': '{REVIEW_SECRET}'}}",
    f'{{"passwd":"{REVIEW_SECRET}"}}',
    f"'pwd'='{REVIEW_SECRET}'",
    f'"secret": "{REVIEW_SECRET}"',
    f"token: {REVIEW_SECRET}",
    f'{{"api_key": "{REVIEW_SECRET}"}}',
    f'{{"client_secret": "{REVIEW_SECRET}"}}',
    f"'authorization': '{REVIEW_SECRET}'",
    f"https://mcp_user:{REVIEW_SECRET}@ch.example.org:8443/",
    f"Authorization: Basic {REVIEW_SECRET}",
    f"Authorization: Bearer {REVIEW_SECRET}",
]


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows
        self.column_names = ["v"]


class _FakeDriverClient:
    """Stands in for clickhouse_connect's client object only."""

    server_version = "26.6.0"

    def __init__(self, answer):
        self.answer = answer

    def query(self, sql, *args, **kwargs):
        outcome = self.answer(sql)
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResult(outcome)

    def close(self):
        pass


def assert_logs_clean(caplog, secret=REVIEW_SECRET):
    """No record from ANY logger has the secret, an email, or a raw newline at WARNING+."""
    for record in caplog.records:
        text = record.getMessage() + (record.exc_text or "")
        assert secret not in text, (record.name, text)
        assert LEAKY_EMAIL not in text, (record.name, text)
        if record.levelno >= logging.WARNING:
            assert "\n" not in text, (record.name, text)


@pytest.fixture
def below_upstream(monkeypatch):
    """Real permission gate + real mcp-clickhouse factory; fake get_client only."""
    import clickhouse_connect
    import mcp_clickhouse.mcp_server as upstream

    from cbioportal_mcp.authentication import permissions

    monkeypatch.setattr(
        "cbioportal_mcp.server.ensure_db_permissions", permissions.ensure_db_permissions
    )
    monkeypatch.setattr(upstream, "create_clickhouse_client", _REAL_CREATE_CLIENT)
    monkeypatch.setattr(startup_check, "_check", _REAL_P6_CHECK)
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "8123")
    monkeypatch.setenv("CLICKHOUSE_USER", "mcp_user")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", REVIEW_SECRET)
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")

    state = {}

    def install(answer=None, connect_error=None):
        def get_client(**kwargs):
            if connect_error is not None:
                raise connect_error
            return _FakeDriverClient(answer)

        monkeypatch.setattr(clickhouse_connect, "get_client", get_client)
        state["answer"] = answer

    return install


def _grants(granted):
    def answer(sql):
        if sql.startswith("CHECK GRANT "):
            return [[1 if granted(sql.removeprefix("CHECK GRANT ")) else 0]]
        raise AssertionError(f"unexpected query {sql}")

    return answer


def test_permission_refusal_missing_select_is_one_line(below_upstream, caplog):
    below_upstream(_grants(lambda _ps: False))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    db = get_mcp_config().mcp_database
    assert f"lacks SELECT ON {db}.*" in line
    assert f"GRANT SELECT ON {db}.* TO" in line
    assert_clean_one_line(line)
    assert_logs_clean(caplog)


def test_permission_refusal_forbidden_privileges_is_one_line(below_upstream, caplog):
    below_upstream(_grants(lambda _ps: True))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert "forbidden privileges on *.*" in line and "REVOKE" in line
    assert_clean_one_line(line)
    assert_logs_clean(caplog)


def test_permission_gate_passes_with_select_only(below_upstream, monkeypatch, caplog):
    db = get_mcp_config().mcp_database
    below_upstream(_grants(lambda ps: ps == f"SELECT ON {db}.*"))
    p6_passes(monkeypatch)  # this test is about the permission gate
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    assert "ClickHouse permission checks passed" in caplog.text
    assert "study_filtering=on transport=http" in caplog.text
    assert_logs_clean(caplog)


def test_permission_gate_does_not_use_upstream_run_query(below_upstream, monkeypatch):
    import mcp_clickhouse.mcp_server as upstream

    db = get_mcp_config().mcp_database
    below_upstream(_grants(lambda ps: ps == f"SELECT ON {db}.*"))
    p6_passes(monkeypatch)
    monkeypatch.setattr(upstream, "run_query", lambda *_a, **_k: pytest.fail("run_query used"))
    _validate_server_startup()


@pytest.mark.parametrize("form", CREDENTIAL_FORMS)
def test_permission_connect_failure_never_logs_credentials(below_upstream, caplog, form):
    """Codex repro: the upstream factory logs the error itself before re-raising."""
    below_upstream(connect_error=ConnectionError(f"{form}\nretry {LEAKY_EMAIL}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert "cannot connect to ClickHouse to run CHECK GRANT (ConnectionError)" in line
    assert_clean_one_line(line)
    # The upstream ERROR record was emitted, and is scrubbed.
    assert any(r.name == "mcp-clickhouse" and r.levelno == logging.ERROR for r in caplog.records)
    assert_logs_clean(caplog)


@pytest.mark.parametrize("form", CREDENTIAL_FORMS)
def test_permission_query_error_never_logs_credentials(below_upstream, caplog, form):
    from clickhouse_connect.driver.exceptions import DatabaseError

    below_upstream(lambda _sql: DatabaseError(f"HTTP 401 {form}\nCode: 516. more"))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert "lacks SELECT" in line
    assert_clean_one_line(line)
    assert "DatabaseError (ClickHouse code 516)" in caplog.text
    assert_logs_clean(caplog)


def test_upstream_run_query_worker_error_is_scrubbed(monkeypatch, caplog):
    """Codex repro for the runtime path: the REAL upstream run_query and worker,
    with client acquisition raising. Upstream logs the error itself."""
    import mcp_clickhouse.mcp_server as upstream
    from fastmcp.exceptions import ToolError

    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_USER", "mcp_user")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", REVIEW_SECRET)

    def acquire(_config):
        raise ConnectionError(f"password={REVIEW_SECRET}\nretry")

    monkeypatch.setattr(upstream, "_acquire_clickhouse_client", acquire)
    with caplog.at_level(logging.DEBUG), pytest.raises(ToolError):
        upstream.run_query("SELECT 1")
    assert any(r.name == "mcp-clickhouse" and r.levelno == logging.ERROR for r in caplog.records)
    assert_logs_clean(caplog)


UNTERMINATED_AUTH_CONNECT_ERRORS = [
    f'Authorization: "Bearer\n{REVIEW_SECRET}',  # Codex re-review #3 repro
    f"Authorization: 'Bearer\n{REVIEW_SECRET}",
    f'Proxy-Authorization: "Digest username="u",\nresponse="{REVIEW_SECRET}"',
    f'Proxy-Authorization: \'Digest username="u",\nresponse="{REVIEW_SECRET}"',
    f'password="abc\n{REVIEW_SECRET}',
]


@pytest.mark.parametrize("detail", UNTERMINATED_AUTH_CONNECT_ERRORS)
def test_permission_gate_unterminated_quoted_credentials_never_logged(
    below_upstream, caplog, detail
):
    """Real permission gate + real upstream factory: clickhouse_connect.get_client
    raises; upstream logs 'Failed to connect to ClickHouse: ...' itself."""
    below_upstream(connect_error=ConnectionError(detail))
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    upstream_errors = [
        r for r in caplog.records if r.name == "mcp-clickhouse" and r.levelno == logging.ERROR
    ]
    assert upstream_errors and "Failed to connect to ClickHouse" in upstream_errors[0].getMessage()
    assert_logs_clean(caplog)
    for record in caplog.records:
        assert "review-secret" not in record.getMessage() + (record.exc_text or "")


def test_startup_with_failing_upstream_acquire_is_scrubbed(below_upstream, monkeypatch, caplog):
    """Codex's exact startup repro: patch upstream acquisition to raise and run
    the real gate. Startup refuses and no record carries the secret."""
    import mcp_clickhouse.mcp_server as upstream

    below_upstream(connect_error=ConnectionError(f"password={REVIEW_SECRET}\nretry"))

    def acquire(_config):
        raise ConnectionError(f"password={REVIEW_SECRET}\nretry")

    monkeypatch.setattr(upstream, "_acquire_clickhouse_client", acquire)
    with caplog.at_level(logging.DEBUG), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert_clean_one_line(line)
    assert_logs_clean(caplog)


# --- P6 through the real checker: driver errors are never echoed -------------


def _p6_answers(failing_sql: str, error):
    """Let P6 get past its first queries, then fail at the given query."""
    db_grants = get_mcp_config().mcp_database

    def answer(sql):
        if sql.startswith("CHECK GRANT "):
            return [[1 if sql.endswith(f"SELECT ON {db_grants}.*") else 0]]
        if failing_sql in sql:
            return error
        if "getSettingOrDefault" in sql:
            return error if failing_sql == "getSettingOrDefault" else [["probe"]]
        return error

    return answer


@pytest.mark.parametrize("form", CREDENTIAL_FORMS)
def test_p6_real_checker_query_error_never_echoes_credentials(below_upstream, caplog, form):
    from clickhouse_connect.driver.exceptions import DatabaseError

    error = DatabaseError(f"HTTP 401 {form} for {LEAKY_EMAIL}\nCode: 516. retry")
    below_upstream(_p6_answers("getSettingOrDefault", error))
    with caplog.at_level(logging.DEBUG):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert "study filtering startup check failed" in line
    assert "SQL_cbio_user rejected" in line
    assert "DatabaseError (ClickHouse code 516)" in line
    assert "HTTP 401" not in line  # free-form text is not echoed at all
    assert_clean_one_line(line)
    assert_logs_clean(caplog)


@pytest.mark.parametrize("form", CREDENTIAL_FORMS)
def test_p6_real_checker_connect_error_never_echoes_credentials(
    below_upstream, monkeypatch, caplog, form
):
    """The SELECT check passes; P6's own client creation then fails."""
    import clickhouse_connect

    db = get_mcp_config().mcp_database
    below_upstream(_grants(lambda ps: ps == f"SELECT ON {db}.*"))
    calls = {"n": 0}
    real_get_client = clickhouse_connect.get_client

    def get_client(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the permission gate's client
            return real_get_client(**kwargs)
        raise ConnectionError(f"{form}\nretry")

    monkeypatch.setattr(clickhouse_connect, "get_client", get_client)
    with caplog.at_level(logging.DEBUG):
        _validate_server_startup()
    assert not availability.available()
    with pytest.raises(ToolError, match="Study data unavailable"):
        availability.require()
    [line] = critical_lines(caplog)
    assert "cannot connect to ClickHouse (ConnectionError)" in line
    assert_clean_one_line(line)
    assert_logs_clean(caplog)


@pytest.mark.parametrize("form", CREDENTIAL_FORMS)
def test_redactor_covers_every_credential_form(form):
    from cbioportal_mcp.redaction import redact, redact_credentials

    assert REVIEW_SECRET not in redact(form)
    assert REVIEW_SECRET not in redact_credentials(form)
    assert REVIEW_SECRET not in redact(f"x {form} for {LEAKY_EMAIL}\ny")


def test_redactor_keeps_non_secret_text():
    from cbioportal_mcp.redaction import redact

    assert redact("cancer_study: study table has only the default-deny") == (
        "cancer_study: study table has only the default-deny"
    )
    assert redact("user mcp_user on db cbio_restricted_green") == (
        "user mcp_user on db cbio_restricted_green"
    )


# --- P6 failure keeps every transport serving in degraded mode ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_p6_failure_serves_degraded_lifespan(monkeypatch, caplog, transport):
    p6_fails(monkeypatch)
    app = ConfiguredFastMCP(name="test")
    asgi = app.http_app(transport=transport)
    with caplog.at_level(logging.INFO):
        async with asgi.router.lifespan_context(asgi):
            assert not availability.available()
            with pytest.raises(ToolError, match="Study data unavailable"):
                availability.require()
    [line] = critical_lines(caplog)
    assert_clean_one_line(line)


@pytest.mark.asyncio
async def test_p6_failure_serves_degraded_local_stdio(monkeypatch):
    from cbioportal_mcp.server import _startup_lifespan
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    monkeypatch.setenv("CBIOPORTAL_MCP_LOCAL_DEV", "1")
    p6_fails(monkeypatch)
    app = ConfiguredFastMCP(name="test")
    async with _startup_lifespan(app):
        assert app.startup_transport == "stdio"
        assert not availability.available()


def test_on_auth_enforce_still_needs_a_login_route(monkeypatch):
    """P2: enforce without Google or a bridge secret refuses, secret-free."""
    monkeypatch.delenv(BRIDGE)
    with pytest.raises(ValueError, match="Auth enforce requires") as error:
        _validate_server_startup()
    assert_clean_one_line(str(error.value))


def test_off_never_runs_p6_or_needs_public_databases(monkeypatch, caplog):
    monkeypatch.setenv(FILTERING, "off")
    monkeypatch.delenv(PUBLIC_DBS)
    monkeypatch.setattr(
        startup_check, "_check", lambda *a, **k: pytest.fail("P6 must not run with off")
    )
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    assert "study_filtering=off transport=http" in caplog.text
