"""Auth rollout switches: strict parsing, invariant, API truth table, expiry."""

import dataclasses
import logging
from datetime import date

import pytest

from cbioportal_mcp import rollout
from cbioportal_mcp.env import validate_startup
from cbioportal_mcp.server import _validate_server_startup

ENFORCEMENT = rollout.AUTH_ENFORCEMENT_ENV
TESTERS = rollout.AUTH_TEST_USERS_ENV
DB_CHECK = rollout.PUBLIC_DB_CHECK_ENV
MODE_VARS = (ENFORCEMENT, DB_CHECK)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Valid HTTP baseline; each test starts before startup has pinned settings."""
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "off")
    monkeypatch.delenv("CBIOPORTAL_MCP_LOCAL_DEV", raising=False)
    monkeypatch.delenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", raising=False)
    monkeypatch.setenv(ENFORCEMENT, "off")
    monkeypatch.setenv(TESTERS, "")
    monkeypatch.setenv(DB_CHECK, "off")
    monkeypatch.setattr(rollout, "_SETTINGS", None)


@pytest.mark.parametrize("name", [ENFORCEMENT, TESTERS, DB_CHECK])
def test_each_switch_is_required_in_http(monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(ValueError, match=name):
        validate_startup()


@pytest.mark.parametrize("name", MODE_VARS)
@pytest.mark.parametrize("value", ["", " ", "on", "true", "1", "enforced", "disabled"])
def test_mode_switches_reject_invalid(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        validate_startup()


@pytest.mark.parametrize(
    "name, field", [(ENFORCEMENT, "auth_enforcement"), (DB_CHECK, "public_db_check")]
)
@pytest.mark.parametrize("value, expected", [("off", "off"), (" ENFORCE ", "enforce")])
def test_mode_switches_accept_valid(monkeypatch, name, field, value, expected):
    monkeypatch.setenv(name, value)
    validate_startup()
    assert getattr(rollout.get(), field) == expected


@pytest.mark.parametrize("value", ["", "   ", ",", " , ,"])
def test_explicitly_empty_tester_list(monkeypatch, value):
    monkeypatch.setenv(TESTERS, value)
    validate_startup()
    assert rollout.get().auth_test_users == frozenset()


def test_tester_parsing_normalizes(monkeypatch):
    monkeypatch.setenv(TESTERS, " Alice@Example.org ,bob@example.org,, ALICE@example.ORG ,")
    validate_startup()
    assert rollout.get().auth_test_users == {"alice@example.org", "bob@example.org"}


@pytest.mark.parametrize(
    "value", ["alice", "alice@", "@example.org", "alice@example", "a b@example.org",
              "a@b@example.org", "ok@example.org,not-an-email"],
)
def test_tester_list_rejects_implausible_entries(monkeypatch, value):
    monkeypatch.setenv(TESTERS, value)
    with pytest.raises(ValueError, match=TESTERS) as error:
        validate_startup()
    assert "not-an-email" not in str(error.value)  # the value itself is not echoed


@pytest.mark.parametrize("name", [ENFORCEMENT, TESTERS, DB_CHECK])
def test_local_stdio_defaults_and_still_rejects_invalid(monkeypatch, name):
    monkeypatch.setenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", "stdio")
    monkeypatch.setenv("CBIOPORTAL_MCP_LOCAL_DEV", "1")
    for var in (ENFORCEMENT, TESTERS, DB_CHECK):
        monkeypatch.delenv(var)
    validate_startup()
    assert rollout.get() == rollout.RolloutSettings("off", frozenset(), "off")
    # An HTTP launch of the same process gets no defaults.
    with pytest.raises(ValueError, match=ENFORCEMENT):
        validate_startup("http")
    monkeypatch.setenv(name, "bogus")
    with pytest.raises(ValueError, match=name):
        validate_startup()


@pytest.mark.parametrize(
    "enforcement, db_check, ok",
    [("enforce", "enforce", True), ("enforce", "off", False),
     ("off", "enforce", False), ("off", "off", False)],
)
def test_study_filtering_requires_both_enforced(monkeypatch, enforcement, db_check, ok):
    monkeypatch.setenv(ENFORCEMENT, enforcement)
    monkeypatch.setenv(DB_CHECK, db_check)
    if ok:
        assert rollout.parse("on", local_dev=False).public_db_check == "enforce"
    else:
        with pytest.raises(ValueError, match="STUDY_FILTERING=on requires"):
            rollout.parse("on", local_dev=False)
    assert rollout.parse("off", local_dev=False).auth_enforcement == enforcement


def test_tester_list_ignored_with_warning_under_enforce(monkeypatch, caplog):
    monkeypatch.setenv(ENFORCEMENT, "enforce")
    monkeypatch.setenv(TESTERS, "alice@example.org")
    with caplog.at_level(logging.WARNING, logger=rollout.logger.name):
        validate_startup()
    assert rollout.get().auth_test_users == frozenset()
    assert TESTERS in caplog.text and "alice" not in caplog.text


@pytest.mark.parametrize(
    "enforcement, email, expected",
    [
        ("enforce", "alice@example.org", True),
        ("enforce", "carol@example.org", True),
        ("enforce", None, True),
        ("off", "alice@example.org", True),
        ("off", " ALICE@Example.org ", True),
        ("off", "carol@example.org", False),
        ("off", "", False),
        ("off", None, False),
    ],
)
def test_auth_checks_apply_truth_table(monkeypatch, enforcement, email, expected):
    monkeypatch.setenv(ENFORCEMENT, enforcement)
    if enforcement == "off":
        monkeypatch.setenv(TESTERS, "alice@example.org")
    validate_startup()
    assert rollout.auth_checks_apply(email) is expected


@pytest.mark.parametrize("value, expected", [("off", False), ("enforce", True)])
def test_public_db_check_enforced(monkeypatch, value, expected):
    monkeypatch.setenv(DB_CHECK, value)
    validate_startup()
    assert rollout.public_db_check_enforced() is expected


def test_api_requires_startup():
    with pytest.raises(RuntimeError, match="before startup"):
        rollout.auth_checks_apply("alice@example.org")


def test_settings_immutable_after_startup(monkeypatch):
    validate_startup()
    settings = rollout.get()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.auth_enforcement = "enforce"
    with pytest.raises(AttributeError):
        settings.auth_test_users.add("mallory@example.org")
    monkeypatch.setenv(ENFORCEMENT, "enforce")
    monkeypatch.setenv(DB_CHECK, "enforce")
    validate_startup()  # later launcher checks revalidate but do not re-pin
    assert rollout.get() is settings
    assert rollout.auth_checks_apply("mallory@example.org") is False


def test_startup_log_lists_switches_and_count_only(monkeypatch, caplog):
    monkeypatch.setenv(TESTERS, "alice@example.org,Bob@Example.org")
    with caplog.at_level(logging.INFO):
        _validate_server_startup()
    lines = [r.getMessage() for r in caplog.records if "rollout switches" in r.getMessage()]
    assert lines == [
        "cBioPortal MCP rollout switches: study_filtering=off auth_enforcement=off "
        "auth_test_users_count=2 public_db_check=off"
    ]
    assert "example.org" not in caplog.text.lower()


@pytest.mark.parametrize("switch", sorted(rollout.EXPIRES))
def test_rollout_switch_has_not_expired(switch):
    expires = rollout.EXPIRES[switch]
    action = "the tester list" if switch == "AUTH_TEST_USERS" else "the 'off' path"
    assert date.today() <= expires, (
        f"CBIOPORTAL_MCP_{switch} expired on {expires}: remove {action} "
        f"from cbioportal_mcp.rollout (and its env var from deployments)"
    )
