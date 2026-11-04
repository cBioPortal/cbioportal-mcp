"""Unit tests for the study-filtering startup check, against a fake ClickHouse.

tests/test_study_authz_live.py runs the same check against ClickHouse 25.8.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import pytest

from cbioportal_mcp import server
from cbioportal_mcp.study_authz import sqlgen, startup_check
from cbioportal_mcp.study_authz.startup_check import (
    PolicyCoverageError,
    check,
    parse_show_grants,
    redact,
    run_startup_check,
)

DB = "d"
ROLE = "cbioportal_mcp_study_restricted"
EMAIL = "probe@startup-probe.invalid"


@dataclass
class FakeClickHouse:
    """Answers the startup check's queries from plain state."""

    user: str = "mcp"
    target: str = ROLE  # the policy target: ROLE or the user itself
    enabled: list = field(default_factory=lambda: [(ROLE, 1, 0)])
    grants: list = field(default_factory=lambda: [f"GRANT {ROLE} TO mcp"])
    tables: dict = field(default_factory=dict)
    policies: list = field(default_factory=list)
    granted: set = field(default_factory=set)  # {(privilege, scope, role or None)}
    counts: dict = field(
        default_factory=lambda: {"visible": 2, "public": 2, "others": 0, "manual": 0}
    )
    accept_custom_setting: bool = True
    can_read_policies: bool = True
    settings_seen: list = field(default_factory=list)

    def __post_init__(self):
        if not self.tables:
            self.tables = {
                "cancer_study": "MergeTree",
                "gene": "MergeTree",
                "user_study": "MergeTree",
                "user_study_manual": "MergeTree",
                "user_study_refresh": "MaterializedView",
                "top_mutated_genes_in_study": "View",
            }
        if not self.policies:
            self.policies = good_policies(self.target)

    def query(self, sql, parameters=None, settings=None):
        settings = settings or {}
        self.settings_seen.append(settings)
        # readonly=1 would hide write privileges from CHECK GRANT; all else sends it.
        assert settings.get("readonly") == (None if sql.startswith("CHECK GRANT") else 1)
        rows = self._answer(sql, settings)
        return type("R", (), {"result_rows": rows})()

    def _answer(self, sql, settings):
        if "getSettingOrDefault" in sql:
            if not self.accept_custom_setting:
                raise RuntimeError(
                    f"Code: 115. Setting SQL_cbio_user={settings['SQL_cbio_user']} is neither "
                    "a builtin setting nor started with the prefix 'SQL_'"
                )
            return [[settings["SQL_cbio_user"]]]
        if "currentUser()" in sql:
            return [[self.user]]
        if "system.enabled_roles" in sql:
            return [list(r) for r in self.enabled]
        if sql == "SHOW GRANTS":
            return [[g] for g in self.grants]
        if "system.row_policies" in sql:
            if not self.can_read_policies:
                raise RuntimeError("Code: 497. mcp: Not enough privileges (ACCESS_DENIED)")
            if "select_filter" in sql:
                return [list(p) for p in self.policies]
            # sqlgen.policy_conflicts: name, table, apply_to_all/list/except.
            return [[p[0], p[1], p[4], p[5], p[6]] for p in self.policies]
        if "system.tables" in sql:
            create = "CREATE VIEW d.v SQL SECURITY INVOKER AS SELECT 1"
            return [[n, e, create if e == "View" else ""] for n, e in self.tables.items()]
        if sql.startswith("CHECK GRANT"):
            m = re.match(r"CHECK GRANT (.+) ON (\S+)$", sql)
            scope = m.group(2).replace("`", "")
            return [[int((m.group(1), scope, settings.get("role")) in self.granted)]]
        if "uniqExact" in sql:
            return [[self.counts["public"]]]
        if "email != ''" in sql:
            return [[self.counts["others"]]]
        if "user_study_manual" in sql:
            return [[self.counts["manual"]]]
        if "cancer_study" in sql:
            return [[self.counts["visible"]]]
        raise AssertionError(f"unexpected query {sql}")


