"""Unit tests for the study-authz generator, groups rule and coverage checker.

The row-level behavior is proven against a real ClickHouse in
tests/test_study_authz_live.py.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cbioportal_mcp.study_authz import catalog, sqlgen
from cbioportal_mcp.study_authz.coverage import Policy, evaluate, user_study_shape_error
from cbioportal_mcp.study_authz.groups import (
    grant_key_matches,
    group_entries,
    is_public,
    normalize_authority,
)
from cbioportal_mcp.study_authz.testdb import _rewrite_view, read_grants_csv

REPO = Path(__file__).resolve().parent.parent
PARAMS = sqlgen.Params(database="cbio_green", public_group="PUBLIC", app_name="cbioportal")


def test_generated_policy_template_is_up_to_date():
    assert sqlgen.ROW_POLICIES_TEMPLATE.read_text() == sqlgen.row_policies_template(), (
        "run: uv run python -m cbioportal_mcp.study_authz generate"
    )


def test_catalog_counts_and_buckets_are_disjoint():
    assert len(catalog.STUDY_SCOPED_TABLE_PATHS) == 45
    assert len(catalog.STUDY_SCOPED_VIEWS) == 21
    buckets = [
        set(catalog.STUDY_SCOPED_TABLE_PATHS),
        set(catalog.STUDY_AGNOSTIC_REFERENCE_TABLES),
        set(catalog.DENIED_TABLES),
        set(catalog.STUDY_SCOPED_VIEWS),
        set(catalog.IDENTITY_SCOPED_TABLES),
    ]
    assert sum(map(len, buckets)) == len(catalog.classified_names())


def test_every_view_and_table_created_by_sql_dir_is_classified():
    created = set()
    for path in (REPO / "sql").rglob("*.sql"):
        text = path.read_text()
        created |= set(re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:VIEW|TABLE)\s+(\w+)", text))
    created -= {"cancer_study_with_counts"}  # sql/6 staging table, dropped after EXCHANGE
    assert created - catalog.classified_names() == set()


def test_every_object_gets_exactly_the_expected_policy():
    sql = sqlgen.row_policies_template()
    names = re.findall(
        r"CREATE ROW POLICY OR REPLACE (\w+)\s+ON __CLICKHOUSE_DATABASE__\.(\S+)", sql
    )
    on = {table: name for name, table in names}
    assert len(names) == len(on) + 1  # '*' carries default-deny and unrestricted-others
    expected = (
        set(catalog.STUDY_SCOPED_TABLE_PATHS)
        | catalog.STUDY_AGNOSTIC_REFERENCE_TABLES
        | catalog.STUDY_SCOPED_VIEWS
        | catalog.IDENTITY_SCOPED_TABLES
        | {"*"}
    )
    assert set(on) == expected
    assert not set(on) & catalog.DENIED_TABLES


@pytest.mark.parametrize("table", sorted(catalog.STUDY_SCOPED_TABLE_PATHS))
def test_study_policies_read_user_study_with_the_agreed_predicate(table):
    condition = sqlgen.study_condition(table)
    assert "__CLICKHOUSE_DATABASE__.user_study\n" in condition
    assert sqlgen.USER_STUDY_MATCH == (
        "(email = '' OR email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', ''))) "
        "AND valid_until > now64(3)"
    )
    assert condition.count(sqlgen.USER_STUDY_MATCH) == condition.count(
        "FROM __CLICKHOUSE_DATABASE__.user_study"
    )
    # Every table reference is qualified with the database placeholder.
    assert not re.search(r"FROM (?!__CLICKHOUSE_DATABASE__\.)", condition)


def test_grant_comes_after_every_policy():
    sql = sqlgen.row_policies_template()
    assert sql.rindex("CREATE ROW POLICY") < sql.index("GRANT SHOW ROW POLICIES ON")
    assert sql.rindex("CREATE ROW POLICY") < sql.index("GRANT SELECT ON")
    # SELECT plus SHOW ROW POLICIES (for the server's startup check), nothing else.
    assert sql.count("GRANT ") == 2


def test_render_fills_every_placeholder_and_statements_split():
    files = dict(sqlgen.rendered_files(PARAMS))
    user_study = files["0-user-study.sql.tmpl"]
    assert "user_study_refresh" not in sqlgen.statements(user_study)[0]
    assert "upperUTF8('PUBLIC')" in user_study and "app_name=cbioportal" in user_study
    assert "REFRESH EVERY 5 MINUTE" in user_study and "toIntervalMinute(15)" in user_study
    stmts = sqlgen.statements(user_study)
    assert [" ".join(s.split()[:6]) for s in stmts] == [
        "CREATE TABLE IF NOT EXISTS `cbio_green`.user_study_manual",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS",
        "ALTER TABLE `cbio_green`.user_study MODIFY QUERY WITH",
        "ALTER TABLE `cbio_green`.user_study MODIFY REFRESH EVERY",
        "ALTER TABLE `cbio_green`.user_study MODIFY SQL SECURITY",
        "ALTER TABLE `cbio_green`.user_study MODIFY COMMENT 'study-authz:",
        "SYSTEM REFRESH VIEW `cbio_green`.user_study",
        "SYSTEM WAIT VIEW `cbio_green`.user_study",
    ]
    # Nothing drops user_study or recreates it: re-apply keeps the snapshot.
    assert not any(re.match(r"(DROP|CREATE OR REPLACE)\b", s) for s in stmts)
    create, modify = stmts[1], stmts[2]
    assert create.startswith("CREATE MATERIALIZED VIEW IF NOT EXISTS `cbio_green`.user_study\n")
    # Refreshable, implicit inner table (no TO), not APPEND, definer kept.
    head = create.split("\nAS\n", 1)[0]
    assert "REFRESH EVERY 5 MINUTE" in head and "ENGINE = MergeTree" in head
    assert not re.search(r"\b(TO|APPEND)\b", head)
    assert "DEFINER = CURRENT_USER SQL SECURITY DEFINER" in head
    assert user_study_shape_error("MaterializedView", create) is None
    # The CREATE and the re-apply ALTER run the very same query.
    assert create.split("\nAS\n", 1)[1] == modify.split("MODIFY QUERY\n", 1)[1]
    assert "FROM `cbio_green`.user_study_manual" in modify
    policies = sqlgen.statements(files["1-row-policies.sql.tmpl"])
    assert sum(s.startswith("CREATE ROW POLICY") for s in policies) == 2 + 1 + 45 + 17 + 21


@pytest.mark.parametrize(
    "kwargs",
    [
        {"public_group": ""},
        {"public_group": "PUB';DROP"},
        {"app_name": ""},
        {"database": "x.y"},
        {"refresh_minutes": 0},
        {"refresh_minutes": 10, "private_grace_minutes": 10},
    ],
)
def test_params_are_required_and_validated(kwargs):
    base = {"database": "db", "public_group": "PUBLIC", "app_name": "cbioportal"}
    with pytest.raises(ValueError):
        sqlgen.Params(**{**base, **kwargs})


def test_public_group_and_app_name_have_no_default():
    with pytest.raises(TypeError):
        sqlgen.Params(database="db")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("groups", "public"),
    [
        ("PUBLIC", True),
        ("public", True),
        ("A;PUBLIC;B", True),
        (";;PUBLIC;", True),
        (" PUBLIC", False),  # no trimming
        ("PUBLIC ", False),
        ("PUBLICX", False),  # whole entry, no LIKE
        ("XPUBLIC;NOTPUBLIC", False),
        ("", False),
        (None, False),
    ],
)
def test_groups_rule(groups, public):
    assert is_public(groups, "PUBLIC") is public


def test_group_entries_drop_empty_only():
    assert group_entries("a;;B; c;") == ["A", "B", " C"]


@pytest.mark.parametrize(
    ("authority", "filter_by_app", "key"),
    [
        ("cbioportal:GRP1", True, "GRP1"),
        ("ROLE_cbioportal:grp1", True, "GRP1"),
        ("CBIOPORTAL:study_a", True, "STUDY_A"),
        ("otherapp:GRP1", True, None),
        ("GRP1", True, None),
        ("GRP1", False, "GRP1"),
        ("ROLE_GRP1", False, "GRP1"),
        ("cbioportal:", True, None),
    ],
)
def test_normalize_authority(authority, filter_by_app, key):
    assert normalize_authority(authority, "cbioportal", filter_by_app) == key


@pytest.mark.parametrize(
    ("key", "study", "groups", "match"),
    [
        ("study_a", "STUDY_A", "", True),
        ("grp1", "s", "X;GRP1", True),
        ("GRP", "s", "GRP1", False),
        ("ALL", "anything", None, True),
        ("all_tcga", "brca_tcga", "", True),
        ("ALL_TCGA", "brca_tcga_pan_can", "", False),
        ("ALL_NCI_TARGET", "aml_target", "", True),
        ("ALL_NCI_TARGET", "all_target_phase2", "", True),
        ("ALL_NCI_TARGET", "brca_tcga", "", False),
        ("", "s", ";", False),
        ("X", "s", "PUBLIC; X", False),  # no trimming: ' X' is not X
        ("x", "s", "PUBLIC;X", True),
    ],
)
def test_grant_key_matches(key, study, groups, match):
    assert grant_key_matches(key, study, groups) is match


def test_restricted_user_sql_grants_role_only():
    sql = sqlgen.restricted_user_sql("mcp_ro", "s3cret", "cbio_green")
    assert "s3cret" not in sql and "sha256_hash" in sql
    assert sql.startswith("CREATE USER OR REPLACE `mcp_ro`")
    assert "GRANT `cbioportal_mcp_study_restricted` TO `mcp_ro`;" in sql
    assert "GRANT SELECT" not in sql and "PROFILE" not in sql and "readonly" not in sql


def test_restricted_user_sql_honours_role_and_user_target():
    sql = sqlgen.restricted_user_sql("mcp_ro", "pw", "db", role="team_role")
    assert "GRANT `team_role` TO `mcp_ro`;" in sql
    assert "SET DEFAULT ROLE `team_role` TO `mcp_ro`;" in sql
    assert "cbioportal_mcp_study_restricted" not in sql
    sql = sqlgen.restricted_user_sql("llm_user", "pw", "db", role=None)
    assert sql.startswith("CREATE USER `llm_user`")  # never OR REPLACE an existing user
    assert "GRANT" not in sql and "ROLE" not in sql


def test_user_sql_cli_honours_policy_role(monkeypatch, capsys):
    from cbioportal_mcp.study_authz.__main__ import main

    monkeypatch.setenv("CBIOPORTAL_MCP_RESTRICTED_PASSWORD", "pw")
    base = ["user-sql", "--user", "u1", "--database", "db"]
    assert main(base + ["--policy-role", "team_role"]) == 0
    assert "GRANT `team_role` TO `u1`;" in capsys.readouterr().out
    assert main(base + ["--user-target"]) == 0
    out = capsys.readouterr().out
    assert "GRANT" not in out and "ROLE" not in out
    assert main(base) == 0
    assert "GRANT `cbioportal_mcp_study_restricted` TO `u1`;" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(base + ["--policy-role", "r", "--user-target"])


HOSTILE = [
    "authz_live.cancer_study WHERE 0 UNION ALL SELECT 'pub_a' --",
    "x`; DROP DATABASE y; --",
    "a.b",
    "a b",
    "a'b",
    "",
    "1abc",
    "x" * 200,
]


@pytest.mark.parametrize("value", HOSTILE)
@pytest.mark.parametrize("field", ["database", "policy_target"])
def test_hostile_identifiers_rejected_by_params(field, value):
    base = {"database": "db", "public_group": "PUBLIC", "app_name": "cbioportal"}
    with pytest.raises(ValueError, match=field.replace("_", " ")):
        sqlgen.Params(**{**base, field: value})


# Values, not identifiers: dots and leading digits are legitimate group names.
HOSTILE_VALUES = [v for v in HOSTILE if v not in ("a.b", "1abc")]


@pytest.mark.parametrize("value", HOSTILE_VALUES + ["PUB'LIC", "PUB\\LIC", "PUB;LIC"])
@pytest.mark.parametrize("field", ["public_group", "app_name"])
def test_hostile_values_rejected_by_params(field, value):
    base = {"database": "db", "public_group": "PUBLIC", "app_name": "cbioportal"}
    with pytest.raises(ValueError):
        sqlgen.Params(**{**base, field: value})


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_identifiers_rejected_by_user_sql(value):
    for kwargs in (
        {"user": value, "database": "db"},
        {"user": "u", "database": value},
        {"user": "u", "database": "db", "role": value},
    ):
        with pytest.raises(ValueError):
            sqlgen.restricted_user_sql(password="pw", **kwargs)


class _RecordingClient:
    def __init__(self):
        self.calls = []

    def query(self, *a, **k):
        self.calls.append(a)
        raise AssertionError("no query may run with a hostile identifier")

    command = query


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_source_database_rejected_before_any_query(value):
    from cbioportal_mcp.study_authz.testdb import make_test_db

    client = _RecordingClient()
    with pytest.raises(ValueError, match="source database"):
        make_test_db(client, value, PARAMS, {"s1": "G"}, ["s2"], [], log=lambda *_: None)
    assert client.calls == []


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_identifiers_rejected_by_check_and_conflicts(value):
    from cbioportal_mcp.study_authz.coverage import fetch_and_evaluate

    for db, target in ((value, "r"), ("db", value)):
        client = _RecordingClient()
        with pytest.raises(ValueError):
            fetch_and_evaluate(client, db, target)
        with pytest.raises(ValueError):
            sqlgen.policy_conflicts(client, db, target)
        assert client.calls == []


@pytest.mark.parametrize("value", ["G'1", "G1') OR 1 --", "PUBLIC\\"])
def test_hostile_test_db_values_rejected(value):
    from cbioportal_mcp.study_authz.testdb import make_test_db

    for gated, public in (({value: "G"}, ["s2"]), ({"s1": value}, ["s2"]), ({"s1": "G"}, [value])):
        with pytest.raises(ValueError):
            make_test_db(_RecordingClient(), "src", PARAMS, gated, public, [], log=lambda *_: None)


def test_string_values_are_escaped_and_identifiers_quoted():
    assert sqlgen.sql_string("a'b\\c") == "'a\\'b\\\\c'"
    assert sqlgen.quote_identifier("database", "cbio_green") == "`cbio_green`"
    rendered = sqlgen.render("upperUTF8('__PUBLIC_GROUP__') FROM __CLICKHOUSE_DATABASE__.t", PARAMS)
    assert rendered == "upperUTF8('PUBLIC') FROM `cbio_green`.t"


class _PolicyClient:
    def __init__(self, rows):
        self.rows = rows

    def query(self, sql, parameters=None):
        assert "system.row_policies" in sql and set(parameters) == {"db"}
        return type("R", (), {"result_rows": self.rows})()


def test_policy_conflicts_flags_foreign_and_retargeted_policies():
    ours = [
        ("cbioportal_mcp_default_deny", "", False, ["llm_user"], []),
        ("cbioportal_mcp_unrestricted_others", "", True, [], ["llm_user"]),
        ("cbioportal_mcp_study_patient", "patient", False, ["llm_user"], []),
    ]
    assert sqlgen.policy_conflicts(_PolicyClient(ours), "db", "llm_user") == []
    foreign = ours + [("outsider_only", "cancer_study", False, ["outsider"], [])]
    found = sqlgen.policy_conflicts(_PolicyClient(foreign), "db", "llm_user")
    assert found == ["outsider_only ON db.cancer_study (not created by study_authz)"]
    # Ours, but for another target: switching target would expose the old one.
    found = sqlgen.policy_conflicts(_PolicyClient(ours), "db", "cbioportal_mcp_study_restricted")
    assert len(found) == 1 and found[0].startswith("3 study_authz policies on db")


def test_apply_refuses_on_conflict_without_running_anything():
    rows = [("someone_elses", "gene", False, ["u"], [])]

    class Client(_PolicyClient):
        def command(self, sql):
            raise AssertionError("apply must not change anything")

    with pytest.raises(ValueError, match="someone_elses ON cbio_green.gene"):
        sqlgen.apply(Client(rows), PARAMS, log=lambda *_: None)


class _ApplyClient:
    """No foreign policies; ``tables`` answers apply's legacy-layout lookup."""

    def __init__(self, tables):
        self.tables = tables
        self.commands = []

    def query(self, sql, parameters=None):
        if "system.row_policies" in sql:
            rows = []
        elif "system.tables" in sql:
            assert set(parameters["names"]) == {"user_study", "user_study_refresh"}
            rows = [[n, e, c] for n, (e, c) in self.tables.items() if n in parameters["names"]]
        else:
            raise AssertionError(sql)
        return type("R", (), {"result_rows": rows})()

    def command(self, sql):
        self.commands.append(sql)


