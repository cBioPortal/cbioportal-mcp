"""CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=authorities (option A).

The MCP keeps one shared ClickHouse user and sends only the caller's verified
email; ClickHouse resolves the studies from cBioPortal's authorities table.
These tests pin the MCP side of that contract with a fake ClickHouse that
behaves like the row policies (rows depend only on the identity setting):

- the identity comes from the validated OAuth token (or, in proxy mode, the
  email header plus the proxy secret) - never tool arguments, other headers,
  or the prompt - and a missing/unverified identity fails closed;
- nothing ever sends the unrestricted '*' value in this mode;
- in-query overrides of the identity setting are refused;
- no cache (ClickHouse's query cache or the MCP's own) can return caller A's
  result to caller B.

The real row filtering is exercised by the local e2e (docker/local-e2e).
"""

from __future__ import annotations

import contextvars
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cbioportal_mcp import clickhouse_query_settings as cqs
from cbioportal_mcp import server
from cbioportal_mcp.authentication import study_access
from cbioportal_mcp.authentication.study_access import (
    AuthorizationError,
    StudyAccessConfigError,
    clickhouse_study_access_settings,
    get_current_study_access,
    guard_query_study_access,
    normalize_email,
    validate_study_access_startup_config,
    verified_caller_identity,
)

USER_SETTING = "SQL_cbio_user"

# What the row policies would let each email see.
ENTITLEMENTS = {
    "alice@example.org": ["study_alpha", "study_public"],
    "bob@example.org": ["study_beta", "study_public"],
}

_TOKEN: contextvars.ContextVar = contextvars.ContextVar("token", default=None)
_HEADERS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("headers", default=None)


_OMIT = object()


def _token(email, *, sub="sub-1", verified=True, record="google_user_data", record_email=_OMIT):
    """An AccessToken shaped like FastMCP's GoogleTokenVerifier output.

    claims carry ``email`` (from v2 userinfo) plus the raw records
    ``google_user_data`` (v2 userinfo) and ``google_token_info`` (v1
    tokeninfo); Google's own ``verified_email`` lives inside them.
    """
    claims = {"sub": sub, "email": email, "google_user_data": {}, "google_token_info": {}}
    if verified is not _OMIT:
        claims[record] = {
            "email": email if record_email is _OMIT else record_email,
            "verified_email": verified,
        }
    return SimpleNamespace(claims=claims)


def login(email, **kwargs):
    _TOKEN.set(_token(email, **kwargs) if email else None)


