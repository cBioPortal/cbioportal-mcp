#!/usr/bin/env python3
"""Exercise the local Keycloak-backed cBioPortal MCP authz stack."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from cbioportal_mcp.authentication.row_policy_catalog import (
    DENIED_TABLES,
    STUDY_SCOPED_TABLES,
    classified_names,
)


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


async def _call_with_token(proxy_url: str, token: str, tool: str, args: dict | None = None) -> Any:
    transport = StreamableHttpTransport(
        proxy_url,
        headers={"authorization": f"Bearer {token}"},
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, args or {}, raise_on_error=False)
    return _payload(result)


# For every study-scoped table in the fixture: (key column, keys owned by
# study_alpha, keys owned by study_beta). An unfiltered `SELECT *` as alice
# (study_alpha only) must return exactly the alpha keys - non-empty, and
# nothing from beta - and vice versa for bob; admin-user (all studies) must
# see both. Covers every STUDY_SCOPED_TABLES entry that exists in
# docker/local-e2e/clickhouse-init.sql (checked below, so a new fixture table
# can't silently skip this).
_ALPHA, _BETA = "study_alpha", "study_beta"
EXPECTED_STUDY_ROWS: dict[str, tuple[str, set, set]] = {
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
    "mutation_event": ("mutation_event_id", {101}, {102}),
    "mutation": ("mutation_event_id", {101}, {102}),
    "mutation_count_by_keyword": ("genetic_profile_id", {11}, {21}),
    "alteration_driver_annotation": ("alteration_event_id", {101}, {102}),
    "allele_specific_copy_number": ("mutation_event_id", {101}, {102}),
    "cna_event": ("cna_event_id", {201}, {202}),
    "sample_cna_event": ("cna_event_id", {201}, {202}),
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


def _rows(result: Any, label: str) -> list[dict]:
    assert isinstance(result, dict) and "rows" in result, f"{label}: {result}"
    return result["rows"]


async def run(keycloak_url: str, proxy_url: str) -> None:
    alice_token = await _token(keycloak_url, "alice", "alice")
    bob_token = await _token(keycloak_url, "bob", "bob")
    admin_token = await _token(keycloak_url, "admin-user", "admin-user")

    alice_access = await _call_with_token(proxy_url, alice_token, "get_current_study_access")
    bob_access = await _call_with_token(proxy_url, bob_token, "get_current_study_access")

    assert alice_access["studies"] == ["study_alpha"], alice_access
    assert bob_access["studies"] == ["study_beta"], bob_access

    alice_studies = await _call_with_token(proxy_url, alice_token, "list_studies")
    alice_study_ids = {row["cancer_study_identifier"] for row in alice_studies}
    assert alice_study_ids == {"study_alpha"}, alice_studies

    async def select(token: str, query: str, label: str) -> list[dict]:
        result = await _call_with_token(
            proxy_url, token, "clickhouse_run_select_query", {"query": query, "max_rows": 1000}
        )
        return _rows(result, label)

    allowed_query = await select(
        alice_token,
        "SELECT sample_unique_id FROM clinical_data_derived "
        "WHERE cancer_study_identifier = 'study_alpha'",
        "explicit allowed study",
    )
    assert len(allowed_query) == 2, allowed_query

    denied_query = await _call_with_token(
        proxy_url,
        alice_token,
        "clickhouse_run_select_query",
        {
            "query": (
                "SELECT sample_unique_id FROM clinical_data_derived "
                "WHERE cancer_study_identifier = 'study_beta'"
            )
        },
    )
    assert "study_beta" in denied_query["error_message"], denied_query

    # Every table in the fixture must be classified, and every study-scoped
    # one must have an expectation below.
    tables_result = await _call_with_token(proxy_url, admin_token, "clickhouse_list_tables")
    fixture_names = {row["name"] for row in tables_result["tables"]}
    unclassified = sorted(fixture_names - classified_names())
    assert not unclassified, f"fixture tables missing from row_policy_catalog: {unclassified}"
    fixture_study_tables = fixture_names & set(STUDY_SCOPED_TABLES)
    missing = sorted(fixture_study_tables - set(EXPECTED_STUDY_ROWS))
    assert not missing, f"study-scoped fixture tables with no e2e expectation: {missing}"

    # Unfiltered SELECT * on every study-scoped table: the app-level guard
    # can't see a study filter here, so this is ClickHouse row policies alone.
    for table in sorted(fixture_study_tables):
        key, alpha_keys, beta_keys = EXPECTED_STUDY_ROWS[table]
        query = f"SELECT * FROM {table}"
        seen = {
            "alice": {row[key] for row in await select(alice_token, query, f"alice {table}")},
            "bob": {row[key] for row in await select(bob_token, query, f"bob {table}")},
            "admin": {row[key] for row in await select(admin_token, query, f"admin {table}")},
        }
        assert seen["alice"] == alpha_keys, (table, "alice", seen)
        assert seen["bob"] == beta_keys, (table, "bob", seen)
        assert seen["admin"] == alpha_keys | beta_keys, (table, "admin", seen)
    print(f"Row policies OK on {len(fixture_study_tables)} study-scoped tables.")

    # Reference tables stay readable; cBioPortal's auth tables stay denied.
    genes = await select(alice_token, "SELECT * FROM gene", "alice gene")
    assert {row["hugo_gene_symbol"] for row in genes} == {"TP53", "KRAS", "EGFR"}, genes
    for table in sorted(fixture_names & DENIED_TABLES):
        rows = await select(admin_token, f"SELECT * FROM {table}", f"admin {table}")
        assert rows == [], (table, rows)

    # A filter the guard can't read (concat) is still enforced by ClickHouse.
    sneaky = await select(
        alice_token,
        "SELECT * FROM sample_derived WHERE cancer_study_identifier = concat('study_', 'beta')",
        "alice concat bypass",
    )
    assert sneaky == [], sneaky

    # Views inherit the underlying tables' policies (sql/4 plain view).
    coverage = await select(
        alice_token, "SELECT * FROM mutation_panel_gene_coverage", "alice coverage view"
    )
    assert coverage and {row["cancer_study_identifier"] for row in coverage} == {_ALPHA}, coverage

    # Parameterized views (sql/5, sql/7): allowed study returns data; a beta
    # study argument hidden from the guard via concat returns nothing.
    coexpression = (
        "SELECT * FROM gene_pair_coexpression("
        "study={study}, gene_a='TP53', gene_b='EGFR', profile_type='mrna')"
    )
    alpha_coexp = await select(
        alice_token, coexpression.format(study="'study_alpha'"), "alice coexpression"
    )
    assert alpha_coexp[0]["num_samples"] in (2, "2"), alpha_coexp
    beta_coexp = await select(
        alice_token, coexpression.format(study="concat('study_', 'beta')"), "alice coexp bypass"
    )
    assert beta_coexp[0]["num_samples"] in (0, "0"), beta_coexp

    # treatment_counts_in_study reads raw clinical_event/clinical_event_data/
    # patient/cancer_study - the multi-hop policies, through a view.
    treatments = "SELECT agent FROM treatment_counts_in_study(study={study})"
    bob_treatments = await select(bob_token, treatments.format(study="'study_beta'"), "bob tx")
    assert [row["agent"] for row in bob_treatments] == ["Cisplatin"], bob_treatments
    alice_sneaky_tx = await select(
        alice_token, treatments.format(study="concat('study_', 'beta')"), "alice tx bypass"
    )
    assert alice_sneaky_tx == [], alice_sneaky_tx

    print("Local Keycloak authz e2e passed.")
    print("Alice access:", alice_access)
    print("Bob access:", bob_access)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keycloak-url", default="http://127.0.0.1:18080")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8001/mcp/")
    args = parser.parse_args()
    asyncio.run(run(args.keycloak_url.rstrip("/"), args.proxy_url))


if __name__ == "__main__":
    main()
