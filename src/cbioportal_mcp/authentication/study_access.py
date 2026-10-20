"""End-user study authorization for cBioPortal MCP.

This module assumes authentication is performed by a trusted reverse proxy
(for example oauth2-proxy or an ingress auth service backed by Keycloak) that
injects identity, group, and optional study-allowlist headers. The MCP server
then enforces study-level authorization consistently across study tools and
the arbitrary ClickHouse SELECT tool.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from cbioportal_mcp.authentication.row_policy_catalog import (
    DENIED_TABLES,
    STUDY_SCOPED_TABLES,
    STUDY_SCOPED_VIEWS,
)
from cbioportal_mcp.authentication.row_policy_catalog import (
    STUDY_AGNOSTIC_REFERENCE_TABLES as _CATALOG_REFERENCE_TABLES,
)
from cbioportal_mcp.env import McpConfig, StudyAccessMode, get_mcp_config

logger = logging.getLogger(__name__)

VALID_STUDY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
# Matches '...' literals in normalized SQL (see _normalize_sql), where quotes
# are always doubled rather than backslash-escaped.
SQL_STRING_PATTERN = re.compile(r"'((?:''|[^'])*)'")

# Identity header names used by common auth proxies (oauth2-proxy, Keycloak
# gatekeepers, ...). The MCP only *reads* the ones configured in
# McpConfig.auth_*_headers; this full list exists so a proxy can strip every
# one of them from inbound requests (see authz_header_names).
IDENTITY_HEADER_CANDIDATES = (
    "x-user-id",
    "x-auth-request-user",
    "x-forwarded-user",
    "x-keycloak-sub",
    "x-keycloak-user",
)
EMAIL_HEADER_CANDIDATES = (
    "x-user-email",
    "x-auth-request-email",
    "x-forwarded-email",
    "x-keycloak-email",
)
GROUP_HEADER_CANDIDATES = (
    "x-user-groups",
    "x-auth-request-groups",
    "x-forwarded-groups",
    "x-keycloak-groups",
)

# Names whose presence makes a query "study-scoped" for the app-level guard.
# Matched as whole words (``\bname\b``), so every table and view is listed by
# its full name - a prefix such as "gene_mutation_frequency" would not match
# "gene_mutation_frequency_in_study". The explicit tuple keeps the original
# hand-written entries; the catalog adds every study-scoped table, view and denied table that
# sql/study-authz/0-row-policies.sql knows about (structural_variant,
# sample_cna_event, clinical_event_derived, ...).
_LEGACY_PROTECTED_QUERY_MARKERS = (
    "clinical_data",
    "gene_mutation_frequency",
    "gene_panel",
    "gene_panel_list",
    "top_mutated_genes",
)
PROTECTED_QUERY_MARKERS = tuple(
    sorted(
        set(_LEGACY_PROTECTED_QUERY_MARKERS)
        | set(STUDY_SCOPED_TABLES)
        | STUDY_SCOPED_VIEWS
        | DENIED_TABLES
    )
)

# Tables known to carry no study-scoped data - reference/lookup data that is
# the same across every study (genes, OncoTree, etc). Defined in
# row_policy_catalog; re-exported here because tests and scripts import it
# from this module. Anything in neither list is unclassified; see
# tests/test_row_policy_sql.py, which fails CI on drift.
STUDY_AGNOSTIC_REFERENCE_TABLES = _CATALOG_REFERENCE_TABLES


class AuthorizationError(PermissionError):
    """Raised when the current request is not authorized for a study/query."""


class StudyAccessConfigError(ValueError):
    """Raised when the current study-access configuration is unsafe to start with."""


@dataclass(frozen=True)
class RequestIdentity:
    """Authenticated request identity derived from trusted proxy headers."""

    user_id: str | None
    user_email: str | None
    groups: frozenset[str]
    client: str

    @property
    def present(self) -> bool:
        return bool(self.user_id or self.user_email)

    @property
    def keys(self) -> set[str]:
        return {value for value in (self.user_id, self.user_email) if value}


@dataclass(frozen=True)
class StudyAccess:
    """Resolved study access for the current request."""

    identity: RequestIdentity
    all_studies: bool
    studies: frozenset[str]

    def allows(self, study_id: str) -> bool:
        return self.all_studies or study_id in self.studies


def _decode_header_value(value: str) -> str:
    if value.startswith("b64:"):
        return base64.b64decode(value[4:]).decode("utf-8", errors="replace")
    return value


def _normalise_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _first_header(headers: dict[str, str], candidates: Iterable[str]) -> str | None:
    for name in candidates:
        value = headers.get(name)
        if value:
            return _decode_header_value(value)
    return None


def parse_list_value(value: str | None) -> list[str]:
    """Parse CSV, whitespace, or JSON-array header/env values."""
    if not value:
        return []
    value = _decode_header_value(value.strip())
    if not value:
        return []

    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    return [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]


def validate_study_ids(study_ids: Iterable[str]) -> frozenset[str]:
    """Validate study IDs before using them in SQL or auth comparisons."""
    valid: set[str] = set()
    for study_id in study_ids:
        if study_id == "*":
            valid.add(study_id)
            continue
        if not VALID_STUDY_ID_PATTERN.fullmatch(study_id):
            raise AuthorizationError(
                f"Invalid study identifier in authorization configuration: {study_id!r}"
            )
        valid.add(study_id)
    return frozenset(valid)


def authz_header_names(config: McpConfig | None = None) -> frozenset[str]:
    """Every inbound header that study authorization could consume.

    A trusted proxy must drop all of these from client requests before
    injecting its verified values; otherwise a client could add, say, an
    extra groups header and have it trusted.
    """
    config = config or get_mcp_config()
    return frozenset(
        {
            *IDENTITY_HEADER_CANDIDATES,
            *EMAIL_HEADER_CANDIDATES,
            *GROUP_HEADER_CANDIDATES,
            *config.auth_user_id_headers,
            *config.auth_email_headers,
            *config.auth_groups_headers,
            config.allowed_studies_header,
            config.auth_proxy_secret_header,
        }
    )


def extract_request_identity(config: McpConfig | None = None) -> RequestIdentity:
    """Read identity headers from the active FastMCP HTTP request.

    Only the configured header names are read (McpConfig.auth_*_headers), so
    a header the trusted proxy doesn't set - and therefore may not strip -
    can't contribute identity or groups.
    """
    config = config or get_mcp_config()
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = _normalise_headers(get_http_headers(include_all=True))
    except Exception as exc:
        logger.debug("Could not read HTTP headers for authz: %s", exc)
        headers = {}

    user_id = _first_header(headers, config.auth_user_id_headers)
    user_email = _first_header(headers, config.auth_email_headers)
    groups: set[str] = set()
    for name in config.auth_groups_headers:
        groups.update(parse_list_value(headers.get(name)))

    client = "authenticated-proxy" if user_id or user_email or groups else "direct"
    return RequestIdentity(
        user_id=user_id or None,
        user_email=user_email or None,
        groups=frozenset(groups),
        client=client,
    )


def _verify_proxy_secret(config: McpConfig) -> None:
    expected = config.auth_proxy_secret
    if not expected:
        return

    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = _normalise_headers(get_http_headers(include_all=True))
    except Exception:
        headers = {}

    actual = headers.get(config.auth_proxy_secret_header) or ""
    if not hmac.compare_digest(actual, expected):
        raise AuthorizationError("Request did not include the expected trusted-proxy secret.")


@lru_cache(maxsize=8)
def _load_acl_file(path: str) -> dict[str, Any]:
    acl_path = Path(path)
    with acl_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise AuthorizationError(f"Study ACL file must contain a JSON object: {path}")
    return data


def _studies_from_acl(config: McpConfig, identity: RequestIdentity) -> set[str]:
    if not config.study_acl_file:
        return set()

    acl = _load_acl_file(config.study_acl_file)
    studies: set[str] = set()

    def values_from_acl(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return parse_list_value(value)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise AuthorizationError("Study ACL entries must be strings or arrays of strings.")

    studies.update(values_from_acl(acl.get("default")))

    users = acl.get("users", {})
    if isinstance(users, dict):
        for key in identity.keys:
            studies.update(values_from_acl(users.get(key)))

    groups = acl.get("groups", {})
    if isinstance(groups, dict):
        for group in identity.groups:
            studies.update(values_from_acl(groups.get(group)))

    return studies


def get_current_study_access(config: McpConfig | None = None) -> StudyAccess:
    """Resolve study access for the current request.

    Public mode returns unrestricted access. Restricted mode unions allowed
    studies from global config, the configured allowed-studies header, user ACL
    entries, group ACL entries, and default ACL entries.
    """
    config = config or get_mcp_config()
    identity = extract_request_identity(config)

    if config.study_access_mode == StudyAccessMode.PUBLIC.value:
        return StudyAccess(identity=identity, all_studies=True, studies=frozenset())

    _verify_proxy_secret(config)

    if config.auth_required and not identity.present:
        raise AuthorizationError("Authentication is required for this cBioPortal MCP deployment.")

    allowed: set[str] = set(parse_list_value(config.global_allowed_studies))

    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = _normalise_headers(get_http_headers(include_all=True))
    except Exception:
        headers = {}
    allowed.update(parse_list_value(headers.get(config.allowed_studies_header)))

    admin_groups = set(parse_list_value(config.auth_admin_groups))
    if admin_groups.intersection(identity.groups):
        allowed.add("*")

    allowed.update(_studies_from_acl(config, identity))
    validated = validate_study_ids(allowed)
    if "*" in validated:
        return StudyAccess(identity=identity, all_studies=True, studies=frozenset())
    return StudyAccess(identity=identity, all_studies=False, studies=frozenset(validated))


def validate_study_access_startup_config(config: McpConfig | None = None) -> None:
    """Fail closed on unsafe restricted-mode config; warn on the weaker (heuristic-only) mode.

    Restricted mode trusts identity/allowlist headers (``x-user-id``,
    ``x-forwarded-groups``, ``x-cbioportal-allowed-studies``) on the sole
    assumption that they were set by a trusted reverse proxy, not forged by
    whoever can reach this process. ``CBIOPORTAL_MCP_AUTH_PROXY_SECRET`` is
    the only thing that lets the server verify that assumption instead of
    just trusting the network topology; without it, anyone who can reach the
    server directly (a misconfigured network policy, same-cluster traffic, a
    debug port-forward) can set those headers themselves and skip
    authentication entirely. Refuse to start rather than silently accept
    that.
    """
    config = config or get_mcp_config()
    if config.study_access_mode != StudyAccessMode.RESTRICTED.value:
        return

    if not config.auth_proxy_secret:
        raise StudyAccessConfigError(
            "CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted requires "
            "CBIOPORTAL_MCP_AUTH_PROXY_SECRET to be set. Without it, this server has no way "
            "to verify that x-user-id / x-forwarded-groups / x-cbioportal-allowed-studies "
            "headers actually came from your trusted auth proxy rather than a direct caller "
            "who can reach this server on the network - restricted mode would be enforcing "
            "access decisions against headers anyone could set themselves. Configure a shared "
            "secret known only to the proxy (see local_keycloak_proxy.py for the reference "
            "implementation), or run in public mode instead."
        )

    if not config.clickhouse_row_policy_enabled:
        logger.warning(
            "CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted is running without "
            "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true. Study access is enforced only "
            "by the app-level SQL guard, a heuristic pattern check on arbitrary SELECT queries "
            "rather than a real SQL parser - not by ClickHouse row policies. This is the "
            "documented, weaker mode; enable row-policy enforcement for real defense-in-depth "
            "isolation, especially before exposing the arbitrary-SQL tool."
        )


def quote_sql_string(value: str) -> str:
    """Quote a validated string literal for ClickHouse SQL."""
    return "'" + value.replace("'", "''") + "'"


def _clickhouse_study_setting_name(config: McpConfig) -> str:
    setting_name = config.clickhouse_allowed_studies_setting
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", setting_name):
        raise AuthorizationError(
            "Invalid ClickHouse study allowlist setting name: " + repr(setting_name)
        )
    return setting_name


def clickhouse_study_access_settings(
    config: McpConfig | None = None, *, all_studies: bool = False
) -> dict[str, str]:
    """Return ClickHouse query settings that carry the current study ACL.

    ClickHouse custom settings are strings, so the allowlist is encoded as a
    comma-separated list of already-validated study IDs. Row policies should
    treat ``*`` as unrestricted and an empty string as no study access.

    ``all_studies=True`` skips request resolution and sends ``*``. It is only
    for server-internal queries whose results are filtered per request in
    Python afterwards (e.g. the shared list_studies snapshot).
    """
    config = config or get_mcp_config()
    if not config.clickhouse_row_policy_enabled:
        return {}

    setting_name = _clickhouse_study_setting_name(config)
    if all_studies:
        return {setting_name: "*"}
    access = get_current_study_access(config)
    value = "*" if access.all_studies else ",".join(sorted(access.studies))
    return {setting_name: value}


def study_access_condition(alias: str | None = None, config: McpConfig | None = None) -> str:
    """Return a SQL condition limiting rows to currently visible studies."""
    access = get_current_study_access(config)
    if access.all_studies:
        return "1"
    if not access.studies:
        return "0"
    column = "cancer_study_identifier"
    if alias:
        column = f"{alias}.{column}"
    values = ", ".join(quote_sql_string(study_id) for study_id in sorted(access.studies))
    return f"{column} IN ({values})"


def authorize_study(study_id: str, config: McpConfig | None = None) -> None:
    """Raise when the current request cannot access ``study_id``."""
    validate_study_ids([study_id])
    access = get_current_study_access(config)
    if not access.allows(study_id):
        raise AuthorizationError(f"Not authorized to access study '{study_id}'.")


# Backslash escapes ClickHouse decodes inside '...' strings and "..." / `...`
# quoted identifiers (\xHH is handled separately).
_SQL_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "r": "\r",
    "n": "\n",
    "t": "\t",
    "0": "\0",
    "a": "\a",
    "v": "\v",
    "e": "\x1b",
}
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEREDOC_DELIMITER = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
# Every internal setting the MCP sends to ClickHouse uses this prefix.
INTERNAL_SETTING_PREFIX = "sql_cbiomcp_"
_SETTING_FUNCTION_CALL = re.compile(r"\bgetsetting(?:ordefault)?\s*\(", re.IGNORECASE)


@dataclass(frozen=True)
class _SqlToken:
    kind: str  # "code", "string", "identifier" or "comment"
    text: str  # raw source text
    value: str = ""  # decoded content, for strings and quoted identifiers


def _read_quoted(query: str, start: int) -> tuple[str, int]:
    """Decode the quoted token opening at ``start``; return (value, end index)."""
    quote = query[start]
    i = start + 1
    out: list[str] = []
    while i < len(query):
        c = query[i]
        if c == "\\":
            if i + 1 >= len(query):
                break
            nxt = query[i + 1]
            hex_digits = query[i + 2 : i + 4]
            if nxt in "xX" and re.fullmatch(r"[0-9A-Fa-f]{2}", hex_digits):
                out.append(chr(int(hex_digits, 16)))
                i += 4
                continue
            out.append(_SQL_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if c == quote:
            if query[i + 1 : i + 2] == quote:  # doubled quote
                out.append(quote)
                i += 2
                continue
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise AuthorizationError("Query has an unterminated quoted string or identifier.")


def _lex_sql(query: str) -> list[_SqlToken]:
    """Split a query into code, string literals, quoted identifiers and comments.

    Follows ClickHouse's lexer (verified on 24.12 and 25.8): '...' strings and "..." /
    `...` identifiers with backslash escapes and doubled quotes, $tag$...$tag$
    heredoc strings, `--` and `# ` / `#!` line comments, and nested
    `/* ... */` block comments. Unterminated strings or comments are rejected
    rather than guessed at.
    """
    tokens: list[_SqlToken] = []
    code_start = 0
    i = 0
    n = len(query)

    def flush(end: int) -> None:
        if end > code_start:
            tokens.append(_SqlToken("code", query[code_start:end]))

    while i < n:
        c = query[i]
        if c in "'\"`":
            flush(i)
            value, end = _read_quoted(query, i)
            kind = "string" if c == "'" else "identifier"
            tokens.append(_SqlToken(kind, query[i:end], value))
            i = code_start = end
        elif query.startswith("--", i) or (
            c == "#" and (i + 1 == n or query[i + 1].isspace() or query[i + 1] == "!")
        ):
            flush(i)
            newline = query.find("\n", i)
            end = n if newline == -1 else newline
            tokens.append(_SqlToken("comment", query[i:end]))
            i = code_start = end
        elif query.startswith("/*", i):
            flush(i)
            depth, j = 1, i + 2
            while j < n and depth:
                if query.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif query.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                raise AuthorizationError("Query has an unterminated /* comment.")
            tokens.append(_SqlToken("comment", query[i:j]))
            i = code_start = j
        elif c == "$" and (match := _HEREDOC_DELIMITER.match(query, i)):
            delimiter = match.group(0)
            close = query.find(delimiter, match.end())
            if close == -1:
                raise AuthorizationError("Query has an unterminated heredoc string.")
            flush(i)
            end = close + len(delimiter)
            tokens.append(_SqlToken("string", query[i:end], query[match.end() : close]))
            i = code_start = end
        else:
            i += 1
    flush(n)
    return tokens


def _normalize_sql(query: str) -> str:
    r"""Canonical text for the guard's pattern checks.

    Comments become spaces, string literals are re-quoted as '...' with ''
    doubling, and quoted identifiers are replaced by their decoded name (so
    `mut\x61tion` is checked as mutation) - or by a placeholder when the
    decoded name isn't a plain identifier, so an alias such as
    `cancer_study_identifier = 'x'` can't pose as a study filter.
    """
    parts: list[str] = []
    for token in _lex_sql(query):
        if token.kind == "comment":
            parts.append(" ")
        elif token.kind == "string":
            parts.append("'" + token.value.replace("'", "''") + "'")
        elif token.kind == "identifier":
            if _PLAIN_IDENTIFIER.fullmatch(token.value):
                parts.append(token.value)
            else:
                parts.append(" __quoted_identifier__ ")
        else:
            parts.append(token.text)
    return "".join(parts)


def _strip_sql_literals(query: str) -> str:
    """Blank out '...' literals in already-normalized SQL."""
    return SQL_STRING_PATTERN.sub("''", query)


def _extract_literals(segment: str) -> set[str]:
    return {match.group(1).replace("''", "'") for match in SQL_STRING_PATTERN.finditer(segment)}


def _unescape_everywhere(query: str) -> str:
    """Apply ClickHouse's backslash escapes across the whole text, token-agnostic."""
    query = re.sub(r"\\[xX]([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), query)
    return re.sub(r"\\(.)", lambda m: _SQL_ESCAPES.get(m.group(1), m.group(1)), query)


def extract_study_filters(query: str) -> tuple[set[str], bool]:
    """Extract literal study filters from a SELECT query.

    Returns ``(study_ids, ambiguous)``. Ambiguous means the query appears to
    reference a study-scoped selector but does not use a literal equality/list
    form that can be checked safely.
    """
    stripped = _normalize_sql(query)
    study_ids: set[str] = set()

    equality_patterns = (
        r"\bcancer_study_identifier\s*=\s*'((?:''|[^'])*)'",
        r"\bstudy\s*=\s*'((?:''|[^'])*)'",
    )
    for pattern in equality_patterns:
        study_ids.update(
            match.group(1).replace("''", "'")
            for match in re.finditer(pattern, stripped, flags=re.IGNORECASE)
        )

    list_patterns = (
        r"\bcancer_study_identifier\s+IN\s*\(([^)]*)\)",
        r"\bstudies\s*=\s*\[([^\]]*)\]",
    )
    for pattern in list_patterns:
        for match in re.finditer(pattern, stripped, flags=re.IGNORECASE):
            segment = match.group(1)
            literals = _extract_literals(segment)
            if literals:
                study_ids.update(literals)

    lower = stripped.lower()
    has_study_selector = (
        "cancer_study_identifier" in lower
        or re.search(r"\bstudy\s*=", lower) is not None
        or re.search(r"\bstudies\s*=", lower) is not None
    )
    ambiguous = has_study_selector and not study_ids
    return study_ids, ambiguous


def references_protected_study_data(query: str) -> bool:
    """Conservatively identify queries that may read study-scoped data."""
    lower = _normalize_sql(query).lower()
    return any(re.search(rf"\b{re.escape(marker)}\b", lower) for marker in PROTECTED_QUERY_MARKERS)


def has_restricted_sql_bypass_shape(query: str) -> bool:
    """Detect query constructs this lightweight guard cannot prove safe."""
    normalized = _strip_sql_literals(_normalize_sql(query)).lower()
    blocked_patterns = (
        r"\bor\b",
        r"\bunion\b",
        r"\bwith\b",
        r"\bfrom\s*\(",
        r"\bjoin\s*\(",
    )
    return any(re.search(pattern, normalized) for pattern in blocked_patterns)


def references_internal_setting(query: str, config: McpConfig) -> bool:
    r"""True if the query names an internal SQL_cbiomcp_* setting or reads settings.

    Deliberately blunt: the setting names are checked against the raw text,
    the raw text with every backslash escape decoded, and every decoded string
    literal and quoted identifier - so a SETTINGS clause, a SET or an escaped
    `\x53QL_cbiomcp_...` identifier are caught regardless of comments or
    quoting. A name computed at runtime can't be seen as text, so any
    getSetting()/getSettingOrDefault() call is refused as well. Legitimate SQL
    needs neither. (ClickHouse itself also rejects in-query SETTINGS changes:
    the server sends readonly=1, see clickhouse_query_settings.)
    """
    names = {INTERNAL_SETTING_PREFIX, _clickhouse_study_setting_name(config).lower()}
    candidates = [query, _unescape_everywhere(query)]
    try:
        candidates.extend(token.value for token in _lex_sql(query) if token.value)
        normalized = _normalize_sql(query)
    except AuthorizationError:
        normalized = query  # unterminated input is rejected separately by the guard
    if any(name in candidate.lower() for candidate in candidates for name in names):
        return True
    # getSetting() can build the name at runtime (concat('SQL_', ...)), which
    # no text check can see, so the function itself is refused. It could only
    # read the caller's own allowlist, but legitimate SQL never needs it.
    return _SETTING_FUNCTION_CALL.search(_strip_sql_literals(normalized)) is not None


def guard_query_study_access(query: str, config: McpConfig | None = None) -> None:
    """Authorize an arbitrary SQL query against the current request's study ACL."""
    config = config or get_mcp_config()
    access = get_current_study_access(config)
    if access.all_studies:
        return

    if references_internal_setting(query, config):
        raise AuthorizationError(
            "Queries may not set or reference the internal ClickHouse study allowlist setting."
        )
    _lex_sql(query)  # raises on unterminated strings/comments

    protected_query = references_protected_study_data(query)
    if (
        protected_query
        and not config.clickhouse_row_policy_enabled
        and has_restricted_sql_bypass_shape(query)
    ):
        raise AuthorizationError(
            "Restricted deployments only allow simple study-scoped SELECT queries. "
            "Avoid OR, UNION, WITH, and nested subqueries in arbitrary SQL; use "
            "literal cancer_study_identifier IN (...) filters."
        )

    study_ids, ambiguous = extract_study_filters(query)
    if ambiguous:
        if not config.clickhouse_row_policy_enabled:
            raise AuthorizationError(
                "Restricted deployments require literal cancer_study_identifier filters "
                "or study/studies parameters so study access can be checked."
            )

    if not study_ids:
        if protected_query:
            if not config.clickhouse_row_policy_enabled:
                raise AuthorizationError(
                    "Restricted deployments require every study-scoped query to include "
                    "an explicit literal cancer_study_identifier filter."
                )
        return

    validated = validate_study_ids(study_ids)
    denied = sorted(study_id for study_id in validated if not access.allows(study_id))
    if denied:
        raise AuthorizationError(
            "Not authorized to access requested study ID(s): " + ", ".join(denied)
        )