class FakeClickHouse:
    """Answers like ClickHouse with the authorities policies applied."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, query, settings):
        self.calls.append((query, dict(settings)))
        email = settings.get(USER_SETTING, settings.get("SQL_portal_user"))
        if email is None:
            raise RuntimeError("Code: 115. Unknown setting 'SQL_cbio_user'")  # getSetting raises
        visible = ENTITLEMENTS.get(email, [])
        if "FROM user_study" in query:
            return {"columns": ["cancer_study_identifier"], "rows": [[s] for s in visible]}
        if "FROM cancer_study" in query:
            columns = ["cancer_study_identifier", "name", "description", "type_of_cancer_id"]
            columns.append("sample_count")
            return {"columns": columns, "rows": [[s, s, "", "brca", 1] for s in visible]}
        # Anything else: a per-caller marker, so a cross-caller reuse is visible.
        return {"columns": ["owner"], "rows": [[email]]}


@pytest.fixture
def clickhouse(monkeypatch):
    fake = FakeClickHouse()
    monkeypatch.setattr(cqs, "run_query_with_settings", fake)
    monkeypatch.setattr(server, "run_query_with_settings", fake)
    monkeypatch.setattr(cqs, "current_surface_problem", lambda: None)
    monkeypatch.setattr(server, "current_surface_problem", lambda: None)
    return fake


@pytest.fixture
def authorities(monkeypatch, clickhouse):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", "authorities")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("CBIOPORTAL_MCP_GOOGLE_BASE_URL", "https://mcp.example.org")
    for name in (
        "CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY",
        "CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING",
        "CBIOPORTAL_MCP_AUTH_PROXY_SECRET",
        "CBIOPORTAL_MCP_STUDY_ACL_FILE",
        "CBIOPORTAL_MCP_ALLOWED_STUDIES",
        "CBIOPORTAL_MCP_AUTH_ADMIN_GROUPS",
    ):
        monkeypatch.delenv(name, raising=False)
    _TOKEN.set(None)
    _HEADERS.set({})
    with (
        patch("fastmcp.server.dependencies.get_access_token", lambda: _TOKEN.get()),
        patch(
            "fastmcp.server.dependencies.get_http_headers",
            lambda include_all=False: _HEADERS.get() or {},
        ),
    ):
        yield clickhouse


# --- identity ---------------------------------------------------------------


def test_settings_carry_only_the_verified_lowercased_email(authorities):
    login("Alice@Example.ORG")
    assert clickhouse_study_access_settings() == {USER_SETTING: "alice@example.org"}


def test_configured_setting_name_is_used(authorities, monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING", "SQL_portal_user")
    login("alice@example.org")
    assert clickhouse_study_access_settings() == {"SQL_portal_user": "alice@example.org"}


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"email": None},  # no token at all
        {"email": ""},
        {"email": "alice@example.org", "verified": False},
        {"email": "alice@example.org", "verified": "false"},
        {"email": "not-an-email"},
        {"email": "alice@example.org\nSQL_cbio_user=bob@example.org"},
        {"email": "a@b@c"},
    ],
)
def test_missing_or_unverified_identity_fails_closed(authorities, token_kwargs):
    email = token_kwargs.pop("email")
    login(email, **token_kwargs)
    with pytest.raises(AuthorizationError, match="verified email"):
        clickhouse_study_access_settings()
    with pytest.raises(AuthorizationError):
        server.run_select_query("SELECT 1", query_label="t")
    # Nothing reached ClickHouse: no empty or '*' fallback query was sent.
    assert authorities.calls == []


def test_identity_headers_are_ignored_in_oauth_mode(authorities):
    # A client-set header must not replace (or supply) the token identity.
    _HEADERS.set({"x-user-email": "bob@example.org", "x-user-id": "bob"})
    login("alice@example.org")
    assert verified_caller_identity().user_email == "alice@example.org"
    login(None)
    with pytest.raises(AuthorizationError):
        verified_caller_identity()


def test_tool_arguments_cannot_choose_the_identity(authorities):
    login("alice@example.org")
    server.run_select_query(
        "SELECT * FROM mutation WHERE note = 'SQL_cbio_user is bob'",
        query_label="t",
        enforce_study_access=False,
    )
    ((_query, settings),) = [call for call in authorities.calls if "mutation" in call[0]]
    assert settings == {USER_SETTING: "alice@example.org"}


def test_unrestricted_value_is_never_sent(authorities):
    login("alice@example.org")
    with pytest.raises(AuthorizationError, match="authorities mode"):
        clickhouse_study_access_settings(all_studies=True)
    with pytest.raises(AuthorizationError):
        server.run_select_query("SELECT 1", query_label="t", all_studies=True)
    assert all("*" not in settings.values() for _q, settings in authorities.calls)


def test_proxy_identity_requires_the_proxy_secret(authorities, monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY", "proxy")
    _HEADERS.set({"x-user-email": "bob@example.org"})
    with pytest.raises(StudyAccessConfigError, match="PROXY_SECRET"):
        validate_study_access_startup_config()
    with pytest.raises(AuthorizationError):
        verified_caller_identity()

    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cret")
    with pytest.raises(AuthorizationError, match="trusted-proxy secret"):
        verified_caller_identity()
    _HEADERS.set({"x-user-email": "Bob@Example.org", "x-cbioportal-mcp-proxy-secret": "s3cret"})
    validate_study_access_startup_config()
    assert verified_caller_identity().user_email == "bob@example.org"
    # The proxy path never trusts a token-less request's other headers for studies.
    _HEADERS.set(
        {
            "x-user-email": "bob@example.org",
            "x-cbioportal-mcp-proxy-secret": "s3cret",
            "x-cbioportal-allowed-studies": "study_alpha",
        }
    )
    assert get_current_study_access().studies == frozenset(ENTITLEMENTS["bob@example.org"])


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED": "false"}, "ROW_POLICY_ENABLED"),
        ({"CBIOPORTAL_MCP_GOOGLE_CLIENT_ID": ""}, "OAuth"),
        ({"CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING": "SQL_cbiomcp_allowed_studies"}, "differ"),
    ],
)
def test_unsafe_authorities_startup_config_is_refused(authorities, monkeypatch, env, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(StudyAccessConfigError, match=message):
        validate_study_access_startup_config()


def test_valid_authorities_startup_config(authorities):
    validate_study_access_startup_config()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice@Example.org", "alice@example.org"),
        (" bob@x.org ", "bob@x.org"),
        ("", None),
        (None, None),
        ("no-at-sign", None),
        ("two@@x.org", None),
        ("tab\t@x.org", None),
        ("a" * 330 + "@x.org", None),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


# --- entitlements come from ClickHouse ---------------------------------------


def test_studies_are_whatever_clickhouse_returns_for_the_email(authorities):
    login("alice@example.org")
    access = get_current_study_access()
    assert access.all_studies is False
    assert access.studies == frozenset(ENTITLEMENTS["alice@example.org"])
    ((query, settings),) = authorities.calls
    assert "FROM user_study" in query
    assert settings == {USER_SETTING: "alice@example.org"}


def test_acl_inputs_are_ignored(authorities, monkeypatch, tmp_path):
    acl = tmp_path / "acl.json"
    acl.write_text('{"default": ["study_beta"], "users": {"alice@example.org": "*"}}')
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACL_FILE", str(acl))
    monkeypatch.setenv("CBIOPORTAL_MCP_ALLOWED_STUDIES", "*")
    _HEADERS.set({"x-cbioportal-allowed-studies": "*", "x-forwarded-groups": "admins"})
    login("alice@example.org")
    access = get_current_study_access()
    assert access.studies == frozenset(ENTITLEMENTS["alice@example.org"])


def test_guard_denies_literal_studies_outside_the_entitlement(authorities):
    login("alice@example.org")
    guard_query_study_access(
        "SELECT * FROM mutation_derived WHERE studyId = 'x' "
        "AND cancer_study_identifier = 'study_alpha'"
    )
    with pytest.raises(AuthorizationError, match="study_beta"):
        guard_query_study_access(
            "SELECT * FROM clinical_data_derived WHERE cancer_study_identifier = 'study_beta'"
        )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM cancer_study SETTINGS SQL_cbio_user='other@x'",
        "SELECT * FROM cancer_study SETTINGS sql_CBIO_user = 'other@x'",
        "SET SQL_cbio_user = 'other@x'",
        "SELECT * FROM cancer_study SETTINGS `SQL_cbio_user`='other@x'",
        "SELECT * FROM cancer_study SETTINGS \\x53QL_cbio_user='other@x'",
        "SELECT getSetting('SQL_cbio_user')",
        "SELECT getSetting(concat('SQL_cbio', '_user'))",
    ],
)
def test_identity_setting_overrides_are_rejected(authorities, query):
    login("alice@example.org")
    with pytest.raises(AuthorizationError, match="identity setting"):
        guard_query_study_access(query)


def test_configured_identity_setting_override_is_rejected(authorities, monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING", "SQL_portal_user")
    login("alice@example.org")
    with pytest.raises(AuthorizationError):
        guard_query_study_access("SELECT 1 SETTINGS SQL_portal_user = 'bob@example.org'")


# --- cache isolation ------------------------------------------------------------


def test_no_result_cache_on_the_query_or_tool_path(authorities):
    # beta (#181) removed the ClickHouse query-cache pilot (query_cache.py);
    # nothing on the query path may cache results across requests. The real
    # MCP tool, run for alice, bob, then alice again with the same SQL, must
    # reach ClickHouse every time with that caller's identity and return only
    # that caller's rows. (Row-policy queries also always carry
    # use_query_cache=0, see test_clickhouse_query_settings.)
    import importlib.util

    assert importlib.util.find_spec("cbioportal_mcp.query_cache") is None
    select = server.clickhouse_run_select_query.fn.__wrapped__
    query = "SELECT owner FROM marker"
    results = []
    for email in ("alice@example.org", "bob@example.org", "alice@example.org"):
        login(email)
        results.append(select(query))
    assert [settings for q, settings in authorities.calls if query in q] == [
        {USER_SETTING: "alice@example.org"},
        {USER_SETTING: "bob@example.org"},
        {USER_SETTING: "alice@example.org"},
    ]
    rendered = [repr(r) for r in results]
    assert "alice@example.org" in rendered[0] and "bob@example.org" not in rendered[0]
    assert "bob@example.org" in rendered[1] and "alice@example.org" not in rendered[1]
    assert "alice@example.org" in rendered[2] and "bob@example.org" not in rendered[2]


def test_run_select_query_results_are_per_caller(authorities):
    # The MCP's own query path keeps no result cache: the same SQL run for
    # alice then bob returns each caller's own rows.
    query = "SELECT owner FROM marker"
    login("alice@example.org")
    assert server.run_select_query(query, query_label="t") == [{"owner": "alice@example.org"}]
    login("bob@example.org")
    assert server.run_select_query(query, query_label="t") == [{"owner": "bob@example.org"}]


def _guide_queries(authorities):
    return [q for q, _s in authorities.calls if "user_study" not in q]


def test_dynamic_study_guide_cache_never_serves_another_caller(authorities, monkeypatch):
    server._clear_study_guide_cache()
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    monkeypatch.setattr(server, "_resolve_study_guide_name", lambda _: None)
    seen = {}

    def fake_select(query, *, query_label, **kwargs):
        # Through the real settings path, so rows depend on the caller.
        email = clickhouse_study_access_settings()[USER_SETTING]
        seen.setdefault(email, 0)
        seen[email] += 1
        if query_label == "study_guide.study_info":
            if "study_public" not in ENTITLEMENTS.get(email, []):
                return []
            return [
                {
                    "cancer_study_identifier": "study_public",
                    "name": f"guide built for {email}",
                    "description": "",
                    "type_of_cancer_id": "brca",
                }
            ]
        return []

    monkeypatch.setattr(server, "run_select_query", fake_select)
    get_guide = server.get_study_guide.fn.__wrapped__
    try:
        login("alice@example.org")
        alice_guide = get_guide("study_public")
        assert "guide built for alice@example.org" in alice_guide
        alice_queries = seen["alice@example.org"]
        assert get_guide("study_public") == alice_guide  # alice's own cache hit
        assert seen["alice@example.org"] == alice_queries

        login("bob@example.org")
        bob_guide = get_guide("study_public")
        assert "alice" not in bob_guide
        assert "guide built for bob@example.org" in bob_guide
        assert seen["bob@example.org"] == alice_queries  # bob's queries actually ran

        login(None)  # no identity: no cached guide either
        assert get_guide("study_public").startswith("Error:")
    finally:
        server._clear_study_guide_cache()


def test_list_studies_reads_per_caller_not_the_shared_snapshot(authorities, monkeypatch):
    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(
        server, "_all_studies_query", lambda: pytest.fail("shared '*' snapshot used")
    )
    list_studies = server.list_studies.fn.__wrapped__
    login("alice@example.org")
    alice = [r["cancer_study_identifier"] for r in list_studies()]
    login("bob@example.org")
    bob = [r["cancer_study_identifier"] for r in list_studies()]
    assert sorted(alice) == sorted(ENTITLEMENTS["alice@example.org"])
    assert sorted(bob) == sorted(ENTITLEMENTS["bob@example.org"])
    login(None)
    assert "error" in list_studies()[0]


def test_cache_partition_is_none_in_public_mode(monkeypatch):
    monkeypatch.delenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", raising=False)
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", "authorities")
    assert study_access.study_access_cache_partition() is None


def test_cache_partition_separates_callers(authorities):
    login("alice@example.org")
    alice = study_access.study_access_cache_partition()
    login("bob@example.org")
    bob = study_access.study_access_cache_partition()
    assert alice != bob and "alice@example.org" in alice


# --- OAuth email verification: positive evidence only --------------------------


@pytest.mark.parametrize(
    ("verified", "record", "accepted"),
    [
        (True, "google_user_data", True),  # v2 userinfo, what GoogleTokenVerifier stores
        (True, "google_token_info", True),  # v1 tokeninfo
        ("true", "google_user_data", False),  # Google returns a JSON bool; a string isn't it
        (_OMIT, "google_user_data", False),  # missing
        (None, "google_user_data", False),
        (0, "google_user_data", False),
        (1, "google_user_data", False),
        (False, "google_user_data", False),
        ("garbage", "google_token_info", False),
        ({"nested": True}, "google_user_data", False),
    ],
)
def test_oauth_email_needs_positive_google_verification(authorities, verified, record, accepted):
    login("alice@example.org", verified=verified, record=record)
    if accepted:
        assert verified_caller_identity().user_email == "alice@example.org"
    else:
        with pytest.raises(AuthorizationError, match="verified email"):
            verified_caller_identity()
        assert authorities.calls == []


def test_top_level_email_verified_is_not_trusted(authorities):
    # GoogleTokenVerifier never sets an OIDC-style top-level email_verified;
    # a claim it doesn't produce isn't evidence.
    claims = {"sub": "s", "email": "alice@example.org", "email_verified": True}
    token = SimpleNamespace(claims=claims)
    _TOKEN.set(token)
    with pytest.raises(AuthorizationError, match="verified email"):
        verified_caller_identity()


def test_verification_must_be_for_the_same_email(authorities):
    login("alice@example.org", record_email="mallory@example.org")
    with pytest.raises(AuthorizationError, match="verified email"):
        verified_caller_identity()
    # Case-insensitive match is the same address.
    login("Alice@Example.org", record_email="alice@example.org")
    assert verified_caller_identity().user_email == "alice@example.org"


@pytest.mark.parametrize(
    "token",
    [
        SimpleNamespace(claims=None),
        SimpleNamespace(claims="not-a-dict"),
        SimpleNamespace(),  # no claims attribute
        SimpleNamespace(claims={"email": 42, "google_user_data": {"verified_email": True}}),
    ],
)
def test_malformed_tokens_fail_closed(authorities, token):
    _TOKEN.set(token)
    with pytest.raises(AuthorizationError):
        verified_caller_identity()


def test_token_retrieval_exception_fails_closed(authorities):
    def boom():
        raise RuntimeError("auth context unavailable")

    with patch("fastmcp.server.dependencies.get_access_token", boom):
        with pytest.raises(AuthorizationError, match="verified email"):
            verified_caller_identity()
        with pytest.raises(AuthorizationError):
            clickhouse_study_access_settings()
    assert authorities.calls == []


# --- cached dynamic guides are re-authorized on every hit -----------------------


@pytest.fixture
def cached_guide(authorities, monkeypatch):
    """alice's dynamic guide for study_alpha, already in the cache."""
    server._clear_study_guide_cache()
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    monkeypatch.setattr(server, "_resolve_study_guide_name", lambda _: None)
    builds = []

    def fake_select(query, *, query_label, **kwargs):
        builds.append(query_label)
        if query_label == "study_guide.study_info":
            return [
                {
                    "cancer_study_identifier": "study_alpha",
                    "name": "Alpha guide",
                    "description": "",
                    "type_of_cancer_id": "brca",
                }
            ]
        return []

    monkeypatch.setattr(server, "run_select_query", fake_select)
    monkeypatch.setitem(ENTITLEMENTS, "alice@example.org", ["study_alpha", "study_public"])
    login("alice@example.org")
    get_guide = server.get_study_guide.fn.__wrapped__
    assert "Alpha guide" in get_guide("STUDY_ALPHA")
    built = len(builds)
    # A plain hit still works and doesn't rebuild...
    login("alice@example.org")  # new request: no memoized entitlements
    assert "Alpha guide" in get_guide("study_alpha")
    assert len(builds) == built
    yield get_guide, builds, built
    server._clear_study_guide_cache()