_LEGACY = {
    "user_study": ("MergeTree", "CREATE TABLE d.user_study (`email` String) ENGINE = MergeTree"),
    "user_study_refresh": (
        "MaterializedView",
        "CREATE MATERIALIZED VIEW d.user_study_refresh REFRESH EVERY 5 MINUTE TO d.user_study "
        "(`email` String) AS SELECT '' AS email",
    ),
}


@pytest.mark.parametrize(
    "tables",
    [
        _LEGACY,
        {"user_study": _LEGACY["user_study"]},  # refresh view already dropped
        {"user_study_refresh": _LEGACY["user_study_refresh"]},  # table already dropped
        {
            "user_study": (
                "MaterializedView",
                "CREATE MATERIALIZED VIEW d.user_study REFRESH EVERY 5 MINUTE TO d.x "
                "(`email` String) AS SELECT 1",
            )
        },
    ],
)
def test_apply_refuses_previous_layout_without_migrate(tables):
    client = _ApplyClient(tables)
    with pytest.raises(ValueError) as exc:
        sqlgen.apply(client, PARAMS, log=lambda *_: None)
    message = str(exc.value)
    assert "\n" not in message and "previous user_study layout" in message
    assert "--migrate" in message and "inactive" in message
    assert client.commands == []


def test_apply_migrate_drops_previous_layout_then_creates_the_view():
    client = _ApplyClient(dict(_LEGACY))
    sqlgen.apply(client, PARAMS, log=lambda *_: None, migrate=True)
    assert client.commands[:3] == [
        "DROP VIEW IF EXISTS `cbio_green`.user_study_refresh SYNC",
        "DROP TABLE IF EXISTS `cbio_green`.user_study SYNC",
        sqlgen.statements(dict(sqlgen.rendered_files(PARAMS))["0-user-study.sql.tmpl"])[0],
    ]
    assert client.commands[3].startswith("CREATE MATERIALIZED VIEW IF NOT EXISTS")
    assert "SYSTEM WAIT VIEW `cbio_green`.user_study" in client.commands
    dropped = [c for c in client.commands if c.startswith("DROP")]
    assert len(dropped) == 2 and not any("user_study_manual" in c for c in dropped)


