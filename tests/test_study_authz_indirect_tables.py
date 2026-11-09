"""Bare-table study policies against a private, throwaway Docker ClickHouse.

Run with ``uv run pytest tests/test_study_authz_indirect_tables.py``.
CBIOPORTAL_MCP_STUDY_AUTHZ_IMAGE overrides clickhouse/clickhouse-server:25.8.
No shared server or CLICKHOUSE_* credentials are used; all emails are fakes.
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
from datetime import datetime, timezone

import pytest

from cbioportal_mcp.study_authz import sqlgen
from cbioportal_mcp.study_authz.catalog import STUDY_SCOPED_TABLE_PATHS, AllOf, Via

IMAGE = os.getenv("CBIOPORTAL_MCP_STUDY_AUTHZ_IMAGE", "clickhouse/clickhouse-server:25.8")
DB = "authz_indirect"
USER = "llm_user"
PARAMS = sqlgen.Params(
    database=DB,
    public_group="PUBLIC",
    app_name="cbioportal",
    policy_target=USER,
    target_is_user=True,
)
TABLES = sorted(
    {table for table, path in STUDY_SCOPED_TABLE_PATHS.items() if isinstance(path, (Via, AllOf))}
    | {"resource_study"}
)
STUDIES = {1: "public", 2: "A", 3: "B", 4: "C"}
IDENTITIES = [
    pytest.param("ab@example.org", {1, 2, 3}, id="granted-A-B"),
    pytest.param("b@example.org", {1, 3}, id="granted-B"),
    pytest.param("c@example.org", {1, 4}, id="granted-C"),
    pytest.param("no-grants@example.org", {1}, id="no-grants"),
    pytest.param("unknown@example.org", {1}, id="unknown"),
    pytest.param("", {1}, id="empty"),
    pytest.param(None, {1}, id="setting-absent"),
]

# Deliberately independent of TABLES: a new catalog path must receive seed data
# here, rather than quietly disappearing from parametrization or testing emptiness.
# Only relationship columns are needed. Int64 IDs line up across each study;
# stable IDs exercise the String-key path as well.
SEED_COLUMNS = {
    "cancer_study": "cancer_study_id cancer_study_identifier groups",
    "patient": "internal_id cancer_study_id",
    "sample": "internal_id patient_id",
    "clinical_event": "clinical_event_id patient_id",
    "clinical_patient": "internal_id",
    "resource_patient": "internal_id",
    "clinical_sample": "internal_id",
    "resource_sample": "internal_id",
    "genetic_profile": "genetic_profile_id cancer_study_id stable_id",
    "allele_specific_copy_number": "genetic_profile_id",
    "alteration_driver_annotation": "genetic_profile_id",
    "genetic_alteration": "genetic_profile_id",
    "genetic_profile_link": "referring_genetic_profile_id referred_genetic_profile_id",
    "genetic_profile_samples": "genetic_profile_id",
    "mutation": "mutation_event_id genetic_profile_id sample_id",
    "mutation_count_by_keyword": "genetic_profile_id",
    "sample_cna_event": "cna_event_id genetic_profile_id",
    "sample_profile": "genetic_profile_id",
    "structural_variant": "genetic_profile_id",
    "generic_assay_data_derived": "profile_stable_id",
    "generic_assay_profile_entity_derived": "profile_stable_id",
    "clinical_event_data": "clinical_event_id",
    "gistic": "gistic_roi_id cancer_study_id",
    "gistic_to_gene": "gistic_roi_id",
    "sample_list": "list_id cancer_study_id",
    "sample_list_list": "list_id",
    "mutation_event": "mutation_event_id",
    "cna_event": "cna_event_id",
    "resource_study": "internal_id",
}
STRING_COLUMNS = {"stable_id", "profile_stable_id"}
NULLABLE_COLUMNS = {"cancer_study_identifier", "groups"}


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
        self.rows = {}
        self.expected = {}

    def select(self, table: str, email: str | None) -> list[str]:
        """No joins, filters, or admin privileges; settings travel on the request."""
        settings = {"readonly": 1}
        if email is not None:
            settings["SQL_cbio_user"] = email
        url = f"http://127.0.0.1:{self.port}/?" + urllib.parse.urlencode(settings)
        request = urllib.request.Request(
            url,
            data=f"SELECT row_tag FROM {DB}.{table} ORDER BY row_tag FORMAT TSV".encode(),
            headers={"X-ClickHouse-User": USER, "X-ClickHouse-Key": "pw"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode().splitlines()
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode()) from None


@pytest.fixture(scope="module")
def server():
    missing = set(TABLES) - SEED_COLUMNS.keys()
    assert not missing, f"Indirect catalog tables without seed data: {sorted(missing)}"
    name = f"study-authz-indirect-{uuid.uuid4().hex[:8]}"
    try:
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
        port_line = subprocess.run(
            ["docker", "port", name, "8123/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        port = int(port_line.rsplit(":", 1)[1])
        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2):
                    pass
                srv = Server(port)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        _load_fixture(srv)
        srv.admin.command(f"CREATE USER {USER} IDENTIFIED BY 'pw' DEFAULT DATABASE {DB}")
        sqlgen.apply(srv.admin, PARAMS, log=lambda *_: None)
        srv.admin.insert(
            f"{DB}.user_study_manual",
            [
                [email, study, datetime(2099, 1, 1, tzinfo=timezone.utc)]
                for email, studies in (
                    ("ab@example.org", ["A", "B"]),
                    ("b@example.org", ["B"]),
                    ("c@example.org", ["C"]),
                )
                for study in studies
            ],
            column_names=["email", "grant_key", "valid_until"],
        )
        srv.admin.command(f"SYSTEM REFRESH VIEW {DB}.user_study_refresh")
        srv.admin.command(f"SYSTEM WAIT VIEW {DB}.user_study_refresh")
        assert srv.admin.query(
            f"SELECT email, cancer_study_identifier FROM {DB}.user_study ORDER BY 1, 2"
        ).result_rows == [
            ("", "public"),
            ("ab@example.org", "A"),
            ("ab@example.org", "B"),
            ("b@example.org", "B"),
            ("c@example.org", "C"),
        ]
        yield srv
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _load_fixture(server: Server) -> None:
    admin = server.admin
    admin.command(f"CREATE DATABASE {DB}")
    for table, column_names in SEED_COLUMNS.items():
        columns = column_names.split()
        definitions = []
        for column in columns:
            kind = "Int64"
            if column in STRING_COLUMNS:
                kind = "String"
            elif column in NULLABLE_COLUMNS:
                kind = "Nullable(String)"
            definitions.append(f"`{column}` {kind}")
        admin.command(
            f"CREATE TABLE {DB}.{table} ({', '.join(definitions)}, row_tag String) "
            "ENGINE=MergeTree ORDER BY tuple()"
        )
        server.rows[table] = []
        server.expected[table] = {}
        for sid, study in STUDIES.items():
            row = {column: sid for column in columns}
            for column in STRING_COLUMNS & row.keys():
                row[column] = f"profile_{sid}"
            if table == "cancer_study":
                row.update(cancer_study_identifier=study, groups="PUBLIC" if sid == 1 else study)
            row["row_tag"] = study
            server.rows[table].append(row)
            server.expected[table][study] = {sid}

    # Every ordered pair: either endpoint may independently be inaccessible.
    for referring in STUDIES:
        for referred in STUDIES:
            if referring == referred:
                continue
            tag = f"link_{referring}_{referred}"
            server.rows["genetic_profile_link"].append(
                {
                    "referring_genetic_profile_id": referring,
                    "referred_genetic_profile_id": referred,
                    "row_tag": tag,
                }
            )
            server.expected["genetic_profile_link"][tag] = {referring, referred}

    for event, child, key in (
        ("mutation_event", "mutation", "mutation_event_id"),
        ("cna_event", "sample_cna_event", "cna_event_id"),
    ):
        for event_id, tag, studies in (
            (99, "shared_public_A", {1, 2}),
            (98, "only_A", {2}),
        ):
            server.rows[event].append({key: event_id, "row_tag": tag})
            server.expected[event][tag] = studies
            for sid in sorted(studies):
                row = dict(server.rows[child][sid - 1])
                row.update({key: event_id, "row_tag": f"{tag}_parent_{sid}"})
                server.rows[child].append(row)
                server.expected[child][row["row_tag"]] = {sid}

    for table, rows in server.rows.items():
        assert rows, f"No seed rows for {table}"
        columns = [*SEED_COLUMNS[table].split(), "row_tag"]
        admin.insert(
            f"{DB}.{table}",
            [[row[column] for column in columns] for row in rows],
            column_names=columns,
        )


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("email,allowed", IDENTITIES)
def test_bare_table_exact_rows(server, table, email, allowed):
    expected = sorted(
        tag
        for tag, studies in server.expected[table].items()
        if (studies <= allowed if table == "genetic_profile_link" else bool(studies & allowed))
    )
    actual = server.select(table, email)
    assert actual == expected, (
        f"{table}, email={email!r}: leaked={sorted(set(actual) - set(expected))}, "
        f"missing={sorted(set(expected) - set(actual))}; actual={actual}, expected={expected}"
    )


@pytest.mark.parametrize("table", ["mutation", "clinical_sample"])
@pytest.mark.parametrize("replacement", [False, True], ids=["dropped-policy", "using-1"])
def test_negative_control(server, table, replacement):
    """Prove both fail-closed deletion and a detectable permissive-policy leak."""
    policy = f"cbioportal_mcp_study_{table}"
    # Restore the exact generated policy, even if a control assertion fails.
    restore = next(
        statement
        for _, sql in sqlgen.rendered_files(PARAMS)
        for statement in sqlgen.statements(sql)
        if statement.startswith(f"CREATE ROW POLICY OR REPLACE {policy}\n")
    )
    try:
        server.admin.command(f"DROP ROW POLICY {policy} ON {DB}.{table}")
        if replacement:
            server.admin.command(f"CREATE ROW POLICY {policy} ON {DB}.{table} USING 1 TO {USER}")
        expected = sorted(row["row_tag"] for row in server.rows[table]) if replacement else []
        for email in (
            "ab@example.org",
            "b@example.org",
            "c@example.org",
            "no-grants@example.org",
            "unknown@example.org",
            "",
            None,
        ):
            assert server.select(table, email) == expected
    finally:
        server.admin.command(restore)
