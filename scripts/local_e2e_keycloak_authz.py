#!/usr/bin/env python3
"""Exercise the local Keycloak-backed cBioPortal MCP authz stack."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from cbioportal_mcp.authentication.row_policy_catalog import (
    DENIED_TABLES,
    IDENTITY_SCOPED_TABLES,
    STUDY_AGNOSTIC_REFERENCE_TABLES,
    STUDY_SCOPED_TABLES,
    STUDY_SCOPED_VIEWS,
    USER_STUDY_REFRESH_VIEW,
    USER_STUDY_TABLE,
    classified_names,
)
from cbioportal_mcp.clickhouse_query_settings import (
    READONLY_CODE,
    RESTRICTED_ROLE_PROBES,
    clickhouse_error_code,
    is_access_denied,
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


# Re-mint a user's access token when less than this much life remains. The
# realm keeps Keycloak's short production-like lifetime (300s), and a full run
# on a loaded machine takes longer than that.
TOKEN_REFRESH_MARGIN_SECONDS = 60


class KeycloakToken:
    """A user's Keycloak access token, re-minted before it expires.

    Every MCP call goes through ``_call_with_token``, which asks for
    ``await token.value()``: the cached token while more than
    TOKEN_REFRESH_MARGIN_SECONDS of its ``expires_in`` remain, otherwise a new
    one from a fresh password grant. A lock keeps concurrent callers from
    minting several at once.
    """

    def __init__(self, base_url: str, username: str, password: str, *, clock=time.monotonic):
        self.base_url = base_url
        self.username = username
        self.password = password
        self._clock = clock
        self._access_token: str | None = None
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()
        self.mints = 0

    async def _mint(self) -> tuple[str, int]:
        token_url = f"{self.base_url}/realms/cbioportal-mcp-local/protocol/openid-connect/token"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "password",
                    "client_id": "mcp-local",
                    "username": self.username,
                    "password": self.password,
                    "scope": "openid profile email",
                },
            )
        response.raise_for_status()
        body = response.json()
        return body["access_token"], int(body.get("expires_in", 0))

    async def value(self) -> str:
        async with self._lock:
            if self._access_token is None or self._clock() >= self._refresh_at:
                issued = self._clock()
                self._access_token, expires_in = await self._mint()
                self._refresh_at = issued + max(0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
                self.mints += 1
            return self._access_token

    def __repr__(self) -> str:  # never print the token itself
        return f"KeycloakToken({self.username!r})"


async def _token(base_url: str, username: str, password: str) -> KeycloakToken:
    token = KeycloakToken(base_url, username, password)
    await token.value()  # fail fast if the user can't sign in
    return token


async def _call_with_token(
    proxy_url: str,
    token: KeycloakToken | str,
    tool: str,
    args: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    bearer = await token.value() if isinstance(token, KeycloakToken) else token
    transport = StreamableHttpTransport(
        proxy_url,
        headers={**(extra_headers or {}), "authorization": f"Bearer {bearer}"},
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, args or {}, raise_on_error=False)
    return _payload(result)


# For every study-scoped table: (key column, keys owned by study_alpha, keys
# owned by study_beta). An unfiltered `SELECT *` as alice (study_alpha only)
# must return exactly the alpha keys - non-empty, and nothing from beta - and
# vice versa for bob; admin-user (all studies) must see both. Every table and
# view in row_policy_catalog must exist in the fixture (checked below).
_ALPHA, _BETA, _PUBLIC = "study_alpha", "study_beta", "study_public"
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


# study_public (groups PUBLIC) only has a cancer_study row: the keys each table
# holds for it. Visible to admin ('*' / ALL) in both sources, and to every
# known user in the authorities source.
EXPECTED_PUBLIC_ROWS: dict[str, set] = {"cancer_study": {3}}

# Studies each Keycloak user may read, per source. acl: study-acl.json groups
# (admin is '*'); authorities: the fixture's authorities/users rows -
# alice direct grant, bob group grant, admin ALL, carol nothing but PUBLIC.
EXPECTED_ACCESS = {
    "acl": {
        "alice": {_ALPHA},
        "bob": {_BETA},
        "admin": {_ALPHA, _BETA, _PUBLIC},
        "carol": set(),
    },
    "authorities": {
        "alice": {_ALPHA, _PUBLIC},
        "bob": {_BETA, _PUBLIC},
        "admin": {_ALPHA, _BETA, _PUBLIC},
        "carol": {_PUBLIC},
    },
}
SETTING_BY_SOURCE = {"acl": "SQL_cbiomcp_allowed_studies", "authorities": "SQL_cbio_user"}
EMAIL = {
    "alice": "alice@example.org",
    "bob": "bob@example.org",
    "admin": "admin@example.org",
    "carol": "carol@example.org",
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
        + ["clickhouse-client", "--multiquery", "-q", statement],
        check=True,
        # An INSERT ... VALUES given with -q would otherwise wait for more
        # data on the (non-tty) stdin docker exec -T attaches.
        stdin=subprocess.DEVNULL,
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


def _coverage_report(clickhouse_url: str, source: str = "acl"):
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
            SETTING_BY_SOURCE[source],
            source=source,
        )
    finally:
        client.close()


def _rows(result: Any, label: str) -> list[dict]:
    assert isinstance(result, dict) and "rows" in result, f"{label}: {result}"
    rows = result["rows"]
    # Beta's compact result format sends columns once and rows as arrays
    # (CBIOPORTAL_MCP_RESULT_FORMAT=compact, the default).
    if "columns" in result and all(isinstance(row, list) for row in rows):
        return [dict(zip(result["columns"], row, strict=True)) for row in rows]
    return rows


def _is_empty_aggregate(rows: list[dict]) -> bool:
    """gene_pair_coexpression always returns one row; empty means num_samples 0."""
    if len(rows) == 1 and "num_samples" in rows[0]:
        return str(rows[0]["num_samples"]) == "0"
    return rows == []


def _expected_keys(table: str, studies: set[str]) -> set:
    _key, alpha_keys, beta_keys = EXPECTED_STUDY_ROWS[table]
    keys: set = set()
    if _ALPHA in studies:
        keys |= alpha_keys
    if _BETA in studies:
        keys |= beta_keys
    if _PUBLIC in studies:
        keys |= EXPECTED_PUBLIC_ROWS.get(table, set())
    return keys


async def run(keycloak_url: str, proxy_url: str, clickhouse_url: str, source: str) -> None:
    expected = EXPECTED_ACCESS[source]
    setting = SETTING_BY_SOURCE[source]
    authorities = source == "authorities"
    print(f"Study access source: {source} (ClickHouse setting {setting})")
    alice_token = await _token(keycloak_url, "alice", "alice")
    bob_token = await _token(keycloak_url, "bob", "bob")
    admin_token = await _token(keycloak_url, "admin-user", "admin-user")
    carol_token = await _token(keycloak_url, "carol", "carol")

    alice_access = await _call_with_token(proxy_url, alice_token, "get_current_study_access")
    bob_access = await _call_with_token(proxy_url, bob_token, "get_current_study_access")
    carol_access = await _call_with_token(proxy_url, carol_token, "get_current_study_access")
    admin_access = await _call_with_token(proxy_url, admin_token, "get_current_study_access")

    assert set(alice_access["studies"]) == expected["alice"], alice_access
    assert set(bob_access["studies"]) == expected["bob"], bob_access
    assert set(carol_access["studies"]) == expected["carol"], carol_access
    assert not carol_access["all_studies"], carol_access
    if authorities:
        # No unrestricted value exists: even admin gets an explicit list.
        assert not admin_access["all_studies"], admin_access
        assert set(admin_access["studies"]) == expected["admin"], admin_access
        assert alice_access["user_email"] == EMAIL["alice"], alice_access
        # bob's users row is 'Bob@Example.org': matched case-insensitively.
        assert bob_access["user_email"] == EMAIL["bob"], bob_access
    else:
        assert admin_access["all_studies"], admin_access

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
            "x-user-email": EMAIL["admin"],
            "x-cbioportal-mcp-proxy-secret": "guessed",
        },
    )
    assert set(spoofed["studies"]) == expected["alice"] and not spoofed["all_studies"], spoofed
    assert spoofed["user_id"] == alice_access["user_id"], spoofed

    alice_studies = await _call_with_token(proxy_url, alice_token, "list_studies")
    alice_study_ids = {row["cancer_study_identifier"] for row in alice_studies}
    assert alice_study_ids == expected["alice"], alice_studies

    async def call(token: KeycloakToken, query: str) -> Any:
        return await _call_with_token(
            proxy_url, token, "clickhouse_run_select_query", {"query": query, "max_rows": 1000}
        )

    async def select(token: KeycloakToken, query: str, label: str) -> list[dict]:
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
    fixture_names = {
        row if isinstance(row, str) else row["name"] for row in tables_result["tables"]
    }
    known = classified_names()
    if not authorities:  # user_study and its refresh MV exist only in authorities mode
        known = known - set(IDENTITY_SCOPED_TABLES) - {USER_STUDY_REFRESH_VIEW}
    assert fixture_names == known, (
        f"missing from fixture: {sorted(known - fixture_names)}; "
        f"unclassified: {sorted(fixture_names - known)}"
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
        for user in ("alice", "bob", "admin", "carol"):
            want = _expected_keys(table, expected[user])
            assert seen[user] == want, (table, user, seen)
        assert seen["alice"] and seen["bob"], (table, "fixture rows missing", seen)
    print(f"Row policies OK on {len(STUDY_SCOPED_TABLES)} study-scoped tables.")

    # Reference tables stay readable; cBioPortal's auth tables stay denied.
    for table in sorted(STUDY_AGNOSTIC_REFERENCE_TABLES):
        rows = await select(carol_token, f"SELECT * FROM {table}", f"carol {table}")
        assert rows, f"reference table {table} should be readable by every user"
    for table in sorted(DENIED_TABLES):
        if table not in fixture_names:
            continue  # user_study_refresh exists only in authorities mode
        rows = await select(admin_token, f"SELECT * FROM {table}", f"admin {table}")
        assert rows == [], (table, rows)
    if authorities:
        # user_study is readable, but only the caller's own entitlements.
        for user, token in (("alice", alice_token), ("carol", carol_token)):
            rows = await select(token, f"SELECT * FROM {USER_STUDY_TABLE}", f"{user} user_study")
            assert {r["email"] for r in rows} == {EMAIL[user]}, (user, rows)
            assert {r["cancer_study_identifier"] for r in rows} == expected[user], (user, rows)
        print(
            "authorities/users/data_access_tokens/user_study_refresh denied; "
            "user_study self-only."
        )

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
            assert "error_message" in hidden or _is_empty_aggregate(_rows(hidden, view)), (
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

    # Allowlist / identity override attempts: rejected by the guard...
    attacks = [
        "SELECT *, '--' AS marker FROM mutation SETTINGS SQL_cbiomcp_allowed_studies='*'",
        "SELECT * FROM mutation SETTINGS `SQL\\x5fcbiomcp_allowed_studies` = '*'",
        "SELECT getSetting('SQL_cbiomcp_allowed_studies')",
    ]
    if authorities:
        attacks += [
            f"SELECT * FROM sample SETTINGS SQL_cbio_user='{EMAIL['admin']}'",
            f"SELECT * FROM sample SETTINGS `SQL\\x5fcbio_user` = '{EMAIL['admin']}'",
            "SELECT getSetting('SQL_cbio_user')",
        ]
    for attack in attacks:
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
    alice_value = EMAIL["alice"] if authorities else "study_alpha"
    widen_value = EMAIL["admin"] if authorities else "*"
    request_settings = {setting: alice_value, "readonly": "1", "use_query_cache": "0"}
    try:
        direct.query(
            f"SELECT count() FROM mutation SETTINGS {setting} = '{widen_value}'",
            settings=request_settings,
        )
    except Exception as e:
        assert clickhouse_error_code(e) == READONLY_CODE, e
    else:
        raise AssertionError("ClickHouse accepted an in-query allowlist/identity override")
    empty = direct.query(
        "SELECT count() FROM sample",
        settings={setting: "", "readonly": "1"},
    )
    assert empty.result_rows == [(0,)], empty.result_rows
    try:
        direct.query("SELECT count() FROM sample", settings={"readonly": "1"})
    except Exception as e:
        assert setting in str(e), e  # absent setting fails closed
    else:
        raise AssertionError("a query without the allowlist/identity setting returned rows")
    if authorities:
        # Disabled (dave) and unknown (ghost) users' authorities grant nothing,
        # not even PUBLIC; an email differing only in case is the same user.
        for email, want in (
            ("dave@example.org", 0),
            ("ghost@example.org", 0),
            ("BOB@EXAMPLE.ORG", 0),  # the MCP always sends lower-case
            ("bob@example.org", 2),  # study_beta via group + study_public
        ):
            got = direct.query(
                "SELECT count() FROM cancer_study", settings={setting: email, "readonly": "1"}
            ).result_rows
            assert got == [(want,)], (email, got)
        # ClickHouse's query cache can't hand alice's rows to bob even if a
        # request asks for it (the MCP itself always sends use_query_cache=0).
        cached = {}
        for email in (EMAIL["alice"], EMAIL["bob"]):
            cached[email] = direct.query(
                "SELECT groupArray(cancer_study_identifier) FROM "
                "(SELECT cancer_study_identifier FROM cancer_study ORDER BY 1)",
                settings={
                    setting: email,
                    "use_query_cache": "1",
                    "query_cache_nondeterministic_function_handling": "ignore",
                },
            ).result_rows
        assert cached[EMAIL["alice"]] != cached[EMAIL["bob"]], cached
        print("Identity override, empty/absent/disabled/unknown identity checks passed.")

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
        report = _coverage_report(clickhouse_url, source)
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
    report = _coverage_report(clickhouse_url, source)
    assert report.ok, report
    direct.close()
    print("Restricted-role surface and SQL SECURITY NONE view checks passed.")

    # The daily job re-runs sql/4, sql/5, sql/7 (DROP + CREATE VIEW) without
    # re-applying the authz SQL, possibly under a profile whose default view
    # security isn't INVOKER. The CREATE VIEWs declare SQL SECURITY INVOKER
    # themselves, so the views stay INVOKER and filtered while the MCP keeps
    # running.
    _reapply_repo_views("--default_normal_view_sql_security=DEFINER")
    report = _coverage_report(clickhouse_url, source)
    assert report.ok, report
    # While a view is being dropped and re-created, system.tables can briefly
    # list it with an empty create_table_query (seen on 25.8), which the MCP's
    # periodic re-check reads as "SQL SECURITY unset" and fails closed on.
    # Wait one re-check so the MCP has re-validated the finished views.
    await asyncio.sleep(_RECHECK_WAIT_SECONDS)
    coverage = await select(alice_token, "SELECT * FROM mutation_panel_gene_coverage", "after")
    assert coverage and {r["cancer_study_identifier"] for r in coverage} == {_ALPHA}, coverage
    print("Views stay SQL SECURITY INVOKER and filtered after re-running sql/4, 5, 7.")

    # Concurrent callers with different allowlists never see each other's rows.
    async def keys(token: KeycloakToken) -> set:
        return {r["internal_id"] for r in await select(token, "SELECT * FROM sample", "conc")}

    results = await asyncio.gather(*[keys(t) for t in [alice_token, bob_token] * 6])
    for i, seen in enumerate(results):
        assert seen == ({1, 2} if i % 2 == 0 else {3}), (i, seen)
    print("Allowlist override, empty/absent allowlist and concurrency checks passed.")

    if authorities:
        await _check_revocation(alice_token, select)
        await _check_stale_entitlements_deny(proxy_url, alice_token, admin_token, select)

    print("Local Keycloak authz e2e passed.")
    print("Alice access:", alice_access)
    print("Bob access:", bob_access)


async def _check_revocation(alice_token: KeycloakToken, select) -> None:
    """A deleted authority stops granting rows after one user_study refresh."""

    def refresh() -> None:
        _clickhouse_admin(
            "SYSTEM REFRESH VIEW cbioportal_authz_e2e.user_study_refresh; "
            "SYSTEM WAIT VIEW cbioportal_authz_e2e.user_study_refresh"
        )

    def studies_seen(rows: list[dict]) -> set:
        return {r["cancer_study_identifier"] for r in rows}

    query = "SELECT cancer_study_identifier FROM cancer_study"
    assert studies_seen(await select(alice_token, query, "before revoke")) == {_ALPHA, _PUBLIC}
    _clickhouse_admin(
        "ALTER TABLE cbioportal_authz_e2e.authorities DELETE "
        "WHERE email = 'alice@example.org' AND authority = 'cbioportal:study_alpha' "
        "SETTINGS mutations_sync = 2"
    )
    try:
        # Until the refresh runs, the previous grant still applies.
        assert _ALPHA in studies_seen(await select(alice_token, query, "before refresh"))
        refresh()
        assert studies_seen(await select(alice_token, query, "after revoke")) == {_PUBLIC}
        rows = await select(alice_token, "SELECT * FROM sample", "after revoke samples")
        assert rows == [], rows
    finally:
        _clickhouse_admin(
            "INSERT INTO cbioportal_authz_e2e.authorities VALUES "
            "('alice@example.org', 'cbioportal:study_alpha')"
        )
        refresh()
    assert studies_seen(await select(alice_token, query, "after re-grant")) == {_ALPHA, _PUBLIC}
    print("Revocation took effect on the next user_study refresh; re-grant restored access.")


# Must match STUDY_AUTHZ_MAX_STALENESS_SECONDS in docker-compose.yml.
_MAX_STALENESS_SECONDS = 90


def _clickhouse_admin_scalar(query: str) -> str:
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "exec", "-T", "clickhouse"]
        + ["clickhouse-client", "-q", query],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    ).stdout.strip()


async def _check_stale_entitlements_deny(proxy_url, alice_token, admin_token, select) -> None:
    """A broken refresh can't keep (revoked) grants alive past the staleness bound.

    Stop user_study_refresh, revoke alice's grant, and confirm that while the
    data is inside the bound the old grant still answers, that once it is
    older than the bound every path is denied for everyone - plain SQL,
    list_studies and alice's already-cached study guide - and that restarting
    the refresh restores access minus the revoked grant.
    """
    db = "cbioportal_authz_e2e"
    studies_query = "SELECT cancer_study_identifier FROM cancer_study"

    def seen(rows):
        return {r["cancer_study_identifier"] for r in rows}

    async def guide(token):
        result = await _call_with_token(
            proxy_url, token, "get_study_guide", {"study_id": "study_alpha"}
        )
        return result if isinstance(result, str) else str(result)

    alice_guide = await guide(alice_token)
    assert "Study Guide" in alice_guide and "Error" not in alice_guide[:20], alice_guide
    _clickhouse_admin(f"SYSTEM STOP VIEW {db}.user_study_refresh")
    try:
        _clickhouse_admin(
            f"ALTER TABLE {db}.authorities DELETE WHERE email = 'alice@example.org' "
            "AND authority = 'cbioportal:study_alpha' SETTINGS mutations_sync = 2"
        )
        # Inside the bound, the last refresh still answers (the revoke is pending).
        assert _ALPHA in seen(await select(alice_token, studies_query, "stale, in bound"))
        # Wait until the last refresh is older than the bound.
        deadline = time.monotonic() + _MAX_STALENESS_SECONDS + 90
        while True:
            age = int(
                _clickhouse_admin_scalar(
                    f"SELECT toInt64(now() - max(refreshed_at)) FROM {db}.user_study"
                )
            )
            if age > _MAX_STALENESS_SECONDS + 2:
                break
            assert time.monotonic() < deadline, f"refreshed_at never aged ({age}s)"
            await asyncio.sleep(min(10, _MAX_STALENESS_SECONDS + 3 - age))
        print(f"user_study is {age}s old (> {_MAX_STALENESS_SECONDS}s bound): checking denial")
        for user, token in (("alice", alice_token), ("admin", admin_token)):
            assert seen(await select(token, studies_query, f"{user} stale")) == set(), user
            assert await select(token, "SELECT * FROM sample", f"{user} stale samples") == []
            assert await select(token, "SELECT * FROM user_study", f"{user} stale self") == []
        listed = await _call_with_token(proxy_url, alice_token, "list_studies")
        assert listed == [] or all("cancer_study_identifier" not in r for r in listed), listed
        cached = await guide(alice_token)
        assert cached.startswith("Error:") and "Study Guide" not in cached, cached
        print("Stale entitlements: SQL, list_studies and the cached guide are all denied.")
        # Restart the refresh: access comes back, without the revoked grant.
        _clickhouse_admin(f"SYSTEM START VIEW {db}.user_study_refresh")
        _clickhouse_admin(
            f"SYSTEM REFRESH VIEW {db}.user_study_refresh; SYSTEM WAIT VIEW {db}.user_study_refresh"
        )
        assert seen(await select(alice_token, studies_query, "after restart")) == {_PUBLIC}
        assert (await guide(alice_token)).startswith("Error:")
    finally:
        _clickhouse_admin(f"SYSTEM START VIEW {db}.user_study_refresh")
        _clickhouse_admin(
            f"INSERT INTO {db}.authorities VALUES ('alice@example.org', 'cbioportal:study_alpha')"
        )
        _clickhouse_admin(
            f"SYSTEM REFRESH VIEW {db}.user_study_refresh; SYSTEM WAIT VIEW {db}.user_study_refresh"
        )
    assert seen(await select(alice_token, studies_query, "re-granted")) == {_ALPHA, _PUBLIC}
    assert "Study Guide" in await guide(alice_token)
    print("Refresh restarted: revoked grant stays gone; re-grant restores access.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keycloak-url", default="http://127.0.0.1:18080")
    parser.add_argument("--proxy-url", default="http://127.0.0.1:8001/mcp/")
    parser.add_argument("--clickhouse-url", default="http://127.0.0.1:8123")
    parser.add_argument(
        "--source",
        choices=sorted(EXPECTED_ACCESS),
        default="acl",
        help="Study access source the stack was started with (STUDY_AUTHZ_SOURCE)",
    )
    args = parser.parse_args()
    asyncio.run(
        run(args.keycloak_url.rstrip("/"), args.proxy_url, args.clickhouse_url, args.source)
    )


if __name__ == "__main__":
    main()
