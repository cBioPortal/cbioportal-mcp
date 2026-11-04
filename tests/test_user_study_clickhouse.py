"""Executable checks of the authorities SQL against a real ClickHouse server.

Runs the rendered sql/study-authz/authorities/*.sql on a throwaway native
ClickHouse (skipped when the `clickhouse` binary isn't installed - a failure
instead when CBIOPORTAL_MCP_REQUIRE_CLICKHOUSE is set, as in CI; see
tests/native_clickhouse.py) and checks
the *results*, not the SQL text:

- user_study expansion, as cBioPortal's CancerStudyPermissionEvaluator does
  it: direct study grants, ROLE_ prefixes, ';'-separated groups (empty
  entries ignored), ALL / ALL_TCGA / ALL_NCI_TARGET (+ ALL_TARGET_PHASE1/2),
  PUBLIC for known enabled users only, case handling - with the app-name
  filter on and off;
- the row policies as the restricted role: per-user rows through FK paths,
  user_study self-only, auth tables and the refresh MV at zero rows;
- freshness: with the refresh stopped, a revocation still applies while the
  data is inside the staleness bound, and once it is older than the bound
  every study-scoped table (and user_study itself) returns nothing; a
  successful refresh restores access minus the revoked grant.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
from native_clickhouse import clickhouse_binary, clickhouse_unavailable

from cbioportal_mcp.authentication.study_access import _lex_sql

REPO = Path(__file__).resolve().parent.parent
AUTHZ = REPO / "sql" / "study-authz"
CLICKHOUSE_BIN = clickhouse_binary()


@pytest.fixture(scope="module", autouse=True)
def _clickhouse_binary():
    if CLICKHOUSE_BIN is None:
        clickhouse_unavailable("clickhouse binary not installed")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_USERS_XML = """<clickhouse>
  <profiles><default/></profiles>
  <users><default><password></password><networks><ip>127.0.0.1</ip></networks>
    <profile>default</profile><quota>default</quota>
    <access_management>1</access_management></default></users>
  <quotas><default/></quotas>
</clickhouse>
"""


@pytest.fixture(scope="module")
def clickhouse(tmp_path_factory):
    root = tmp_path_factory.mktemp("ch_user_study")
    http_port, tcp_port = _free_port(), _free_port()
    (root / "users.xml").write_text(_USERS_XML)
    (root / "config.xml").write_text(
        f"""<clickhouse>
  <logger><level>warning</level><console>0</console><log>{root}/server.log</log>
    <errorlog>{root}/error.log</errorlog></logger>
  <http_port>{http_port}</http_port><tcp_port>{tcp_port}</tcp_port>
  <listen_host>127.0.0.1</listen_host>
  <path>{root}/data/</path><tmp_path>{root}/data/tmp/</tmp_path>
  <user_files_path>{root}/data/user_files/</user_files_path>
  <user_directories><users_xml><path>{root}/users.xml</path></users_xml>
    <local_directory><path>{root}/data/access/</path></local_directory></user_directories>
  <custom_settings_prefixes>SQL_</custom_settings_prefixes>
  <mark_cache_size>268435456</mark_cache_size>