def test_cached_guide_denied_after_revocation(cached_guide, authorities, monkeypatch):
    get_guide, builds, built = cached_guide
    monkeypatch.setitem(ENTITLEMENTS, "alice@example.org", ["study_public"])  # revoked
    login("alice@example.org")
    guide = get_guide("study_alpha")
    assert guide.startswith("Error:") and "study_alpha" in guide, guide
    assert "Alpha guide" not in guide
    assert len(builds) == built  # denied on the hit itself
    # The check went to ClickHouse (user_study), not a stale in-process answer.
    assert any("user_study" in q for q, _s in authorities.calls[-2:])


def test_cached_guide_denied_for_a_disabled_user(cached_guide, monkeypatch):
    get_guide, _builds, _built = cached_guide
    # A disabled (or stale) user has no user_study rows at all.
    monkeypatch.delitem(ENTITLEMENTS, "alice@example.org")
    login("alice@example.org")
    assert "Alpha guide" not in get_guide("study_alpha")
    assert get_guide("study_alpha").startswith("Error:")


def test_cached_guide_denied_while_the_surface_check_fails(cached_guide, monkeypatch):
    get_guide, _builds, _built = cached_guide
    problem = "unsafe objects in the database: leak (SQL SECURITY NONE)"
    monkeypatch.setattr(cqs, "current_surface_problem", lambda: problem)
    monkeypatch.setattr(server, "current_surface_problem", lambda: problem)
    login("alice@example.org")
    guide = get_guide("study_alpha")
    assert guide.startswith("Error:") and "safety check" in guide, guide
    assert "Alpha guide" not in guide