def good_policies(target):
    """system.row_policies rows as the generated template creates them."""
    to = (False, (target,), ())
    study = sqlgen.study_condition("cancer_study").replace(sqlgen.DB, DB)
    return [
        ("cbioportal_mcp_unrestricted_others", "", "1", 0, True, (), (target,)),
        ("cbioportal_mcp_default_deny", "", "0", 0, *to),
        ("cbioportal_mcp_identity_user_study", "user_study", sqlgen.USER_STUDY_MATCH, 0, *to),
        ("cbioportal_mcp_study_cancer_study", "cancer_study", study, 0, *to),
        ("cbioportal_mcp_reference_gene", "gene", "1", 0, *to),
        ("cbioportal_mcp_view_top", "top_mutated_genes_in_study", "1", 0, *to),
    ]


def user_target(**kw):
    return FakeClickHouse(
        user="llm_user",
        target="llm_user",
        enabled=[],
        grants=["GRANT SELECT ON other_pub.* TO llm_user", f"GRANT SELECT ON {DB}.* TO llm_user"],
        **kw,
    )


def refused(fake, needle):
    with pytest.raises(PolicyCoverageError) as exc:
        check(fake, DB, EMAIL)
    message = str(exc.value)
    assert needle in message, message
    assert "\n" not in message and "@" not in message
    return message


def test_role_target_passes():
    fake = FakeClickHouse()
    result = check(fake, DB, EMAIL)
    assert (result.user, result.target, result.target_is_user) == ("mcp", ROLE, False)
    assert result.public_studies == 2
    # Every query carried the probe email and readonly=1.
    assert all(s["SQL_cbio_user"] == EMAIL for s in fake.settings_seen)


def test_user_target_passes_with_direct_grants():
    result = check(user_target(), DB, EMAIL)
    assert (result.target, result.target_is_user) == ("llm_user", True)


def test_role_target_with_direct_select_refused():
    fake = FakeClickHouse(grants=[f"GRANT {ROLE} TO mcp", f"GRANT SELECT ON {DB}.* TO mcp"])
    refused(fake, "direct SELECT grant ON d.* bypasses role")


@pytest.mark.parametrize(
    "line",
    ["GRANT SELECT ON *.* TO mcp", "GRANT SELECT(x, y) ON d.cancer_study TO mcp"],
)
def test_role_target_direct_select_any_scope_refused(line):
    refused(FakeClickHouse(grants=[f"GRANT {ROLE} TO mcp", line]), "bypasses role")


def test_other_role_with_select_refused():
    fake = FakeClickHouse(
        enabled=[(ROLE, 1, 0), ("analyst", 1, 0)],
        grants=[f"GRANT {ROLE}, analyst TO mcp"],
        granted={("SELECT", f"{DB}.*", "analyst")},
    )
    refused(fake, "role analyst grants SELECT ON d.* without")


def test_role_granted_but_not_default_refused():
    fake = FakeClickHouse(enabled=[])
    refused(fake, f"policy role {ROLE} is granted to mcp but not a default role")


def test_user_name_that_is_an_email_is_redacted():
    fake = FakeClickHouse(user="Svc@Example.org", enabled=[], grants=[])
    message = refused(fake, "no study row policy applies to <email>")
    assert "Example" not in message


def test_no_policy_for_user_refused():
    fake = FakeClickHouse(enabled=[("someone_else", 1, 0)], grants=["GRANT someone_else TO mcp"])
    refused(fake, "no study row policy applies to mcp")


def test_missing_policy_on_one_table_refused():
    fake = FakeClickHouse()
    fake.policies = [p for p in fake.policies if p[1] != "cancer_study"]
    refused(fake, "cancer_study: study table has only the default-deny")