</clickhouse>
"""
    )
    proc = subprocess.Popen(
        [CLICKHOUSE_BIN, "server", f"--config-file={root / 'config.xml'}"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 120
        while True:
            if proc.poll() is not None:
                clickhouse_unavailable(f"clickhouse exited early; see {root / 'error.log'}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{http_port}/ping", timeout=2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    clickhouse_unavailable("clickhouse did not start in 120s")
                time.sleep(0.3)
        import clickhouse_connect
        from clickhouse_connect import common

        common.set_setting("invalid_setting_action", "send")
        admin = clickhouse_connect.get_client(host="127.0.0.1", port=http_port, username="default")
        yield http_port, admin
        admin.close()
    finally:
        # A throwaway server: no need to wait out its graceful shutdown.
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _statements(sql: str) -> list[str]:
    """Split on ';' outside strings/comments (the SQL contains splitByChar(';'))."""
    out, current = [], []
    for token in _lex_sql(sql):
        if token.kind == "comment":
            continue
        if token.kind != "code":
            current.append(token.text)
            continue
        parts = token.text.split(";")
        for i, part in enumerate(parts):
            current.append(part)
            if i < len(parts) - 1:
                out.append("".join(current))
                current = []
    out.append("".join(current))
    return [s.strip() for s in out if s.strip()]


def _render(path: Path, database: str, **env: str) -> str:
    full_env = {**os.environ, "CLICKHOUSE_DATABASE": database, **env}
    return subprocess.run(
        ["bash", str(AUTHZ / "render.sh"), str(path)],
        env=full_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _apply(admin, sql: str, *, skip_alters: bool = False) -> None:
    for statement in _statements(sql):
        if skip_alters and statement.startswith("ALTER TABLE") and "SQL SECURITY" in statement:
            continue  # the sql/4-7 views don't exist in this minimal fixture
        admin.command(statement)


FIXTURE = """
CREATE TABLE {db}.users (email String, name String, enabled UInt8)
    ENGINE = MergeTree ORDER BY email;
CREATE TABLE {db}.authorities (email String, authority String)
    ENGINE = MergeTree ORDER BY email;
CREATE TABLE {db}.data_access_tokens (token String, username String)
    ENGINE = MergeTree ORDER BY token;
CREATE TABLE {db}.cancer_study (
    cancer_study_id Int64, cancer_study_identifier Nullable(String), `groups` Nullable(String)
) ENGINE = MergeTree ORDER BY cancer_study_id;
CREATE TABLE {db}.patient (internal_id Int64, stable_id String, cancer_study_id Int64)
    ENGINE = MergeTree ORDER BY internal_id;
INSERT INTO {db}.users VALUES
    ('Direct@Example.org', 'direct grant, mixed-case users row', 1),
    ('group@example.org', 'group grant via ROLE_ + upper-case app', 1),
    ('tcga@example.org', 'ALL_TCGA', 1),
    ('target@example.org', 'ALL_NCI_TARGET', 1),
    ('all@example.org', 'ALL', 1),
    ('plain@example.org', 'no authorities: PUBLIC only', 1),
    ('unprefixed@example.org', 'authorities without the app prefix', 1),
    ('disabled@example.org', 'disabled', 0);
INSERT INTO {db}.authorities VALUES
    ('direct@example.org', 'cbioportal:Study_A'),
    ('direct@example.org', 'otherapp:study_b'),
    ('group@example.org', 'ROLE_CBIOPORTAL:grp_x'),
    ('tcga@example.org', 'cbioportal:ALL_TCGA'),
    ('target@example.org', 'cbioportal:ALL_NCI_TARGET'),
    ('all@example.org', 'cbioportal:ALL'),
    ('unprefixed@example.org', 'study_b'),
    ('disabled@example.org', 'cbioportal:study_a'),
    ('ghost@example.org', 'cbioportal:study_a'),
    ('group@example.org', 'cbioportal:');
INSERT INTO {db}.cancer_study VALUES
    (1, 'study_a', NULL),
    (2, 'study_b', 'GRP_X;other'),
    (3, 'study_pub', 'PUBLIC'),
    (4, 'brca_tcga', ';grp_y;'),
    (5, 'nbl_target', NULL),
    (6, 'all_target_phase1', NULL),
    (7, 'not_target_suffix', 'grp_x ;');
