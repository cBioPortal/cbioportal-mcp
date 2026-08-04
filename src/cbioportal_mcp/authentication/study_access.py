"""End-user study authorization for cBioPortal MCP.

This module assumes authentication is performed by a trusted reverse proxy
(for example oauth2-proxy or an ingress auth service backed by Keycloak) that
injects identity, group, and optional study-allowlist headers. The MCP server
then enforces study-level authorization consistently across study tools and
the arbitrary ClickHouse SELECT tool.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from cbioportal_mcp.env import McpConfig, StudyAccessMode, get_mcp_config

logger = logging.getLogger(__name__)

VALID_STUDY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
SQL_STRING_PATTERN = re.compile(r"'((?:''|[^'])*)'")

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

PROTECTED_QUERY_MARKERS = (
    "cancer_study",
    "cancer_study_query_preferences",
    "clinical_data",
    "clinical_data_derived",
    "clinical_event",
    "genetic_alteration",
    "genetic_alteration_derived",
    "genetic_profile",
    "genomic_event_derived",
    "gene_mutation_frequency",
    "top_mutated_genes",
    "sample",
    "sample_to_gene_panel_derived",
    "patient",
    "mutation",
    "resource_sample",
    "resource_patient",
    "resource_study",
)


class AuthorizationError(PermissionError):
    """Raised when the current request is not authorized for a study/query."""


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


def extract_request_identity() -> RequestIdentity:
    """Read identity headers from the active FastMCP HTTP request."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = _normalise_headers(get_http_headers(include_all=True))
    except Exception as exc:
        logger.debug("Could not read HTTP headers for authz: %s", exc)
        headers = {}

    user_id = _first_header(headers, IDENTITY_HEADER_CANDIDATES)
    user_email = _first_header(headers, EMAIL_HEADER_CANDIDATES)
    groups: set[str] = set()
    for name in GROUP_HEADER_CANDIDATES:
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

    actual = headers.get(config.auth_proxy_secret_header)
    if actual != expected:
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
    identity = extract_request_identity()

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


def clickhouse_study_access_settings(config: McpConfig | None = None) -> dict[str, str]:
    """Return ClickHouse query settings that carry the current study ACL.

    ClickHouse custom settings are strings, so the allowlist is encoded as a
    comma-separated list of already-validated study IDs. Row policies should
    treat ``*`` as unrestricted and an empty string as no study access.
    """
    config = config or get_mcp_config()
    if not config.clickhouse_row_policy_enabled:
        return {}

    access = get_current_study_access(config)
    setting_name = _clickhouse_study_setting_name(config)
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


def _strip_sql_comments(query: str) -> str:
    query = re.sub(r"/\*.*?\*/", " ", query, flags=re.DOTALL)
    return re.sub(r"--[^\n\r]*", " ", query)


def _strip_sql_literals(query: str) -> str:
    return SQL_STRING_PATTERN.sub("''", query)


def _extract_literals(segment: str) -> set[str]:
    return {match.group(1).replace("''", "'") for match in SQL_STRING_PATTERN.finditer(segment)}


def extract_study_filters(query: str) -> tuple[set[str], bool]:
    """Extract literal study filters from a SELECT query.

    Returns ``(study_ids, ambiguous)``. Ambiguous means the query appears to
    reference a study-scoped selector but does not use a literal equality/list
    form that can be checked safely.
    """
    stripped = _strip_sql_comments(query)
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
    lower = _strip_sql_comments(query).lower()
    return any(re.search(rf"\b{re.escape(marker)}\b", lower) for marker in PROTECTED_QUERY_MARKERS)


def has_restricted_sql_bypass_shape(query: str) -> bool:
    """Detect query constructs this lightweight guard cannot prove safe."""
    normalized = _strip_sql_literals(_strip_sql_comments(query)).lower()
    blocked_patterns = (
        r"\bor\b",
        r"\bunion\b",
        r"\bwith\b",
        r"\bfrom\s*\(",
        r"\bjoin\s*\(",
    )
    return any(re.search(pattern, normalized) for pattern in blocked_patterns)


def _references_clickhouse_study_setting(query: str, config: McpConfig) -> bool:
    setting_name = _clickhouse_study_setting_name(config)
    normalized = _strip_sql_literals(_strip_sql_comments(query))
    return re.search(rf"\b{re.escape(setting_name)}\b", normalized, flags=re.IGNORECASE) is not None


def guard_query_study_access(query: str, config: McpConfig | None = None) -> None:
    """Authorize an arbitrary SQL query against the current request's study ACL."""
    config = config or get_mcp_config()
    access = get_current_study_access(config)
    if access.all_studies:
        return

    if config.clickhouse_row_policy_enabled and _references_clickhouse_study_setting(query, config):
        raise AuthorizationError(
            "Queries may not set or reference the internal ClickHouse study allowlist setting."
        )

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
