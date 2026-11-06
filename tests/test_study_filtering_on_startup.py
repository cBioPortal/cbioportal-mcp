"""CBIOPORTAL_MCP_STUDY_FILTERING=on: starts only when every precondition holds.

Preconditions (P1.5 + P6), all checked by the shared server startup gate:

1. CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce
2. CBIOPORTAL_MCP_PUBLIC_DB_CHECK=enforce
3. CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES explicitly set (may be '')
4. The P6 database coverage check passes (mocked here; see
   test_study_authz_startup_check.py and test_study_authz_live.py for the check
   itself).

Each missing precondition must refuse startup with one line that names the
problem and contains no secret or email.
"""

import logging

import pytest

from cbioportal_mcp import rollout
from cbioportal_mcp.env import get_mcp_config
from cbioportal_mcp.server import ConfiguredFastMCP, _validate_server_startup
from cbioportal_mcp.study_authz import startup_check

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
    # exit-2 handling all run for real.
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


# --- negative: each missing precondition refuses startup -------------------


@pytest.mark.parametrize(
    "name, value",
    [
        (ENFORCEMENT, "off"),
        (DB_CHECK, "off"),
        (ENFORCEMENT, None),
        (DB_CHECK, None),
    ],
)
def test_on_refused_without_both_switches_enforced(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        startup_check, "_check", lambda *a, **k: pytest.fail("P6 must not run first")
    )
    with pytest.raises(ValueError) as error:
        _validate_server_startup()
    message = str(error.value)
    assert name in message
    assert_clean_one_line(message)


def test_on_refused_with_both_switches_off(monkeypatch):
    monkeypatch.setenv(ENFORCEMENT, "off")
    monkeypatch.setenv(DB_CHECK, "off")
    with pytest.raises(ValueError, match=f"{FILTERING}=on requires") as error:
        _validate_server_startup()
    assert_clean_one_line(str(error.value))


def test_on_refused_at_construction_without_switches(monkeypatch):
    """Alternate launchers (ASGI import, fastmcp dev) refuse before serving too."""
    monkeypatch.setenv(ENFORCEMENT, "off")
    with pytest.raises(ValueError, match=f"{FILTERING}=on requires") as error:
        ConfiguredFastMCP(name="test")
    assert_clean_one_line(str(error.value))


def test_on_refused_without_public_databases_setting(monkeypatch, caplog):
    monkeypatch.delenv(PUBLIC_DBS)
    monkeypatch.setattr(
        "mcp_clickhouse.mcp_server.create_clickhouse_client",
        lambda: pytest.fail("must not connect before the declaration is validated"),
    )
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert PUBLIC_DBS in line and "required" in line
    assert_clean_one_line(line)
    assert "study_filtering=on transport" not in caplog.text


@pytest.mark.parametrize("raw", [",", "a,,b", "bad-name", "db`x"])
def test_on_refused_with_invalid_public_databases(monkeypatch, caplog, raw):
    monkeypatch.setenv(PUBLIC_DBS, raw)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert_clean_one_line(line)


def test_on_refused_when_p6_check_fails(monkeypatch, caplog):
    p6_fails(monkeypatch)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert "study filtering startup check failed" in line
    assert "default-deny" in line
    assert "<email>" in line
    assert_clean_one_line(line)
    assert "study_filtering=on transport" not in caplog.text


def test_on_refused_when_clickhouse_unreachable(monkeypatch, caplog):
    def unreachable():
        raise ConnectionError(f"auth failed for {LEAKY_EMAIL} password={SECRET}\nretry")

    monkeypatch.setattr("mcp_clickhouse.mcp_server.create_clickhouse_client", unreachable)
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        _validate_server_startup()
    assert exc.value.code == 2
    [line] = critical_lines(caplog)
    assert "cannot connect to ClickHouse" in line
    assert "\n" not in line and LEAKY_EMAIL not in line


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
