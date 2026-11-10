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
    redact,
    run_startup_check,
)

DB = "d"
ROLE = "cbioportal_mcp_study_restricted"
EMAIL = "probe@startup-probe.invalid"
USER_STUDY_UUID = "0b5e4a3c-0000-4000-8000-00000000c0de"
# user_study as system.tables stores the rendered template's view.
USER_STUDY_CREATE = (
    f"CREATE MATERIALIZED VIEW {DB}.user_study REFRESH EVERY 5 MINUTE (`email` String) "
    "ENGINE = MergeTree ORDER BY (email, cancer_study_identifier) DEFINER = admin "
    f"SQL SECURITY DEFINER AS SELECT '' AS email FROM {DB}.cancer_study"
)


@pytest.fixture(autouse=True)
def _explicit_public_databases(monkeypatch):
    monkeypatch.setenv(startup_check.PUBLIC_DATABASES_ENV, "")


@dataclass
class FakeClickHouse:
    """Answers the startup check's queries from plain state."""

    user: str = "mcp"
    target: str = ROLE  # the policy target: ROLE or the user itself
    enabled: list = field(default_factory=lambda: [(ROLE, 1, 0)])
    grants: list = field(default_factory=lambda: [f"GRANT {ROLE} TO mcp"])
    tables: dict = field(default_factory=dict)
    policies: list = field(default_factory=list)
    role_grants: dict = field(
        default_factory=lambda: {
            ROLE: [
                f"GRANT SELECT ON {DB}.* TO {ROLE}",
                f"GRANT SHOW ROW POLICIES ON {DB}.* TO {ROLE}",
            ]
        }
    )  # role -> its SHOW GRANTS lines
    counts: dict = field(
        default_factory=lambda: {"visible": 2, "public": 2, "others": 0, "manual": 0}
    )
    accept_custom_setting: bool = True
    can_read_policies: bool = True
    can_read_grants: bool = True
    other_objects: dict = field(default_factory=dict)
    other_study_columns: dict = field(default_factory=dict)
    other_policies: dict = field(default_factory=dict)
    settings_seen: list = field(default_factory=list)
    creates: dict = field(default_factory=lambda: {"user_study": USER_STUDY_CREATE})
    user_study_uuid: str = USER_STUDY_UUID

    def __post_init__(self):
        if not self.tables:
            self.tables = {
                "cancer_study": "MergeTree",
                "gene": "MergeTree",
                "user_study": "MaterializedView",
                f".inner_id.{USER_STUDY_UUID}": "MergeTree",
                "user_study_manual": "MergeTree",
                "top_mutated_genes_in_study": "View",
            }
        if not self.policies:
            self.policies = good_policies(self.target)

    def query(self, sql, parameters=None, settings=None):
        settings = settings or {}
        self.settings_seen.append(settings)
        assert settings.get("readonly") == 1
        rows = self._answer(sql, settings, parameters)
        return type("R", (), {"result_rows": rows})()

    def _answer(self, sql, settings, parameters):
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
            if not self.can_read_grants:
                raise RuntimeError("cannot read own grants")
            return [[g] for g in self.grants]
        if sql == "SHOW GRANTS FINAL":
            lines, queue, seen = list(self.grants), list(self.grants), set()
            while queue:
                line = queue.pop()
                if " ON " in line:
                    continue
                match = re.fullmatch(r"GRANT (.+) TO \S+(?: WITH ADMIN OPTION)?", line)
                assert match, line
                for role in match[1].split(", "):
                    if role not in seen:
                        seen.add(role)
                        nested = self.role_grants.get(role, [])
                        lines.extend(nested)
                        queue.extend(nested)
            return [
                [re.sub(r" TO \S+", " TO `" + self.user + "`", g)] for g in lines if " ON " in g
            ]
        if "system.row_policies" in sql:
            if "SELECT DISTINCT database" in sql:
                return [(DB,)] + [(db,) for db in self.other_policies]
            if not self.can_read_policies:
                raise RuntimeError("Code: 497. mcp: Not enough privileges (ACCESS_DENIED)")
            if parameters["db"] != DB:
                return self.other_policies.get(parameters["db"], [])
            if "select_filter" in sql:
                return [list(p) for p in self.policies]
            # sqlgen.policy_conflicts: name, table, apply_to_all/list/except.
            return [[p[0], p[1], p[4], p[5], p[6]] for p in self.policies]
        if "system.columns" in sql:
            return [(name,) for name in self.other_study_columns.get(parameters["db"], [])]
        if "system.tables" in sql and "toString(uuid)" in sql:
            assert parameters == {"db": DB, "t": "user_study"}
            return [[self.user_study_uuid]] if "user_study" in self.tables else []
        if "system.tables" in sql:
            if parameters["db"] != DB:
                return self.other_objects.get(parameters["db"], [])
            view = "CREATE VIEW d.v SQL SECURITY INVOKER AS SELECT 1"
            return [
                [n, e, self.creates.get(n, view if e == "View" else "")]
                for n, e in self.tables.items()
            ]
        if "groupUniqArray" in sql:
            kind = "public" if "user_study" in sql else "visible"
            return [[self.counts.get(kind + "_ids", list(range(self.counts[kind])))]]
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
        role_grants={
            ROLE: [
                f"GRANT SELECT ON {DB}.* TO {ROLE}",
                f"GRANT SHOW ROW POLICIES ON {DB}.* TO {ROLE}",
            ],
            "analyst": [f"GRANT SELECT ON {DB}.* TO analyst"],
        },
    )
    refused(fake, "only policy role")