@pytest.mark.parametrize("migrate", [False, True])
def test_apply_on_the_current_layout_drops_nothing(migrate):
    tables = {"user_study": ("MaterializedView", _USER_STUDY_CREATE)}
    for current in [tables, {}]:  # existing view, or a fresh database
        client = _ApplyClient(current)
        sqlgen.apply(client, PARAMS, log=lambda *_: None, migrate=migrate)
        assert not any(c.startswith("DROP") for c in client.commands)


def test_apply_cli_refusal_is_one_line(monkeypatch, capsys):
    from cbioportal_mcp.study_authz import __main__ as cli

    client = _ApplyClient(dict(_LEGACY))
    monkeypatch.setattr(cli, "_client", lambda: client)
    args = ["apply", "--database", "cbio_green", "--public-group", "P", "--app-name", "a"]
    with pytest.raises(SystemExit, match="^error: refusing .*--migrate"):
        cli.main(args)
    assert client.commands == []
    assert cli.main(args + ["--migrate"]) == 0
    assert client.commands[0].startswith("DROP VIEW IF EXISTS `cbio_green`.user_study_refresh")


def test_coverage_reports_conflicts():
    objects, policies = _good_objects_and_policies()
    report = _evaluate(objects, policies, conflicts=["x ON d.gene (not created by study_authz)"])
    assert not report.ok and any("x ON d.gene" in e for e in report.errors)


