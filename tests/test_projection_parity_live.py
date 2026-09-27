"""Live parity check for sql/9-projections.sql against a real ClickHouse.

Projections must be invisible to results: every query shape the agent or a
recipe view can produce has to return exactly what the base table returns.
This test builds the real derived-table DDL (tests/fixtures/projection_parity/
schema.sql), loads synthetic data in several layouts, applies sql/4 and sql/9
verbatim, and compares each shape with projections on vs. off.

"Off" is ClickHouse defaults with optimize_use_projections = 0, which is how
the MCP reads the database today. "On" is optimize_use_projections = 1 plus
PROJECTION_SAFE_SETTINGS, the settings the MCP's startup gate requires. With
ClickHouse's default optimize_use_implicit_projections = 1 a bare count()
over-counts (see sql/9-projections.sql); the report prints that column too.

Needs a ClickHouse binary (run as ``clickhouse local``, no server or Docker):

    CLICKHOUSE_BINARY=/path/to/clickhouse pytest -s tests/test_projection_parity_live.py
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from cbioportal_mcp.authentication.permissions import PROJECTION_SAFE_SETTINGS

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
SCHEMA = Path(__file__).resolve().parent / "fixtures" / "projection_parity" / "schema.sql"
VIEWS_SQL = SQL_DIR / "4-mutation-frequency-views.sql"
PROJECTIONS_SQL = SQL_DIR / "9-projections.sql"

BINARY = os.environ.get("CLICKHOUSE_BINARY")
pytestmark = pytest.mark.skipif(
    not BINARY, reason="set CLICKHOUSE_BINARY to a clickhouse binary to run live parity"
)

STUDY, STUDY2, GENE, GENE2 = "study_03", "study_07", "TP53", "KRAS"

# layout -> (rows, studies, one profile per study?, insert batches, projections before data?)
LAYOUTS = {
    # The reviewer's repro shape: one profile per study, so base-key granules
    # are single-study and the exact-count path fires. Single part.
    "aligned": (400_000, 20, True, 1, False),
    # Profiles per alteration type, several unmerged parts.
    "realistic": (600_000, 24, False, 3, False),
    # Projections defined before the data arrives, so INSERT builds them.
    "insert_after_add": (300_000, 12, False, 2, True),
}

OFF = {"optimize_use_projections": "0"}
ON_DEFAULT = {"optimize_use_projections": "1"}
ON_SAFE = {"optimize_use_projections": "1", **PROJECTION_SAFE_SETTINGS}

GED = "genomic_event_derived"
STGP = "sample_to_gene_panel_derived"
S, S2 = f"cancer_study_identifier = '{STUDY}'", f"cancer_study_identifier = '{STUDY2}'"
MUT = "variant_type = 'mutation' AND mutation_status != 'UNCALLED' AND off_panel = 0"

SHAPES = {
    "bare count, study": f"SELECT count() FROM {GED} WHERE {S}",
    "sum(1), study": f"SELECT sum(1) FROM {GED} WHERE {S}",
    "count(*) from subquery": f"SELECT count(*) FROM (SELECT 1 FROM {GED} WHERE {S})",
    "count GROUP BY study": (
        f"SELECT cancer_study_identifier, count() FROM {GED} WHERE {S} "
        "GROUP BY cancer_study_identifier"
    ),
    "count DISTINCT sample, study": f"SELECT count(DISTINCT sample_unique_id) FROM {GED} WHERE {S}",
    "uniqExact sample, study": f"SELECT uniqExact(sample_unique_id) FROM {GED} WHERE {S}",
    "bare count, study IN": f"SELECT count() FROM {GED} WHERE {S} OR {S2}",
    "bare count, study range": (
        f"SELECT count() FROM {GED} "
        "WHERE cancer_study_identifier > 'study_01' AND cancer_study_identifier < 'study_05'"
    ),
    "bare count, study+gene+variant": (
        f"SELECT count() FROM {GED} "
        f"WHERE {S} AND hugo_gene_symbol = '{GENE}' AND variant_type = 'mutation'"
    ),
    "bare count, gene": f"SELECT count() FROM {GED} WHERE hugo_gene_symbol = '{GENE}'",
    "bare count, gene+variant": (
        f"SELECT count() FROM {GED} WHERE hugo_gene_symbol = '{GENE}' AND variant_type = 'cna'"
    ),
    "bare count, gene LIKE": f"SELECT count() FROM {GED} WHERE hugo_gene_symbol LIKE 'GENE1%'",
    "count, non-key filter": f"SELECT count() FROM {GED} WHERE {S} AND {MUT}",
    "countIf, study": f"SELECT countIf(variant_type = 'cna') FROM {GED} WHERE {S}",
    "min/max sample, study": (
        f"SELECT min(sample_unique_id), max(sample_unique_id) FROM {GED} WHERE {S}"
    ),
    "count no WHERE": f"SELECT count() FROM {GED}",
    "top genes in study": (
        f"SELECT hugo_gene_symbol, count(DISTINCT sample_unique_id) AS n FROM {GED} "
        f"WHERE {S} AND {MUT} GROUP BY hugo_gene_symbol ORDER BY n DESC, hugo_gene_symbol LIMIT 20"
    ),
    "gene across studies": (
        f"SELECT cancer_study_identifier, count(DISTINCT sample_unique_id) FROM {GED} "
        f"WHERE hugo_gene_symbol = '{GENE}' AND {MUT} GROUP BY cancer_study_identifier"
    ),
    "co-occurrence INTERSECT": (
        f"SELECT count() FROM (SELECT sample_unique_id FROM {GED} WHERE {S} AND {MUT} "
        f"AND hugo_gene_symbol = '{GENE}' INTERSECT DISTINCT SELECT sample_unique_id FROM {GED} "
        f"WHERE {S} AND {MUT} AND hugo_gene_symbol = '{GENE2}')"
    ),
    "co-occurrence 2x2": (
        "SELECT countIf(a AND b), countIf(a AND NOT b), countIf(b AND NOT a) FROM ("
        f"SELECT sample_unique_id, max(hugo_gene_symbol = '{GENE}') AS a, "
        f"max(hugo_gene_symbol = '{GENE2}') AS b FROM {GED} WHERE {S} AND {MUT} "
        f"AND hugo_gene_symbol IN ('{GENE}', '{GENE2}') GROUP BY sample_unique_id)"
    ),
    "amplified samples, gene": (
        f"SELECT count(DISTINCT sample_unique_id) FROM {GED} "
        f"WHERE hugo_gene_symbol = '{GENE}' AND variant_type = 'cna' AND cna_alteration = 2"
    ),
    "panel rows, study": f"SELECT count() FROM {STGP} WHERE {S}",
    "panel rows, study+type": (
        f"SELECT count() FROM {STGP} WHERE {S} AND alteration_type = 'MUTATION_EXTENDED'"
    ),
    "profiled samples, study": (
        f"SELECT count(DISTINCT sample_unique_id) FROM {STGP} "
        f"WHERE {S} AND alteration_type = 'MUTATION_EXTENDED'"
    ),
}

# Shapes that must actually be served by a projection under ON_SAFE; parity
# on a query that never touches a projection proves nothing.
MUST_USE = {
    "bare count, study": "ged_by_study_gene",
    "bare count, gene": "ged_by_gene_study",
    "co-occurrence 2x2": "ged_by_study_gene",
    "panel rows, study": "stgp_by_study",
}

PARAM_VALUES = {
    "preference": "'pan_cancer_tcga'",
    "gene": f"'{GENE}'",
    "study": f"'{STUDY}'",
    "studies": f"['{STUDY}', '{STUDY2}']",
    "top_n": "20",
}
ALTERATIONS = ("mutation", "amplification", "deep_deletion", "structural_variant")


def _view_shapes() -> dict[str, str]:
    """One shape per sql/4 view (one per alteration token where it takes one)."""
    text = re.sub(r"--[^\n]*", "", VIEWS_SQL.read_text())
    shapes = {}
    blocks = re.split(r"^CREATE VIEW ", text, flags=re.M)[1:]
    assert blocks, "no views found in sql/4"
    for block in blocks:
        name = block.split()[0]
        body = block.split(";", 1)[0]
        params = sorted(set(re.findall(r"\{(\w+):", body)))
        if not params:
            # Coverage building blocks return samples x genes rows; compare an
            # order-independent checksum instead of the rows themselves.
            shapes[f"view {name}"] = f"SELECT count(), sum(cityHash64(*)) FROM {name}"
            continue
        unknown = set(params) - set(PARAM_VALUES) - {"alteration"}
        assert not unknown, f"{name}: add test values for parameters {unknown}"
        args = [f"{p} = {PARAM_VALUES[p]}" for p in params if p != "alteration"]
        if "alteration" in params:
            for alt in ALTERATIONS:
                assert f"'{alt}'" in body, f"{name}: alteration token {alt!r} not handled"
                call = ", ".join(args + [f"alteration = '{alt}'"])
                shapes[f"view {name}({alt})"] = f"SELECT * FROM {name}({call})"
        else:
            shapes[f"view {name}"] = f"SELECT * FROM {name}({', '.join(args)})"
    return shapes


ALL_SHAPES = {**SHAPES, **_view_shapes()}


def _data_sql(rows: int, studies: int, one_profile: bool, batches: int) -> str:
    samples = studies * 500
    sample = f"number % {samples}"
    study = f"concat('study_', leftPad(toString(({sample}) % {studies}), 2, '0'))"
    panel = (
        f"multiIf((({sample}) % {studies}) % 3 = 0, 'WES', "
        f"(({sample}) % {studies}) % 3 = 1, 'IMPACT341', 'IMPACT468')"
    )
    gene_idx = "if(cityHash64(number, 1) % 100 < 20, cityHash64(number, 2) % 10, 10 + number % 990)"
    if one_profile:
        vtype = "'mutation'"
    else:
        vtype = "multiIf(number % 10 < 6, 'mutation', number % 10 < 9, 'cna', 'structural_variant')"
    sql = [
        "INSERT INTO gene SELECT number + 1, if(number < 10, "
        "['TP53','KRAS','PIK3CA','EGFR','BRAF','APC','PTEN','IDH1','MYC','CDKN2A'][number + 1], "
        "concat('GENE', toString(number))), number + 1, 'protein-coding' FROM numbers(1000);",
        "INSERT INTO gene_panel VALUES (1, 'IMPACT341', NULL), (2, 'IMPACT468', NULL);",
        "INSERT INTO gene_panel_list SELECT 1, number + 1 FROM numbers(341);",
        "INSERT INTO gene_panel_list SELECT 2, number + 1 FROM numbers(468);",
        f"INSERT INTO cancer_study (cancer_study_id, cancer_study_identifier, type_of_cancer_id, "
        f"name, description, public) SELECT number + 1, concat('study_', leftPad(toString(number), "
        f"2, '0')), 'mixed', '', '', 1 FROM numbers({studies});",
        "INSERT INTO genetic_profile (genetic_profile_id, stable_id, cancer_study_id, "
        "genetic_alteration_type, datatype, name, show_profile_in_analysis_tab) "
        "SELECT cancer_study_id * 10 + p.1, concat(cancer_study_identifier, p.2), cancer_study_id, "
        "p.3, p.4, '', 1 FROM cancer_study ARRAY JOIN [(1, '_mutations', 'MUTATION_EXTENDED', "
        "'MAF'), (2, '_gistic', 'COPY_NUMBER_ALTERATION', 'DISCRETE'), (3, '_structural_variants', "
        "'STRUCTURAL_VARIANT', 'SV')] AS p;",
        "INSERT INTO cancer_study_query_preferences SELECT 'pan_cancer_tcga', "
        "cancer_study_identifier, '' FROM cancer_study WHERE cancer_study_id <= 8;",
        "INSERT INTO cancer_study_query_preferences SELECT 'all_studies_non_redundant', "
        "cancer_study_identifier, '' FROM cancer_study;",
        f"INSERT INTO sample_to_gene_panel_derived SELECT concat({study}, '_S', toString(number)), "
        f"t.1, {panel}, {study}, concat({study}, t.2) FROM numbers({samples}) "
        "ARRAY JOIN [('MUTATION_EXTENDED', '_mutations'), ('COPY_NUMBER_ALTERATION', '_gistic'), "
        "('STRUCTURAL_VARIANT', '_structural_variants')] AS t;",
        f"INSERT INTO sample_derived (sample_unique_id, sample_stable_id, patient_unique_id, "
        f"cancer_study_identifier, internal_id) SELECT concat({study}, '_S', toString(number)), "
        f"concat('S', toString(number)), concat({study}, '_P', toString(intDiv(number, 2))), "
        f"{study}, number FROM numbers({samples});",
        "INSERT INTO clinical_data_derived SELECT number, "
        f"concat({study}, '_S', toString(number)), "
        f"concat({study}, '_P', toString(intDiv(number, 2))), 'CANCER_TYPE', "
        "['Breast','Lung','Colorectal','Glioma','Melanoma','Prostate'][number % 6 + 1], "
        f"{study}, 'sample' FROM numbers({samples});",
        "INSERT INTO genetic_alteration_derived SELECT concat(cancer_study_identifier, '_S', "
        "toString(s)), cancer_study_identifier, g, 'gistic', toString([-2,-1,0,1,2][(s + "
        "length(g)) % 5 + 1]) FROM (SELECT DISTINCT cancer_study_identifier, toUInt32(extract("
        "sample_unique_id, '_S(\\\\d+)$')) AS s FROM sample_to_gene_panel_derived) "
        "ARRAY JOIN ['TP53','KRAS','MYC'] AS g;",
    ]
    per = rows // batches
    for b in range(batches):
        sql.append(
            "INSERT INTO genomic_event_derived SELECT "
            f"concat({study}, '_S', toString({sample})), g.hugo_gene_symbol, "
            f"toInt32(g.entrez_gene_id), {panel}, {study}, "
            f"concat({study}, multiIf(vt = 'mutation', '_mutations', vt = 'cna', '_gistic', "
            "'_structural_variants')), vt, "
            "if(vt = 'mutation', concat('p.X', toString(number % 900), 'Y'), ''), "
            "if(vt = 'mutation', ['Missense_Mutation','Nonsense_Mutation'][number % 2 + 1], ''), "
            "if(vt = 'mutation', if(number % 17 = 0, 'UNCALLED', 'SOMATIC'), ''), "
            "'Unknown', '', '', '', "
            "if(vt = 'cna', toInt8([-2, -1, 1, 2][number % 4 + 1]), NULL), "
            "if(vt = 'cna', concat(toString(number % 22 + 1), 'p'), ''), "
            "if(vt = 'structural_variant', concat(g.hugo_gene_symbol, ' fusion'), ''), "
            f"concat({study}, '_P', toString(intDiv({sample}, 2))), "
            f"{panel} != 'WES' AND g.entrez_gene_id > if({panel} = 'IMPACT341', 341, 468) "
            f"FROM (SELECT number, {vtype} AS vt, toInt64({gene_idx} + 1) AS gid "
            f"FROM numbers({b * per}, {per})) AS e JOIN gene AS g ON g.entrez_gene_id = e.gid;"
        )
    return "\n".join(sql)


def _run(db_path: Path, sql: str, settings: dict[str, str] | None = None) -> str:
    args = [BINARY, "local", "--path", str(db_path), "--multiquery"]
    for k, v in (settings or {}).items():
        args.append(f"--{k}={v}")
    # A named database: on 24.8, clickhouse local's default database is not
    # persisted under --path between invocations.
    sql = "CREATE DATABASE IF NOT EXISTS parity; USE parity;\n" + sql
    proc = subprocess.run(args, input=sql, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise AssertionError(f"clickhouse local failed:\n{proc.stderr[-3000:]}")
    return proc.stdout


def _wrap(query: str) -> str:
    return f"SELECT * FROM ({query}) ORDER BY ALL FORMAT TSV;"


def _run_shapes(db_path: Path, settings: dict[str, str]) -> dict[str, str]:
    """Output per shape. An erroring shape yields "ERROR <code>", so parity also
    requires the same failures (some sql/4 views don't run on 24.8 at all)."""
    script = []
    for i, query in enumerate(ALL_SHAPES.values()):
        script.append(f"SELECT '@@{i}' FORMAT TSV;")
        script.append(_wrap(query))
    try:
        out = _run(db_path, "\n".join(script), settings)
    except AssertionError:
        return {name: _run_one(db_path, query, settings) for name, query in ALL_SHAPES.items()}
    chunks = re.split(r"^@@(\d+)\n", out, flags=re.M)[1:]
    names = list(ALL_SHAPES)
    results = {names[int(chunks[k])]: chunks[k + 1] for k in range(0, len(chunks), 2)}
    assert set(results) == set(ALL_SHAPES), "some shapes produced no output marker"
    return results


def _run_one(db_path: Path, query: str, settings: dict[str, str]) -> str:
    try:
        return _run(db_path, _wrap(query), settings)
    except AssertionError as e:
        code = re.search(r"Code: (\d+)", str(e))
        return f"ERROR {code.group(1) if code else '?'}"


def _build(tmp_path_factory, layout: str) -> Path:
    rows, studies, one_profile, batches, projections_first = LAYOUTS[layout]
    path = tmp_path_factory.mktemp(f"ch-{layout}")
    schema = SCHEMA.read_text()
    views = VIEWS_SQL.read_text()
    projections = PROJECTIONS_SQL.read_text()
    data = _data_sql(rows, studies, one_profile, batches)
    if projections_first:
        _run(path, "\n".join([schema, projections, data, views]))
    else:
        _run(path, "\n".join([schema, data, views, projections]))
    return path


@pytest.fixture(scope="module", params=list(LAYOUTS))
def database(request, tmp_path_factory):
    path = _build(tmp_path_factory, request.param)
    proj_parts = _run(
        path,
        "SELECT count() FROM system.projection_parts WHERE active AND "
        "database = currentDatabase() FORMAT TSV;",
    )
    assert int(proj_parts) > 0, "sql/9 built no projection parts"
    return request.param, path


def test_projections_return_identical_results(database):
    layout, path = database
    off = _run_shapes(path, OFF)
    on_safe = _run_shapes(path, ON_SAFE)
    on_default = _run_shapes(path, ON_DEFAULT)

    def cell(out: str) -> str:
        text = out.strip().replace("\n", " | ").replace("\t", ",")
        return text if len(text) <= 40 else f"{len(out.splitlines())} rows"

    print(f"\n[{layout}] {BINARY}")
    print(f"| shape | projections off | on (CH defaults) | on ({PROJECTION_SAFE_SETTINGS}) |")
    print("|---|---|---|---|")
    for name in ALL_SHAPES:
        default_mark = "" if on_default[name] == off[name] else " ❌"
        safe_mark = "" if on_safe[name] == off[name] else " ❌"
        print(
            f"| {name} | {cell(off[name])} | {cell(on_default[name])}{default_mark} "
            f"| {cell(on_safe[name])}{safe_mark} |"
        )
    mismatches = [name for name in ALL_SHAPES if on_safe[name] != off[name]]
    assert not mismatches, f"[{layout}] results differ with projections on: {mismatches}"
    broken = [name for name in SHAPES if not off[name].strip() or off[name].startswith("ERROR")]
    assert not broken, f"[{layout}] shapes returned nothing or failed, parity is vacuous: {broken}"


def test_projections_are_actually_used(database):
    layout, path = database
    for name, projection in MUST_USE.items():
        plan = _run(path, f"EXPLAIN indexes = 1 {SHAPES[name]};", ON_SAFE)
        assert (
            f"ReadFromMergeTree ({projection})" in plan
        ), f"[{layout}] {name!r} did not read {projection}:\n{plan}"