def test_role_granted_but_not_default_refused():
    fake = FakeClickHouse(enabled=[])
    refused(fake, f"policy role {ROLE} is granted to mcp but not a default role")


def test_user_name_that_is_an_email_is_redacted():
    fake = FakeClickHouse(user="Svc@Example.org", enabled=[], grants=[])
    message = refused(fake, "no study row policy applies to <email>")
    assert "Example" not in message


def test_no_policy_for_user_refused():
    fake = FakeClickHouse(
        enabled=[("someone_else", 1, 0)],
        grants=["GRANT someone_else TO mcp"],
        role_grants={"someone_else": []},
    )
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
    refused(fake, "attached to other_role")


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
    fake = FakeClickHouse(role_grants={ROLE: [f"GRANT {privilege} ON {scope} TO {ROLE}"]})
    refused(fake, f"user holds {privilege} ON {scope}")


def test_direct_write_grant_refused_even_for_user_target():
    fake = user_target()
    fake.grants.append(f"GRANT INSERT ON {DB}.user_study TO llm_user")
    refused(fake, "user holds INSERT ON d.user_study")
    fake = user_target()
    fake.grants[-1] = f"GRANT SELECT ON {DB}.* TO llm_user WITH GRANT OPTION"
    refused(fake, "WITH GRANT OPTION")


def test_role_with_admin_option_refused():
    fake = FakeClickHouse(grants=[f"GRANT {ROLE} TO mcp WITH ADMIN OPTION"])
    refused(fake, "WITH ADMIN OPTION")


def test_unreadable_own_grants_refused():
    refused(FakeClickHouse(can_read_grants=False), "cannot audit SHOW GRANTS")


def test_target_role_grant_option_refused():
    fake = FakeClickHouse(
        role_grants={ROLE: [f"GRANT SELECT ON {DB}.* TO {ROLE} WITH GRANT OPTION"]}
    )
    refused(fake, "WITH GRANT OPTION")


def test_user_target_final_grants_from_roles_are_audited():
    fake = user_target()
    fake.grants.append("GRANT analyst TO llm_user")
    fake.role_grants = {"analyst": [f"GRANT SELECT ON {DB}.* TO analyst"]}
    assert check(fake, DB, EMAIL).target_is_user


