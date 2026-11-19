"""Negative authorization matrix: a caller WITHOUT grants gets no gated study data.

End to end against a private, throwaway Docker ClickHouse and the real MCP
server process (``cbioportal_mcp.server:main``) with
CBIOPORTAL_MCP_STUDY_FILTERING=on and CBIOPORTAL_MCP_AUTH_ENFORCEMENT=enforce,
reached over HTTP through the LibreChat bridge headers. Every query form is run
for two callers:

- ``granted@example.org`` (control): holds a group grant covering every gated
  study, so each form must actually surface gated rows for it. A form that
  returns nothing for the control proves nothing, and fails.
- ``nogrant@example.org``: no grants; must never see a gated study, sample,
  patient id or row, through any form.

Run with ``uv run pytest tests/test_study_authz_negative_matrix.py``;
CBIOPORTAL_MCP_STUDY_AUTHZ_IMAGE overrides clickhouse/clickhouse-server:25.8
(also run on 26.6). No shared server or CLICKHOUSE_* credentials are used and
all emails are example.org fakes.

Every gated identifier carries the marker ``zq9secret``, and the public study
is ``open_pub``; a leak is any response for the no-grant caller that contains
the marker (or an aggregate/oracle that differs from its public-only value).
If a form ever leaks, do not weaken its assertion: mark it
``xfail(strict=True, reason="LEAK: ...")`` and report it.

Known leaks (BOUNDARY_LEAKS): on ClickHouse < 26.6 (verified 25.8.33.6)
``mergeTreeIndex()`` and ``mergeTreeProjection()`` ignore row policies. They are
strict xfails there and must pass on 26.6 (verified 26.6.8.7), which refuses them
on any table with a row policy.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
import test_study_authz_indirect_tables as indirect

from cbioportal_mcp.study_authz import sqlgen

IMAGE = os.getenv("CBIOPORTAL_MCP_STUDY_AUTHZ_IMAGE", "clickhouse/clickhouse-server:25.8")
DB = "authz_matrix"
USER = "llm_user"
GRANTED = "granted@example.org"
NOGRANT = "nogrant@example.org"
MARKER = "zq9secret"
PUBLIC = "open_pub"
GROUP = "GRP_ZQ9"
# Same study ids (1..4) and entity id spaces as the indirect-tables seed.
STUDIES = {1: PUBLIC, 2: f"{MARKER}_a", 3: f"{MARKER}_b", 4: f"{MARKER}_c"}
GATED = [ident for sid, ident in STUDIES.items() if sid != 1]
ABSENT = f"{MARKER}_z"  # same shape as a gated id, but in no table
SAMPLES_PER_STUDY = 60  # > domain_tools.MIN_PROFILED_SAMPLES, so cohort rows qualify
PREFERENCE = "authz_matrix_pref"
PARAMS = sqlgen.Params(
    database=DB,
    public_group="PUBLIC",
    app_name="cbioportal",
    policy_target=USER,
    target_is_user=True,
    private_grace_minutes=7 * 24 * 60,  # Frozen grants survive slow CI/debug runs.
)

# Extra (defaulted) columns the MCP tools read, on top of the indirect seed schema.
EXTRA_COLUMNS = {
    "cancer_study": [
        "name String DEFAULT concat('Study ', row_tag)",
        "description String DEFAULT concat('About ', row_tag)",
        "type_of_cancer_id String DEFAULT 'brca'",
        f"sample_count UInt64 DEFAULT {SAMPLES_PER_STUDY}",
    ],
    "genetic_profile": [
        "datatype String DEFAULT 'MAF'",
        "genetic_alteration_type String DEFAULT 'MUTATION_EXTENDED'",
    ],
    "mutation_event": ["entrez_gene_id Int64 DEFAULT 7157"],
}

# Directly study-scoped (identifier column) tables and reference tables, shaped
# like tests/aggregate_fixture.py. Partitioning, FINAL and SAMPLE need their engines.
DIRECT_DDL = [
    "CREATE TABLE {db}.sample_derived (sample_unique_id String, patient_unique_id String, "
    "cancer_study_identifier String, row_tag String) ENGINE = ReplacingMergeTree "
    "PARTITION BY cancer_study_identifier ORDER BY (cancer_study_identifier, sample_unique_id)",
    "CREATE TABLE {db}.clinical_data_derived (internal_id Int64, sample_unique_id String, "
    "patient_unique_id String, attribute_name String, attribute_value String, "
    "cancer_study_identifier String, type String, row_tag String) ENGINE = MergeTree "
    "ORDER BY intHash32(internal_id) SAMPLE BY intHash32(internal_id)",
    "CREATE TABLE {db}.sample_to_gene_panel_derived (sample_unique_id String, "
    "alteration_type String, gene_panel_id String, cancer_study_identifier String, "
    "genetic_profile_id String, row_tag String) ENGINE = MergeTree ORDER BY sample_unique_id",
    "CREATE TABLE {db}.genomic_event_derived (sample_unique_id String, hugo_gene_symbol String, "
    "entrez_gene_id Int32, gene_panel_stable_id String, cancer_study_identifier String, "
    "genetic_profile_stable_id String, variant_type String, mutation_status String, "
    "cna_alteration Nullable(Int8), patient_unique_id String, off_panel Boolean DEFAULT FALSE, "
    "mutation_variant String DEFAULT '', mutation_type String DEFAULT '', "
    "cna_cytoband String DEFAULT '', row_tag String) ENGINE = MergeTree "
    "ORDER BY (cancer_study_identifier, sample_unique_id)",
    "CREATE TABLE {db}.cancer_study_query_preferences (preference_name String, "
    "cancer_study_identifier String, notes String, row_tag String) ENGINE = MergeTree "
    "ORDER BY (preference_name, cancer_study_identifier)",
    "CREATE TABLE {db}.gene (entrez_gene_id Int64, hugo_gene_symbol String) "
    "ENGINE = MergeTree ORDER BY entrez_gene_id",
    "CREATE TABLE {db}.gene_panel (internal_id Int64, stable_id String) "
    "ENGINE = MergeTree ORDER BY internal_id",
    "CREATE TABLE {db}.gene_panel_list (internal_id Int64, gene_id Int64) "
    "ENGINE = MergeTree ORDER BY internal_id",
]
# Stand-ins named like catalog views (SQL SECURITY INVOKER, like sql/4, 5, 7).
VIEW_DDL = [
    "CREATE VIEW {db}.clinical_attribute_counts SQL SECURITY INVOKER AS "
    "SELECT cancer_study_identifier, attribute_name, count() AS n "
    "FROM {db}.clinical_data_derived GROUP BY cancer_study_identifier, attribute_name",
    "CREATE VIEW {db}.cna_wes_coverage SQL SECURITY INVOKER AS "
    "SELECT row_tag, genetic_profile_id FROM {db}.sample_cna_event",
    # Parameterized, like gene_mutation_frequency_by_cancer_type(preference, gene).
    "CREATE VIEW {db}.gene_mutation_variants_in_study SQL SECURITY INVOKER AS "
    "SELECT cancer_study_identifier, sample_unique_id, mutation_variant "
    "FROM {db}.genomic_event_derived WHERE hugo_gene_symbol = {{gene:String}}",
]
VIEWS = ["clinical_attribute_counts", "cna_wes_coverage", "gene_mutation_variants_in_study"]
DIRECT_TABLES = [
    "sample_derived",
    "clinical_data_derived",
    "sample_to_gene_panel_derived",
    "genomic_event_derived",
    "cancer_study_query_preferences",
]


# --- ClickHouse + MCP server fixtures (one container and one server per module) ---


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, deadline: float, ok=lambda r: True):
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read().decode()
                if ok(body):
                    return body
                last = body
        except Exception as e:  # noqa: BLE001 - retried until the deadline
            last = e
        time.sleep(0.5)
    raise TimeoutError(f"{url} not ready: {last!r}")


@pytest.fixture(scope="module")
def clickhouse():
    if not indirect.DOCKER_AVAILABLE:
        if os.getenv("CI", "").strip().lower() in {"1", "true", "yes"}:
            pytest.fail("Docker is required when CI is truthy")
        pytest.skip("Docker is not available")
    import clickhouse_connect

    name = f"study-authz-matrix-{uuid.uuid4().hex[:8]}"
    try:
        run = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8123",
             "-e", "CLICKHOUSE_USER=admin", "-e", "CLICKHOUSE_PASSWORD=admin",
             "-e", "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1", IMAGE],
            capture_output=True, text=True,
        )  # fmt: skip
        if run.returncode:
            pytest.fail(f"could not start {IMAGE}: {run.stderr.strip()[:1000]}")
        port_line = subprocess.run(
            ["docker", "port", name, "8123/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        port = int(port_line.rsplit(":", 1)[1])
        _wait_http(f"http://127.0.0.1:{port}/ping", time.monotonic() + 90)
        admin = clickhouse_connect.get_client(
            host="127.0.0.1", port=port, username="admin", password="admin"
        )
        _load_fixture(admin)
        admin.command(f"CREATE USER {USER} IDENTIFIED BY 'pw' DEFAULT DATABASE {DB}")
        sqlgen.apply(admin, PARAMS, log=lambda *_: None)
        admin.insert(
            f"{DB}.user_study_manual",
            [[GRANTED, GROUP.lower(), datetime(2099, 1, 1, tzinfo=timezone.utc)]],
            column_names=["email", "grant_key", "valid_until"],
        )
        admin.command(f"SYSTEM REFRESH VIEW {DB}.user_study")
        admin.command(f"SYSTEM WAIT VIEW {DB}.user_study")
        grants = admin.query(
            f"SELECT email, cancer_study_identifier FROM {DB}.user_study ORDER BY 1, 2"
        ).result_rows
        assert grants == [("", PUBLIC)] + [(GRANTED, g) for g in GATED], grants
        # Pin the grant snapshot for the whole module.
        admin.command(f"SYSTEM STOP VIEW {DB}.user_study")
        admin.command(f"SYSTEM WAIT VIEW {DB}.user_study")
        yield {"port": port, "admin": admin, "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _load_fixture(admin) -> None:
    """Every indirect catalog table (indirect.SEED_COLUMNS, same id spaces) has one
    row per study, plus the direct-identifier tables, reference tables and views."""
    admin.command(f"CREATE DATABASE {DB}")
    for table, column_names in indirect.SEED_COLUMNS.items():
        columns = column_names.split()
        definitions = []
        for column in columns:
            kind = "Int64"
            if column in indirect.STRING_COLUMNS:
                kind = "String"
            elif column in indirect.NULLABLE_COLUMNS:
                kind = "Nullable(String)"
            definitions.append(f"`{column}` {kind}")
        definitions += ["row_tag String", *EXTRA_COLUMNS.get(table, [])]
        admin.command(
            f"CREATE TABLE {DB}.{table} ({', '.join(definitions)}) "
            "ENGINE = MergeTree ORDER BY tuple()"
        )
        rows = []
        for sid, ident in STUDIES.items():
            row = {c: indirect._seed_value(table, c, sid) for c in columns}
            if table == "cancer_study":
                row.update(cancer_study_identifier=ident, groups="PUBLIC" if sid == 1 else GROUP)
            row["row_tag"] = ident
            rows.append(row)
        if table == "genetic_profile_link":
            # Only a self-link keeps both endpoints public; every other pair is gated.
            rows = [
                {
                    "referring_genetic_profile_id": indirect._entity_id("genetic_profile", a),
                    "referred_genetic_profile_id": indirect._entity_id("genetic_profile", b),
                    "row_tag": f"link:{STUDIES[a]}>{STUDIES[b]}",
                }
                for a in STUDIES
                for b in STUDIES
                if a == b == 1 or a != b
            ]
        names = [*columns, "row_tag"]
        admin.insert(f"{DB}.{table}", [[r[c] for c in names] for r in rows], column_names=names)

    for statement in DIRECT_DDL + VIEW_DDL:
        admin.command(statement.format(db=DB))
    # A projection, so mergeTreeProjection() has something to read.
    admin.command(f"ALTER TABLE {DB}.genomic_event_derived ADD PROJECTION p "
                  "(SELECT sample_unique_id, cancer_study_identifier ORDER BY sample_unique_id)")
    samples, clinical, panels, events, prefs = [], [], [], [], []
    for sid, ident in STUDIES.items():
        prefs.append([PREFERENCE, ident, "", ident])
        for n in range(SAMPLES_PER_STUDY):
            sample, patient = f"{ident}:S{n}", f"{ident}:P{n}"
            samples.append([sample, patient, ident, ident])
            clinical.append([sid * 1000 + n, sample, patient, "CANCER_TYPE",
                             f"{ident} carcinoma", ident, "SAMPLE", ident])  # fmt: skip
            panels.append([sample, "MUTATION_EXTENDED", "PANEL_X", ident, f"{ident}_mut", ident])
            events.append([sample, "TP53", 7157, "PANEL_X", ident, f"{ident}_mut", "mutation",
                           "SOMATIC", None, patient, False, "R175H", "Missense", "",
                           ident])  # fmt: skip
    admin.insert(f"{DB}.sample_derived", samples)
    admin.insert(f"{DB}.clinical_data_derived", clinical)
    admin.insert(f"{DB}.sample_to_gene_panel_derived", panels)
    admin.insert(f"{DB}.genomic_event_derived", events)
    admin.insert(f"{DB}.cancer_study_query_preferences", prefs)
    admin.insert(f"{DB}.gene", [[7157, "TP53"]])
    admin.insert(f"{DB}.gene_panel", [[1, "PANEL_X"]])
    admin.insert(f"{DB}.gene_panel_list", [[1, 7157]])


@dataclass
class Reply:
    status: int
    result: dict | None = None  # JSON-RPC "result" of a tools/call
    error: dict | None = None  # JSON-RPC "error"
    text: str = ""  # everything the caller received, for marker scans

    @property
    def payload(self):
        """The tool's own return value (dict / list / str)."""
        if self.result is None:
            return None
        structured = self.result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        if structured is not None:
            return structured
        texts = [c.get("text", "") for c in self.result.get("content", [])]
        try:
            return json.loads(texts[0])
        except (IndexError, ValueError):
            return "\n".join(texts)


