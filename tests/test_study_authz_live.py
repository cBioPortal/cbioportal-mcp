"""study-authz against a real ClickHouse 25.8 started in Docker.

Skipped when Docker is unavailable (like the repo's other live tests, which
skip without a server). The module starts its own throwaway container, so it
never touches a shared server:

    uv run pytest tests/test_study_authz_live.py

CBIOPORTAL_MCP_STUDY_AUTHZ_IMAGE overrides the image (default
clickhouse/clickhouse-server:25.8). All emails are example.org fakes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from cbioportal_mcp.study_authz import sqlgen
from cbioportal_mcp.study_authz.catalog import ROW_POLICY_ROLE
from cbioportal_mcp.study_authz.coverage import fetch_and_evaluate
from cbioportal_mcp.study_authz.groups import grant_key_matches, is_public
from cbioportal_mcp.study_authz.testdb import Grant, make_test_db

IMAGE = os.getenv("CBIOPORTAL_MCP_STUDY_AUTHZ_IMAGE", "clickhouse/clickhouse-server:25.8")
DB = "authz_live"
PUBLIC_GROUP = "PUBLIC"
PARAMS = sqlgen.Params(database=DB, public_group=PUBLIC_GROUP, app_name="cbioportal")
FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)

# study id -> (identifier, groups). Covers the groups rule edge cases.
STUDIES = {
    1: ("pub_a", "PUBLIC"),
    2: ("pub_b", "other;public"),  # case-insensitive whole entry
    3: ("priv_1", "GRP1"),
    4: ("priv_2", "GRP2;;"),
    5: ("not_pub", " PUBLIC"),  # no trimming -> private
    6: ("brca_tcga", "GRP3"),
    7: ("priv_x", "PUBLICX;XPUBLIC"),  # no LIKE -> private
    8: ("priv_3", "OTHER;GRP1"),  # GRP1 unpacks to priv_1 AND priv_3
    9: ("space_grp", "GRP9; GRP1"),  # ' GRP1' is not GRP1 (no trimming)
}
MANUAL = [
    ("Alice@Example.org", "grp1", FUTURE),  # email lower-cased, key case-insensitive
    ("alice@example.org", "PRIV_2", FUTURE),  # by study id
    ("bob@example.org", "GRP1", PAST),  # expired
    ("carol@example.org", "ALL_TCGA", FUTURE),
    ("", "GRP2", FUTURE),  # empty email must never become public
]
PUBLIC = {"pub_a", "pub_b"}
EXPECTED = {
    "alice@example.org": PUBLIC | {"priv_1", "priv_2", "priv_3"},
    "ALICE@EXAMPLE.ORG": PUBLIC | {"priv_1", "priv_2", "priv_3"},
    "bob@example.org": PUBLIC,
    "carol@example.org": PUBLIC | {"brca_tcga"},
    "nobody@example.org": PUBLIC,
    "": PUBLIC,
    None: PUBLIC,  # setting not sent at all
}
IDENT = {sid: ident for sid, (ident, _) in STUDIES.items()}


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="Docker is not available")


class Server:
    def __init__(self, port: int):
        import clickhouse_connect

        self.port = port
        self.admin = clickhouse_connect.get_client(
            host="127.0.0.1", port=port, username="admin", password="admin"
        )

    def http(self, sql: str, user: str, password: str = "pw", **settings) -> str:
        """Raw HTTP query, so the settings sent are exactly the ones given."""
        params = {k: v for k, v in settings.items() if v is not None}
        url = f"http://127.0.0.1:{self.port}/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            data=sql.encode(),
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        )
        try:
            return urllib.request.urlopen(req, timeout=30).read().decode().strip()
        except urllib.error.HTTPError as e:
            raise RuntimeError(e.read().decode()) from None

    def studies_seen(self, sql: str, user: str = "mcp_restricted", **settings) -> set[str]:
        out = self.http(sql + " FORMAT TSV", user, **settings)
        return {line for line in out.splitlines() if line}


@pytest.fixture(scope="module")
def server():
    name = f"study-authz-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::8123",
            "-e",
            "CLICKHOUSE_USER=admin",
            "-e",
            "CLICKHOUSE_PASSWORD=admin",
            "-e",
            "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
            IMAGE,
        ],
        capture_output=True,
        text=True,
    )
    if run.returncode:
        pytest.skip(f"could not start {IMAGE}: {run.stderr.strip()[:200]}")
    try:
        port_line = subprocess.run(
            ["docker", "port", name, "8123/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        port = int(port_line.rsplit(":", 1)[1])
        deadline = time.time() + 90
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2)
                srv = Server(port)
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(1)
        # custom_settings_prefixes must allow SQL_ (the image default).
        probe = "SELECT getSettingOrDefault('SQL_probe', 'unset') SETTINGS SQL_probe = 'set'"
        assert srv.admin.query(probe).result_rows[0][0] == "set"
        _load_fixture(srv.admin)
        sqlgen.apply(srv.admin, PARAMS, log=lambda *_: None)
        srv.admin.insert(
            f"{DB}.user_study_manual",
            [[e, k, v] for e, k, v in MANUAL],
            column_names=["email", "grant_key", "valid_until"],
        )
        srv.admin.command(f"SYSTEM REFRESH VIEW {DB}.user_study_refresh")
        srv.admin.command(f"SYSTEM WAIT VIEW {DB}.user_study_refresh")
        _create_users(srv.admin)
        yield srv
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _load_fixture(admin, db: str = DB) -> None:
    ddl = [
        f"CREATE DATABASE {db}",
        f"CREATE TABLE {db}.cancer_study (cancer_study_id Int64, cancer_study_identifier "
        "Nullable(String), `groups` Nullable(String), name String) "
        "ENGINE=MergeTree ORDER BY tuple()",
        f"CREATE TABLE {db}.patient (internal_id Int64, cancer_study_id Int64) "
        "ENGINE=MergeTree ORDER BY internal_id",
        f"CREATE TABLE {db}.sample (internal_id Int64, patient_id Int64) "
        "ENGINE=MergeTree ORDER BY internal_id",
        f"CREATE TABLE {db}.clinical_patient (internal_id Int64, attr_value String) "
        "ENGINE=MergeTree ORDER BY internal_id",
        f"CREATE TABLE {db}.genetic_profile (genetic_profile_id Int64, cancer_study_id Int64) "
        "ENGINE=MergeTree ORDER BY genetic_profile_id",
        f"CREATE TABLE {db}.mutation (mutation_event_id Int64, genetic_profile_id Int64, "
        "sample_id Int64) ENGINE=MergeTree ORDER BY mutation_event_id",
        f"CREATE TABLE {db}.mutation_event (mutation_event_id Int64, hugo String) "
        "ENGINE=MergeTree ORDER BY mutation_event_id",
        f"CREATE TABLE {db}.sample_derived (cancer_study_identifier String, sample_unique_id "
        "String) ENGINE=MergeTree ORDER BY tuple()",
        f"CREATE TABLE {db}.gene (entrez_gene_id Int64, hugo_gene_symbol String) "
        "ENGINE=MergeTree ORDER BY entrez_gene_id",
        f"CREATE TABLE {db}.users (email String, enabled Int8) ENGINE=MergeTree ORDER BY email",
        f"CREATE TABLE {db}.authorities (email String, authority String) "
        "ENGINE=MergeTree ORDER BY email",
        f"CREATE VIEW {db}.clinical_attribute_counts SQL SECURITY INVOKER AS SELECT "
        f"cancer_study_identifier, count() AS n FROM {db}.sample_derived GROUP BY 1",
    ]
    for statement in ddl:
        admin.command(statement)
    ins = admin.insert
    ins(f"{db}.cancer_study", [[s, i, g, i] for s, (i, g) in STUDIES.items()])
    ins(f"{db}.patient", [[s * 10, s] for s in STUDIES])
    ins(f"{db}.sample", [[s * 100, s * 10] for s in STUDIES])
    ins(f"{db}.clinical_patient", [[s * 10, "x"] for s in STUDIES])
    ins(f"{db}.genetic_profile", [[s, s] for s in STUDIES])
    # Event 9999 is shared by pub_a and priv_1: visible to everyone via pub_a.
    ins(
        f"{db}.mutation",
        [[s * 1000, s, s * 100] for s in STUDIES] + [[9999, 1, 100], [9999, 3, 300]],
    )
    ins(f"{db}.mutation_event", [[s * 1000, "TP53"] for s in STUDIES] + [[9999, "KRAS"]])
    ins(f"{db}.sample_derived", [[i, f"{i}:s"] for i, _ in STUDIES.values()])
    ins(f"{db}.gene", [[7157, "TP53"]])
    ins(f"{db}.users", [["alice@example.org", 1]])
    ins(f"{db}.authorities", [["alice@example.org", "cbioportal:GRP1"]])


def _create_users(admin) -> None:
    for statement in sqlgen.statements(sqlgen.restricted_user_sql("mcp_restricted", "pw", DB)):
        admin.command(statement)
    # Misconfigured on purpose: role AND a direct grant (the documented bypass).
    for statement in sqlgen.statements(sqlgen.restricted_user_sql("mcp_direct", "pw", DB)):
        admin.command(statement)
    admin.command(f"GRANT SELECT ON {DB}.* TO mcp_direct")


# Per table: SQL returning the cancer_study_identifier of each visible row.
TABLE_QUERIES = {
    "cancer_study": f"SELECT cancer_study_identifier FROM {DB}.cancer_study",
    "patient": f"SELECT cancer_study_id FROM {DB}.patient",
    "sample": f"SELECT intDiv(internal_id, 100) FROM {DB}.sample",
    "clinical_patient": f"SELECT intDiv(internal_id, 10) FROM {DB}.clinical_patient",
    "genetic_profile": f"SELECT cancer_study_id FROM {DB}.genetic_profile",
    "mutation": f"SELECT genetic_profile_id FROM {DB}.mutation WHERE mutation_event_id != 9999",
    "mutation_event": f"SELECT intDiv(mutation_event_id, 1000) FROM {DB}.mutation_event "
    "WHERE mutation_event_id != 9999",
    "sample_derived": f"SELECT cancer_study_identifier FROM {DB}.sample_derived",
    "view": f"SELECT cancer_study_identifier FROM {DB}.clinical_attribute_counts",
}


def _as_identifiers(values: set[str]) -> set[str]:
    return {IDENT[int(v)] if v.isdigit() else v for v in values}


def test_user_study_matches_python_rules(server):
    rows = server.admin.query(
        f"SELECT email, cancer_study_identifier, source FROM {DB}.user_study"
    ).result_rows
    expected = {("", i, "public") for i, g in STUDIES.values() if is_public(g, PUBLIC_GROUP)}
    for email, key, until in MANUAL:
        if email and until > datetime.now(timezone.utc):
            expected |= {
                (email.lower(), i, "manual")
                for i, g in STUDIES.values()
                if grant_key_matches(key, i, g)
            }
    assert set(rows) == expected


def test_manual_entries_unpack_to_one_row_per_study(server):
    """Group and study-id grants become (email, study) rows; nothing else."""
    rows = server.admin.query(
        f"SELECT email, groupArraySorted(10)(cancer_study_identifier) FROM {DB}.user_study "
        "GROUP BY email ORDER BY email"
    ).result_rows
    assert dict(rows) == {
        "": ["pub_a", "pub_b"],  # public group studies (case-insensitive entry match)
        # 'grp1' (mixed case, group) -> priv_1 + priv_3, not space_grp (' GRP1');
        # 'PRIV_2' (study id, other case) -> priv_2.
        "alice@example.org": ["priv_1", "priv_2", "priv_3"],
        "carol@example.org": ["brca_tcga"],
        # bob's grant expired; the '' email manual row is dropped.
    }
    columns = server.admin.query(f"DESCRIBE TABLE {DB}.user_study").result_rows
    assert [c[0] for c in columns] == [
        "email",
        "cancer_study_identifier",
        "valid_until",
        "source",
        "issued_at",
    ]


@pytest.mark.parametrize("email", list(EXPECTED), ids=lambda e: repr(e))
@pytest.mark.parametrize("table", list(TABLE_QUERIES))
def test_caller_sees_own_grants_plus_public(server, table, email):
    seen = _as_identifiers(server.studies_seen(TABLE_QUERIES[table], SQL_cbio_user=email))
    assert seen == EXPECTED[email]


def test_shared_event_visible_through_public_study(server):
    seen = server.studies_seen(
        f"SELECT hugo FROM {DB}.mutation_event WHERE mutation_event_id = 9999"
    )
    assert seen == {"KRAS"}


def test_reference_tables_readable(server):
    assert server.studies_seen(f"SELECT hugo_gene_symbol FROM {DB}.gene") == {"TP53"}


@pytest.mark.parametrize(
    "table", ["users", "authorities", "user_study_manual", "user_study_refresh"]
)
def test_restricted_role_cannot_read_denied_objects(server, table):
    out = server.http(
        f"SELECT count() FROM {DB}.{table}", "mcp_restricted", SQL_cbio_user="alice@example.org"
    )
    assert out == "0"


def test_user_study_shows_only_own_and_public_rows(server):
    emails = server.studies_seen(
        f"SELECT DISTINCT email FROM {DB}.user_study", SQL_cbio_user="alice@example.org"
    )
    assert emails == {"alice@example.org"}  # plus '' (TSV prints it as an empty line)
    rows = server.http(
        f"SELECT count() FROM {DB}.user_study WHERE email NOT IN ('', 'alice@example.org')",
        "mcp_restricted",
        SQL_cbio_user="alice@example.org",
    )
    assert rows == "0"
    assert server.http(
        f"SELECT count() FROM {DB}.user_study WHERE email = ''", "mcp_restricted"
    ) == str(len(PUBLIC))


def test_clickhouse_connect_needs_invalid_setting_action_send(server):
    """Notes for the server change (P7): clickhouse_connect rejects SQL_cbio_user client-side.

    Its default invalid_setting_action='error' raises before sending, and 'drop'
    would silently degrade to public-only, so P7 must set it to 'send'.
    """
    import clickhouse_connect
    from clickhouse_connect import common
    from clickhouse_connect.driver.exceptions import ProgrammingError

    client = clickhouse_connect.get_client(
        host="127.0.0.1", port=server.port, username="mcp_restricted", password="pw"
    )
    sql = f"SELECT cancer_study_identifier FROM {DB}.cancer_study"
    settings = {"SQL_cbio_user": "alice@example.org", "readonly": "1"}
    previous = common.get_setting("invalid_setting_action")
    try:
        common.set_setting("invalid_setting_action", "error")
        with pytest.raises(ProgrammingError, match="unknown or readonly"):
            client.query(sql, settings=settings)
        common.set_setting("invalid_setting_action", "send")
        seen = {r[0] for r in client.query(sql, settings=settings).result_rows}
        assert seen == EXPECTED["alice@example.org"]
    finally:
        common.set_setting("invalid_setting_action", previous)


def test_inline_setting_rejected_under_readonly(server):
    sql = f"SELECT count() FROM {DB}.cancer_study SETTINGS SQL_cbio_user = 'alice@example.org'"
    with pytest.raises(RuntimeError, match="READONLY"):
        server.http(sql, "mcp_restricted", SQL_cbio_user="nobody@example.org", readonly=1)
    # Why the MCP must send readonly=1 itself: without it, the query could
    # impersonate another caller.
    assert server.http(sql, "mcp_restricted", SQL_cbio_user="nobody@example.org") == "5"


def test_role_is_the_only_path_and_direct_grant_bypasses(server):
    sql = f"SELECT count() FROM {DB}.cancer_study"
    # Role only: dropping the role leaves no privilege at all.
    server.http("SET ROLE NONE", "mcp_restricted", session_id="s-role-only")
    with pytest.raises(RuntimeError, match="ACCESS_DENIED"):
        server.http(sql, "mcp_restricted", session_id="s-role-only")
    # Role + direct grant (misconfiguration, P6's startup check will refuse it):
    # filtered while the role is active, everything once it is not.
    assert server.http(sql, "mcp_direct") == str(len(PUBLIC))
    server.http("SET ROLE NONE", "mcp_direct", session_id="s-direct")
    assert server.http(sql, "mcp_direct", session_id="s-direct") == str(len(STUDIES))


def test_expired_rows_grant_nothing_and_stalled_refresh_fails_closed(server):
    admin = server.admin
    admin.command(f"SYSTEM STOP VIEW {DB}.user_study_refresh")  # simulate a failing refresh
    try:
        now = datetime.now(timezone.utc)
        admin.insert(
            f"{DB}.user_study",
            [
                ["erin@example.org", "priv_1", now - timedelta(seconds=1), "manual", now],
                ["erin@example.org", "not_pub", now + timedelta(seconds=2), "manual", now],
            ],
            column_names=["email", "cancer_study_identifier", "valid_until", "source", "issued_at"],
        )
        q = TABLE_QUERIES["cancer_study"]
        assert server.studies_seen(q, SQL_cbio_user="erin@example.org") == PUBLIC | {"not_pub"}
        time.sleep(2.5)
        # Private rows lapse with no refresh running; public rows remain.
        assert server.studies_seen(q, SQL_cbio_user="erin@example.org") == PUBLIC
    finally:
        admin.command(f"SYSTEM START VIEW {DB}.user_study_refresh")
        admin.command(f"SYSTEM REFRESH VIEW {DB}.user_study_refresh")
        admin.command(f"SYSTEM WAIT VIEW {DB}.user_study_refresh")
    assert not admin.query(
        f"SELECT 1 FROM {DB}.user_study WHERE email = 'erin@example.org'"
    ).result_rows


def test_private_rows_capped_by_grace_public_far_future(server):
    rows = server.admin.query(
        f"SELECT source, max(valid_until) FROM {DB}.user_study GROUP BY source"
    ).result_rows
    until = dict(rows)
    assert until["public"].year == 2299
    limit = datetime.now(timezone.utc) + timedelta(minutes=PARAMS.private_grace_minutes + 1)
    manual = until["manual"]
    assert (manual if manual.tzinfo else manual.replace(tzinfo=timezone.utc)) < limit


def test_reapply_is_idempotent_and_keeps_manual_grants(server):
    sqlgen.apply(server.admin, PARAMS, log=lambda *_: None)
    seen = server.studies_seen(TABLE_QUERIES["cancer_study"], SQL_cbio_user="alice@example.org")
    assert seen == EXPECTED["alice@example.org"]


def test_coverage_check(server):
    admin = server.admin
    report = fetch_and_evaluate(admin, DB, ROW_POLICY_ROLE)
    assert report.ok, (report.errors, report.unclassified)

    admin.command(f"CREATE TABLE {DB}.brand_new (x Int64) ENGINE=MergeTree ORDER BY x")
    admin.command(f"DROP ROW POLICY cbioportal_mcp_study_patient ON {DB}.patient")
    try:
        report = fetch_and_evaluate(admin, DB, ROW_POLICY_ROLE)
        assert report.unclassified == ["brand_new"]
        assert any(e.startswith("patient:") for e in report.errors)
        # The new table fails closed meanwhile.
        assert server.http(f"SELECT count() FROM {DB}.brand_new", "mcp_restricted") == "0"
    finally:
        admin.command(f"DROP TABLE {DB}.brand_new SYNC")
        sqlgen.apply(admin, PARAMS, log=lambda *_: None)
    assert fetch_and_evaluate(admin, DB, ROW_POLICY_ROLE).ok


def test_make_test_db(server):
    admin = server.admin
    source = "authz_source"
    _load_fixture(admin, source)  # no policies, users/authorities present
    target = sqlgen.Params(
        database="authz_e2e_test",
        public_group="PUBLIC",
        app_name="cbioportal",
        policy_target="e2e_policy",
    )
    gated = {"priv_1": "TEAM_A", "priv_2": "TEAM_B", "brca_tcga": "TEAM_A"}
    grants = [
        Grant("frank@example.org", "TEAM_A", FUTURE),
        Grant("gina@example.org", "priv_2", FUTURE),
    ]
    counts = make_test_db(admin, source, target, gated, ["pub_a"], grants, log=lambda *_: None)

    assert counts["cancer_study"] == 4 and counts["patient"] == 4 and counts["gene"] == 1
    assert "users" not in counts and "authorities" not in counts
    groups = dict(
        admin.query(
            "SELECT cancer_study_identifier, `groups` FROM authz_e2e_test.cancer_study"
        ).result_rows
    )
    assert groups == {
        "pub_a": "PUBLIC",
        "priv_1": "TEAM_A",
        "priv_2": "TEAM_B",
        "brca_tcga": "TEAM_A",
    }
    assert fetch_and_evaluate(admin, "authz_e2e_test", "e2e_policy").ok

    for statement in sqlgen.statements(
        sqlgen.restricted_user_sql("mcp_e2e", "pw", "authz_e2e_test", role="e2e_policy")
    ):
        admin.command(statement)
    q = "SELECT cancer_study_identifier FROM authz_e2e_test.cancer_study"
    assert server.studies_seen(q, "mcp_e2e", SQL_cbio_user="frank@example.org") == {
        "pub_a",
        "priv_1",
        "brca_tcga",
    }
    assert server.studies_seen(q, "mcp_e2e", SQL_cbio_user="gina@example.org") == {
        "pub_a",
        "priv_2",
    }
    assert server.studies_seen(q, "mcp_e2e") == {"pub_a"}
    v = "SELECT cancer_study_identifier FROM authz_e2e_test.clinical_attribute_counts"
    assert server.studies_seen(v, "mcp_e2e", SQL_cbio_user="gina@example.org") == {
        "pub_a",
        "priv_2",
    }

    with pytest.raises(ValueError, match="exists"):
        make_test_db(admin, source, target, gated, ["pub_a"], grants, log=lambda *_: None)
    make_test_db(admin, source, target, gated, ["pub_a"], [], replace=True, log=lambda *_: None)
    with pytest.raises(ValueError, match="only works"):
        make_test_db(
            admin,
            source,
            sqlgen.Params(
                database=DB,
                public_group="PUBLIC",
                app_name="cbioportal",
                policy_target="e2e_policy",
            ),
            gated,
            ["pub_a"],
            [],
            replace=True,
        )


def test_user_targeted_policies_direct_grant_is_not_a_bypass(server):
    """Policies TO an existing user (named like the real llm_user) that has
    direct grants on other databases and no roles."""
    admin = server.admin
    admin.command("CREATE USER llm_user IDENTIFIED BY 'pw'")
    admin.command("CREATE DATABASE authz_other_pub")
    admin.command("CREATE TABLE authz_other_pub.t (x Int64) ENGINE=MergeTree ORDER BY x")
    admin.insert("authz_other_pub.t", [[1], [2], [3]])
    admin.command("GRANT SELECT ON authz_other_pub.* TO llm_user")

    source = "authz_source_user"
    _load_fixture(admin, source)
    target = sqlgen.Params(
        database="authz_user_target",
        public_group="PUBLIC",
        app_name="cbioportal",
        policy_target="llm_user",
        target_is_user=True,
    )
    gated = {"priv_1": "TEAM_A", "priv_2": "TEAM_B"}
    grants = [Grant("frank@example.org", "TEAM_A", FUTURE)]
    make_test_db(admin, source, target, gated, ["pub_a"], grants, log=lambda *_: None)
    assert fetch_and_evaluate(admin, "authz_user_target", "llm_user").ok

    q = "SELECT cancer_study_identifier FROM authz_user_target.cancer_study"
    s = "SELECT cancer_study_identifier FROM authz_user_target.sample_derived"
    assert server.studies_seen(q, "llm_user", SQL_cbio_user="frank@example.org") == {
        "pub_a",
        "priv_1",
    }
    assert server.studies_seen(s, "llm_user", SQL_cbio_user="frank@example.org") == {
        "pub_a",
        "priv_1",
    }
    assert server.studies_seen(q, "llm_user") == {"pub_a"}  # omitted setting: public only
    # Direct grants + SET ROLE NONE change nothing: the policies name the user.
    server.http("SET ROLE NONE", "llm_user", session_id="s-llm-fake")
    assert server.studies_seen(q, "llm_user", session_id="s-llm-fake") == {"pub_a"}
    for denied in ["user_study_manual", "user_study_refresh"]:
        assert server.http(f"SELECT count() FROM authz_user_target.{denied}", "llm_user") == "0"
    emails = server.studies_seen(
        "SELECT DISTINCT email FROM authz_user_target.user_study",
        "llm_user",
        SQL_cbio_user="gina@example.org",
    )
    assert emails == set()  # only '' rows (gina has no grants)
    # Its other databases are untouched; admin still sees everything.
    assert server.http("SELECT count() FROM authz_other_pub.t", "llm_user") == "3"
    assert admin.query(f"SELECT count() FROM {target.database}.cancer_study").result_rows[0][0] == 3

    with pytest.raises(ValueError, match="does not exist"):
        sqlgen.apply(
            admin,
            sqlgen.Params(
                database="authz_user_target",
                public_group="PUBLIC",
                app_name="cbioportal",
                policy_target="no_such_user",
                target_is_user=True,
            ),
            log=lambda *_: None,
        )


def test_p7_user_tools_and_cache_isolation(server, monkeypatch):
    """Test the request wiring directly, without the server startup gate."""
    import mcp_clickhouse.mcp_server as upstream
    from clickhouse_connect import common

    from cbioportal_mcp import identity, rollout
    from cbioportal_mcp import server as mcp_server

    # A first server import validates filtering=off and pins it after fixtures
    # have reset rollout. Drop that pin so this test can switch filtering on.
    monkeypatch.setattr(rollout, "_PIN", None)

    # The P4 minimal fixture omits list_studies' deployment metadata columns.
    for column in ("description String", "type_of_cancer_id String", "sample_count UInt64"):
        server.admin.command(f"ALTER TABLE {DB}.cancer_study ADD COLUMN IF NOT EXISTS {column}")
    previous = common.get_setting("invalid_setting_action")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_FILTERING", "on")
    # Upstream client preparation also validates environment-based credentials.
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_USER", "mcp_restricted")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "pw")
    config = dict(
        host="127.0.0.1",
        port=server.port,
        username="mcp_restricted",
        password="pw",
        database=DB,
        secure=False,
        verify=True,
        connect_timeout=10,
        send_receive_timeout=30,
    )
    monkeypatch.setattr(upstream, "_resolve_client_config", lambda: config)
    tool = mcp_server.clickhouse_run_select_query.fn.__wrapped__
    studies = mcp_server.list_studies.fn.__wrapped__
    try:
        for email in (
            "alice@example.org",
            "carol@example.org",
            "nobody@example.org",
            "alice@example.org",
        ):
            token = identity._CURRENT.set(
                identity.Decision(identity.Identity("id", email, "oauth"), True, True)
            )
            try:
                result = tool("SELECT cancer_study_identifier FROM cancer_study")
                assert {r[0] for r in result["rows"]} == EXPECTED[email]
                listed = studies(limit=100)
                assert {
                    r["cancer_study_identifier"] for r in listed if "cancer_study_identifier" in r
                } == EXPECTED[email]
                assert ("study_scope_note" in result) == (email == "nobody@example.org")
                assert (
                    "identity settings"
                    in tool("SELECT 1 SETTINGS SQL_cbio_user='alice@example.org'")["error_message"]
                )
            finally:
                identity._CURRENT.reset(token)
        assert "verified user identity" in tool("SELECT 1")["error_message"]
        assert "verified user identity" in studies()[0]["error"]
    finally:
        common.set_setting("invalid_setting_action", previous)


def test_apply_refuses_to_widen_an_existing_restricted_principal(server):
    """Reviewer repro: a foreign policy is OR-ed with the catch-all USING 1."""
    admin = server.admin
    db = "authz_outsider"
    _load_fixture(admin, db)
    admin.command("CREATE USER outsider IDENTIFIED BY 'pw'")
    admin.command(f"GRANT SELECT ON {db}.* TO outsider")
    admin.command(
        f"CREATE ROW POLICY outsider_only ON {db}.cancer_study "
        "USING cancer_study_id = 1 TO outsider"
    )
    q = f"SELECT cancer_study_identifier FROM {db}.cancer_study"
    assert server.studies_seen(q, "outsider") == {"pub_a"}

    params = sqlgen.Params(database=db, public_group=PUBLIC_GROUP, app_name="cbioportal")
    with pytest.raises(ValueError, match=f"outsider_only ON {db}.cancer_study"):
        sqlgen.apply(admin, params, log=lambda *_: None)
    # Nothing was changed: no policy, no user_study, outsider still sees 1 study.
    assert server.studies_seen(q, "outsider") == {"pub_a"}
    assert (
        admin.query(
            "SELECT count() FROM system.row_policies WHERE database = {db:String}",
            parameters={"db": db},
        ).result_rows[0][0]
        == 1
    )
    assert not admin.query(
        "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = 'user_study'",
        parameters={"db": db},
    ).result_rows


def test_check_fails_when_a_foreign_policy_appears_after_apply(server):
    admin = server.admin
    admin.command("CREATE USER IF NOT EXISTS late_user IDENTIFIED BY 'pw'")
    admin.command(f"GRANT SELECT ON {DB}.* TO late_user")
    admin.command(
        f"CREATE ROW POLICY late_policy ON {DB}.cancer_study USING cancer_study_id = 1 TO late_user"
    )
    try:
        # The catch-all is OR-ed with it: late_user sees everything, which check reports.
        q = f"SELECT cancer_study_identifier FROM {DB}.cancer_study"
        assert len(server.studies_seen(q, "late_user")) == len(STUDIES)
        report = fetch_and_evaluate(admin, DB, ROW_POLICY_ROLE)
        assert not report.ok
        assert any(f"late_policy ON {DB}.cancer_study" in e for e in report.errors)
        with pytest.raises(ValueError, match="late_policy"):
            sqlgen.apply(admin, PARAMS, log=lambda *_: None)
    finally:
        admin.command(f"DROP ROW POLICY IF EXISTS late_policy ON {DB}.cancer_study")
        admin.command("DROP USER IF EXISTS late_user")
    assert fetch_and_evaluate(admin, DB, ROW_POLICY_ROLE).ok


def test_apply_refuses_to_switch_policy_target(server):
    """Re-targeting would put the old role into ALL EXCEPT <new target>, unfiltered."""
    switched = sqlgen.Params(
        database=DB,
        public_group=PUBLIC_GROUP,
        app_name="cbioportal",
        policy_target="mcp_restricted",
        target_is_user=True,
    )
    with pytest.raises(ValueError, match="attached to cbioportal_mcp_study_restricted, not"):
        sqlgen.apply(server.admin, switched, log=lambda *_: None)
    seen = server.studies_seen(TABLE_QUERIES["cancer_study"], SQL_cbio_user="alice@example.org")
    assert seen == EXPECTED["alice@example.org"]


def test_hostile_source_database_is_rejected_live(server):
    """Reviewer repro: this source name used to return pub_a via UNION ALL."""
    hostile = f"{DB}.cancer_study WHERE 0 UNION ALL SELECT 'pub_a' --"
    target = sqlgen.Params(database="authz_inject", public_group="PUBLIC", app_name="cbioportal")
    with pytest.raises(ValueError, match="source database"):
        make_test_db(
            server.admin, hostile, target, {"priv_1": "G"}, ["pub_a"], [], log=lambda *_: None
        )
    assert not server.admin.query(
        "SELECT 1 FROM system.databases WHERE name = 'authz_inject'"
    ).result_rows


def test_user_sql_with_custom_policy_role(server):
    admin = server.admin
    db = "authz_custom_role"
    _load_fixture(admin, db)
    params = sqlgen.Params(
        database=db, public_group=PUBLIC_GROUP, app_name="cbioportal", policy_target="team_role"
    )
    sqlgen.apply(admin, params, log=lambda *_: None)
    for statement in sqlgen.statements(
        sqlgen.restricted_user_sql("team_member", "pw", db, role="team_role")
    ):
        admin.command(statement)
    q = f"SELECT cancer_study_identifier FROM {db}.cancer_study"
    assert server.studies_seen(q, "team_member") == PUBLIC
    assert fetch_and_evaluate(admin, db, "team_role").ok


def test_review_repro_placeholder_database_cannot_redirect_apply(server):
    """Round-2 repro: database '__PUBLIC_GROUP__' + public_group 'review_victim'."""
    admin = server.admin
    victim = "review_victim"
    _load_fixture(admin, victim)
    admin.command("CREATE DATABASE `__PUBLIC_GROUP__`")
    admin.command("CREATE USER review_outsider IDENTIFIED BY 'pw'")
    admin.command(f"GRANT SELECT ON {victim}.* TO review_outsider")
    admin.command(
        f"CREATE ROW POLICY review_outsider_only ON {victim}.cancer_study "
        "USING cancer_study_id = 1 TO review_outsider"
    )
    q = f"SELECT cancer_study_identifier FROM {victim}.cancer_study"
    assert server.studies_seen(q, "review_outsider") == {"pub_a"}

    with pytest.raises(ValueError, match="placeholder"):
        sqlgen.Params(database="__PUBLIC_GROUP__", public_group=victim, app_name="cbioportal")
    # Even with validation bypassed, apply refuses before touching the server.
    bypassed = sqlgen.Params(database="db", public_group="PUBLIC", app_name="cbioportal")
    object.__setattr__(bypassed, "database", "__PUBLIC_GROUP__")
    object.__setattr__(bypassed, "public_group", victim)
    with pytest.raises(ValueError, match="placeholder"):
        sqlgen.apply(admin, bypassed, log=lambda *_: None)

    assert server.studies_seen(q, "review_outsider") == {"pub_a"}
    policies = admin.query(
        "SELECT database, short_name FROM system.row_policies "
        "WHERE database IN ('review_victim', '__PUBLIC_GROUP__')"
    ).result_rows
    assert policies == [(victim, "review_outsider_only")]
    assert not admin.query(
        "SELECT 1 FROM system.tables WHERE database IN ('review_victim', '__PUBLIC_GROUP__') "
        "AND name LIKE 'user_study%'"
    ).result_rows


# --- P6: the server's startup check, run as the runtime user itself ----------


def _runtime_client(server, user: str):
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host="127.0.0.1", port=server.port, username=user, password="pw"
    )


def _startup_check(server, user: str, database: str = DB, public_databases=frozenset()):
    from cbioportal_mcp.study_authz import startup_check

    with startup_check._send_custom_settings():
        return startup_check.check(
            _runtime_client(server, user), database, public_databases=public_databases
        )


def _refused(
    server, user: str, needle: str, database: str = DB, public_databases=frozenset()
) -> str:
    from cbioportal_mcp.study_authz.startup_check import PolicyCoverageError

    with pytest.raises(PolicyCoverageError) as exc:
        _startup_check(server, user, database, public_databases)
    message = str(exc.value)
    assert needle in message, message
    assert "\n" not in message and "@" not in message
    return message


def test_startup_check_role_target_passes(server):
    result = _startup_check(server, "mcp_restricted")
    assert (result.user, result.target, result.target_is_user) == (
        "mcp_restricted",
        ROW_POLICY_ROLE,
        False,
    )
    assert result.public_studies == len(PUBLIC)


def test_startup_check_role_target_with_direct_grant_refused(server):
    _refused(server, "mcp_direct", f"direct SELECT grant ON {DB}.* bypasses role")


def test_startup_check_other_role_with_select_refused(server):
    """A second default role holding SELECT bypasses the policy role via ?role=."""
    admin = server.admin
    for statement in sqlgen.statements(sqlgen.restricted_user_sql("p6_two_roles", "pw", DB)):
        admin.command(statement)
    admin.command("CREATE ROLE IF NOT EXISTS p6_analyst")
    admin.command(f"GRANT SELECT ON {DB}.* TO p6_analyst")
    admin.command("GRANT p6_analyst TO p6_two_roles")
    admin.command("SET DEFAULT ROLE ALL TO p6_two_roles")
    try:
        _refused(server, "p6_two_roles", "only policy role")
    finally:
        admin.command("DROP USER IF EXISTS p6_two_roles")
        admin.command("DROP ROLE IF EXISTS p6_analyst")


def test_startup_check_missing_policy_on_one_table_refused(server):
    admin = server.admin
    admin.command(f"DROP ROW POLICY cbioportal_mcp_study_patient ON {DB}.patient")
    try:
        _refused(server, "mcp_restricted", "patient: study table has only the default-deny")
    finally:
        sqlgen.apply(admin, PARAMS, log=lambda *_: None)
    _startup_check(server, "mcp_restricted")


def test_startup_check_readable_user_study_manual_refused(server):
    admin = server.admin
    admin.command(
        f"CREATE ROW POLICY p6_leak ON {DB}.user_study_manual FOR SELECT USING 1 "
        f"TO {ROW_POLICY_ROLE}"
    )
    try:
        assert server.http(f"SELECT count() FROM {DB}.user_study_manual", "mcp_restricted") != "0"
        _refused(server, "mcp_restricted", "user_study_manual: denied object has policies")
    finally:
        admin.command(f"DROP ROW POLICY p6_leak ON {DB}.user_study_manual")
    _startup_check(server, "mcp_restricted")


def test_startup_check_foreign_policy_refused(server):
    """P4's ownership rule at startup: a foreign PERMISSIVE policy TO the policy
    role is OR-ed with ours and widens the MCP user; one TO another principal
    is widened by our catch-all. Both refuse."""
    admin = server.admin
    sql = f"SELECT count() FROM {DB}.cancer_study"
    admin.command(
        f"CREATE ROW POLICY team_view ON {DB}.cancer_study USING cancer_study_id > 0 "
        f"TO {ROW_POLICY_ROLE}"
    )
    try:
        assert server.http(sql, "mcp_restricted") == str(len(STUDIES))  # widened
        _refused(server, "mcp_restricted", f"team_view ON {DB}.cancer_study (not created")
    finally:
        admin.command(f"DROP ROW POLICY IF EXISTS team_view ON {DB}.cancer_study")
    admin.command("CREATE USER IF NOT EXISTS p6_outsider IDENTIFIED BY 'pw'")
    admin.command(f"CREATE ROW POLICY outsider_only ON {DB}.patient USING 0 TO p6_outsider")
    try:
        assert server.http(sql, "mcp_restricted") == str(len(PUBLIC))  # MCP unaffected...
        _refused(server, "mcp_restricted", f"outsider_only ON {DB}.patient")  # ...still refused
    finally:
        admin.command(f"DROP ROW POLICY IF EXISTS outsider_only ON {DB}.patient")
        admin.command("DROP USER IF EXISTS p6_outsider")
    _startup_check(server, "mcp_restricted")


def test_startup_check_admin_privilege_refused(server):
    admin = server.admin
    admin.command(f"GRANT INSERT ON {DB}.user_study_manual TO {ROW_POLICY_ROLE}")
    try:
        _refused(server, "mcp_restricted", f"user holds INSERT ON {DB}.user_study_manual")
    finally:
        admin.command(f"REVOKE INSERT ON {DB}.user_study_manual FROM {ROW_POLICY_ROLE}")


def test_startup_check_user_target_passes(server):
    """Policies TO the user itself (llm_user style): its direct grants are fine."""
    admin = server.admin
    admin.command("CREATE USER p6_llm_user IDENTIFIED BY 'pw'")
    admin.command("CREATE DATABASE IF NOT EXISTS authz_p6_other")
    admin.command("GRANT SELECT ON authz_p6_other.* TO p6_llm_user")
    source = "authz_source_p6"
    _load_fixture(admin, source)
    target = sqlgen.Params(
        database="authz_p6_user_target",
        public_group="PUBLIC",
        app_name="cbioportal",
        policy_target="p6_llm_user",
        target_is_user=True,
    )
    make_test_db(admin, source, target, {"priv_1": "TEAM_A"}, ["pub_a"], [], log=lambda *_: None)
    result = _startup_check(server, "p6_llm_user", target.database)
    assert (result.target, result.target_is_user, result.public_studies) == (
        "p6_llm_user",
        True,
        1,
    )
    assert server.http("CHECK GRANT SELECT ON system.grants", "p6_llm_user") == "0"
    assert server.http("CHECK GRANT SELECT ON system.role_grants", "p6_llm_user") == "0"
    # Without the SHOW ROW POLICIES grant the check cannot see the policies: refuse.
    admin.command(f"REVOKE SHOW ROW POLICIES ON {target.database}.* FROM p6_llm_user")
    try:
        _refused(server, "p6_llm_user", "grant SHOW ROW POLICIES", target.database)
    finally:
        admin.command(f"GRANT SHOW ROW POLICIES ON {target.database}.* TO p6_llm_user")


def test_startup_check_through_the_mcp_connection(server, monkeypatch):
    """run_startup_check with filtering on builds mcp-clickhouse's own client."""
    from mcp_clickhouse import mcp_env

    from cbioportal_mcp.study_authz import startup_check

    for key, value in {
        "CLICKHOUSE_HOST": "127.0.0.1",
        "CLICKHOUSE_PORT": str(server.port),
        "CLICKHOUSE_USER": "mcp_restricted",
        "CLICKHOUSE_PASSWORD": "pw",
        "CLICKHOUSE_SECURE": "false",
        "CLICKHOUSE_VERIFY": "false",
        "CLICKHOUSE_DATABASE": DB,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(mcp_env, "_CONFIG_INSTANCE", None)
    on = {startup_check.FILTERING_ENV: "on", startup_check.PUBLIC_DATABASES_ENV: ""}
    assert startup_check.run_startup_check(DB, env=on).target == ROW_POLICY_ROLE
    assert startup_check.run_startup_check(DB, env={}) is None
    monkeypatch.setenv("CLICKHOUSE_USER", "mcp_direct")
    monkeypatch.setattr(mcp_env, "_CONFIG_INSTANCE", None)
    with pytest.raises(startup_check.PolicyCoverageError, match="bypasses role"):
        startup_check.run_startup_check(DB, env=on)


# --- P6 allow-list audit: the reviewer's false-PASS repros -------------------


def _p6_user_target_db(admin, db: str, user: str):
    """A user-targeted policy database and its llm_user-like runtime user."""
    admin.command(f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY 'pw'")
    source = f"{db}_source"
    _load_fixture(admin, source)
    target = sqlgen.Params(
        database=db,
        public_group=PUBLIC_GROUP,
        app_name="cbioportal",
        policy_target=user,
        target_is_user=True,
    )
    make_test_db(admin, source, target, {"priv_1": "TEAM_A"}, ["pub_a"], [], log=lambda *_: None)
    return target


def _p6_role_target_db(admin, db: str, role: str, user: str):
    """A role-targeted policy database plus a runtime user with that role."""
    source = f"{db}_source"
    _load_fixture(admin, source)
    target = sqlgen.Params(
        database=db,
        public_group=PUBLIC_GROUP,
        app_name="cbioportal",
        policy_target=role,
    )
    make_test_db(admin, source, target, {"priv_1": "TEAM_A"}, ["pub_a"], [], log=lambda *_: None)
    admin.command(f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY 'pw'")
    admin.command(f"GRANT {role} TO {user}")
    admin.command(f"SET DEFAULT ROLE {role} TO {user}")
    return target


def test_startup_check_wildcard_database_grant_refused(server):
    """Repro 1: GRANT SELECT ON authz*.* was compared literally, so a
    SET ROLE NONE bypass read the whole database."""
    admin = server.admin
    db, role, user = "authz_p6_wild", "p6_wild_policy", "p6_wild_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command(f"GRANT SELECT ON authz*.* TO {user}")
    _refused(server, user, "wildcard database", db)


def test_startup_check_other_role_table_select_refused(server):
    """Repro 2: an extra role with table-level SELECT was invisible to the
    whole-database CHECK GRANT."""
    admin = server.admin
    db, role, user = "authz_p6_table", "p6_table_policy", "p6_table_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command("CREATE ROLE p6_sample_reader")
    admin.command(f"GRANT SELECT ON {db}.sample TO p6_sample_reader")
    admin.command(f"GRANT p6_sample_reader TO {user}")
    _refused(server, user, "only policy role", db)


def test_startup_check_role_grant_option_refused(server):
    """Repro 3: the policy role's SELECT carries WITH GRANT OPTION, so the
    user can grant itself a roleless path and read everything."""
    admin = server.admin
    db, role, user = "authz_p6_option", "p6_option_policy", "p6_option_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command(f"GRANT SELECT ON {db}.* TO {role} WITH GRANT OPTION")
    _refused(server, user, "WITH GRANT OPTION", db)


def test_startup_check_non_default_role_admin_privilege_refused(server):
    """Repro 4: a non-default granted role holds ACCESS MANAGEMENT ON *.*."""
    admin = server.admin
    db, role, user = "authz_p6_nondefault", "p6_nondefault_policy", "p6_nondefault_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command("CREATE ROLE p6_nondefault_admin")
    admin.command("GRANT ACCESS MANAGEMENT ON *.* TO p6_nondefault_admin")
    admin.command(f"GRANT p6_nondefault_admin TO {user}")  # deliberately not default
    _refused(server, user, "only policy role", db)


def test_startup_check_target_role_table_ddl_refused(server):
    """Repro 5: table-level DDL on an authz table via the policy role."""
    admin = server.admin
    db, role, user = "authz_p6_ddl", "p6_ddl_policy", "p6_ddl_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command(f"GRANT DROP TABLE ON {db}.user_study TO {role}")
    _refused(server, user, "DROP TABLE", db)


@pytest.mark.parametrize(
    ("grant", "needle"),
    [
        ("MYSQL", "MYSQL"),
        ("SYSTEM DROP DNS CACHE", "SYSTEM DROP DNS CACHE"),
    ],
)
def test_startup_check_target_role_table_function_refused(server, grant, needle):
    """Repro 6: table functions the old deny list did not enumerate."""
    admin = server.admin
    suffix = "mysql" if grant == "MYSQL" else "system"
    db, role, user = f"authz_p6_func_{suffix}", f"p6_{suffix}_policy", f"p6_{suffix}_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command(f"GRANT {grant} ON *.* TO {role}")
    _refused(server, user, needle, db)


def test_startup_check_quoted_object_name_refused(server):
    """Quoted names with spaces are parsed as objects, never as roles."""
    admin = server.admin
    db, user = "authz_p6_quoted", "p6_quoted_user"
    _p6_user_target_db(admin, db, user)
    admin.command("CREATE DATABASE IF NOT EXISTS `p6 weird db`")
    admin.command(
        "CREATE TABLE IF NOT EXISTS `p6 weird db`.`my table` (x Int64) "
        "ENGINE=MergeTree ORDER BY x"
    )
    admin.command(f"GRANT SELECT ON `p6 weird db`.`my table` TO {user}")
    _refused(server, user, "p6 weird db", db)


def test_startup_check_nested_role_grant_refused(server):
    """A nested role is audited (or refused if unreadable); a grant inside it
    cannot hide behind the outer role."""
    admin = server.admin
    db, role, user = "authz_p6_nested", "p6_nested_policy", "p6_nested_user"
    _p6_role_target_db(admin, db, role, user)
    admin.command("CREATE ROLE p6_nested_inner")
    admin.command(f"GRANT SELECT ON {db}.sample TO p6_nested_inner WITH GRANT OPTION")
    admin.command(f"GRANT p6_nested_inner TO {role}")
    _refused(server, user, "WITH GRANT OPTION", db)


def test_startup_check_user_target_with_five_other_databases_passes(server):
    """The llm_user shape: no roles, direct SELECT on five unrelated databases,
    policies attached directly to the user."""
    admin = server.admin
    db, user = "authz_p6_llm_five", "p6_llm_five"
    admin.command(f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY 'pw'")
    others = [f"p6_other_{i}" for i in range(5)]
    for other in others:
        admin.command(f"CREATE DATABASE IF NOT EXISTS {other}")
        admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    _p6_user_target_db(admin, db, user)
    assert admin.query(
        "SELECT count() FROM system.role_grants WHERE user_name = {u:String}",
        parameters={"u": user},
    ).result_rows == [(0,)]
    # Docker does not register Shared* engines; exercise the corresponding
    # MergeTree families here and Shared* metadata in the unit/Cloud checks.
    engines = [
        "MergeTree",
        "ReplacingMergeTree",
        "SummingMergeTree",
        "AggregatingMergeTree",
        "CoalescingMergeTree",
    ]
    for other, engine in zip(others, engines, strict=True):
        admin.command(f"CREATE TABLE {other}.cancer_study (id Int64) ENGINE={engine} ORDER BY id")
    _refused(server, user, "not declared public", db)
    # A public declaration permits study copies, never privileged objects.
    result = _startup_check(server, user, db, frozenset(others))
    assert (result.target, result.target_is_user, result.public_studies) == (user, True, 1)


def test_startup_check_unfiltered_source_requires_public_declaration(server):
    admin = server.admin
    db, user = "authz_p6_copy", "p6_copy_user"
    _p6_user_target_db(admin, db, user)
    source = db + "_source"
    admin.command(f"GRANT SELECT ON {source}.* TO {user}")
    assert server.http(f"SELECT count() FROM {source}.cancer_study", user) == "9"
    _refused(server, user, "not declared public", db)
    assert _startup_check(server, user, db, frozenset({source})).public_studies == 1


@pytest.mark.parametrize("kind", ["definer", "none", "dictionary", "merge"])
def test_startup_check_unsafe_other_database_even_when_public(server, kind):
    admin = server.admin
    db, user, other = f"authz_p6_{kind}", f"p6_{kind}_user", f"other_p6_{kind}"
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    if kind in {"definer", "none"}:
        security = (
            "DEFINER=admin SQL SECURITY DEFINER" if kind == "definer" else "SQL SECURITY NONE"
        )
        admin.command(f"CREATE VIEW {other}.leak {security} AS SELECT * FROM {db}.cancer_study")
        needle = "SQL SECURITY INVOKER"
    elif kind == "dictionary":
        admin.command(
            f"CREATE DICTIONARY {other}.leak (cancer_study_id UInt64, name String) "
            "PRIMARY KEY cancer_study_id SOURCE(CLICKHOUSE(HOST '127.0.0.1' PORT 9000 "
            f"USER 'admin' PASSWORD 'admin' DB '{db}' TABLE 'cancer_study')) "
            "LAYOUT(FLAT()) LIFETIME(0)"
        )
        needle = "Dictionary engine is not allowed"
    else:
        admin.command(
            f"CREATE TABLE {other}.leak AS {db}.cancer_study "
            f"ENGINE=Merge('{db}', '^cancer_study$')"
        )
        needle = "Merge engine is not allowed"
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    if kind == "definer":
        assert server.http(f"SELECT count() FROM {other}.leak", user) == "2"
    _refused(server, user, needle, db, frozenset({other}))


@pytest.mark.parametrize("scope", ["system.*", "system.query_log"])
def test_startup_check_system_grants_refused(server, scope):
    admin = server.admin
    suffix = "all" if scope.endswith("*") else "log"
    db, user = f"authz_p6_sys_{suffix}", f"p6_sys_{suffix}"
    _p6_user_target_db(admin, db, user)
    admin.command(f"GRANT SELECT ON {scope} TO {user}")
    _refused(server, user, "grants on system are not allowed", db)


@pytest.mark.parametrize("kind", ["url_source", "url_encoded_policy", "mysql_source"])
def test_startup_check_credentials_in_external_engine_refused(server, kind):
    admin = server.admin
    db, user, other = f"authz_r3_{kind}", f"r3_{kind}_user", f"other_r3_{kind}"
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    if kind.startswith("url"):
        source = db if kind == "url_encoded_policy" else db + "_source"
        query = f"SELECT cancer_study_identifier FROM {source}.cancer_study FORMAT TSV"
        encoded = urllib.parse.quote(query, safe="")
        if kind == "url_encoded_policy":
            encoded = encoded.replace(db, "".join(f"%{ord(c):02X}" for c in db))
        url = f"http://127.0.0.1:8123/?user=admin&password=admin&query={encoded}"
        engine = f"URL('{url}', TSV)"
        expected = "2" if kind == "url_encoded_policy" else "9"
        needle = "URL engine is not allowed"
    else:
        engine = f"MySQL('127.0.0.1:9004', '{db}_source', 'cancer_study', 'admin', 'admin')"
        expected, needle = "9", "MySQL engine is not allowed"
    admin.command(
        f"CREATE TABLE {other}.leak (cancer_study_identifier Nullable(String)) ENGINE={engine}"
    )
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    assert server.http(f"SELECT count() FROM {other}.leak", user) == expected
    _refused(server, user, needle, db)
    _refused(server, user, needle, db, frozenset({other}))


def test_startup_and_admin_check_reject_view_security_alias_spoof(server):
    admin = server.admin
    db, user, other = "authz_r3_spoof", "r3_spoof_user", "other_r3_spoof"
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    select = f"SELECT cancer_study_identifier AS `a SQL SECURITY INVOKER b` FROM {db}.cancer_study"
    admin.command(f"CREATE VIEW {other}.leak DEFINER=admin SQL SECURITY DEFINER AS {select}")
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    assert server.http(f"SELECT count() FROM {other}.leak", user) == "2"
    _refused(server, user, "SQL SECURITY INVOKER", db)
    _refused(server, user, "SQL SECURITY INVOKER", db, frozenset({other}))
    admin.command(
        f"CREATE OR REPLACE VIEW {db}.clinical_attribute_counts "
        f"DEFINER=admin SQL SECURITY DEFINER AS {select}"
    )
    report = fetch_and_evaluate(admin, db, user)
    assert any("view is SQL SECURITY DEFINER" in error for error in report.errors)


@pytest.mark.parametrize("source_table", ["sample_derived", "mutation"])
def test_startup_check_renamed_study_copy_refused(server, source_table):
    admin = server.admin
    db, user, other = (
        f"authz_r3_copy_{source_table}",
        f"r3_copy_{source_table}",
        f"other_r3_{source_table}",
    )
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    admin.command(
        f"CREATE TABLE {other}.renamed ENGINE=Memory AS SELECT * FROM {db}_source.{source_table}"
    )
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    assert int(server.http(f"SELECT count() FROM {other}.renamed", user)) >= 9
    _refused(server, user, "not declared public", db)
    assert _startup_check(server, user, db, frozenset({other})).public_studies == 1


@pytest.mark.parametrize("default_security", ["INVOKER", "DEFINER"])
def test_clauseless_stored_view_is_invoker_under_both_defaults(server, default_security):
    from cbioportal_mcp.study_authz.coverage import view_sql_security

    admin = server.admin
    db, user, other = (
        f"authz_r3_implicit_{default_security.lower()}",
        f"r3_implicit_{default_security.lower()}",
        f"other_r3_implicit_{default_security.lower()}",
    )
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    admin.command(
        f"CREATE VIEW {other}.v AS SELECT count() AS n FROM {db}.cancer_study",
        settings={"default_normal_view_sql_security": default_security},
    )
    create = admin.query(
        "SELECT create_table_query FROM system.tables WHERE database={db:String} AND name='v'",
        parameters={"db": other},
    ).result_rows[0][0]
    assert "SQL SECURITY" not in create
    assert view_sql_security(create) == "INVOKER"
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    assert server.http(f"SELECT n FROM {other}.v", user) == "1"
    assert _startup_check(server, user, db).public_studies == 1


@pytest.mark.parametrize("grant", ["SELECT(sample_unique_id)", "SELECT"])
def test_startup_check_other_database_narrow_grant_refused(server, grant):
    admin = server.admin
    suffix = "column" if "(" in grant else "table"
    db, user, other = f"authz_c1_{suffix}", f"c1_{suffix}_user", f"other_c1_{suffix}"
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    admin.command(
        f"CREATE TABLE {other}.zz ENGINE=MergeTree ORDER BY tuple() "
        f"AS SELECT * FROM {db}_source.sample_derived"
    )
    admin.command(f"GRANT {grant} ON {other}.zz TO {user}")
    # A column grant hides the study-link column while still exposing private IDs.
    assert "priv_1:s" in server.studies_seen(f"SELECT sample_unique_id FROM {other}.zz", user)
    if suffix == "column":
        assert (
            server.http(
                "SELECT name FROM system.columns "
                f"WHERE database='{other}' AND table='zz' "
                "AND name='cancer_study_identifier'",
                user,
            )
            == ""
        )
    _refused(server, user, "table/column-scoped grants", db)
    _refused(server, user, "table/column-scoped grants", db, frozenset({other}))
    admin.command(f"REVOKE SELECT ON {other}.zz FROM {user}")
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    _refused(server, user, "not declared public", db)
    assert _startup_check(server, user, db, frozenset({other})).public_studies == 1


@pytest.mark.parametrize(
    "column,source_column",
    [
        ("CANCER_STUDY_ID", "cancer_study_id"),
        ("CANCER_STUDY_IDENTIFIER", "cancer_study_identifier"),
    ],
)
def test_startup_check_uppercase_study_column_detected(server, column, source_column):
    admin = server.admin
    suffix = "id" if column.endswith("_ID") else "identifier"
    db, user, other = f"authz_c1_upper_{suffix}", f"c1_upper_{suffix}", f"other_c1_upper_{suffix}"
    _p6_user_target_db(admin, db, user)
    admin.command(f"CREATE DATABASE {other}")
    admin.command(
        f"CREATE TABLE {other}.zz ENGINE=Memory AS "
        f"SELECT {source_column} AS {column} FROM {db}_source.cancer_study"
    )
    admin.command(f"GRANT SELECT ON {other}.* TO {user}")
    _refused(server, user, "not declared public", db)
    assert _startup_check(server, user, db, frozenset({other})).public_studies == 1