def _good_objects_and_policies(db="d"):
    objects = {t: ("MergeTree", "") for t in ["cancer_study", "gene", "users"]}
    objects["user_study"] = ("MaterializedView", _USER_STUDY_CREATE)
    objects[f".inner_id.{_UUID}"] = ("MergeTree", "")
    objects["top_mutated_genes_in_study"] = ("View", "CREATE VIEW d.v SQL SECURITY INVOKER AS")
    policies = [
        Policy("deny", "", "0"),
        Policy("id", "user_study", sqlgen.USER_STUDY_MATCH),
        Policy("s", "cancer_study", sqlgen.study_condition("cancer_study").replace(sqlgen.DB, db)),
        Policy("r", "gene", "1"),
        Policy("v", "top_mutated_genes_in_study", "1"),
    ]
    return objects, policies


_UUID = "6f1c1a52-0000-4000-8000-0000000000aa"
# As system.tables stores the rendered CREATE (25.8 / 26.6).
_USER_STUDY_CREATE = (
    "CREATE MATERIALIZED VIEW d.user_study REFRESH EVERY 5 MINUTE (`email` String COMMENT "
    "'x', `source` LowCardinality(String)) ENGINE = MergeTree ORDER BY (email, "
    "cancer_study_identifier) SETTINGS index_granularity = 8192 DEFINER = admin SQL SECURITY "
    "DEFINER COMMENT 'study-authz: TO APPEND' AS WITH now64(3) AS t SELECT '' AS email "
    "FROM d.cancer_study"
)
_TO_FORM = (
    "CREATE MATERIALIZED VIEW d.user_study REFRESH EVERY 5 MINUTE TO d.user_study_data "
    "(`email` String) DEFINER = admin SQL SECURITY DEFINER AS SELECT '' AS email"
)
_APPEND_FORM = (
    "CREATE MATERIALIZED VIEW d.user_study REFRESH EVERY 5 MINUTE APPEND (`email` String) "
    "ENGINE = MergeTree ORDER BY email DEFINER = admin SQL SECURITY DEFINER AS SELECT '' AS email"
)
_APPEND_TO_FORM = (
    "CREATE MATERIALIZED VIEW d.user_study REFRESH EVERY 5 MINUTE APPEND TO d.t "
    "(`email` String) AS SELECT '' AS email"
)
_NOT_REFRESHABLE = (
    "CREATE MATERIALIZED VIEW d.user_study (`email` String) ENGINE = MergeTree ORDER BY email "
    "AS SELECT '' AS email FROM d.cancer_study"
)


