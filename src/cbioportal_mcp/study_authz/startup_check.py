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
3. Role target: no direct grant on the database (a direct ``SELECT`` plus
   ``SET ROLE NONE`` reads everything through ``ALL EXCEPT <role>``), and no
   other granted role that gives ``SELECT`` without the policy role. User
   target: direct grants are expected; only non-read privileges are refused.
4. No admin-ish privileges: access management, write/DDL on the database,
   temporary tables, table functions, ``dictGet``, ``SYSTEM``.
5. A probe as a reserved, random, non-existent email sees exactly the public
   studies, no other caller's ``user_study`` rows and no ``user_study_manual``.

Needs ``SHOW ROW POLICIES ON <db>.*`` for the user (the policy template grants
it to the target). Failures are one line that names the table or condition and
never an email. With filtering off none of this runs.
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
from cbioportal_mcp.study_authz.sqlgen import (
    policy_conflicts,
    quote_identifier,
)

FILTERING_ENV = "CBIOPORTAL_MCP_STUDY_FILTERING"
EMAIL_SETTING = "SQL_cbio_user"
# RFC 2606 reserves .invalid: no real caller can ever have this address.
PROBE_DOMAIN = "startup-probe.invalid"

# Privileges a direct grant on the database may carry (user target only).
READ_PRIVILEGES = frozenset(
    {
        "SELECT",
        "SHOW",
        "SHOW TABLES",
        "SHOW COLUMNS",
        "SHOW DATABASES",
        "SHOW DICTIONARIES",
        "SHOW ROW POLICIES",
        "SHOW POLICIES",
    }
)
SELECT_PRIVILEGES = frozenset({"SELECT", "ALL", "ALL PRIVILEGES"})

# Effective privileges (user + default roles) that must be absent, ON *.*.
FORBIDDEN_GLOBAL = (
    "ACCESS MANAGEMENT",
    "CREATE USER",
    "ALTER USER",
    "DROP USER",
    "CREATE ROLE",
    "ALTER ROLE",
    "DROP ROLE",
    "ROLE ADMIN",
    "CREATE ROW POLICY",
    "ALTER ROW POLICY",
    "DROP ROW POLICY",
    "CREATE QUOTA",
    "ALTER QUOTA",
    "CREATE SETTINGS PROFILE",
    "ALTER SETTINGS PROFILE",
    "SHOW USERS",
    "ALLOW SQL SECURITY NONE",
    "SYSTEM",
    "CREATE TEMPORARY TABLE",
    "REMOTE",
    "URL",
    "FILE",
    "S3",
)
# ON <db>.* and ON the authz tables (catches table-level grants there).
FORBIDDEN_ON_DATABASE = (
    "INSERT",
    "ALTER",
    "ALTER UPDATE",
    "ALTER DELETE",
    "CREATE TABLE",
    "CREATE VIEW",
    "CREATE DICTIONARY",
    "DROP TABLE",
    "DROP VIEW",
    "TRUNCATE",
    "OPTIMIZE",
    "CREATE ROW POLICY",
    "ALTER ROW POLICY",
    "DROP ROW POLICY",
    "dictGet",
)
FORBIDDEN_ON_AUTHZ_TABLES = ("INSERT", "ALTER UPDATE", "ALTER DELETE", "TRUNCATE")
AUTHZ_TABLES = (USER_STUDY_TABLE, USER_STUDY_MANUAL_TABLE, "cancer_study")

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


@dataclass(frozen=True)
class Grant:
    """One ``SHOW GRANTS`` privilege line: ``GRANT <privileges> ON <object>``."""

    privileges: tuple[str, ...]
    database: str  # '*' for ON *.* / ON *
    table: str
    grant_option: bool


def filtering_enabled(env=os.environ) -> bool:
    return env.get(FILTERING_ENV, "off").strip().lower() == "on"


def redact(text: str) -> str:
    """One line, no email addresses (ClickHouse errors can echo settings)."""
    line = " ".join(str(text).split())
    return _EMAIL_RE.sub("<email>", line)[:300]


def probe_email() -> str:
    return f"cbio-mcp-probe-{uuid.uuid4().hex}@{PROBE_DOMAIN}"