@dataclass
class Mcp:
    url: str
    secret: str
    log_path: str
    version: tuple = (0, 0)
    inner: str = ""  # user_study's implicit inner table
    sessions: dict = field(default_factory=dict)

    def headers(self, email: str | None, **overrides) -> dict:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "x-cbioportal-mcp-bridge-secret": self.secret,
            "x-user-email": email or "",
            "x-user-id": f"id-{email}",
            "x-user-email-verified": "true",
        }
        for key, value in overrides.items():
            key = key.replace("_", "-")
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        return headers

    def post(self, message: dict, headers, session: str | None = None):
        import httpx

        if session:
            headers = [*(headers.items() if isinstance(headers, dict) else headers),
                       ("mcp-session-id", session)]  # fmt: skip
        return httpx.post(self.url, json=message, headers=headers, timeout=60)

    def open(self, headers) -> tuple[int, str | None]:
        """initialize + notifications/initialized; returns (status, session id)."""
        init = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "authz-negative-matrix", "version": "0"},
            },
        }
        response = self.post(init, headers)
        if response.status_code != 200:
            return response.status_code, None
        session = response.headers.get("mcp-session-id")
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert self.post(note, headers, session).status_code in (200, 202)
        return 200, session

    def call(self, tool: str, args: dict, email: str | None = None, *, headers=None,
             session: str | None = None) -> Reply:  # fmt: skip
        if headers is None:
            headers = self.headers(email)
            if session is None:
                if email not in self.sessions:
                    status, opened = self.open(headers)
                    assert status == 200, f"bridge login failed for {email}: {status}"
                    self.sessions[email] = opened
                session = self.sessions[email]
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        response = self.post(message, headers, session)
        reply = Reply(response.status_code, text=response.text)
        if response.status_code != 200:
            return reply
        body = response.text
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            datas = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            messages = [json.loads(d) for d in datas if d]
            body_json = next(m for m in messages if m.get("id") == 1)
        else:
            body_json = response.json()
        reply.result, reply.error = body_json.get("result"), body_json.get("error")
        reply.text = json.dumps(body_json)
        return reply

    def sql(self, query: str, email: str) -> Reply:
        return self.call("clickhouse_run_select_query", {"query": query}, email)


