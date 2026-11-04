"""Local e2e for per-deployment login providers (docker/local-e2e, profile "providers").

    docker compose -f docker/local-e2e/docker-compose.yml --profile providers up -d --build --wait
    uv run python scripts/local_e2e_auth_providers.py

Three MCP servers run against the same ClickHouse fixture with the acl row
policies (sql/study-authz/0-row-policies.sql), each configured only through
environment variables:

- ``keycloak``: CBIOPORTAL_MCP_AUTH_PROVIDER=oidc against the local Keycloak
  realm (studies from realm roles ``cbioportal:<id>``);
- ``okta``: the same code against an Okta-shaped mock issuer (``uid`` user
  id, login in ``sub``, custom ``cbio_studies`` claim, no email_verified),
  proving nothing depends on Keycloak;
- ``dat``: CBIOPORTAL_MCP_AUTH_PROVIDER=portal_dat against a fake portal.

For the OIDC modes the script runs the real browser flow against the MCP's
OAuth endpoints (dynamic registration, /authorize, the IdP's login, the
MCP's /auth/callback, /token) and calls tools with the token the MCP
issued. Every check reads rows through real ClickHouse row policies.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import re
import secrets
import time
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

STUDY_QUERY = "SELECT DISTINCT cancer_study_identifier FROM cancer_study ORDER BY 1"
PATIENT_QUERY = (
    "SELECT DISTINCT cs.cancer_study_identifier AS s FROM patient p "
    "JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id ORDER BY 1"
)
DAT_TOKENS = {
    "alice": "e2e-dat-alice-0000-0000-000000000001",
    "bob": "e2e-dat-bob-0000-0000-0000-000000000002",
    "carol": "e2e-dat-carol-000-0000-0000-000000000003",
}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit("FAIL: " + message)


async def _call(url: str, headers: dict, tool: str, args: dict | None = None) -> dict:
    async with Client(StreamableHttpTransport(url, headers=headers)) as client:
        result = await client.call_tool(tool, args or {}, raise_on_error=False)
    if result.is_error:
        return {"error": " ".join(getattr(c, "text", "") for c in result.content)}
    data = result.structured_content or {}
    return data


def call(url: str, headers: dict, tool: str, args: dict | None = None) -> dict:
    return asyncio.run(_call(url, headers, tool, args))


def studies_via_sql(url: str, headers: dict, query: str = STUDY_QUERY) -> list[str] | str:
    result = call(url, headers, "clickhouse_run_select_query", {"query": query, "max_rows": 100})
    if "error" in result or "error_message" in result:
        return result.get("error") or result["error_message"]
    rows = result.get("rows") or result.get("result") or []
    if isinstance(rows, dict):
        rows = rows.get("rows", [])
    values = []
    for row in rows:
        values.append(next(iter(row.values())) if isinstance(row, dict) else row[0])
    return sorted(set(values))


def oauth_login(base: str, username: str, password: str | None = None) -> str:
    """The MCP client OAuth flow; logs in at the IdP (Keycloak form, or mock issuer)."""
    redirect_uri = "http://127.0.0.1:9/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    with httpx.Client(follow_redirects=False, timeout=120) as http:
        reg = http.post(
            f"{base}/register",
            json={
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "local-e2e",
            },
        )
        _check(reg.status_code in (200, 201), f"register: {reg.status_code} {reg.text[:200]}")
        client_id = reg.json()["client_id"]
        resp = http.get(
            f"{base}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
                "state": "e2e",
                # The mock issuer reads login_hint only if forwarded; it also
                # honours a per-request ?login_hint on its own authorize URL.
            },
        )
        for _ in range(10):
            if resp.status_code in (301, 302, 303, 307):
                location = urljoin(str(resp.url), resp.headers["location"])
                if location.startswith(redirect_uri):
                    break
                if "/v1/authorize" in location and "login_hint=" not in location:
                    location += "&login_hint=" + username  # mock issuer only
                resp = http.get(location)
                continue
            form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', resp.text)
            _check(form is not None, f"no redirect or login form ({resp.status_code})")
            # Keycloak marks its cookies Secure; browsers still send them to
            # http://127.0.0.1 (a secure context), httpx does not, so replay
            # them the way a browser would.
            cookies = "; ".join(f"{c.name}={c.value}" for c in http.cookies.jar)
            resp = http.post(
                html.unescape(form.group(1)),
                data={"username": username, "password": password or username},
                headers={"cookie": cookies},
            )
        location = resp.headers.get("location", "")
        _check(location.startswith(redirect_uri), f"login did not complete: {resp.status_code}")
        code = parse_qs(urlparse(location).query)["code"][0]
        token = http.post(
            f"{base}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        _check(token.status_code == 200, f"token: {token.status_code} {token.text[:200]}")
        return token.json()["access_token"]


def run_oidc(name: str, base: str, users: dict[str, list[str] | None]) -> None:
    url = base + "/mcp/"
    with httpx.Client() as http:
        raw = base + "/mcp"
        _check(http.post(raw).status_code == 401, f"{name}: unauthenticated request not 401")
        bad = http.post(raw, headers={"authorization": "Bearer not-a-token"})
        _check(bad.status_code == 401, f"{name}: bad token not 401")
    for user, expected in users.items():
        token = oauth_login(base, user)
        headers = {"authorization": f"Bearer {token}"}
        access = call(url, headers, "get_current_study_access")
        if expected is None:
            message = access.get("error_message", access.get("error", ""))
            _check("study claim" in message, f"{name}/{user}: expected denial, got {access}")
            _check(
                not isinstance(studies_via_sql(url, headers), list),
                f"{name}/{user}: SQL ran without a study claim",
            )
            continue
        _check(access.get("studies") == expected, f"{name}/{user}: access {access}")
        _check(studies_via_sql(url, headers) == expected, f"{name}/{user}: cancer_study rows")
        _check(
            studies_via_sql(url, headers, PATIENT_QUERY) == expected,
            f"{name}/{user}: patient rows (row policy through FK)",
        )
        spoof = {**headers, "x-cbioportal-allowed-studies": "*", "x-user-email": "admin@x"}
        _check(
            call(url, spoof, "get_current_study_access").get("studies") == expected,
            f"{name}/{user}: headers changed access",
        )
    print(f"{name}: OIDC login, jwt studies and row policies OK for {sorted(users)}")


def run_dat(base: str, portal: str, ttl: float) -> None:
    url = base + "/mcp/"

    def h(user):
        return {"x-auth-token": DAT_TOKENS[user]}

    _check("x-auth-token" in call(url, {}, "list_studies").get("error", ""), "dat: no token")
    unknown = call(url, {"x-auth-token": "e2e-unknown-token-0000"}, "list_studies")
    _check("error" in unknown or "error_message" in unknown, "dat: unknown token")
    for user, expected in (("alice", ["study_alpha"]), ("bob", ["study_beta"])):
        _check(call(url, h(user), "get_current_study_access")["studies"] == expected, user)
        _check(studies_via_sql(url, h(user)) == expected, f"dat/{user}: rows")
        _check(studies_via_sql(url, h(user), PATIENT_QUERY) == expected, f"dat/{user}: patients")
    spoof = {**h("bob"), "x-cbioportal-allowed-studies": "study_alpha", "x-user-id": "alice"}
    _check(call(url, spoof, "get_current_study_access")["studies"] == ["study_beta"], "spoof")

    # Revoked token: still served from cache within the TTL, denied after it.
    both = ["study_alpha", "study_beta"]
    _check(studies_via_sql(url, h("carol")) == both, "dat/carol before revoke")
    httpx.post(f"{portal}/_test/revoke", params={"user": "carol"}).raise_for_status()
    time.sleep(ttl + 1)
    _check(not isinstance(studies_via_sql(url, h("carol")), list), "revoked token still served")
    revoked = call(url, h("carol"), "get_current_study_access")
    _check("error" in revoked or "error_message" in revoked, "revoked token access")

    # Portal outage: denied once the cached lookup expires.
    httpx.post(f"{portal}/_test/outage", params={"on": "1"}).raise_for_status()
    try:
        time.sleep(ttl + 1)
        outage = call(url, h("alice"), "get_current_study_access")
        _check("error" in outage or "error_message" in outage, "outage allowed")
        _check(not isinstance(studies_via_sql(url, h("alice")), list), "outage served rows")
    finally:
        httpx.post(f"{portal}/_test/outage", params={"on": "0"}).raise_for_status()

    requests = httpx.get(f"{portal}/_test/requests").json()
    _check({m for m, _p, _a in requests} == {"GET"}, f"dat: non-GET portal call {requests}")
    _check(all("/api/data-access-tokens/" not in p for _m, p, _a in requests), "token in URL")
    _check(all(t not in p for _m, p, _a in requests for t in DAT_TOKENS.values()), "token in URL")
    print("dat: portal studies, revocation after TTL, outage denial and read-only portal OK")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keycloak-mcp", default="http://127.0.0.1:18100")
    parser.add_argument("--okta-mcp", default="http://127.0.0.1:18101")
    parser.add_argument("--dat-mcp", default="http://127.0.0.1:18102")
    parser.add_argument("--fake-portal", default="http://127.0.0.1:18091")
    parser.add_argument("--dat-cache-seconds", type=float, default=5.0)
    parser.add_argument("--only", choices=["keycloak", "okta", "dat"], action="append")
    args = parser.parse_args()
    modes = args.only or ["keycloak", "okta", "dat"]
    if "keycloak" in modes:
        run_oidc(
            "keycloak",
            args.keycloak_mcp,
            # carol has no realm roles, so Keycloak omits realm_access: denied.
            {"alice": ["study_alpha"], "bob": ["study_beta"], "carol": None},
        )
    if "okta" in modes:
        run_oidc(
            "okta",
            args.okta_mcp,
            {
                "alice@example.org": ["study_alpha"],
                "bob@example.org": ["study_beta"],
                "carol@example.org": None,
            },
        )
    if "dat" in modes:
        run_dat(args.dat_mcp, args.fake_portal, args.dat_cache_seconds)
    print("Local auth provider e2e passed.")


if __name__ == "__main__":
    main()