def test_wildcard_database_grant_refused():
    fake = user_target()
    fake.grants.append("GRANT SELECT ON authz*.* TO llm_user")
    refused(fake, "wildcard database")


def test_custom_setting_rejected_refused_without_echoing_email():
    message = refused(FakeClickHouse(accept_custom_setting=False), "SQL_cbio_user rejected")
    # Driver/server text (which echoes the probe email) is not repeated at all:
    # only the exception class and the ClickHouse error code.
    assert "@" not in message and startup_check.PROBE_DOMAIN not in message
    assert "(ClickHouse code 115)" in message


def test_policies_unreadable_refused():
    refused(FakeClickHouse(can_read_policies=False), "grant SHOW ROW POLICIES ON d.*")


def test_bad_database_name_refused():
    for bad in ("d; DROP", "d\n", "1d"):  # fullmatch: no trailing newline
        with pytest.raises(PolicyCoverageError, match="database must match"):
            check(FakeClickHouse(), bad, EMAIL)


def test_many_failures_summarized_on_one_line():
    privileges = ["INSERT", "ALTER", "DROP TABLE", "TRUNCATE", "dictGet", "SYSTEM"]
    fake = FakeClickHouse(role_grants={ROLE: [f"GRANT {p} ON *.* TO {ROLE}" for p in privileges]})
    message = refused(fake, "more)")
    assert message.count(";") < startup_check.MAX_REASONS


def test_probe_email_is_reserved_and_random():
    a, b = startup_check.probe_email(), startup_check.probe_email()
    assert a != b and a.endswith("@startup-probe.invalid") and a == a.lower()


def test_final_includes_nested_nondefault_grants():
    fake = FakeClickHouse()
    fake.role_grants[ROLE].append("GRANT outer TO " + ROLE)
    fake.role_grants["outer"] = ["GRANT inner TO outer"]
    fake.role_grants["inner"] = ["GRANT SELECT ON d.sample TO inner WITH GRANT OPTION"]
    refused(fake, "WITH GRANT OPTION")


def test_empty_nested_role_graph_passes_with_cycle():
    fake = FakeClickHouse()
    fake.role_grants[ROLE].append("GRANT outer TO " + ROLE)
    fake.role_grants["outer"] = ["GRANT inner TO outer"]
    fake.role_grants["inner"] = ["GRANT outer TO inner"]
    assert check(fake, DB, EMAIL).target == ROLE


def test_user_target_role_on_unrelated_database_with_safe_final_grants_passes():
    fake = user_target()
    fake.grants.append("GRANT extra TO llm_user")
    fake.role_grants = {"extra": ["GRANT SELECT ON other.* TO extra"]}
    assert check(fake, DB, EMAIL).target_is_user


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
    env = {startup_check.FILTERING_ENV: " On ", startup_check.PUBLIC_DATABASES_ENV: ""}
    run_startup_check(DB, env=env, runner=lambda db, **_: calls.append(db))
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


@pytest.mark.parametrize(
    "line",
    [
        "GRANT SELECT ON authz*.* TO mcp",
        "GRANT SELECT ON `authz`*.* TO mcp",
        "GRANT SELECT ON d.sample* TO mcp",
        "GRANT SELECT ON d.`sample`* TO mcp",
    ],
)
def test_wildcard_names_refused(line):
    from cbioportal_mcp.study_authz.grants import HeldGrants, audit_grants, parse_grants

    grants = parse_grants([line], "mcp")
    assert any(
        "wildcard" in r for r in audit_grants(HeldGrants(grants, grants), "mcp", "mcp", True, DB)
    )


@pytest.mark.parametrize(
    "line",
    [
        "REVOKE SELECT ON d.t FROM mcp",
        "GRANT SELECT ON d.* TO mcp garbage",
        "GRANT SELECT ON d.* TO other",
        "GRANT SELECT( ON d.* TO mcp",
        "GRANT SELECT ON d.* TO mcp WITH ADMIN OPTION",
        "GRANT role TO mcp WITH GRANT OPTION",
        "",
        "GRANT SELECT ON d.* TO mcp;",
        "GRANT SELECT ON d.`*` TO mcp",
        "GRANT SELECT ON d.* , TO mcp",
    ],
)
def test_unrecognized_grants_fail_closed(line):
    from cbioportal_mcp.study_authz.grants import GrantParseError, parse_grants

    with pytest.raises(GrantParseError):
        parse_grants([line], "mcp")