def _evaluate(objects, policies, **kw):
    return evaluate(objects, policies, "d", user_study_uuid=_UUID, **kw)


def test_coverage_accepts_the_generated_shape():
    objects, policies = _good_objects_and_policies()
    report = _evaluate(objects, policies)
    assert report.ok, report
    # The refresh's staging copy appears while a refresh runs.
    objects[f".tmp.inner_id.{_UUID}"] = ("MergeTree", "")
    assert _evaluate(objects, policies).ok


def test_coverage_accepts_the_rendered_template_shape():
    stmt = sqlgen.statements(dict(sqlgen.rendered_files(PARAMS))["0-user-study.sql.tmpl"])[1]
    objects, policies = _good_objects_and_policies()
    objects["user_study"] = ("MaterializedView", stmt)
    assert _evaluate(objects, policies).ok


@pytest.mark.parametrize(
    ("engine", "create", "needle"),
    [
        ("MaterializedView", _TO_FORM, "writes TO another table"),
        ("MaterializedView", _APPEND_FORM, "APPEND"),
        ("MaterializedView", _APPEND_TO_FORM, "APPEND"),
        ("MaterializedView", _NOT_REFRESHABLE, "not refreshable"),
        ("MaterializedView", "CREATE MATERIALIZED VIEW d.user_study 'unterminated", "unreadable"),
        ("MergeTree", "", "previous layout"),  # the old plain table
        ("View", "CREATE VIEW d.user_study SQL SECURITY INVOKER AS SELECT 1", "not a refreshable"),
    ],
)
def test_coverage_rejects_any_other_user_study_shape(engine, create, needle):
    assert needle in (user_study_shape_error(engine, create) or "")
    objects, policies = _good_objects_and_policies()
    objects["user_study"] = (engine, create)
    report = _evaluate(objects, policies)
    assert any(e.startswith("user_study:") and needle in e for e in report.errors), report.errors


@pytest.mark.parametrize(
    "create",
    [_USER_STUDY_CREATE.replace("user_study", "other_mv"), _TO_FORM, _APPEND_FORM],
)
def test_materialized_views_other_than_user_study_still_bypass(create):
    objects, policies = _good_objects_and_policies()
    objects["other_mv"] = ("MaterializedView", create)
    objects["user_study_refresh"] = ("MaterializedView", _TO_FORM)  # previous layout's writer
    report = _evaluate(objects, policies)
    for name in ["other_mv", "user_study_refresh"]:
        assert f"{name}: MaterializedView bypasses row policies; remove it" in report.errors


@pytest.mark.parametrize("prefix", [".inner_id.", ".tmp.inner_id."])
def test_user_study_inner_tables_are_denied(prefix):
    objects, policies = _good_objects_and_policies()
    inner = f"{prefix}{_UUID}"
    objects[inner] = ("MergeTree", "")
    assert _evaluate(objects, policies).ok
    # A policy would expose every caller's grants, skipping the identity policy.
    report = _evaluate(objects, policies + [Policy("leak", inner, "1")])
    assert any(
        e.startswith(f"{inner}: user_study's inner table has policies") for e in report.errors
    )
    objects[inner] = ("Dictionary", "")
    assert any("not MergeTree" in e for e in _evaluate(objects, policies).errors)
    # Another view's inner table (other uuid) is not classified.
    other = f"{prefix}00000000-1111-4000-8000-000000000000"
    objects = dict(_good_objects_and_policies()[0], **{other: ("MergeTree", "")})
    assert _evaluate(objects, policies).unclassified == [other]
    # Without the uuid nothing is classified as inner (fail closed).
    assert evaluate(objects | {inner: ("MergeTree", "")}, policies, "d").unclassified