INSERT INTO {db}.patient VALUES (11, 'P_A', 1), (12, 'P_B', 2), (13, 'P_PUB', 3), (14, 'P_T', 4);
"""

ALL_STUDIES = {
    "study_a", "study_b", "study_pub", "brca_tcga", "nbl_target",
    "all_target_phase1", "not_target_suffix",
}  # fmt: skip
EXPECTED_FILTERED = {
    # app-name filter on (cBioPortal's default)
    "direct@example.org": {"study_a", "study_pub"},  # otherapp: ignored
    "group@example.org": {"study_b", "study_pub"},  # 'grp_x ;' isn't GRP_X (no trim)
    "tcga@example.org": {"brca_tcga", "study_pub"},
    "target@example.org": {"nbl_target", "all_target_phase1", "study_pub"},
    "all@example.org": ALL_STUDIES,
    "plain@example.org": {"study_pub"},
    "unprefixed@example.org": {"study_pub"},  # 'study_b' has no app prefix
}
EXPECTED_UNFILTERED = {
    # filter_groups_by_appname=false: the whole authority is the grant
    "direct@example.org": {"study_pub"},  # 'CBIOPORTAL:STUDY_A' is no study or group
    "group@example.org": {"study_pub"},
    "tcga@example.org": {"study_pub"},
    "target@example.org": {"study_pub"},
    "all@example.org": {"study_pub"},
    "plain@example.org": {"study_pub"},
    "unprefixed@example.org": {"study_b", "study_pub"},
}


def _setup(admin, db: str, *, cancer_study_id_type: str = "Int64", **env: str) -> None:
    admin.command(f"DROP DATABASE IF EXISTS {db} SYNC")
    admin.command(f"CREATE DATABASE {db}")
    fixture = FIXTURE.format(db=db).replace(
        "cancer_study_id Int64, cancer_study_identifier",
        f"cancer_study_id {cancer_study_id_type}, cancer_study_identifier",
    )
    _apply(admin, fixture)
    _apply(admin, _render(AUTHZ / "authorities" / "0-user-study.sql", db, **env))


def _user_study(admin, db: str) -> dict[str, set[str]]:
    rows = admin.query(f"SELECT email, cancer_study_identifier FROM {db}.user_study").result_rows
    out: dict[str, set[str]] = {}
    for email, study in rows:
        out.setdefault(email, set()).add(study)
    return out


@pytest.mark.parametrize(
    ("filter_by_app", "expected"),
    [("true", EXPECTED_FILTERED), ("false", EXPECTED_UNFILTERED)],
)
def test_user_study_expansion_matches_cbioportal(clickhouse, filter_by_app, expected):
    _port, admin = clickhouse
    db = f"ent_filter_{filter_by_app}"
    _setup(admin, db, STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME=filter_by_app)
    # disabled@ and ghost@ (no users row) get nothing at all, not even PUBLIC.
    assert _user_study(admin, db) == expected
    stamps = admin.query(
        f"SELECT min(refreshed_at) > now() - 120, count() FROM {db}.user_study"
    ).result_rows
    assert stamps[0][0] == 1 and stamps[0][1] > 0


def test_public_group_can_be_disabled(clickhouse):
    _port, admin = clickhouse
    _setup(admin, "ent_no_public", STUDY_AUTHZ_PUBLIC_GROUP="")
    seen = _user_study(admin, "ent_no_public")
    assert "plain@example.org" not in seen
    assert seen["direct@example.org"] == {"study_a"}


@pytest.fixture(scope="module")
def restricted(clickhouse):
    """Policies applied for the authorities source, and a restricted runtime user."""
    port, admin = clickhouse
    db = "ent_policies"
    _setup(admin, db, STUDY_AUTHZ_REFRESH_MINUTES="5", STUDY_AUTHZ_MAX_STALENESS_SECONDS="600")
    _apply(
        admin,
        _render(AUTHZ / "authorities" / "1-row-policies.sql", db,
                STUDY_AUTHZ_REFRESH_MINUTES="5", STUDY_AUTHZ_MAX_STALENESS_SECONDS="600"),
        skip_alters=True,
    )  # fmt: skip
    admin.command("CREATE USER IF NOT EXISTS mcp_test IDENTIFIED WITH no_password")
    admin.command("GRANT cbioportal_mcp_study_restricted TO mcp_test")
    admin.command("SET DEFAULT ROLE cbioportal_mcp_study_restricted TO mcp_test")
    import clickhouse_connect

    user = clickhouse_connect.get_client(
        host="127.0.0.1", port=port, username="mcp_test", database=db
    )
    yield admin, user, db
    user.close()


def _as(user, email: str, query: str) -> set:
    rows = user.query(query, settings={"SQL_cbio_user": email, "readonly": "1"}).result_rows
    return {row[0] for row in rows}


def test_policies_filter_per_verified_email(restricted):
    _admin, user, _db = restricted
    studies = "SELECT cancer_study_identifier FROM cancer_study"
    assert _as(user, "direct@example.org", studies) == {"study_a", "study_pub"}
    assert _as(user, "group@example.org", studies) == {"study_b", "study_pub"}
    assert _as(user, "all@example.org", studies) == ALL_STUDIES
    assert _as(user, "disabled@example.org", studies) == set()
    assert _as(user, "", studies) == set()
    # Numeric FK path (patient.cancer_study_id -> user_study.cancer_study_id).
    assert _as(user, "tcga@example.org", "SELECT stable_id FROM patient") == {"P_T", "P_PUB"}
    # user_study is self-only; auth tables and the MV object are empty for the role.
    assert _as(user, "direct@example.org", "SELECT DISTINCT email FROM user_study") == {
        "direct@example.org"
    }
    for table in ("authorities", "users", "data_access_tokens", "user_study_refresh"):
        assert _as(user, "all@example.org", f"SELECT count() FROM {table}") == {0}, table


def test_stale_entitlements_deny_and_revocation_cannot_outlive_the_bound(restricted):
    admin, user, db = restricted
    studies = "SELECT cancer_study_identifier FROM cancer_study"
    patients = "SELECT stable_id FROM patient"
    # The refresh breaks (stopped here), then a grant is revoked.
    admin.command(f"SYSTEM STOP VIEW {db}.user_study_refresh")
    try:
        admin.command(
            f"ALTER TABLE {db}.authorities DELETE WHERE email = 'direct@example.org' "
            "AND authority = 'cbioportal:Study_A' SETTINGS mutations_sync = 2"
        )
        # Inside the staleness bound the last refresh still answers...
        assert _as(user, "direct@example.org", studies) == {"study_a", "study_pub"}
        # ...but once the last refresh is older than the bound (simulated by
        # ageing its stamp past 600s), nothing grants anything - for every user.
        admin.command(
            f"ALTER TABLE {db}.user_study UPDATE refreshed_at = now() - 601 WHERE 1 "
            "SETTINGS mutations_sync = 2"
        )
        for email in ("direct@example.org", "all@example.org", "plain@example.org"):
            assert _as(user, email, studies) == set(), email
            assert _as(user, email, patients) == set(), email
            assert _as(user, email, "SELECT email FROM user_study") == set(), email
    finally:
        admin.command(f"SYSTEM START VIEW {db}.user_study_refresh")
        admin.command(f"SYSTEM REFRESH VIEW {db}.user_study_refresh")
        admin.command(f"SYSTEM WAIT VIEW {db}.user_study_refresh")
    # A successful refresh restores access, without the revoked grant.
    assert _as(user, "direct@example.org", studies) == {"study_pub"}
    assert _as(user, "all@example.org", studies) == ALL_STUDIES


def test_reapplying_is_idempotent_and_upgrades_old_tables(clickhouse):
    _port, admin = clickhouse
    db = "ent_upgrade"
    admin.command(f"DROP DATABASE IF EXISTS {db} SYNC")
    admin.command(f"CREATE DATABASE {db}")
    _apply(admin, FIXTURE.format(db=db))
    # A user_study from before refreshed_at existed.
    admin.command(
        f"CREATE TABLE {db}.user_study (email String, cancer_study_identifier String, "
        "cancer_study_id Int64) ENGINE = MergeTree ORDER BY (email, cancer_study_identifier)"
    )
    sql = _render(AUTHZ / "authorities" / "0-user-study.sql", db)
    _apply(admin, sql)
    _apply(admin, sql)
    assert _user_study(admin, db) == EXPECTED_FILTERED


# --- the refresh-MV exemption, against ClickHouse's real stored DDL -------------


def _stored_mv(admin, db: str) -> str:
    return admin.query(
        "SELECT create_table_query FROM system.tables "
        f"WHERE database = '{db}' AND name = 'user_study_refresh'"
    ).result_rows[0][0]


def _runtime_formatter(client):
    """What the server's startup/periodic check does: formatQuerySingleLine on a literal."""
    from cbioportal_mcp.clickhouse_query_settings import clickhouse_string_literal

    def format_query(sql: str) -> str:
        literal = clickhouse_string_literal(sql)
        return client.query(f"SELECT formatQuerySingleLine({literal})").result_rows[0][0]

    return format_query