@pytest.fixture(scope="module")
def mcp(clickhouse, tmp_path_factory):
    port = _free_port()
    secret = secrets.token_urlsafe(24)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLICKHOUSE", "CBIOPORTAL_MCP", "DD_", "OTEL_", "REDIS"))
    }
    env.update({
        "CLICKHOUSE_HOST": "127.0.0.1", "CLICKHOUSE_PORT": str(clickhouse["port"]),
        "CLICKHOUSE_USER": USER, "CLICKHOUSE_PASSWORD": "pw", "CLICKHOUSE_DATABASE": DB,
        "CLICKHOUSE_SECURE": "false", "CLICKHOUSE_VERIFY": "false",
        "CLICKHOUSE_MCP_SERVER_TRANSPORT": "http", "CLICKHOUSE_MCP_BIND_HOST": "127.0.0.1",
        "CLICKHOUSE_MCP_BIND_PORT": str(port),
        "CBIOPORTAL_MCP_STUDY_FILTERING": "on",
        "CBIOPORTAL_MCP_AUTH_ENFORCEMENT": "enforce",
        "CBIOPORTAL_MCP_AUTH_TEST_USERS": "",
        "CBIOPORTAL_MCP_PUBLIC_DB_CHECK": "enforce",
        "CBIOPORTAL_MCP_STUDY_FILTERING_PUBLIC_DATABASES": "",
        "CBIOPORTAL_MCP_BRIDGE_SECRET": secret,
    })  # fmt: skip
    log_path = tmp_path_factory.mktemp("matrix") / "mcp.log"
    log = open(log_path, "w")  # noqa: SIM115 - closed below
    process = subprocess.Popen(
        [sys.executable, "-c", "from cbioportal_mcp.server import main; main()"],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        try:
            health = _wait_http(
                f"http://127.0.0.1:{port}/health",
                time.monotonic() + 120,
                ok=lambda body: '"reason":"ready"' in body.replace(" ", ""),
            )
        except TimeoutError:
            log.flush()
            pytest.fail(f"MCP never became ready:\n{open(log_path).read()[-4000:]}")
        assert json.loads(health)["study_data_available"] is True
        admin = clickhouse["admin"]
        server = Mcp(f"http://127.0.0.1:{port}/mcp", secret, str(log_path))
        server.version = tuple(int(x) for x in admin.server_version.split(".")[:2])
        server.inner = ".inner_id." + admin.query(
            f"SELECT toString(uuid) FROM system.tables WHERE database = '{DB}' "
            "AND name = 'user_study'"
        ).result_rows[0][0]
        # Prime the logs a no-grant caller might read: the control names gated
        # ids in query text and in an error, before any matrix case runs.
        server.sql(f"SELECT count() FROM cancer_study WHERE row_tag = '{GATED[0]}'", GRANTED)
        server.sql("SELECT toInt64(row_tag) FROM cancer_study ORDER BY row_tag DESC", GRANTED)
        clickhouse["admin"].command("SYSTEM FLUSH LOGS")
        yield server
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


# --- The matrix ---------------------------------------------------------------

ROWS = "rows"  # control sees the marker; no-grant sees no marker (and no error)
VALUE = "value"  # first cell equals (nogrant, granted)
SAFE = "safe"  # no-grant never sees the marker (an error is fine)
ORACLE = "oracle"  # existence oracle: control errors, no-grant must not
ERROR_TEXT = "error_text"  # both error; only the control's error may name gated ids


@dataclass(frozen=True)
class Case:
    id: str
    sql: str
    kind: str = ROWS
    expected: tuple = ()
    public_row: bool = True  # ROWS: the no-grant result must include open_pub
    leak: str | None = None  # "LEAK: ..." -> xfail(strict=True)
    fixed_in: tuple | None = None  # leak only below this ClickHouse (major, minor)


def _rows_cases():
    study_tables = sorted(set(indirect.SEED_COLUMNS) | set(DIRECT_TABLES))
    yield from (Case(f"bare-{t}", f"SELECT * FROM {t}") for t in study_tables)
    yield from (Case(f"bare-view-{v}", f"SELECT * FROM {v}") for v in VIEWS[:-1])
    yield Case(
        "bare-view-parameterized",
        "SELECT * FROM gene_mutation_variants_in_study(gene = 'TP53')",
    )
    yield Case("bare-user_study", "SELECT * FROM user_study")
    yield Case("qualified-quoted", f"SELECT * FROM `{DB}`.`patient`")
    n, g = SAMPLES_PER_STUDY, SAMPLES_PER_STUDY * len(STUDIES)
    yield from [
        # Aggregates.
        Case("count-cancer_study", "SELECT count() FROM cancer_study", VALUE, ("1", "4")),
        Case("count-sample", "SELECT count() FROM sample", VALUE, ("1", "4")),
        Case("count-mutation_event", "SELECT count() FROM mutation_event", VALUE, ("1", "4")),
        Case("count-sample_derived", "SELECT count() FROM sample_derived", VALUE, (str(n), str(g))),
        Case("count-star-link", "SELECT count(*) FROM genetic_profile_link", VALUE, ("1", "13")),
        Case("uniq-patient", "SELECT uniq(row_tag) FROM patient", VALUE, ("1", "4")),
        Case("uniqExact-samples", "SELECT uniqExact(sample_unique_id) FROM clinical_data_derived",
             VALUE, (str(n), str(g))),  # fmt: skip
        Case("sum-ids", "SELECT sum(cancer_study_id) FROM cancer_study", VALUE, ("1", "10")),
        Case("min-hidden-id", "SELECT min(cancer_study_id) FROM cancer_study "
             "WHERE cancer_study_id > 1", VALUE, ("0", "2")),  # fmt: skip
        Case("countIf-prefix", "SELECT countIf(startsWith(row_tag, 'zq9')) FROM sample_derived",
             VALUE, ("0", str(g - n))),  # fmt: skip
        Case("groupArray", "SELECT groupArray(row_tag) FROM mutation"),
        Case("groupUniqArray", "SELECT groupUniqArray(row_tag) FROM clinical_event_data"),
        Case("max", "SELECT max(row_tag) FROM sample"),
        Case("argMax", "SELECT argMax(row_tag, cancer_study_id) FROM cancer_study"),
        Case("topK", "SELECT topK(10)(cancer_study_identifier) FROM genomic_event_derived"),
        Case("any-last", "SELECT anyLast(row_tag) FROM (SELECT row_tag FROM sample_profile "
             "ORDER BY row_tag)"),  # fmt: skip
        # DISTINCT / FINAL / SAMPLE / ARRAY JOIN / LIMIT.
        Case("distinct", "SELECT DISTINCT row_tag FROM mutation"),
        Case("distinct-on-derived", "SELECT DISTINCT cancer_study_identifier FROM sample_derived"),
        Case("count-distinct", "SELECT count(DISTINCT row_tag) FROM genetic_alteration",
             VALUE, ("1", "4")),  # fmt: skip
        Case("final", "SELECT * FROM sample_derived FINAL"),
        Case("sample-1", "SELECT * FROM clinical_data_derived SAMPLE 1"),
        Case("sample-ratio-offset", "SELECT * FROM clinical_data_derived SAMPLE 1/1 OFFSET 0"),
        Case("array-join", "SELECT t FROM sample ARRAY JOIN [row_tag, 'x'] AS t"),
        Case("array-join-agg", "SELECT t FROM (SELECT groupArray(row_tag) AS a FROM patient) "
             "ARRAY JOIN a AS t"),  # fmt: skip
        Case("arrayJoin-fn", "SELECT arrayJoin([row_tag]) FROM structural_variant"),
        Case("limit-offset", "SELECT row_tag FROM patient ORDER BY row_tag LIMIT 3 OFFSET 1",
             public_row=False),  # fmt: skip
        Case("limit-desc", "SELECT row_tag FROM sample ORDER BY row_tag DESC LIMIT 1"),
        Case("limit-by", "SELECT row_tag FROM sample_derived ORDER BY row_tag LIMIT 1 BY row_tag"),
        # Joins from reference tables into study tables.
        Case("join-gene-mutation_event", "SELECT g.hugo_gene_symbol, me.row_tag FROM gene g "
             "JOIN mutation_event me ON me.entrez_gene_id = g.entrez_gene_id"),  # fmt: skip
        Case("left-join-gene-events", "SELECT g.hugo_gene_symbol, e.sample_unique_id FROM gene g "
             "LEFT JOIN genomic_event_derived e ON e.entrez_gene_id = g.entrez_gene_id"),
        Case("join-gene_panel-stgp", "SELECT gp.stable_id, s.sample_unique_id FROM gene_panel gp "
             "JOIN sample_to_gene_panel_derived s ON s.gene_panel_id = gp.stable_id"),
        Case("right-join", "SELECT gp.stable_id, s.row_tag FROM gene_panel gp RIGHT JOIN "
             "sample_to_gene_panel_derived s ON s.gene_panel_id = gp.stable_id"),
        Case("full-join", "SELECT g.hugo_gene_symbol, m.row_tag FROM gene g FULL OUTER JOIN "
             "mutation_event m ON m.entrez_gene_id = g.entrez_gene_id"),
        Case("cross-join", "SELECT g.hugo_gene_symbol, cs.row_tag FROM gene g "
             "CROSS JOIN cancer_study cs"),  # fmt: skip
        Case("comma-join", "SELECT gp.stable_id, s.row_tag FROM gene_panel gp, sample s"),
        Case("paste-join", "SELECT * FROM (SELECT hugo_gene_symbol FROM gene) a PASTE JOIN "
             "(SELECT row_tag FROM sample_list ORDER BY row_tag DESC) b"),
        Case("join-gene-list-chain", "SELECT ge.hugo_gene_symbol, s.sample_unique_id "
             "FROM gene ge JOIN gene_panel_list gpl ON gpl.gene_id = ge.entrez_gene_id "
             "JOIN gene_panel gp ON gp.internal_id = gpl.internal_id "
             "JOIN sample_to_gene_panel_derived s ON s.gene_panel_id = gp.stable_id"),
        Case("join-study-to-study", "SELECT p.row_tag, s.row_tag FROM patient p "
             "JOIN sample s ON s.patient_id = p.internal_id"),  # fmt: skip
        # CTEs, subqueries, set operations, windows.
        Case("cte", "WITH s AS (SELECT row_tag FROM sample) SELECT * FROM s"),
        Case("cte-scalar", "WITH (SELECT groupArray(row_tag) FROM patient) AS tags SELECT tags"),
        Case("cte-recursive", "WITH RECURSIVE r AS (SELECT row_tag, 1 AS d FROM gistic "
             "UNION ALL SELECT row_tag, d + 1 FROM r WHERE d < 2) SELECT * FROM r"),
        Case("subquery-from", "SELECT * FROM (SELECT * FROM clinical_event)"),
        Case("subquery-scalar", "SELECT (SELECT max(row_tag) FROM clinical_patient)"),
        Case("subquery-in", "SELECT row_tag FROM sample_list WHERE list_id IN "
             "(SELECT list_id FROM sample_list_list)"),  # fmt: skip
        Case("exists-oracle", "SELECT EXISTS(SELECT 1 FROM cancer_study "
             "WHERE startsWith(row_tag, 'zq9'))", VALUE, ("0", "1")),  # fmt: skip
        Case("in-literal-oracle", f"SELECT '{GATED[0]}' IN (SELECT cancer_study_identifier "
             "FROM cancer_study)", VALUE, ("0", "1")),  # fmt: skip
        Case("union-all", "SELECT hugo_gene_symbol AS t FROM gene "
             "UNION ALL SELECT row_tag FROM structural_variant"),  # fmt: skip
        Case("union-distinct", "SELECT row_tag FROM gistic_to_gene "
             "UNION DISTINCT SELECT row_tag FROM resource_sample"),  # fmt: skip
        Case("except", f"SELECT row_tag FROM sample EXCEPT SELECT '{PUBLIC}'", public_row=False),
        Case("intersect", "SELECT row_tag FROM sample INTERSECT SELECT row_tag FROM patient"),
        Case("window", "SELECT row_tag, row_number() OVER () FROM gistic"),
        Case("rollup", "SELECT row_tag, count() FROM gistic_to_gene GROUP BY row_tag WITH ROLLUP"),
    ]


def _identity_cases():
    """Attempts to act as the control caller. Rejected, or no gated rows."""
    target = f"'{GRANTED}'"
    base = "SELECT row_tag FROM cancer_study"
    forms = {
        "settings": f"{base} SETTINGS SQL_cbio_user = {target}",
        "settings-lowercase": f"{base} settings sql_cbio_user = {target}",
        "settings-uppercase": f"{base} SETTINGS SQL_CBIO_USER = {target}",
        "settings-double-quoted": f'{base} SETTINGS "SQL_cbio_user" = {target}',
        "settings-backquoted": f"{base} SETTINGS `SQL_cbio_user` = {target}",
        "settings-escaped-ident": f"{base} SETTINGS `SQL\\x5fcbio_user` = {target}",
        "settings-comment": f"{base} SETTINGS /* x */ SQL_cbio_user /* y */ = {target}",
        "settings-line-comment": f"{base} SETTINGS -- x\n SQL_cbio_user = {target}",
        "settings-concat": f"{base} SETTINGS SQL_cbio_user = concat('granted', '@example.org')",
        "settings-in-subquery": f"SELECT * FROM ({base} SETTINGS SQL_cbio_user = {target})",
        "settings-readonly-0": f"{base} SETTINGS readonly = 0",
        "settings-table-filter": f"{base} SETTINGS additional_table_filters = "
        "{'cancer_study': '1'}",
        "set-multi-statement": f"SET SQL_cbio_user = {target}; {base}",
        "multi-statement-trailing": f"{base}; SET SQL_cbio_user = {target}",
        "multi-statement-select": f"SELECT 1; {base}",
        "getSetting": "SELECT getSetting('SQL_cbio_user')",
        "getSettingOrDefault": "SELECT getSettingOrDefault('SQL_cbio_user', '')",
        "getSetting-concat": "SELECT getSetting(concat('SQL_', 'cbio_user'))",
        "getSetting-case": "SELECT GETSETTING('SQL_cbio_user')",
        "getSetting-backquoted-fn": "SELECT `getSetting`('SQL_cbio_user')",
        "system.settings": "SELECT name, value FROM system.settings WHERE name ILIKE 'sql%'",
        "system.settings-quoted": "SELECT * FROM `system`.`settings` WHERE changed",
        "system.settings-spaced": "SELECT * FROM system . settings WHERE changed",
        "system.settings_changes": "SELECT * FROM system.settings_changes",
        "query-parameter": "SELECT {SQL_cbio_user:String}",
        "user_study-other-emails": "SELECT * FROM user_study WHERE email != ''",
        "user_study_manual": "SELECT * FROM user_study_manual",
    }
    for name, sql in forms.items():
        yield Case(f"identity-{name}", sql, SAFE)


# Known leaks, by the first ClickHouse version verified to block them (25.8.33.6
# leaks, 26.6.8.7 refuses: "Cannot read from `mergeTreeIndex` because a row
# policy is applied on table ..."). Below that version they are strict xfails.
FIXED_IN = (26, 6)
_INDEX_LEAK = (
    "LEAK (ClickHouse < 26.6): mergeTreeIndex() ignores row policies and returns the "
    "sorting-key values of every granule's first row"
)
BOUNDARY_LEAKS = {
    "mergeTreeIndex": _INDEX_LEAK + ": gated cancer_study_identifier, sample_unique_id",
    "mergeTreeIndex-current": _INDEX_LEAK + ": gated cancer_study_identifier, sample_unique_id",
    "mergeTreeIndex-minmax": _INDEX_LEAK + ", and per-part partition min/max (gated study ids)",
    "mergeTreeIndex-stgp": _INDEX_LEAK + ": gated sample_unique_id",
    "mergeTreeIndex-preferences": _INDEX_LEAK + ": gated cancer_study_identifier",
    "mergeTreeIndex-user_study_manual": _INDEX_LEAK + " of the denied user_study_manual: "
    "other callers' emails and grant keys",
    "mergeTreeIndex-user_study-inner": _INDEX_LEAK + " of user_study's inner table: other "
    "callers' emails and their gated cancer_study_identifier",
    "mergeTreeProjection": "LEAK (ClickHouse < 26.6): mergeTreeProjection() ignores row "
    "policies and returns every row of the projection (gated sample/study ids)",
}


def _boundary_cases():
    """Table functions, dictionaries, system tables, virtual columns, EXPLAIN."""
    q = "SELECT+row_tag+FROM+authz_matrix.cancer_study"
    forms = {
        "remote-admin": f"SELECT * FROM remote('127.0.0.1:9000', {DB}.cancer_study, "
        "'admin', 'admin')",
        "remote-default": f"SELECT * FROM remote('127.0.0.1:9000', {DB}, cancer_study)",
        "remoteSecure": f"SELECT * FROM remoteSecure('127.0.0.1:9440', {DB}.cancer_study)",
        "cluster": f"SELECT * FROM cluster('default', {DB}, cancer_study)",
        "clusterAllReplicas": f"SELECT * FROM clusterAllReplicas('default', {DB}.cancer_study)",
        "merge-db": f"SELECT * FROM merge('{DB}', '^cancer_study$')",
        "merge-current": "SELECT row_tag FROM merge('^sample')",
        "merge-regexp-db": "SELECT row_tag FROM merge(REGEXP('^authz'), '^patient$')",
        "url": f"SELECT * FROM url('http://127.0.0.1:8123/?user=admin&password=admin&query={q}',"
        " 'TSV', 'x String')",
        "file": "SELECT * FROM file('/etc/passwd', 'LineAsString')",
        "file-store": "SELECT * FROM file('../store/*/*/*/*/row_tag.bin', 'RawBLOB')",
        "mysql": f"SELECT * FROM mysql('127.0.0.1:9004', '{DB}', 'cancer_study', 'admin', "
        "'admin')",
        "postgresql": "SELECT * FROM postgresql('127.0.0.1:9005', 'x', 'y', 'admin', 'admin')",
        "s3": f"SELECT * FROM s3('http://127.0.0.1:8123/?user=admin&password=admin&query={q}', "
        "'TSV', 'x String')",
        "executable": "SELECT * FROM executable('cat', 'TSV', 'x String')",
        "input": "SELECT * FROM input('x String')",
        "mergeTreeIndex": f"SELECT * FROM mergeTreeIndex({DB}, sample_derived, with_marks = 1)",
        "mergeTreeIndex-current": "SELECT * FROM mergeTreeIndex(currentDatabase(), "
        "genomic_event_derived)",
        "mergeTreeIndex-minmax": f"SELECT * FROM mergeTreeIndex({DB}, sample_derived, "
        "with_marks = 1, with_minmax = 1)",
        "mergeTreeIndex-stgp": f"SELECT * FROM mergeTreeIndex({DB}, sample_to_gene_panel_derived)",
        "mergeTreeIndex-preferences": f"SELECT * FROM mergeTreeIndex({DB}, "
        "'cancer_study_query_preferences')",
        "mergeTreeIndex-user_study_manual": f"SELECT * FROM mergeTreeIndex({DB}, "
        "user_study_manual)",
        "mergeTreeIndex-user_study-inner": f"SELECT * FROM mergeTreeIndex({DB}, `{{inner}}`)",
        "mergeTreeIndex-clinical-hash-key": f"SELECT * FROM mergeTreeIndex({DB}, "
        "clinical_data_derived)",  # sorting key is intHash32(internal_id): no ids
        "mergeTreeIndex-no-key": f"SELECT * FROM mergeTreeIndex({DB}, cancer_study)",
        # The rest of system.table_functions' part/index family on 25.8 and 26.6
        # (absent on one of them: an error is safe).
        "mergeTreeProjection": f"SELECT * FROM mergeTreeProjection({DB}, genomic_event_derived, p)",
        "mergeTreeAnalyzeIndexes": f"SELECT * FROM mergeTreeAnalyzeIndexes({DB}, "
        "sample_derived)",
        "mergeTreeAnalyzeIndexes-events": f"SELECT * FROM mergeTreeAnalyzeIndexes({DB}, "
        "genomic_event_derived)",
        "mergeTreeAnalyzeIndexesUUID": "SELECT * FROM mergeTreeAnalyzeIndexesUUID("
        "(SELECT uuid FROM system.tables WHERE name = 'sample_derived'))",
        "mergeTreeTextIndex": f"SELECT * FROM mergeTreeTextIndex({DB}, sample_derived, x)",
        "dictGet": "SELECT dictGet('authz_matrix.nope', 'x', toUInt64(1))",
        "dictionary-fn": "SELECT * FROM dictionary('authz_matrix.nope')",
        "system.query_log": "SELECT query, exception FROM system.query_log",
        "system.query_log-marker": "SELECT query FROM system.query_log "
        "WHERE positionCaseInsensitive(query, concat('zq9', 'secret')) > 0",
        "system.query_thread_log": "SELECT * FROM system.query_thread_log",
        "system.processes": "SELECT query, Settings FROM system.processes",
        "system.parts": "SELECT table, partition, name FROM system.parts",
        "system.part_log": "SELECT * FROM system.part_log",
        "system.parts_columns": "SELECT * FROM system.parts_columns",
        "system.columns": f"SELECT * FROM system.columns WHERE database = '{DB}'",
        "system.row_policies": "SELECT * FROM system.row_policies",
        "system.tables": f"SELECT name, create_table_query FROM system.tables "
        f"WHERE database = '{DB}'",
        "system.errors": "SELECT name, last_error_message FROM system.errors",
        "system.text_log": "SELECT message FROM system.text_log",
        "system.mutations": "SELECT * FROM system.mutations",
        "system.dictionaries": "SELECT * FROM system.dictionaries",
        "system.users": "SELECT * FROM system.users",
        "system.grants": "SELECT * FROM system.grants",
        "information_schema.tables": "SELECT * FROM INFORMATION_SCHEMA.TABLES",
        "information_schema.views": "SELECT * FROM information_schema.views",
        "information_schema.columns": f"SELECT * FROM information_schema.columns "
        f"WHERE table_schema = '{DB}'",
        "explain": "EXPLAIN SELECT * FROM cancer_study",
        "explain-actions": "EXPLAIN actions = 1 SELECT * FROM sample_derived",
        "explain-indexes": "EXPLAIN indexes = 1 SELECT * FROM sample_derived "
        "WHERE cancer_study_identifier > ''",
        "explain-estimate": "EXPLAIN ESTIMATE SELECT * FROM sample_derived",
        "explain-pipeline": "EXPLAIN PIPELINE SELECT * FROM sample_derived",
        "explain-syntax": "EXPLAIN SYNTAX SELECT * FROM sample_derived",
        "explain-query-tree": "EXPLAIN QUERY TREE SELECT * FROM sample_derived",
        "virtual-part": "SELECT _part, _part_offset, _table FROM patient",
    }
    for name, sql in forms.items():
        leak = BOUNDARY_LEAKS.get(name)
        yield Case(f"boundary-{name}", sql, SAFE, leak=leak, fixed_in=FIXED_IN if leak else None)
    # Forms that read through the normal policy path: the control sees gated rows.
    yield from [
        Case("boundary-view-fn", "SELECT * FROM view(SELECT row_tag FROM cancer_study)"),
        # Needs CREATE TEMPORARY TABLE, which the runtime user lacks: denied for both callers.
        Case("boundary-loop", "SELECT * FROM loop(cancer_study) LIMIT 8", SAFE),
        Case("boundary-virtual-columns", "SELECT _part, _table, _partition_value, row_tag "
             "FROM sample_derived"),  # fmt: skip
        Case("boundary-partition-value", "SELECT DISTINCT _partition_value FROM sample_derived"),
        Case("boundary-partition-id-count", "SELECT uniqExact(_partition_id) FROM sample_derived",
             VALUE, ("1", "4")),  # fmt: skip
    ]


def _error_cases():
    yield from [
        Case("errors-throwIf-count", "SELECT throwIf(count() > 1, 'more') FROM cancer_study",
             ORACLE),  # fmt: skip
        Case("errors-throwIf-row", "SELECT throwIf(startsWith(row_tag, 'zq9')) FROM sample",
             ORACLE),  # fmt: skip
        Case("errors-throwIf-row-message", "SELECT throwIf(1, row_tag) FROM sample", SAFE),
        Case("errors-toInt64", "SELECT toInt64(row_tag) FROM cancer_study ORDER BY row_tag DESC",
             ERROR_TEXT),  # fmt: skip
        Case("errors-uuid-cast", "SELECT CAST(sample_unique_id AS UUID) FROM sample_derived "
             "ORDER BY sample_unique_id DESC", ERROR_TEXT),  # fmt: skip
        Case("errors-date-cast", "SELECT toDate(assumeNotNull(cancer_study_identifier)) FROM cancer_study "
             "ORDER BY cancer_study_identifier DESC", ERROR_TEXT),  # fmt: skip
        Case("errors-enum-cast", "SELECT CAST(row_tag, 'Enum8(\\'x\\' = 1)') FROM patient "
             "ORDER BY row_tag DESC", ERROR_TEXT),  # fmt: skip
        Case("errors-int-shorthand", "SELECT row_tag::Int32 FROM clinical_sample "
             "ORDER BY row_tag DESC", ERROR_TEXT),  # fmt: skip
        Case("errors-patient-id-cast", "SELECT toUInt8(patient_unique_id) FROM sample_derived "
             "ORDER BY patient_unique_id DESC", ERROR_TEXT),  # fmt: skip
        Case("errors-json", "SELECT JSONExtract(row_tag, 'Int64') + throwIf(row_tag != '', "
             "'x') FROM mutation_event", SAFE),  # fmt: skip
    ]


def _params(cases):
    params = []
    for case in cases:
        marks = (
            [pytest.mark.xfail(strict=True, reason=case.leak)]
            if case.leak and not case.fixed_in
            else []
        )
        params.append(pytest.param(case, id=case.id, marks=marks))
    return params


def _first_cell(reply: Reply):
    payload = reply.payload
    assert isinstance(payload, dict), reply.text[:500]
    assert "error_message" not in payload, payload["error_message"][:500]
    rows = payload.get("rows") or []
    if not rows:
        return None
    row = rows[0]
    value = row[0] if isinstance(row, list) else next(iter(row.values()))
    return str(value)


def _has_marker(reply: Reply) -> bool:
    return MARKER in reply.text.lower()


def _is_error(reply: Reply) -> bool:
    payload = reply.payload
    return reply.status != 200 or bool(
        reply.error
        or (reply.result or {}).get("isError")
        or (isinstance(payload, dict) and "error_message" in payload)
    )


def _check(mcp: Mcp, case: Case, request=None):
    if case.leak and case.fixed_in and mcp.version < case.fixed_in:
        request.applymarker(pytest.mark.xfail(strict=True, reason=case.leak))
    sql = case.sql.replace("{inner}", mcp.inner)
    nogrant = mcp.sql(sql, NOGRANT)
    assert nogrant.status == 200, nogrant.text[:500]
    if case.kind == SAFE:
        assert not _has_marker(nogrant), f"LEAK via {case.id}: {nogrant.text[:2000]}"
        if GRANTED not in sql:  # another caller's identity/grants
            assert GRANTED not in nogrant.text, f"LEAK via {case.id}: {nogrant.text[:2000]}"
        return
    granted = mcp.sql(sql, GRANTED)
    if case.kind == VALUE:
        assert _first_cell(granted) == case.expected[1], "control lost its gated rows"
        actual = _first_cell(nogrant)
        assert actual == case.expected[0], f"LEAK via {case.id}: {actual!r}"
    elif case.kind == ROWS:
        assert not _is_error(granted), granted.text[:1000]
        assert _has_marker(granted), f"control saw no gated rows: {granted.text[:1000]}"
        assert not _is_error(nogrant), nogrant.text[:1000]
        assert not _has_marker(nogrant), f"LEAK via {case.id}: {nogrant.text[:2000]}"
        if case.public_row:
            assert PUBLIC in nogrant.text, f"query found no public row: {nogrant.text[:500]}"
    elif case.kind == ORACLE:
        assert _is_error(granted), f"control oracle did not fire: {granted.text[:500]}"
        assert not _is_error(nogrant), f"LEAK (existence oracle) via {case.id}: {nogrant.text}"
    elif case.kind == ERROR_TEXT:
        assert _is_error(granted) and _has_marker(granted), granted.text[:500]
        assert _is_error(nogrant), nogrant.text[:500]
        assert not _has_marker(nogrant), f"LEAK via {case.id}: {nogrant.text[:2000]}"
    else:  # pragma: no cover
        raise AssertionError(case.kind)


@pytest.mark.parametrize("case", _params(_rows_cases()))
def test_query_forms(mcp, case, request):
    _check(mcp, case, request)


@pytest.mark.parametrize("case", _params(_identity_cases()))
def test_identity_override_forms(mcp, case, request):
    _check(mcp, case, request)
    # Never another caller's grants, whatever the form returned.
    assert GRANTED not in mcp.sql(case.sql, NOGRANT).text.replace(f"'{GRANTED}'", "")


@pytest.mark.parametrize("case", _params(_boundary_cases()))
def test_table_functions_system_tables_and_explain(mcp, case, request):
    _check(mcp, case, request)


@pytest.mark.parametrize("case", _params(_error_cases()))
def test_error_messages(mcp, case, request):
    _check(mcp, case, request)


def test_explain_estimate_does_not_count_gated_rows(mcp):
    """The estimate must not reveal how many gated rows exist."""
    reply = mcp.sql("EXPLAIN ESTIMATE SELECT * FROM sample_derived", NOGRANT)
    payload = reply.payload
    if isinstance(payload, dict) and "error_message" in payload:
        return  # rejected: nothing revealed
    columns = payload.get("columns") or list((payload.get("rows") or [{}])[0])
    rows = [
        dict(zip(columns, r, strict=True)) if isinstance(r, list) else r for r in payload["rows"]
    ]
    total = sum(int(r.get("rows", 0)) for r in rows)
    assert total <= SAMPLES_PER_STUDY, f"LEAK: estimate counts {total} rows, {rows}"


# --- Domain tools -------------------------------------------------------------


def _normalized(reply: Reply, study: str) -> str:
    return reply.text.replace(study, "<STUDY>")


def test_list_studies(mcp):
    granted = mcp.call("list_studies", {"limit": 100}, GRANTED)
    assert all(g in granted.text for g in GATED), granted.text[:1000]
    for args in ({"limit": 100}, {"search": "secret"}, {"search": "zq9", "verbose": True}):
        nogrant = mcp.call("list_studies", args, NOGRANT)
        assert not _is_error(nogrant), nogrant.text[:500]
        assert not _has_marker(nogrant), f"LEAK via list_studies{args}: {nogrant.text[:2000]}"


def test_list_study_guides(mcp):
    reply = mcp.call("list_study_guides", {}, NOGRANT)
    assert not _has_marker(reply), reply.text[:2000]


DOMAIN_CALLS = {
    "get_study_guide": lambda s: ("get_study_guide", {"study_id": s}),
    "read_guide-study": lambda s: ("read_guide", {"uri": f"cbioportal://study-guide/{s}"}),
    "get_alteration_frequency": lambda s: (
        "get_alteration_frequency",
        {"gene": "TP53", "study_id": s},
    ),
    "get_top_altered_genes": lambda s: ("get_top_altered_genes", {"study_id": s}),
    "get_profiled_counts": lambda s: ("get_profiled_counts", {"study_id": s}),
}


@pytest.mark.parametrize("tool", list(DOMAIN_CALLS))
@pytest.mark.parametrize("study", GATED)
def test_study_tools_cannot_tell_gated_from_absent(mcp, tool, study):
    """No-grant: a gated study id answers exactly like an id that does not exist."""
    granted = mcp.call(*DOMAIN_CALLS[tool](study), GRANTED)
    assert not _is_error(granted), granted.text[:500]
    absent_for_granted = mcp.call(*DOMAIN_CALLS[tool](ABSENT), GRANTED)
    # Control: the granted caller really gets the study (differs from absent).
    assert _normalized(granted, study) != _normalized(absent_for_granted, ABSENT)
    nogrant = mcp.call(*DOMAIN_CALLS[tool](study), NOGRANT)
    absent = mcp.call(*DOMAIN_CALLS[tool](ABSENT), NOGRANT)
    assert _normalized(nogrant, study) == _normalized(absent, ABSENT), (
        f"LEAK via {tool}({study}): {nogrant.text[:2000]}"
    )
    # Beyond echoing the requested id, nothing gated.
    assert MARKER not in _normalized(nogrant, study).lower()


@pytest.mark.parametrize("alteration_type", ["any", "mutation"])
def test_gene_frequency_by_cancer_type(mcp, alteration_type):
    args = {"gene": "TP53", "alteration_type": alteration_type, "preference": PREFERENCE}
    granted = mcp.call("get_gene_frequency_by_cancer_type", args, GRANTED)
    nogrant = mcp.call("get_gene_frequency_by_cancer_type", args, NOGRANT)
    assert not _has_marker(nogrant), f"LEAK: {nogrant.text[:2000]}"
    if alteration_type == "any":  # 'mutation' needs the real sql/4 recipe view
        assert _has_marker(granted), f"control saw no gated cancer type: {granted.text[:1000]}"
        assert PUBLIC in nogrant.text, nogrant.text[:1000]


# --- Bridge headers -----------------------------------------------------------

BAD_HEADERS = {
    "no-bridge-headers": dict(
        x_cbioportal_mcp_bridge_secret=None,
        x_user_email=None,
        x_user_id=None,
        x_user_email_verified=None,
    ),
    "secret-missing": dict(x_cbioportal_mcp_bridge_secret=None),
    "secret-wrong": dict(x_cbioportal_mcp_bridge_secret="not-the-secret"),
    "secret-empty": dict(x_cbioportal_mcp_bridge_secret=""),
    "secret-placeholder": dict(x_cbioportal_mcp_bridge_secret="${BRIDGE_SECRET}"),
    "verified-false": dict(x_user_email_verified="false"),
    "verified-missing": dict(x_user_email_verified=None),
    "verified-one": dict(x_user_email_verified="1"),
    "email-missing": dict(x_user_email=None),
    "email-invalid": dict(x_user_email="granted"),
    "user-id-missing": dict(x_user_id=None),
    "bearer-junk": dict(authorization="Bearer junk"),
}


@pytest.mark.parametrize("bad", list(BAD_HEADERS))
@pytest.mark.parametrize("email", [GRANTED, NOGRANT])
def test_bad_bridge_headers_get_401(mcp, bad, email):
    headers = mcp.headers(email, **BAD_HEADERS[bad])
    status, _ = mcp.open(headers)
    assert status == 401
    # Also on a tool call riding a session that a valid login opened.
    session = mcp.open(mcp.headers(email))[1]
    reply = mcp.call(
        "clickhouse_run_select_query",
        {"query": "SELECT row_tag FROM cancer_study"},
        headers=headers,
        session=session,
    )
    assert reply.status == 401, reply.text[:500]
    assert not _has_marker(reply)


def test_duplicate_email_headers_get_401(mcp):
    headers = [(k, v) for k, v in mcp.headers(NOGRANT).items()] + [("x-user-email", GRANTED)]
    status, _ = mcp.open(headers)
    assert status == 401
    headers = [(k, v) for k, v in mcp.headers(GRANTED).items()] + [("x-user-email", NOGRANT)]
    assert mcp.open(headers)[0] == 401


def test_mid_session_email_switch(mcp):
    """A session opened by the control does not carry its grants to other headers."""
    sql = {"query": "SELECT row_tag FROM cancer_study"}
    session = mcp.open(mcp.headers(GRANTED))[1]
    own = mcp.call("clickhouse_run_select_query", sql, headers=mcp.headers(GRANTED),
                   session=session)  # fmt: skip
    assert _has_marker(own), own.text[:500]
    switched = mcp.call("clickhouse_run_select_query", sql, headers=mcp.headers(NOGRANT),
                        session=session)  # fmt: skip
    assert switched.status in (200, 401, 404), switched.text[:500]
    assert not _has_marker(switched), f"LEAK: {switched.text[:2000]}"
    if switched.status == 200:
        assert PUBLIC in switched.text
    for bad in ("secret-missing", "verified-false", "no-bridge-headers"):
        reply = mcp.call("clickhouse_run_select_query", sql,
                         headers=mcp.headers(GRANTED, **BAD_HEADERS[bad]), session=session)
        assert reply.status == 401 and not _has_marker(reply), (bad, reply.text[:500])
    # The original caller is unaffected afterwards.
    again = mcp.call("clickhouse_run_select_query", sql, headers=mcp.headers(GRANTED),
                     session=session)  # fmt: skip
    assert _has_marker(again)


def test_unknown_verified_email_is_public_only(mcp):
    reply = mcp.sql("SELECT row_tag FROM cancer_study", "stranger@example.org")
    assert not _has_marker(reply) and PUBLIC in reply.text, reply.text[:500]