def test_probe_same_count_wrong_study_identifiers_refused():
    fake = FakeClickHouse()
    fake.counts["visible_ids"] = ["public_a", "private_b"]
    fake.counts["public_ids"] = ["public_a", "public_c"]
    refused(fake, "study identifiers differ from public studies")


@pytest.mark.parametrize("raw", [None, "a,,b", "a,", "bad-name", "authz*", "a.b"])
def test_public_database_setting_is_required_and_strict(raw):
    env = {} if raw is None else {startup_check.PUBLIC_DATABASES_ENV: raw}
    with pytest.raises(ValueError):
        startup_check.parse_public_databases(env)


@pytest.mark.parametrize("raw,expected", [("", set()), ("a, b", {"a", "b"})])
def test_public_database_setting_accepts_explicit_values(raw, expected):
    assert (
        startup_check.parse_public_databases({startup_check.PUBLIC_DATABASES_ENV: raw}) == expected
    )


def test_filtering_on_requires_public_database_setting_before_connecting(monkeypatch):
    monkeypatch.delenv(startup_check.PUBLIC_DATABASES_ENV)
    with pytest.raises(PolicyCoverageError, match="required and must be explicitly set"):
        run_startup_check(
            DB,
            env={startup_check.FILTERING_ENV: "on"},
            runner=lambda *_a, **_k: pytest.fail("must not connect"),
        )


@pytest.mark.parametrize(
    "engine,create",
    [
        ("View", "CREATE VIEW o.v SQL SECURITY DEFINER AS SELECT 1"),
        ("View", "CREATE VIEW o.v SQL SECURITY NONE AS SELECT 1"),
        ("View", ""),
        ("Dictionary", ""),
        ("Merge", ""),
        ("Merge", "CREATE TABLE o.t ENGINE=Merge('d', 'cancer_study')"),
        ("Distributed", "CREATE TABLE o.t ENGINE=Distributed('c', 'd', 'cancer_study')"),
        ("Buffer", "CREATE TABLE o.t ENGINE=Buffer(d, t, 1,1,1,1,1,1,1)"),
        ("MaterializedView", "CREATE MATERIALIZED VIEW o.t AS SELECT * FROM d.cancer_study"),
        ("MySQL", "CREATE TABLE o.t ENGINE=MySQL('host', 'd', 't', 'u', 'pw')"),
    ],
)
def test_public_declaration_never_overrides_unsafe_objects(engine, create):
    fake = user_target()
    fake.other_objects["other_pub"] = [("t", engine, create)]
    with pytest.raises(PolicyCoverageError):
        check(fake, DB, EMAIL, public_databases=frozenset({"other_pub"}))


def test_unfiltered_study_database_needs_declaration():
    fake = user_target()
    fake.other_objects["other_pub"] = [("cancer_study", "Memory", "CREATE TABLE t ENGINE=Memory")]
    refused(fake, "not declared public")
    assert check(fake, DB, EMAIL, public_databases=frozenset({"other_pub"})).target_is_user


def test_other_study_database_with_applicable_owned_default_deny_passes():
    fake = user_target()
    fake.other_objects["other_pub"] = [("cancer_study", "Memory", "CREATE TABLE t ENGINE=Memory")]
    fake.other_policies["other_pub"] = good_policies("llm_user")
    assert check(fake, DB, EMAIL).target_is_user


@pytest.mark.parametrize("scope", ["system.*", "system.query_log"])
def test_all_system_grants_refused(scope):
    fake = user_target()
    fake.grants.append(f"GRANT SELECT ON {scope} TO llm_user")
    refused(fake, "grants on system are not allowed")


