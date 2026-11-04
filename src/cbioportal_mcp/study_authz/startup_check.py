"""Startup gate for study filtering: the MCP's own user must be covered.

With ``CBIOPORTAL_MCP_STUDY_FILTERING=on`` the server refuses to start unless
the ClickHouse user it connects as (``currentUser()``) is really filtered by
the study row policies of its database. Using that same connection:

1. ``SQL_cbio_user`` reaches ``getSettingOrDefault`` (the ``SQL_`` custom
   settings prefix is accepted) together with ``readonly=1``.
2. The policies apply to this user: the database-wide default-deny targets
   either the user itself, or a role that is enabled by default for it.
   ``coverage.evaluate`` (the admin ``check``) then runs over exactly the
   policies that apply to the user and its enabled roles, so every study
   table, reference table and view needs its own policy, ``user_study`` has
   only the identity predicate and the denied tables (``user_study_manual``...)
   have none.
   Every row policy on the database must be ours and attached to this target
   (``sqlgen.policy_conflicts``, P4's ownership rule): a foreign PERMISSIVE
   policy is OR-ed with ours.
3. Audit system.grants and system.role_grants for the user and every
   transitively granted role, default or not. Only SELECT and SHOW ROW POLICIES
   on the policy database and SELECT on unrelated databases are allowed.
   Only the default policy role may carry grants for a role target; for a user
   target no role may carry grants. Grant/admin options always refuse.
   SHOW GRANTS FINAL additionally detects wildcard names: system.grants loses
   the trailing wildcard on both ClickHouse 25.8 and 26.6.
4. A reserved, random, non-existent caller sees exactly the public studies,
   no other caller's user_study rows, and no user_study_manual rows.

Needs SHOW ROW POLICIES ON <db>.* and SELECT on system.grants and
system.role_grants. Unreadable metadata refuses startup. With filtering off
none of this runs. Failures are one line and never contain emails.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import uuid
from dataclasses import dataclass

from cbioportal_mcp.study_authz.catalog import (
    USER_STUDY_MANUAL_TABLE,
    USER_STUDY_TABLE,
)
from cbioportal_mcp.study_authz.coverage import (
    PolicyRow,
    _squash,
    evaluate,
    fetch_objects,
    fetch_policy_rows,
)
from cbioportal_mcp.study_authz.grants import audit_grants, collect_grants
from cbioportal_mcp.study_authz.sqlgen import (
    policy_conflicts,
    quote_identifier,
    validate_identifier,
)

FILTERING_ENV = "CBIOPORTAL_MCP_STUDY_FILTERING"
EMAIL_SETTING = "SQL_cbio_user"
# RFC 2606 reserves .invalid: no real caller can ever have this address.
PROBE_DOMAIN = "startup-probe.invalid"

_EMAIL_RE = re.compile(r"[^\s@'\"`]+@[^\s@'\"`]+")
MAX_REASONS = 5


class PolicyCoverageError(RuntimeError):
    """The connected user is not provably filtered; the message is one line."""


@dataclass(frozen=True)
class CheckResult:
    user: str
    target: str
    target_is_user: bool
    objects: int
    public_studies: int


def filtering_enabled(env=os.environ) -> bool:
    return env.get(FILTERING_ENV, "off").strip().lower() == "on"


def redact(text: str) -> str:
    """One line, no email addresses (ClickHouse errors can echo settings)."""
    line = " ".join(str(text).split())
    return _EMAIL_RE.sub("<email>", line)[:300]


def probe_email() -> str:
    return f"cbio-mcp-probe-{uuid.uuid4().hex}@{PROBE_DOMAIN}"


class _Session:
    """Runs every query as the runtime user with readonly=1 and the probe email."""

    def __init__(self, client, email: str):
        self.client = client
        self.email = email

    def rows(self, sql: str, parameters=None, **extra):
        settings = {EMAIL_SETTING: self.email, "readonly": 1, **extra}
        settings = {k: v for k, v in settings.items() if v is not None}
        return self.client.query(sql, parameters=parameters, settings=settings).result_rows

    def scalar(self, sql: str, parameters=None, **extra):
        return self.rows(sql, parameters, **extra)[0][0]


def check(client, database: str, email: str | None = None) -> CheckResult:
    """Run every startup check over ``client`` (the MCP's own connection).

    Raises PolicyCoverageError naming the first few failures, on one line and
    without email addresses.
    """
    try:
        return _check(client, database, email)
    except PolicyCoverageError as e:
        raise PolicyCoverageError(redact(e)) from None


def _check(client, database: str, email: str | None) -> CheckResult:
    try:
        qdb = quote_identifier("database", database)
    except ValueError as e:
        raise PolicyCoverageError(str(e)) from None
    s = _Session(client, email or probe_email())

    def step(what: str, fn):
        try:
            return fn()
        except PolicyCoverageError:
            raise
        except Exception as e:  # noqa: BLE001 - any driver/server error refuses
            raise PolicyCoverageError(f"{what}: {redact(e)}") from None

    # 1. The per-query email setting must reach the policies.
    echoed = step(
        f"{EMAIL_SETTING} rejected (needs getSettingOrDefault and the SQL_ settings prefix)",
        lambda: s.scalar(f"SELECT getSettingOrDefault('{EMAIL_SETTING}', '')"),
    )
    if echoed != s.email:
        raise PolicyCoverageError(f"{EMAIL_SETTING} did not reach getSettingOrDefault")

    user = step("currentUser()", lambda: s.scalar("SELECT currentUser()"))
    enabled = {
        r[0]: (bool(r[1]), bool(r[2]))
        for r in step(
            "system.enabled_roles",
            lambda: s.rows(
                "SELECT role_name, is_default, with_admin_option FROM system.enabled_roles"
            ),
        )
    }
    principal_grants = step(
        "grant metadata (needs SELECT ON system.grants and system.role_grants)",
        lambda: collect_grants(s, user),
    )
    granted_roles = principal_grants.roles
    rows: list[PolicyRow] = step(
        f"cannot read system.row_policies (grant SHOW ROW POLICIES ON {database}.* "
        "to the policy target)",
        lambda: fetch_policy_rows(_SessionClient(s), database),
    )

    # 2. Who the policies target.
    deny_targets: set[str] = set()
    for row in rows:
        if row.policy.table == "" and _squash(row.policy.select_filter) == "0":
            deny_targets |= set(row.apply_to_list)
    if user in deny_targets:
        target, target_is_user = user, True
    else:
        role_targets = sorted(deny_targets & set(enabled))
        if not role_targets:
            dormant = sorted(deny_targets & set(granted_roles))
            if dormant:
                raise PolicyCoverageError(
                    f"policy role {dormant[0]} is granted to {user} but not a default role"
                )
            raise PolicyCoverageError(
                f"no study row policy applies to {user}: the default-deny on {database}.* "
                "targets neither the user nor one of its default roles"
            )
        target, target_is_user = role_targets[0], False

    try:
        validate_identifier("policy target", target)
    except ValueError as e:
        raise PolicyCoverageError(str(e)) from None
    reasons: list[str] = []
    principals = {user} | set(enabled)
    applicable = [r.policy for r in rows if r.applies_to(principals)]
    objects = step(
        f"system.tables for {database}",
        lambda: fetch_objects(_SessionClient(s), database, dictionaries=False),
    )
    if not objects:
        raise PolicyCoverageError(f"no tables visible in {database}")
    # The refresh status (system.view_refreshes) is admin-only; stale private
    # rows expire on their own, and the admin `check` covers it.
    # P4's ownership rule: every row policy on the database is ours and
    # attached to this target. A foreign PERMISSIVE policy is OR-ed with ours,
    # so one that applies to this user widens what it reads; one on another
    # principal is widened by our catch-all. Either way, refuse.
    conflicts = step(
        "policy ownership",
        lambda: policy_conflicts(_SessionClient(s), database, target),
    )
    report = evaluate(objects, applicable, database, refresh_error=None, conflicts=conflicts)
    reasons += report.errors
    reasons += [
        f"{name}: unclassified table/view (classify in catalog.py)" for name in report.unclassified
    ]

    # 3. Positive allow list over every grant the user holds, directly or
    # through any (default or non-default) granted role.
    if not target_is_user and not enabled[target][0]:
        reasons.append(f"policy role {target} is not a default role")
    reasons += audit_grants(principal_grants, user, target, target_is_user, database)

    # 4. Behavior as an unknown caller.
    public = 0
    if not reasons:
        db = qdb
        visible = step(
            "probe cancer_study", lambda: s.scalar(f"SELECT count() FROM {db}.`cancer_study`")
        )
        public = step(
            "probe user_study",
            lambda: s.scalar(
                f"SELECT uniqExact(cancer_study_identifier) FROM {db}.`{USER_STUDY_TABLE}` "
                "WHERE email = ''"
            ),
        )
        others = step(
            "probe user_study",
            lambda: s.scalar(f"SELECT count() FROM {db}.`{USER_STUDY_TABLE}` WHERE email != ''"),
        )
        manual = step(
            "probe user_study_manual",
            lambda: s.scalar(f"SELECT count() FROM {db}.`{USER_STUDY_MANUAL_TABLE}`"),
        )
        if int(visible) != int(public):
            reasons.append(
                f"an unknown caller sees {visible} cancer_study rows but {public} public studies"
            )
        visible_ids = step(
            "probe cancer_study identifiers",
            lambda: s.scalar(
                f"SELECT arraySort(groupUniqArray(cancer_study_identifier)) "
                f"FROM {db}.`cancer_study`"
            ),
        )
        public_ids = step(
            "probe public study identifiers",
            lambda: s.scalar(
                f"SELECT arraySort(groupUniqArray(cancer_study_identifier)) "
                f"FROM {db}.`{USER_STUDY_TABLE}` WHERE email = ''"
            ),
        )
        if visible_ids != public_ids:
            reasons.append("an unknown caller's study identifiers differ from public studies")
        if int(others):
            reasons.append(f"{USER_STUDY_TABLE}: an unknown caller sees other callers' rows")
        if int(manual):
            reasons.append(f"{USER_STUDY_MANUAL_TABLE}: readable by the runtime user")

    if reasons:
        shown_reasons = []
        for reason in reasons[:MAX_REASONS]:
            if shown_reasons and len("; ".join(shown_reasons + [reason])) > 230:
                break
            shown_reasons.append(reason[:230])
        shown = "; ".join(shown_reasons)
        remaining = len(reasons) - len(shown_reasons)
        more = f" (+{remaining} more)" if remaining else ""
        raise PolicyCoverageError(f"{database}: {shown}{more}")
    return CheckResult(user, target, target_is_user, len(objects), int(public))


class _SessionClient:
    """Adapts _Session to the ``client.query(...).result_rows`` shape coverage.py uses."""

    def __init__(self, session: _Session):
        self._s = session

    def query(self, sql: str, parameters=None):
        rows = self._s.rows(sql, parameters)
        return type("Result", (), {"result_rows": rows})()


@contextlib.contextmanager
def _send_custom_settings():
    """clickhouse_connect drops or rejects settings the server does not list
    (SQL_cbio_user, the ``role`` URL parameter) unless told to send them."""
    from clickhouse_connect import common

    previous = common.get_setting("invalid_setting_action")
    # It logs a WARNING per query that sends one; expected here.
    driver_log = logging.getLogger("clickhouse_connect.driver.client")
    previous_level = driver_log.level
    common.set_setting("invalid_setting_action", "send")
    driver_log.setLevel(logging.ERROR)
    try:
        yield
    finally:
        common.set_setting("invalid_setting_action", previous)
        driver_log.setLevel(previous_level)


def check_with_mcp_connection(database: str) -> CheckResult:
    """``check`` over a fresh client built exactly like mcp-clickhouse's own."""
    from mcp_clickhouse.mcp_server import create_clickhouse_client

    try:
        client = create_clickhouse_client()
    except Exception as e:  # noqa: BLE001
        raise PolicyCoverageError(f"cannot connect to ClickHouse: {redact(e)}") from None
    try:
        with _send_custom_settings():
            return check(client, database)
    finally:
        with contextlib.suppress(Exception):
            client.close()


def run_startup_check(database: str, env=os.environ, runner=None):
    """No-op (returns None) unless study filtering is on."""
    if not filtering_enabled(env):
        return None
    return (runner or check_with_mcp_connection)(database)