def test_refresh_error_is_reported_on_user_study():
    objects, policies = _good_objects_and_policies()
    report = _evaluate(objects, policies, refresh_error="last refresh failed: boom")
    assert report.errors == ["user_study: last refresh failed: boom"]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda o, p: p.pop(0), "default-deny"),
        (lambda o, p: p.append(Policy("open", "", "1")), "not USING 0"),
        (lambda o, p: p.__setitem__(2, Policy("s", "cancer_study", "1")), "USING 1"),
        (lambda o, p: p.__setitem__(1, Policy("id", "user_study", "1")), "exactly"),
        (lambda o, p: p.pop(4), "no pass-through"),
        (lambda o, p: p.append(Policy("x", "users", "1")), "denied object"),
        (
            lambda o, p: o.__setitem__(
                "top_mutated_genes_in_study", ("View", "SQL SECURITY DEFINER")
            ),
            "DEFINER",
        ),
        (lambda o, p: o.__setitem__("dict", ("Dictionary", "")), "bypasses"),
        (lambda o, p: p.append(Policy("f", "gene", "x IN (SELECT 1 FROM other.t)")), "other"),
        (lambda o, p: o.pop("user_study"), "user_study is missing"),
    ],
)
def test_coverage_flags_problems(mutate, needle):
    objects, policies = _good_objects_and_policies()
    mutate(objects, policies)
    report = _evaluate(objects, policies)
    assert any(needle in e for e in report.errors), report.errors


def test_coverage_fails_on_unclassified():
    objects, policies = _good_objects_and_policies()
    objects["new_table"] = ("MergeTree", "")
    report = _evaluate(objects, policies)
    assert report.unclassified == ["new_table"] and not report.ok