@pytest.mark.parametrize("scope", ["*", "*.*"])
def test_global_grant_reason_is_explicit(scope):
    fake = user_target()
    fake.grants.append(f"GRANT SELECT ON {scope} TO llm_user")
    message = refused(fake, "global grant is not allowed")
    assert "wildcard" not in message


def test_indirect_engine_cannot_name_another_policy_database():
    fake = user_target()
    fake.other_policies["hidden_policy_db"] = good_policies("outsider")
    fake.other_objects["other_pub"] = [
        ("leak", "Distributed", "CREATE TABLE t ENGINE=Distributed('c', 'hidden_policy_db', 't')")
    ]
    with pytest.raises(PolicyCoverageError, match="Distributed engine is not allowed"):
        check(fake, DB, EMAIL, public_databases=frozenset({"other_pub"}))


def test_other_study_database_default_deny_must_apply_to_runtime_user():
    fake = user_target()
    fake.other_objects["other_pub"] = [("cancer_study", "Memory", "CREATE TABLE t ENGINE=Memory")]
    fake.other_policies["other_pub"] = good_policies("outsider")
    refused(fake, "not declared public")


def test_parser_accepts_combined_scopes_and_column_lists():
    from cbioportal_mcp.study_authz.grants import parse_grants

    parsed = parse_grants(["GRANT SELECT(a, b), SHOW ROW POLICIES ON d.*, SELECT ON o.t TO u"], "u")
    assert [(g.database, g.table, g.columns) for g in parsed.grants] == [
        ("d", "*", True),
        ("o", "t", False),
    ]


@pytest.mark.parametrize(
    "engine",
    [
        "MergeTree",
        "ReplacingMergeTree",
        "CoalescingMergeTree",
        "ReplicatedMergeTree",
        "ReplicatedReplacingMergeTree",
        "SharedMergeTree",
        "SharedReplacingMergeTree",
        "SharedAggregatingMergeTree",
        "Memory",
        "Log",
        "TinyLog",
        "StripeLog",
        "Null",
        "Set",
        "Join",
    ],
)
def test_plain_storage_engine_allow_list_accepts_cloud_variants(engine):
    fake = user_target()
    fake.other_objects["other_pub"] = [("t", engine, "")]
    assert check(fake, DB, EMAIL).target_is_user


@pytest.mark.parametrize(
    "engine",
    [
        "URL",
        "MySQL",
        "PostgreSQL",
        "S3",
        "File",
        "Distributed",
        "Merge",
        "Buffer",
        "MaterializedView",
        "Dictionary",
        "UnknownMergeTree",
    ],
)
@pytest.mark.parametrize("listed", [False, True])
def test_non_plain_engines_always_refused_without_reading_definition(engine, listed):
    fake = user_target()
    fake.other_objects["other_pub"] = [("t", engine, "no policy database name here")]
    with pytest.raises(PolicyCoverageError, match="engine is not allowed"):
        check(fake, DB, EMAIL, public_databases=frozenset({"other_pub"}) if listed else frozenset())


@pytest.mark.parametrize("name", ["sample_derived", "mutation", "gene", "user_study"])
def test_every_catalog_classified_table_marks_another_database_as_study_data(name):
    fake = user_target()
    fake.other_objects["other_pub"] = [(name, "Memory", "")]
    refused(fake, "not declared public")


@pytest.mark.parametrize(
    "column", ["cancer_study_identifier", "cancer_study_id", "genetic_profile_id"]
)
def test_renamed_study_tables_detected_by_columns(column):
    from cbioportal_mcp.study_authz.grants import STUDY_LINK_COLUMNS

    assert column in STUDY_LINK_COLUMNS
    fake = user_target()
    fake.other_objects["other_pub"] = [("renamed", "Memory", "")]
    fake.other_study_columns["other_pub"] = ["renamed"]
    refused(fake, "not declared public")
    assert check(fake, DB, EMAIL, public_databases=frozenset({"other_pub"})).target_is_user


