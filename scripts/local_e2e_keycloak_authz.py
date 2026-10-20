#!/usr/bin/env python3
"""Exercise the local Keycloak-backed cBioPortal MCP authz stack."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from cbioportal_mcp.authentication.row_policy_catalog import (
    DENIED_TABLES,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLES,
    STUDY_SCOPED_VIEWS,
    classified_names,
)
from cbioportal_mcp.clickhouse_query_settings import RESTRICTED_ROLE_PROBES, is_access_denied


def _jsonish(value: Any) -> Any:
    if hasattr(value, "root"):
        return _jsonish(value.root)
    if hasattr(value, "model_dump"):
        return _jsonish(value.model_dump(mode="json"))
    if isinstance(value, list):
        return [_jsonish(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonish(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonish(item) for key, item in value.items()}
    return value


def _payload(result: Any) -> Any:
    if getattr(result, "structured_content", None) is not None:
        structured = result.structured_content
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return _jsonish(structured["result"])
        return _jsonish(structured)
    if getattr(result, "structuredContent", None) is not None:
        structured = result.structuredContent
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return _jsonish(structured["result"])
        return _jsonish(structured)
    if getattr(result, "data", None) is not None:
        return _jsonish(result.data)

    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        text = content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return result


async def _token(base_url: str, username: str, password: str) -> str:
    token_url = f"{base_url}/realms/cbioportal-mcp-local/protocol/openid-connect/token"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            token_url,
            data={
                "grant_type": "password",
                "client_id": "mcp-local",
                "username": username,
                "password": password,
                "scope": "openid profile email",
            },
        )
    response.raise_for_status()
    return response.json()["access_token"]


async def _call_with_token(
    proxy_url: str,
    token: str,
    tool: str,
    args: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    transport = StreamableHttpTransport(
        proxy_url,
        headers={**(extra_headers or {}), "authorization": f"Bearer {token}"},
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, args or {}, raise_on_error=False)
    return _payload(result)


# For every study-scoped table: (key column, keys owned by study_alpha, keys
# owned by study_beta). An unfiltered `SELECT *` as alice (study_alpha only)
# must return exactly the alpha keys - non-empty, and nothing from beta - and
# vice versa for bob; admin-user (all studies) must see both. Every table and
# view in row_policy_catalog must exist in the fixture (checked below).
_ALPHA, _BETA = "study_alpha", "study_beta"
EXPECTED_STUDY_ROWS: dict[str, tuple[str | tuple[str, ...], set, set]] = {
    "cancer_study": ("cancer_study_id", {1}, {2}),
    "cancer_study_tags": ("cancer_study_id", {1}, {2}),
    "cancer_study_query_preferences": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "patient": ("internal_id", {1, 2}, {3}),
    "sample": ("internal_id", {1, 2}, {3}),
    "sample_list": ("list_id", {1}, {2}),
    "sample_list_list": ("sample_id", {1, 2}, {3}),
    "clinical_patient": ("internal_id", {1, 2}, {3}),
    "clinical_sample": ("internal_id", {1, 2}, {3}),
    "clinical_attribute_meta": ("cancer_study_id", {1}, {2}),
    "clinical_event": ("clinical_event_id", {1001}, {1002}),
    "clinical_event_data": ("clinical_event_id", {1001}, {1002}),
    "genetic_profile": ("genetic_profile_id", {11, 12, 13, 14, 15}, {21, 22, 23, 24, 25}),
    "genetic_profile_link": ("referring_genetic_profile_id", {11}, {21}),
    "genetic_profile_samples": ("genetic_profile_id", {13}, {23}),
    "genetic_alteration": ("genetic_profile_id", {13}, {23}),
    "sample_profile": ("sample_id", {1, 2}, {3}),
    # Events 103 / 203 are observed in both studies: both users see the shared
    # event row, but each sees only its own study's observation of it, so the
    # observation tables are keyed by (event, genetic_profile_id, sample_id).
    "mutation_event": ("mutation_event_id", {101, 103}, {102, 103}),
    "mutation": (
        ("mutation_event_id", "genetic_profile_id", "sample_id"),
        {(101, 11, 1), (103, 11, 2)},
        {(102, 21, 3), (103, 21, 3)},
    ),
    "mutation_count_by_keyword": ("genetic_profile_id", {11}, {21}),
    "alteration_driver_annotation": ("alteration_event_id", {101}, {102}),
    "allele_specific_copy_number": ("mutation_event_id", {101}, {102}),
    "cna_event": ("cna_event_id", {201, 203}, {202, 203}),
    "sample_cna_event": (
        ("cna_event_id", "genetic_profile_id", "sample_id"),
        {(201, 12, 1), (203, 12, 2)},
        {(202, 22, 3), (203, 22, 3)},
    ),
    "structural_variant": ("internal_id", {301}, {302}),
    "copy_number_seg": ("seg_id", {401}, {402}),
    "copy_number_seg_file": ("seg_file_id", {501}, {502}),
    "gistic": ("gistic_roi_id", {601}, {602}),
    "gistic_to_gene": ("gistic_roi_id", {601}, {602}),
    "mut_sig": ("cancer_study_id", {1}, {2}),
    "resource_definition": ("cancer_study_id", {1}, {2}),
    "resource_sample": ("internal_id", {1}, {3}),
    "resource_patient": ("internal_id", {1}, {3}),
    "resource_study": ("internal_id", {1}, {2}),
    "resource": ("cancer_study_id", {1}, {2}),
    "clinical_data_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "clinical_event_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "clinical_event_data_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "genetic_alteration_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "genomic_event_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "sample_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "sample_to_gene_panel_derived": ("cancer_study_identifier", {_ALPHA}, {_BETA}),
    "mutation_derived": ("studyId", {_ALPHA}, {_BETA}),
    "generic_assay_data_derived": (
        "profile_stable_id",
        {"study_alpha_treatment_ic50"},
        {"study_beta_treatment_ic50"},
    ),
    "generic_assay_profile_entity_derived": (
        "profile_stable_id",
        {"study_alpha_treatment_ic50"},
        {"study_beta_treatment_ic50"},
    ),
}


_BETA_CANCER_TYPE = "Non-Small Cell Lung Cancer"

# Every sql/4, sql/5, sql/7 view: (kind, arguments). "{study}" is filled with
# the study under test. Kinds:
# - coverage:   plain view with a cancer_study_identifier column;
# - study:      parameterized by one study - alpha's data for alice, nothing
#               when the beta study is passed in a form the guard can't read;
# - preference: runs over a preference cohort holding both studies - alice
#               must get her share only (fewer rows/counts than admin, no beta
#               cancer type).
VIEW_CASES: dict[str, tuple[str, str]] = {
    "mutation_panel_gene_coverage": ("coverage", ""),
    "mutation_wes_coverage": ("coverage", ""),
    "cna_panel_gene_coverage": ("coverage", ""),
    "cna_wes_coverage": ("coverage", ""),
    "sv_panel_gene_coverage": ("coverage", ""),
    "sv_wes_coverage": ("coverage", ""),
    "gene_mutation_frequency_by_cancer_type": (
        "preference",
        "(preference='local_e2e', gene='TP53')",
    ),
    "gene_alteration_frequency_by_cancer_type": (
        "preference",
        "(preference='local_e2e', gene='EGFR', alteration='amplification')",
    ),
    "top_mutated_genes_in_cohort": ("preference", "(preference='local_e2e', top_n=10)"),
    "gene_mutation_frequency_in_study": ("study", "(study={study}, gene='TP53')"),
    "gene_mutation_frequency_in_studies": ("study", "(studies=[{study}], gene='TP53')"),
    "top_mutated_genes_in_study": ("study", "(study={study}, top_n=10)"),
    "gene_mutation_variants_in_study": ("study", "(study={study}, gene='TP53')"),
    "co_altered_genes_in_study": ("study", "(study={study}, gene='TP53', top_n=10)"),
    "top_cna_genes_in_study": ("study", "(study={study}, top_n=10)"),
    "gene_cna_distribution_in_study": ("study", "(study={study}, gene='EGFR')"),
    "top_sv_genes_in_study": ("study", "(study={study}, top_n=10)"),
    "gene_pair_coexpression": (
        "study",
        "(study={study}, gene_a='TP53', gene_b='EGFR', profile_type='mrna')",
    ),
    "treatment_counts_in_study": ("study", "(study={study})"),
    "treatment_regimens_in_study": ("study", "(study={study})"),
    "clinical_attribute_counts": ("study", "(study={study}, attribute='CANCER_TYPE')"),
}
_HIDDEN_BETA = "concat('study_', 'beta')"


_COMPOSE_FILE = (
    Path(__file__).resolve().parent.parent / "docker" / "local-e2e" / "docker-compose.yml"
)


def _clickhouse_admin(statement: str) -> None:
    """Run DDL as the container's local default user (no network access)."""
    subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "exec", "-T", "clickhouse"]
        + ["clickhouse-client", "-q", statement],
        check=True,
    )