@pytest.mark.parametrize(
    ("db", "env"),
    [
        ("ent_mv_default", {}),
        ("ent_mv_hourly", {"STUDY_AUTHZ_REFRESH_MINUTES": "60"}),  # stored as 1 HOUR
        ("ent_mv_params", {
            "STUDY_AUTHZ_APP_NAME": "my.portal-app",
            "STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME": "false",
            "STUDY_AUTHZ_PUBLIC_GROUP": "",
            "STUDY_AUTHZ_REFRESH_MINUTES": "1440",
            "STUDY_AUTHZ_MAX_STALENESS_SECONDS": "100000",
        }),
    ],
)  # fmt: skip
def test_applied_refresh_mv_passes_the_exact_definition_check(clickhouse, restricted, db, env):
    from cbioportal_mcp.clickhouse_query_settings import _is_allowed_refresh_view

    _port, admin = clickhouse
    _ra, user, _rdb = restricted
    _setup(admin, db, **env)
    stored = _stored_mv(admin, db)
    # The restricted runtime role can run the formatter (no table access needed).
    assert _is_allowed_refresh_view("user_study_refresh", stored, db, _runtime_formatter(user))
    assert _is_allowed_refresh_view("user_study_refresh", stored, db, _runtime_formatter(admin))
    assert not _is_allowed_refresh_view(
        "user_study_refresh", stored, "other_db", _runtime_formatter(admin)
    )


