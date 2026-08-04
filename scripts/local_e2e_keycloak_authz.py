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
    token_url = (
        f"{base_url}/realms/cbioportal-mcp-local/protocol/openid-connect/token"
    )
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


async def run(keycloak_url: str, proxy_url: str) -> None:
    alice_token = await _token(keycloak_url, "alice", "alice")
    bob_token = await _token(keycloak_url, "bob", "bob")

    alice_access = await _call_with_token(proxy_url, alice_token, "get_current_study_access")
    bob_access = await _call_with_token(proxy_url, bob_token, "get_current_study_access")

    assert alice_access["studies"] == ["study_alpha"], alice_access
    assert bob_access["studies"] == ["study_beta"], bob_access

    alice_studies = await _call_with_token(proxy_url, alice_token, "list_studies")
    alice_study_ids = {row["cancer_study_identifier"] for row in alice_studies}
    assert alice_study_ids == {"study_alpha"}, alice_studies

    allowed_query = await _call_with_token(
        proxy_url,
        alice_token,
        "clickhouse_run_select_query",
        {
            "query": (
                "SELECT sample_unique_id FROM clinical_data_derived "
                "WHERE cancer_study_identifier = 'study_alpha'"
            )
        },
    )
    assert len(allowed_query["rows"]) == 2, allowed_query

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

    indirect_mutations = await _call_with_token(
        proxy_url,
        alice_token,
        "clickhouse_run_select_query",
        {
            "query": (
                "SELECT mutation_event_id, hugo_gene_symbol, mutation_variant "
                "FROM mutation ORDER BY mutation_event_id"
            )
        },
    )
    assert indirect_mutations["rows"] == [
        {
            "mutation_event_id": 101,
            "hugo_gene_symbol": "TP53",
            "mutation_variant": "p.R175H",
        }
    ], indirect_mutations

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