def _reapply_repo_views(*client_args: str) -> None:
    """Re-run sql/4, sql/5, sql/7 in the container, as the daily job would."""
    for name in (
        "4-mutation-frequency-views.sql",
        "5-gene-expression-views.sql",
        "7-clinical-views.sql",
    ):
        subprocess.run(
            ["docker", "compose", "-f", str(_COMPOSE_FILE), "exec", "-T", "clickhouse"]
            + ["clickhouse-client", "--database", "cbioportal_authz_e2e", "--multiquery"]
            + list(client_args)
            + ["--queries-file", f"/cbioportal-sql/{name}"],
            check=True,
        )


# Probes that only make sense where the resource is known to exist (the e2e
# server has a 'default' cluster and query logging); the server's own startup
# check sticks to probes that exist everywhere plus SHOW GRANTS FINAL.
_RECHECK_WAIT_SECONDS = 8  # > CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS + one check

_E2E_EXTRA_PROBES = {
    "clusterAllReplicas()": "SELECT 1 FROM clusterAllReplicas('default', system.one) LIMIT 0",
    "dictionary()": "SELECT * FROM dictionary('cbiomcp_probe') LIMIT 0",
    "system.query_log": "SELECT 1 FROM system.query_log LIMIT 0",
}


def _coverage_report(clickhouse_url: str):
    """Run scripts/verify_row_policy_coverage.py's check as local_e2e_admin."""
    import clickhouse_connect

    script = Path(__file__).resolve().parent / "verify_row_policy_coverage.py"
    spec = importlib.util.spec_from_file_location("verify_row_policy_coverage", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    url = httpx.URL(clickhouse_url)
    client = clickhouse_connect.get_client(
        host=url.host, port=url.port, username="local_e2e_admin", password="local_e2e_admin_pw"
    )
    try:
        return module.check_coverage(
            client,
            "cbioportal_authz_e2e",
            "cbioportal_mcp_study_restricted",
            "SQL_cbiomcp_allowed_studies",
        )
    finally:
        client.close()


def _rows(result: Any, label: str) -> list[dict]:
    assert isinstance(result, dict) and "rows" in result, f"{label}: {result}"
    return result["rows"]


def _is_empty_aggregate(rows: list[dict]) -> bool:
    """gene_pair_coexpression always returns one row; empty means num_samples 0."""
    if len(rows) == 1 and "num_samples" in rows[0]:
        return str(rows[0]["num_samples"]) == "0"
    return rows == []


async def run(keycloak_url: str, proxy_url: str, clickhouse_url: str) -> None:
    alice_token = await _token(keycloak_url, "alice", "alice")
    bob_token = await _token(keycloak_url, "bob", "bob")
    admin_token = await _token(keycloak_url, "admin-user", "admin-user")
    carol_token = await _token(keycloak_url, "carol", "carol")

    alice_access = await _call_with_token(proxy_url, alice_token, "get_current_study_access")
    bob_access = await _call_with_token(proxy_url, bob_token, "get_current_study_access")
    carol_access = await _call_with_token(proxy_url, carol_token, "get_current_study_access")

    assert alice_access["studies"] == ["study_alpha"], alice_access
    assert bob_access["studies"] == ["study_beta"], bob_access
    assert carol_access["studies"] == [] and not carol_access["all_studies"], carol_access

    # Client-supplied identity/group/allowlist headers are dropped by the proxy.
    spoofed = await _call_with_token(
        proxy_url,
        alice_token,
        "get_current_study_access",
        extra_headers={
            "x-keycloak-groups": "/cbioportal/admins",
            "x-user-groups": "/cbioportal/admins",
            "x-forwarded-groups": "/cbioportal/admins",
            "x-cbioportal-allowed-studies": "*",
            "x-user-id": "admin",
        },
    )
    assert spoofed["studies"] == ["study_alpha"] and not spoofed["all_studies"], spoofed
    assert spoofed["user_id"] == alice_access["user_id"], spoofed

    alice_studies = await _call_with_token(proxy_url, alice_token, "list_studies")
    alice_study_ids = {row["cancer_study_identifier"] for row in alice_studies}
    assert alice_study_ids == {"study_alpha"}, alice_studies

    async def call(token: str, query: str) -> Any:
        return await _call_with_token(
            proxy_url, token, "clickhouse_run_select_query", {"query": query, "max_rows": 1000}
        )

    async def select(token: str, query: str, label: str) -> list[dict]:
        return _rows(await call(token, query), label)

    allowed_query = await select(
        alice_token,
        "SELECT DISTINCT sample_unique_id FROM clinical_data_derived "
        "WHERE cancer_study_identifier = 'study_alpha'",
        "explicit allowed study",
    )
    assert {"study_alpha_S1", "study_alpha_S2"} <= {r["sample_unique_id"] for r in allowed_query}

    denied_query = await call(
        alice_token,
        "SELECT sample_unique_id FROM clinical_data_derived "
        "WHERE cancer_study_identifier = 'study_beta'",
    )
    assert "study_beta" in denied_query["error_message"], denied_query

    # The fixture must hold every table and view the catalog knows about, and
    # nothing the catalog doesn't.
    tables_result = await _call_with_token(proxy_url, admin_token, "clickhouse_list_tables")
    fixture_names = {row["name"] for row in tables_result["tables"]}
    assert fixture_names == classified_names(), (
        f"missing from fixture: {sorted(classified_names() - fixture_names)}; "
        f"unclassified: {sorted(fixture_names - classified_names())}"
    )
    assert set(EXPECTED_STUDY_ROWS) == set(STUDY_SCOPED_TABLES), sorted(
        set(STUDY_SCOPED_TABLES) ^ set(EXPECTED_STUDY_ROWS)
    )
    assert set(VIEW_CASES) == set(STUDY_SCOPED_VIEWS), sorted(
        set(STUDY_SCOPED_VIEWS) ^ set(VIEW_CASES)
    )

    # Unfiltered SELECT * on every study-scoped table: the app-level guard
    # can't see a study filter here, so this is ClickHouse row policies alone.
    for table in sorted(STUDY_SCOPED_TABLES):
        key, alpha_keys, beta_keys = EXPECTED_STUDY_ROWS[table]
        query = f"SELECT * FROM {table}"

        def row_key(row: dict, key=key) -> Any:
            return tuple(row[k] for k in key) if isinstance(key, tuple) else row[key]

        seen = {
            user: {row_key(row) for row in await select(token, query, f"{user} {table}")}
            for user, token in (
                ("alice", alice_token),
                ("bob", bob_token),
                ("admin", admin_token),
                ("carol", carol_token),
            )
        }
        assert seen["alice"] == alpha_keys, (table, "alice", seen)
        assert seen["bob"] == beta_keys, (table, "bob", seen)
        assert seen["admin"] == alpha_keys | beta_keys, (table, "admin", seen)
        assert seen["carol"] == set(), (table, "carol (empty allowlist)", seen)
    print(f"Row policies OK on {len(STUDY_SCOPED_TABLES)} study-scoped tables.")

    # Reference tables stay readable; cBioPortal's auth tables stay denied.
    for table in sorted(STUDY_AGNOSTIC_REFERENCE_TABLES):
        rows = await select(carol_token, f"SELECT * FROM {table}", f"carol {table}")
        assert rows, f"reference table {table} should be readable by every user"
    for table in sorted(DENIED_TABLES):
        rows = await select(admin_token, f"SELECT * FROM {table}", f"admin {table}")
        assert rows == [], (table, rows)

    # A filter the guard can't read (concat) is still enforced by ClickHouse.
    sneaky = await select(
        alice_token,
        f"SELECT * FROM sample_derived WHERE cancer_study_identifier = {_HIDDEN_BETA}",
        "alice concat bypass",
    )
    assert sneaky == [], sneaky

    # Every view: allowed data comes back, denied-study data never does.
    for view in sorted(VIEW_CASES):
        kind, args = VIEW_CASES[view]
        if kind == "coverage":
            alice = await select(alice_token, f"SELECT * FROM {view}", f"alice {view}")
            admin = await select(admin_token, f"SELECT * FROM {view}", f"admin {view}")
            assert alice and {r["cancer_study_identifier"] for r in alice} == {_ALPHA}, view
            assert {r["cancer_study_identifier"] for r in admin} == {_ALPHA, _BETA}, view
        elif kind == "study":
            q = f"SELECT * FROM {view}{args}"
            alice = await select(alice_token, q.format(study="'study_alpha'"), f"alice {view}")
            bob = await select(bob_token, q.format(study="'study_beta'"), f"bob {view}")
            hidden = await call(alice_token, q.format(study=_HIDDEN_BETA))
            assert not _is_empty_aggregate(alice), (view, "alice", alice)
            assert not _is_empty_aggregate(bob), (view, "bob", bob)
            # Either the guard rejects it outright, or ClickHouse returns nothing.
            assert "error_message" in hidden or _is_empty_aggregate(hidden["rows"]), (
                view,
                "alice -> hidden beta",
                hidden,
            )
        else:
            q = f"SELECT * FROM {view}{args}"
            alice = await select(alice_token, q, f"alice {view}")
            admin = await select(admin_token, q, f"admin {view}")
            assert alice, (view, "alice", alice)
            assert alice != admin, (view, "alice saw the whole cohort", alice)
            assert _BETA_CANCER_TYPE not in str(alice), (view, alice)
            if "cancer_type" in admin[0]:
                assert _BETA_CANCER_TYPE in str(admin), (view, admin)
    print(f"Row policies OK through all {len(VIEW_CASES)} views.")

    # A studies array the guard can't read, naming both studies: ClickHouse
    # still returns only alpha's cancer type.
    mixed = await select(
        alice_token,
        "SELECT * FROM gene_mutation_frequency_in_studies("
        f"studies=arrayConcat(['study_alpha'], [{_HIDDEN_BETA}]), gene='TP53')",
        "alice mixed studies",
    )
    assert [r["cancer_type"] for r in mixed] == ["Breast Cancer"], mixed

    # Allowlist override attempts: rejected by the guard...
    for attack in (
        "SELECT *, '--' AS marker FROM mutation SETTINGS SQL_cbiomcp_allowed_studies='*'",
        "SELECT * FROM mutation SETTINGS `SQL\\x5fcbiomcp_allowed_studies` = '*'",
        "SELECT getSetting('SQL_cbiomcp_allowed_studies')",
    ):
        result = await call(alice_token, attack)
        assert "allowlist" in result.get("error_message", ""), (attack, result)

    # ...and by ClickHouse itself: with readonly=1 on the request (what the
    # MCP sends), a query can't change the setting the request carries.
    import clickhouse_connect
    from clickhouse_connect import common as clickhouse_connect_common

    clickhouse_connect_common.set_setting("invalid_setting_action", "send")
    url = httpx.URL(clickhouse_url)
    direct = clickhouse_connect.get_client(
        host=url.host,
        port=url.port,
        username="mcp_authz",
        password="mcp_authz_pw",
        database="cbioportal_authz_e2e",
    )
    request_settings = {"SQL_cbiomcp_allowed_studies": "study_alpha", "readonly": "1"}
    try:
        direct.query(
            "SELECT count() FROM mutation SETTINGS SQL_cbiomcp_allowed_studies = '*'",
            settings=request_settings,
        )
    except Exception as e:
        assert "164" in str(e) or "readonly" in str(e).lower(), e
    else:
        raise AssertionError("ClickHouse accepted an in-query allowlist override")
    empty = direct.query(
        "SELECT count() FROM sample",
        settings={"SQL_cbiomcp_allowed_studies": "", "readonly": "1"},
    )
    assert empty.result_rows == [(0,)], empty.result_rows
    try:
        direct.query("SELECT count() FROM sample", settings={"readonly": "1"})
    except Exception as e:
        assert "SQL_cbiomcp_allowed_studies" in str(e), e  # absent setting fails closed
    else:
        raise AssertionError("a query without the allowlist setting returned rows")

    # The minimal restricted role (SELECT ON <db>.* only) can't reach outside
    # the row-policy boundary or read other requests' SQL / allowlists.
    # Only ACCESS_DENIED (497) counts: a missing cluster/dictionary or a
    # syntax error would prove nothing about privileges.
    for name, probe in {**RESTRICTED_ROLE_PROBES, **_E2E_EXTRA_PROBES}.items():
        try:
            direct.query(probe, settings=request_settings)
        except Exception as e:
            assert is_access_denied(e), (name, str(e)[:300])
            continue
        raise AssertionError(f"restricted role could run {name}: {probe}")

    # A view switched to SQL SECURITY NONE runs without the caller's row
    # policies and leaks every study through its USING 1 policy. Show the
    # leak, check the coverage verifier flags it, then restore INVOKER.
    view = "mutation_panel_gene_coverage"
    leak_query = f"SELECT DISTINCT cancer_study_identifier AS s FROM {view} ORDER BY s"
    _clickhouse_admin(f"ALTER TABLE cbioportal_authz_e2e.{view} MODIFY SQL SECURITY NONE")
    try:
        leaked = direct.query(leak_query, settings=request_settings).result_rows
        assert leaked == [("study_alpha",), ("study_beta",)], leaked  # the leak is real
        report = _coverage_report(clickhouse_url)
        assert any(view in e and "SQL SECURITY NONE" in e for e in report.errors), report
        # The running MCP's periodic re-check (3s in this stack) notices and
        # refuses study-data queries instead of serving the leak.
        await asyncio.sleep(_RECHECK_WAIT_SECONDS)
        refused = await call(alice_token, "SELECT * FROM sample")
        assert "disabled until" in refused.get("error_message", ""), refused
    finally:
        _clickhouse_admin(f"ALTER TABLE cbioportal_authz_e2e.{view} MODIFY SQL SECURITY INVOKER")
    restored = direct.query(leak_query, settings=request_settings).result_rows
    assert restored == [("study_alpha",)], restored
    await asyncio.sleep(_RECHECK_WAIT_SECONDS)
    assert await select(alice_token, "SELECT * FROM sample", "after re-check recovers")
    assert _coverage_report(clickhouse_url).ok
    direct.close()
    print("Restricted-role surface and SQL SECURITY NONE view checks passed.")

    # The daily job re-runs sql/4, sql/5, sql/7 (DROP + CREATE VIEW) without
    # re-applying the authz SQL, possibly under a profile whose default view
    # security isn't INVOKER. The CREATE VIEWs declare SQL SECURITY INVOKER
    # themselves, so the views stay INVOKER and filtered while the MCP keeps
    # running.
    _reapply_repo_views("--default_normal_view_sql_security=DEFINER")
    report = _coverage_report(clickhouse_url)
    assert report.ok, report
    coverage = await select(alice_token, "SELECT * FROM mutation_panel_gene_coverage", "after")
    assert coverage and {r["cancer_study_identifier"] for r in coverage} == {_ALPHA}, coverage
    print("Views stay SQL SECURITY INVOKER and filtered after re-running sql/4, 5, 7.")

    # Concurrent callers with different allowlists never see each other's rows.
    async def keys(token: str) -> set:
        return {r["internal_id"] for r in await select(token, "SELECT * FROM sample", "conc")}

    results = await asyncio.gather(*[keys(t) for t in [alice_token, bob_token] * 6])
    for i, seen in enumerate(results):
        assert seen == ({1, 2} if i % 2 == 0 else {3}), (i, seen)
    print("Allowlist override, empty/absent allowlist and concurrency checks passed.")

    print("Local Keycloak authz e2e passed.")
    print("Alice access:", alice_access)
    print("Bob access:", bob_access)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keycloak-url", default="http://127.0.0.1:18080")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8001/mcp/")
    parser.add_argument("--clickhouse-url", default="http://127.0.0.1:8123")
    args = parser.parse_args()
    asyncio.run(run(args.keycloak_url.rstrip("/"), args.proxy_url, args.clickhouse_url))


if __name__ == "__main__":
    main()