def test_cached_guide_denied_without_identity(cached_guide):
    get_guide, _builds, _built = cached_guide
    login(None)
    assert get_guide("study_alpha").startswith("Error:")


def test_acl_mode_cached_guide_rechecks_gate_and_allowlist(clickhouse, monkeypatch):
    """acl source: the hit is re-authorized against the current allowlist and gate."""
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE", "acl")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_PROXY_SECRET", "s3cret")
    headers = {
        "x-user-id": "alice",
        "x-cbioportal-allowed-studies": "study_alpha",
        "x-cbioportal-mcp-proxy-secret": "s3cret",
    }
    server._clear_study_guide_cache()
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    monkeypatch.setattr(server, "_resolve_study_guide_name", lambda _: None)
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query, *, query_label, **kw: [
            {"cancer_study_identifier": "study_alpha", "name": "Alpha guide",
             "description": "", "type_of_cancer_id": "brca"}
        ]
        if query_label == "study_guide.study_info"
        else [],
    )  # fmt: skip
    get_guide = server.get_study_guide.fn.__wrapped__
    with patch("fastmcp.server.dependencies.get_http_headers", lambda include_all=False: headers):
        try:
            assert "Alpha guide" in get_guide("study_alpha")
            monkeypatch.setattr(server, "current_surface_problem", lambda: "grant added")
            assert get_guide("study_alpha").startswith("Error: Study-data queries are disabled")
            monkeypatch.setattr(server, "current_surface_problem", lambda: None)
            assert "Alpha guide" in get_guide("study_alpha")
            # A different allowlist is a different partition: never this entry.
            headers["x-cbioportal-allowed-studies"] = "study_beta"
            assert "Alpha guide" not in get_guide("study_alpha")
        finally:
            server._clear_study_guide_cache()