def test_policy_for_another_principal_does_not_count():
    fake = FakeClickHouse()
    fake.policies = [
        (p[0], p[1], p[2], p[3], p[4], ("other_role",), p[6]) if p[1] == "gene" else p
        for p in fake.policies
    ]
    refused(fake, "gene: reference table has only the default-deny")


def test_extra_open_policy_through_another_enabled_role_refused():
    fake = FakeClickHouse(enabled=[(ROLE, 1, 0), ("analyst", 1, 0)])
    fake.policies.append(("open", "user_study_manual", "1", 0, False, ("analyst",), ()))
    refused(fake, "user_study_manual: denied object has policies")


def test_foreign_policy_widening_the_user_refused():
    """P4's ownership rule: a policy not named cbioportal_mcp_* is OR-ed with ours."""
    fake = FakeClickHouse()
    study = sqlgen.study_condition("cancer_study").replace(sqlgen.DB, DB)
    fake.policies.append(("team_view", "cancer_study", study, 0, False, (ROLE,), ()))
    refused(fake, "team_view ON d.cancer_study (not created by study_authz)")


def test_foreign_policy_on_another_principal_refused():
    fake = FakeClickHouse()
    fake.policies.append(("outsider_only", "cancer_study", "0", 0, False, ("outsider",), ()))
    refused(fake, "outsider_only ON d.cancer_study (not created by study_authz)")


def test_own_policies_attached_to_another_target_refused():
    fake = FakeClickHouse()
    fake.policies.append(
        ("cbioportal_mcp_reference_gene", "gene", "1", 0, False, ("old_role",), ())
    )
    refused(fake, "attached to old_role, not " + ROLE)


def test_user_study_manual_readable_refused():
    fake = FakeClickHouse()
    fake.policies.append(("leak", "user_study_manual", "1", 0, False, (ROLE,), ()))
    refused(fake, "user_study_manual: denied object has policies")


def test_user_study_manual_rows_visible_refused():
    fake = FakeClickHouse()
    fake.counts["manual"] = 3
    refused(fake, "user_study_manual: readable by the runtime user")


def test_user_study_other_rows_refused():
    fake = FakeClickHouse()
    fake.policies = [
        (p[0], p[1], "1", *p[3:]) if p[1] == "user_study" else p for p in fake.policies
    ]
    refused(fake, "user_study: policy must be exactly")
    fake = FakeClickHouse()
    fake.counts["others"] = 5
    refused(fake, "other callers' rows")


def test_probe_count_mismatch_refused():
    fake = FakeClickHouse()
    fake.counts["visible"] = 9
    refused(fake, "unknown caller sees 9 cancer_study rows but 2 public studies")


def test_unclassified_table_refused():
    fake = FakeClickHouse()
    fake.tables["brand_new"] = "MergeTree"
    refused(fake, "brand_new: unclassified")


@pytest.mark.parametrize(
    ("privilege", "scope"),
    [
        ("ACCESS MANAGEMENT", "*.*"),
        ("CREATE ROW POLICY", "*.*"),
        ("INSERT", f"{DB}.*"),
        ("ALTER", f"{DB}.*"),
        ("INSERT", f"{DB}.user_study_manual"),
        ("CREATE TEMPORARY TABLE", "*.*"),
        ("dictGet", f"{DB}.*"),
    ],
)
def test_admin_privileges_refused(privilege, scope):
    fake = FakeClickHouse(granted={(privilege, scope, None)})
    refused(fake, f"user holds {privilege} ON {scope}")


def test_direct_write_grant_refused_even_for_user_target():
    fake = user_target()
    fake.grants.append(f"GRANT INSERT ON {DB}.user_study TO llm_user")
    refused(fake, "direct grant INSERT ON d.user_study")
    fake = user_target()
    fake.grants[-1] = f"GRANT SELECT ON {DB}.* TO llm_user WITH GRANT OPTION"
    refused(fake, "WITH GRANT OPTION")


def test_role_with_admin_option_refused():
    fake = FakeClickHouse(grants=[f"GRANT {ROLE} TO mcp WITH ADMIN OPTION"])
    refused(fake, "WITH ADMIN OPTION")