def test_grants_csv(tmp_path):
    path = tmp_path / "grants.csv"
    path.write_text(
        "email,grant_key,valid_until\n"
        "Ann@Example.org,TEAM_A,2030-01-01T00:00:00Z\n"
        "ben@example.org,study_b,\n"
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    grants = read_grants_csv(path, now=now)
    assert grants[0].email == "ann@example.org"
    assert grants[0].valid_until == datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert grants[1].valid_until == datetime(2026, 1, 8, tzinfo=timezone.utc)
    path.write_text("email,grant_key\n,TEAM_A\n")
    with pytest.raises(ValueError):
        read_grants_csv(path)


def test_view_rewrite_moves_every_reference():
    q = "CREATE VIEW src.v SQL SECURITY INVOKER AS SELECT 1 FROM src.a JOIN `src`.b USING x"
    assert _rewrite_view(q, "src", "tgt") == (
        "CREATE VIEW `tgt`.v SQL SECURITY INVOKER AS SELECT 1 FROM `tgt`.a JOIN `tgt`.b USING x"
    )


def test_cli_refuses_public_looking_database(capsys):
    from cbioportal_mcp.study_authz.__main__ import main

    with pytest.raises(SystemExit, match="public clone"):
        main(
            [
                "render",
                "--database",
                "cbioportal_public_librechat_blue",
                "--public-group",
                "PUBLIC",
                "--app-name",
                "cbioportal",
            ]
        )
    assert (
        main(
            [
                "render",
                "--database",
                "cbio_restricted_blue",
                "--public-group",
                "PUBLIC",
                "--app-name",
                "cbioportal",
            ]
        )
        == 0
    )
    assert "`cbio_restricted_blue`.user_study" in capsys.readouterr().out


def test_nothing_under_sql_dir_or_apply_sql_references_study_authz():
    # The daily public clone applies sql/ with scripts/apply_sql.sh; filtering
    # must stay off there, so none of this may be reachable from that path.
    assert not list((REPO / "sql").rglob("*.tmpl"))
    assert "study_authz" not in (REPO / "scripts" / "apply_sql.sh").read_text()


def test_policy_target_role_creates_the_role_first():
    policies = dict(sqlgen.rendered_files(PARAMS))["1-row-policies.sql.tmpl"]
    stmts = sqlgen.statements(policies)
    assert stmts[0] == "CREATE ROLE IF NOT EXISTS `cbioportal_mcp_study_restricted`"
    assert "TO ALL EXCEPT `cbioportal_mcp_study_restricted`" in policies


def test_policy_target_user_attaches_every_policy_to_that_user():
    params = sqlgen.Params(
        database="authz_e2e_test",
        public_group="PUBLIC",
        app_name="cbioportal",
        policy_target="llm_user",
        target_is_user=True,
    )
    policies = dict(sqlgen.rendered_files(params))["1-row-policies.sql.tmpl"]
    stmts = sqlgen.statements(policies)
    assert not any("ROLE" in s.split("\n")[0] for s in stmts)
    created = [s for s in stmts if s.startswith("CREATE ROW POLICY")]
    assert len(created) == 2 + 1 + 45 + 17 + 21
    targets = {re.split(r"\sTO\s", s)[-1] for s in created}
    assert targets == {"`llm_user`", "ALL EXCEPT `llm_user`"}
    assert stmts[-1] == "GRANT SELECT ON `authz_e2e_test`.* TO `llm_user`"
    identity = next(s for s in created if "cbioportal_mcp_identity_user_study" in s)
    assert sqlgen.USER_STUDY_MATCH in identity and identity.endswith("TO `llm_user`")
    on_tables = {re.search(r"\bON `authz_e2e_test`\.(\S+)", s).group(1) for s in created}
    assert "user_study_manual" not in on_tables  # denied by the default-deny


def test_policy_target_validated_and_cli_flags_exclusive():
    with pytest.raises(ValueError):
        sqlgen.Params(database="d", public_group="P", app_name="a", policy_target="x;y")
    from cbioportal_mcp.study_authz.__main__ import main

    base = ["render", "--database", "d", "--public-group", "P", "--app-name", "a"]
    with pytest.raises(SystemExit):
        main(base + ["--policy-role", "r", "--policy-user", "u"])


def _bypass(params: sqlgen.Params, **fields) -> sqlgen.Params:
    """A Params whose fields skipped __post_init__ validation (simulates a bug)."""
    for name, value in fields.items():
        object.__setattr__(params, name, value)
    return params


def test_review_repro_placeholder_database_is_rejected():
    # Round-2 repro: database '__PUBLIC_GROUP__' + public_group 'review_victim'
    # used to render SQL against review_victim.
    with pytest.raises(ValueError, match="placeholder"):
        sqlgen.Params(
            database="__PUBLIC_GROUP__", public_group="review_victim", app_name="cbioportal"
        )


@pytest.mark.parametrize("token", ["__PUBLIC_GROUP__", "__CLICKHOUSE_DATABASE__", "x__APP_NAME__y"])
@pytest.mark.parametrize("field", ["database", "policy_target", "public_group", "app_name"])
def test_placeholder_tokens_rejected_in_every_parameter(field, token):
    base = {"database": "db", "public_group": "PUBLIC", "app_name": "cbioportal"}
    with pytest.raises(ValueError, match="placeholder"):
        sqlgen.Params(**{**base, field: token})


def test_render_is_single_pass():
    params = _bypass(
        sqlgen.Params(database="cbio_green", public_group="PUBLIC", app_name="cbioportal"),
        public_group="__CLICKHOUSE_DATABASE__",
        app_name="__POLICY_TARGET__",
    )
    template = "upperUTF8('__PUBLIC_GROUP__') '__APP_NAME__' FROM __CLICKHOUSE_DATABASE__.t"
    assert sqlgen.render(template, params) == (
        "upperUTF8('__CLICKHOUSE_DATABASE__') '__POLICY_TARGET__' FROM `cbio_green`.t"
    )
    with pytest.raises(ValueError, match="unrendered"):
        sqlgen.render("SELECT __NOT_A_PLACEHOLDER__", PARAMS)


def test_apply_with_bypassed_review_repro_params_runs_nothing():
    params = _bypass(
        sqlgen.Params(database="db", public_group="PUBLIC", app_name="cbioportal"),
        database="__PUBLIC_GROUP__",
        public_group="review_victim",
    )
    client = _RecordingClient()
    with pytest.raises(ValueError, match="placeholder"):
        sqlgen.apply(client, params, log=lambda *_: None)
    assert client.calls == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM `review_victim`.cancer_study",
        "CREATE ROW POLICY p ON `review_victim`.* USING 1 TO r",
        "SELECT 1 FROM review_victim.cancer_study",
        "INSERT INTO review_victim.t SELECT 1",
        "CREATE TABLE IF NOT EXISTS review_victim.t (x Int8) ENGINE = Memory",
    ],
)
def test_assert_only_database_refuses_other_databases(sql):
    with pytest.raises(ValueError, match="review_victim"):
        sqlgen.assert_only_database(sql, "cbio_green")


def test_assert_only_database_accepts_rendered_files_and_ignores_comments_strings():
    for _, sql in sqlgen.rendered_files(PARAMS):
        sqlgen.assert_only_database(sql, "cbio_green")
    sqlgen.assert_only_database(
        "-- FROM other.t\nSELECT 'FROM other.t', g.x FROM `cbio_green`.t AS g JOIN s ON g.a = s.a",
        "cbio_green",
    )
    with pytest.raises(ValueError):
        sqlgen.assert_only_database(sqlgen.rendered_files(PARAMS)[1][1], "cbio_blue")


@pytest.mark.parametrize("suffix", ["\n", "\r", "\x00", "\r\n"])
def test_trailing_control_characters_rejected(suffix):
    from cbioportal_mcp.study_authz.groups import validate_name

    with pytest.raises(ValueError):
        sqlgen.validate_identifier("database", "db" + suffix)
    with pytest.raises(ValueError):
        validate_name("public group", "PUBLIC" + suffix)
    base = {"database": "db", "public_group": "PUBLIC", "app_name": "cbioportal"}
    for field in ("database", "policy_target", "public_group", "app_name"):
        with pytest.raises(ValueError):
            sqlgen.Params(**{**base, field: base.get(field, "r") + suffix})
    with pytest.raises(ValueError):
        sqlgen.restricted_user_sql("u" + suffix, "pw", "db")