@pytest.mark.parametrize("scope", ["d.*", "other_pub.*", "other_pub.zz"])
@pytest.mark.parametrize("privilege", ["SELECT", "SELECT(sample_unique_id)"])
def test_grants_must_be_database_wide_without_column_lists(scope, privilege):
    from cbioportal_mcp.study_authz.grants import HeldGrants, audit_grants, parse_grants

    parsed = parse_grants([f"GRANT {privilege} ON {scope} TO llm_user"], "llm_user")
    reasons = audit_grants(HeldGrants(parsed, parsed), "llm_user", "llm_user", True, DB)
    if scope.endswith(".*") and privilege == "SELECT":
        assert reasons == []
    else:
        assert any("table/column-scoped grants" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("create", "needle"),
    [
        (
            f"CREATE MATERIALIZED VIEW {DB}.user_study REFRESH EVERY 5 MINUTE TO {DB}.data "
            "(`email` String) DEFINER = admin SQL SECURITY DEFINER AS SELECT '' AS email",
            "user_study: refreshable view writes TO another table",
        ),
        (
            f"CREATE MATERIALIZED VIEW {DB}.user_study REFRESH EVERY 5 MINUTE APPEND "
            "(`email` String) ENGINE = MergeTree ORDER BY email AS SELECT '' AS email",
            "user_study: refreshable view uses APPEND",
        ),
        (
            f"CREATE MATERIALIZED VIEW {DB}.user_study REFRESH EVERY 5 MINUTE APPEND TO {DB}.data "
            "(`email` String) AS SELECT '' AS email",
            "user_study: refreshable view uses APPEND",
        ),
        (
            f"CREATE MATERIALIZED VIEW {DB}.user_study (`email` String) ENGINE = MergeTree "
            "ORDER BY email AS SELECT '' AS email",
            "user_study: materialized view is not refreshable",
        ),
    ],
)
def test_user_study_view_must_be_refreshable_without_to_or_append(create, needle):
    fake = FakeClickHouse()
    fake.creates["user_study"] = create
    refused(fake, needle)


def test_previous_user_study_layout_refused():
    fake = FakeClickHouse()
    fake.tables["user_study"] = "MergeTree"
    fake.tables.pop(f".inner_id.{USER_STUDY_UUID}")
    fake.tables["user_study_refresh"] = "MaterializedView"
    fake.creates["user_study_refresh"] = (
        f"CREATE MATERIALIZED VIEW {DB}.user_study_refresh REFRESH EVERY 5 MINUTE "
        f"TO {DB}.user_study (`email` String) AS SELECT '' AS email"
    )
    message = refused(fake, "user_study: MergeTree table, not a refreshable materialized view")
    assert "user_study_refresh: MaterializedView bypasses row policies" in message


@pytest.mark.parametrize("prefix", [".inner_id.", ".tmp.inner_id."])
def test_user_study_inner_tables_must_stay_denied(prefix):
    inner = f"{prefix}{USER_STUDY_UUID}"
    fake = FakeClickHouse()
    fake.tables[inner] = "MergeTree"
    assert check(fake, DB, EMAIL).target == ROLE
    fake.policies.append(("cbioportal_mcp_x", inner, "1", 0, False, (ROLE,), ()))
    refused(fake, f"{inner}: user_study's inner table has policies")


def test_inner_table_of_another_view_is_unclassified():
    fake = FakeClickHouse()
    fake.tables[".inner_id.00000000-1111-4000-8000-000000000000"] = "MergeTree"
    refused(fake, "unclassified table/view")


def test_other_materialized_view_still_refused_next_to_user_study():
    fake = FakeClickHouse()
    fake.tables["shadow"] = "MaterializedView"
    fake.creates["shadow"] = USER_STUDY_CREATE.replace("user_study", "shadow")
    refused(fake, "shadow: MaterializedView bypasses row policies")