@pytest.mark.parametrize("cancer_study_id_type", ["Int32", "UInt64"])
def test_applied_refresh_mv_passes_with_other_source_column_types(
    clickhouse, restricted, cancer_study_id_type
):
    # The stored header lists the SELECT's output types, which follow
    # cancer_study.cancer_study_id (Int64 upstream, Int32 in the local e2e
    # fixture). A correctly applied MV must not be refused over that.
    from cbioportal_mcp.clickhouse_query_settings import _is_allowed_refresh_view

    _port, admin = clickhouse
    _ra, user, _rdb = restricted
    db = f"ent_mv_{cancer_study_id_type.lower()}"
    _setup(admin, db, cancer_study_id_type=cancer_study_id_type)
    stored = _stored_mv(admin, db)
    assert f"`cancer_study_id` {cancer_study_id_type}" in stored
    assert _is_allowed_refresh_view("user_study_refresh", stored, db, _runtime_formatter(user))


@pytest.mark.parametrize("lookalike", ["cross_join", "comma_join"])
def test_review_lookalike_mvs_fail_the_check_on_real_clickhouse(clickhouse, lookalike):
    """The two definitions the round-2 review got past the previous validator,
    created for real under the exempted name and read back from system.tables."""
    from cbioportal_mcp.clickhouse_query_settings import _is_allowed_refresh_view

    _port, admin = clickhouse
    db = f"ent_lookalike_{lookalike}"
    admin.command(f"DROP DATABASE IF EXISTS {db} SYNC")
    admin.command(f"CREATE DATABASE {db}")
    admin.command("CREATE DATABASE IF NOT EXISTS secrets")
    admin.command(
        "CREATE TABLE IF NOT EXISTS secrets.entitlements (email String, s String) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )
    _apply(admin, FIXTURE.format(db=db))
    admin.command(
        f"CREATE TABLE {db}.user_study (email String, cancer_study_identifier String, "
        "cancer_study_id Int64, refreshed_at DateTime) "
        "ENGINE = MergeTree ORDER BY (email, cancer_study_identifier)"
    )
    header = (
        f"CREATE MATERIALIZED VIEW {db}.user_study_refresh REFRESH EVERY 5 MINUTE "
        f"TO {db}.user_study DEFINER = CURRENT_USER SQL SECURITY DEFINER AS "
    )
    body = {
        # every user every study, stamped ten years ahead
        "cross_join": (
            "SELECT lower(u.email) AS email, "
            "assumeNotNull(s.cancer_study_identifier) AS cancer_study_identifier, "
            "s.cancer_study_id AS cancer_study_id, now() + INTERVAL 10 YEAR AS refreshed_at "
            f"FROM {db}.users AS u CROSS JOIN {db}.cancer_study AS s"
        ),
        # another database through a comma join
        "comma_join": (
            "SELECT u.email AS email, x.s AS cancer_study_identifier, "
            "toInt64(0) AS cancer_study_id, now() AS refreshed_at "
            f"FROM {db}.users u, secrets.entitlements x"
        ),
    }[lookalike]
    admin.command(header + body)
    stored = _stored_mv(admin, db)
    assert not _is_allowed_refresh_view("user_study_refresh", stored, db, _runtime_formatter(admin))


# --- freshness can't be influenced by the caller -------------------------------


def test_epoch_and_future_stamps_and_caller_attempts(restricted):
    admin, user, db = restricted
    studies = "SELECT cancer_study_identifier FROM cancer_study"
    admin.command(f"SYSTEM STOP VIEW {db}.user_study_refresh")
    try:
        # Rows from before refreshed_at existed (epoch default) grant nothing.
        admin.command(
            f"ALTER TABLE {db}.user_study UPDATE refreshed_at = toDateTime(0) WHERE 1 "
            "SETTINGS mutations_sync = 2"
        )
        assert _as(user, "all@example.org", studies) == set()
        # The caller's session timezone doesn't move now() or the stamps.
        for tz in ("Pacific/Kiritimati", "Etc/GMT+12"):
            rows = user.query(
                studies,
                settings={"SQL_cbio_user": "all@example.org", "session_timezone": tz},
            ).result_rows
            assert rows == [], tz
        # The restricted role can't write or re-stamp user_study, or change the
        # bound from inside a query (readonly=1 rejects in-query SETTINGS).
        for statement in (
            f"INSERT INTO {db}.user_study VALUES ('all@example.org', 'study_a', 1, now())",
            f"ALTER TABLE {db}.user_study UPDATE refreshed_at = now() WHERE 1",
            # (a SETTINGS clause repeating the request's own value is a no-op
            # ClickHouse allows; any other identity is refused)
            f"{studies} SETTINGS SQL_cbio_user = 'direct@example.org'",
        ):
            with pytest.raises(Exception, match="(?i)not enough privileges|readonly|ACCESS_DENIED"):
                user.query(
                    statement, settings={"SQL_cbio_user": "all@example.org", "readonly": "1"}
                )
        assert _as(user, "all@example.org", studies) == set()
    finally:
        admin.command(f"SYSTEM START VIEW {db}.user_study_refresh")
        admin.command(f"SYSTEM REFRESH VIEW {db}.user_study_refresh")
        admin.command(f"SYSTEM WAIT VIEW {db}.user_study_refresh")
    assert _as(user, "all@example.org", studies) == ALL_STUDIES