@pytest.mark.parametrize(
    "quoted",
    [
        "`a SQL SECURITY INVOKER b`",
        '"a SQL SECURITY INVOKER b"',
        "'a SQL SECURITY INVOKER b'",
        r"`a\` SQL SECURITY INVOKER b`",
        r"'a\' SQL SECURITY INVOKER b'",
        "`a`` SQL SECURITY INVOKER b`",
        '"a"" SQL SECURITY INVOKER b"',
        "'a'' SQL SECURITY INVOKER b'",
    ],
)
def test_view_security_ignores_quoted_spoofs(quoted):
    from cbioportal_mcp.study_authz.coverage import view_sql_security

    assert (
        view_sql_security(f"CREATE VIEW d.v ({quoted} String) SQL SECURITY DEFINER AS SELECT 1")
        == "DEFINER"
    )


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("CREATE VIEW d.v AS SELECT 1", "INVOKER"),
        ("CREATE VIEW d.v SQL SECURITY INVOKER AS SELECT 1", "INVOKER"),
        ("CREATE VIEW d.v SQL SECURITY NONE AS SELECT 1", "NONE"),
        ("CREATE VIEW d.v SQL SECURITY INVOKER SQL SECURITY DEFINER AS SELECT 1", None),
        ("CREATE VIEW d.v SQL SECURITY UNKNOWN AS SELECT 1", None),
        ("CREATE VIEW d.v AS SELECT 'unterminated", None),
        ("CREATE VIEW d.v /* SQL SECURITY INVOKER", None),
        ("CREATE VIEW d.v AS", None),
        ("", None),
        ("CREATE VIEW d.v /* SQL SECURITY INVOKER */ SQL SECURITY DEFINER AS SELECT 1", "DEFINER"),
        ("CREATE VIEW d.v -- SQL SECURITY INVOKER\n SQL SECURITY DEFINER AS SELECT 1", "DEFINER"),
        (
            "CREATE VIEW d.v /* outer /* SQL SECURITY INVOKER */ inner */ "
            "SQL SECURITY DEFINER AS SELECT 1",
            "DEFINER",
        ),
    ],
)
def test_view_security_fails_closed_on_ambiguity(sql, expected):
    from cbioportal_mcp.study_authz.coverage import view_sql_security

    assert view_sql_security(sql) == expected


def test_admin_coverage_rejects_view_security_alias_spoof():
    objects, policies = _good_objects_and_policies()
    objects["top_mutated_genes_in_study"] = (
        "View",
        "CREATE VIEW d.v (`a SQL SECURITY INVOKER b` UInt64) SQL SECURITY DEFINER AS SELECT 1",
    )
    report = evaluate(objects, policies, "d")
    assert any("view is SQL SECURITY DEFINER" in error for error in report.errors)


_BYPASS_CREATES = {
    "MaterializedView": _TO_FORM,
    "Dictionary": "",
    "LiveView": "",
    "WindowView": "",
}


@pytest.mark.parametrize("engine", sorted(_BYPASS_CREATES))
@pytest.mark.parametrize("name", sorted(catalog.DENIED_TABLES))
def test_denied_names_refuse_policy_bypassing_engines(name, engine):
    # A denied name only means "no policy"; it must not admit an engine that
    # bypasses row policies (an MV/dictionary at user_study_manual, users...).
    objects, policies = _good_objects_and_policies()
    objects[name] = (engine, _BYPASS_CREATES[engine])
    report = _evaluate(objects, policies)
    assert f"{name}: {engine} bypasses row policies; remove it" in report.errors, report.errors


_IDENTITY_AS_STORED = (  # system.row_policies.select_filter on 25.8 and 26.6
    "((email = '') OR (email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', '')))) "
    "AND (valid_until > now64(3))"
)


@pytest.mark.parametrize(
    "select_filter",
    [sqlgen.USER_STUDY_MATCH, _IDENTITY_AS_STORED],
)
def test_identity_policy_accepts_clickhouse_renderings(select_filter):
    objects, policies = _good_objects_and_policies()
    policies[1] = Policy("id", "user_study", select_filter)
    assert _evaluate(objects, policies).ok


@pytest.mark.parametrize(
    "select_filter",
    [
        # No outer parentheses: AND binds tighter, so public rows never expire
        # and the caller's rows are not ORed under the validity check.
        "email = '' OR email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', '')) "
        "AND valid_until > now64(3)",
        # The same, as ClickHouse stores it.
        "(email = '') OR ((email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', ''))) "
        "AND (valid_until > now64(3)))",
        "(email = '' OR email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', '')))",
        "((email = '') OR (email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', '')))) "
        "AND (valid_until > now64(3)) OR 1",
        "(email = '' OR email = lowerUTF8(getSettingOrDefault('SQL_cbio_user', ''))) "
        "AND valid_until > now64(3",
    ],
)
def test_identity_policy_grouping_must_match_exactly(select_filter):
    objects, policies = _good_objects_and_policies()
    policies[1] = Policy("id", "user_study", select_filter)
    report = _evaluate(objects, policies)
    assert any(e.startswith("user_study: policy must be exactly") for e in report.errors)