def test_custom_setting_rejected_refused_without_echoing_email():
    message = refused(FakeClickHouse(accept_custom_setting=False), "SQL_cbio_user rejected")
    assert "<email>" in message


def test_policies_unreadable_refused():
    refused(FakeClickHouse(can_read_policies=False), "grant SHOW ROW POLICIES ON d.*")


def test_bad_database_name_refused():
    for bad in ("d; DROP", "d\n", "1d"):  # fullmatch: no trailing newline
        with pytest.raises(PolicyCoverageError, match="database must match"):
            check(FakeClickHouse(), bad, EMAIL)


def test_many_failures_summarized_on_one_line():
    fake = FakeClickHouse(granted={(p, "*.*", None) for p in startup_check.FORBIDDEN_GLOBAL})
    message = refused(fake, "more)")
    assert message.count(";") == startup_check.MAX_REASONS - 1


def test_probe_email_is_reserved_and_random():
    a, b = startup_check.probe_email(), startup_check.probe_email()
    assert a != b and a.endswith("@startup-probe.invalid") and a == a.lower()


def test_parse_show_grants():
    grants, roles = parse_show_grants(
        [
            "GRANT SELECT, SHOW ROW POLICIES ON d.* TO u",
            "GRANT SELECT(a, b), INSERT ON `d`.`t` TO u WITH GRANT OPTION",
            "REVOKE SELECT ON d.x FROM u",
            "GRANT r1, `r2` TO u",
            "GRANT r3 TO u WITH ADMIN OPTION",
        ]
    )
    assert [(g.privileges, g.database, g.table, g.grant_option) for g in grants] == [
        (("SELECT", "SHOW ROW POLICIES"), "d", "*", False),
        (("SELECT", "INSERT"), "d", "t", True),
    ]
    assert roles == {"r1": False, "r2": False, "r3": True}


def test_redact_strips_emails_and_newlines():
    assert redact("a\nb Alice@Example.org c") == "a b <email> c"


@pytest.mark.parametrize("value", [None, "off", "OFF", "", "yes"])
def test_filtering_off_runs_nothing(value):
    env = {} if value is None else {startup_check.FILTERING_ENV: value}

    def runner(_db):
        raise AssertionError("must not run with filtering off")

    assert run_startup_check(DB, env=env, runner=runner) is None


def test_filtering_on_runs_the_check():
    calls = []
    env = {startup_check.FILTERING_ENV: " On "}
    run_startup_check(DB, env=env, runner=calls.append)
    assert calls == [DB]


def test_server_exits_2_with_one_critical_line(monkeypatch, caplog):
    def failing(_db):
        raise PolicyCoverageError("d: cancer_study: study table has only the default-deny")

    monkeypatch.setattr(startup_check, "run_startup_check", failing)
    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as exc:
        server.enforce_study_policy_coverage(DB)
    assert exc.value.code == 2
    [record] = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert "cancer_study" in record.getMessage() and "\n" not in record.getMessage()


def test_server_does_nothing_with_filtering_off(monkeypatch):
    monkeypatch.delenv(startup_check.FILTERING_ENV, raising=False)
    monkeypatch.setattr(
        startup_check,
        "check_with_mcp_connection",
        lambda _db: pytest.fail("must not connect with filtering off"),
    )
    server.enforce_study_policy_coverage(DB)


def test_template_grants_show_row_policies_before_select():
    stmts = sqlgen.statements(sqlgen.render(sqlgen.row_policies_template(), _params()))
    assert stmts[-2] == f"GRANT SHOW ROW POLICIES ON `{DB}`.* TO `{ROLE}`"
    assert stmts[-1] == f"GRANT SELECT ON `{DB}`.* TO `{ROLE}`"


def _params():
    return sqlgen.Params(database=DB, public_group="PUBLIC", app_name="cbioportal")