def _split_privileges(text: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [re.sub(r"\(.*\)", "", p).strip().upper() for p in out if p.strip()]


def parse_show_grants(lines: list[str]) -> tuple[list[Grant], dict[str, bool]]:
    """(privilege grants, granted role -> with admin option) for the current user.

    ``REVOKE`` lines are ignored: a partially revoked direct grant is still a
    direct grant.
    """
    grants: list[Grant] = []
    roles: dict[str, bool] = {}
    for raw in lines:
        line = raw.strip()
        if not line.upper().startswith("GRANT "):
            continue
        match = re.match(r"GRANT\s+(.+?)\s+ON\s+(\S+)\s+TO\s+", line, re.I)
        if match:
            target = match.group(2).replace("`", "")
            db, _, table = target.partition(".")
            grants.append(
                Grant(
                    tuple(_split_privileges(match.group(1))),
                    db,
                    table or "*",
                    "WITH GRANT OPTION" in line.upper(),
                )
            )
            continue
        match = re.match(r"GRANT\s+(.+?)\s+TO\s+\S+", line, re.I)
        if match:
            admin = "WITH ADMIN OPTION" in line.upper()
            for role in match.group(1).split(","):
                roles[role.strip().strip("`")] = admin
    return grants, roles


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

    def granted(self, privilege: str, scope: str, **extra) -> bool:
        # Without readonly: under readonly=1 CHECK GRANT reports every write and
        # access-management privilege as absent, and the MCP's readonly=1 is a
        # per-query setting that someone holding these credentials can omit.
        sql = f"CHECK GRANT {privilege} ON {scope}"
        return int(self.scalar(sql, readonly=None, **extra)) == 1


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
    grants, granted_roles = parse_show_grants(
        [r[0] for r in step("SHOW GRANTS", lambda: s.rows("SHOW GRANTS"))]
    )
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

    # 3. Grants that bypass a role target, and admin options.
    if not target_is_user:
        if not enabled[target][0]:
            reasons.append(f"policy role {target} is not a default role")
        for grant in grants:
            if grant.database in (database, "*") and SELECT_PRIVILEGES & set(grant.privileges):
                reasons.append(
                    f"direct SELECT grant ON {grant.database}.{grant.table} bypasses role "
                    f"{target} (SET ROLE NONE)"
                )
        for other in sorted(set(granted_roles) - deny_targets):
            if step(
                f"CHECK GRANT as role {other}",
                lambda o=other: s.granted("SELECT", f"{qdb}.*", role=o),
            ):
                reasons.append(f"role {other} grants SELECT ON {database}.* without {target}")
    for grant in grants:
        if grant.database not in (database, "*"):
            continue
        extra = sorted(set(grant.privileges) - READ_PRIVILEGES - SELECT_PRIVILEGES)
        if extra:
            reasons.append(f"direct grant {', '.join(extra)} ON {grant.database}.{grant.table}")
        if grant.grant_option:
            reasons.append(f"direct grant ON {grant.database}.{grant.table} WITH GRANT OPTION")
    for role, admin in granted_roles.items():
        if admin or enabled.get(role, (False, False))[1]:
            reasons.append(f"role {role} is granted WITH ADMIN OPTION")

    # 4. Admin-ish effective privileges (the user plus its default roles).
    checks = [(p, "*.*", "*.*") for p in FORBIDDEN_GLOBAL]
    checks += [(p, f"{qdb}.*", f"{database}.*") for p in FORBIDDEN_ON_DATABASE]
    checks += [
        (p, f"{qdb}.`{t}`", f"{database}.{t}")
        for t in AUTHZ_TABLES
        for p in FORBIDDEN_ON_AUTHZ_TABLES
    ]
    for privilege, scope, label in checks:
        if step(f"CHECK GRANT {privilege}", lambda p=privilege, sc=scope: s.granted(p, sc)):
            reasons.append(f"user holds {privilege} ON {label}")

    # 5. Behavior as an unknown caller.
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
        if int(others):
            reasons.append(f"{USER_STUDY_TABLE}: an unknown caller sees other callers' rows")
        if int(manual):
            reasons.append(f"{USER_STUDY_MANUAL_TABLE}: readable by the runtime user")

    if reasons:
        shown = "; ".join(reasons[:MAX_REASONS])
        more = f" (+{len(reasons) - MAX_REASONS} more)" if len(reasons) > MAX_REASONS else ""
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
