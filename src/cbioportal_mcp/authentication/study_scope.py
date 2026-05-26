"""Study-level authorization model and Keycloak role parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from cbioportal_mcp.env import McpConfig

VALID_STUDY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class StudyScope:
    """Authorized cBioPortal study scope for one request/user."""

    allowed_studies: frozenset[str]
    allow_all: bool = False
    source: str = "unknown"

    def __post_init__(self) -> None:
        normalized = frozenset(_validate_study_id(study_id) for study_id in self.allowed_studies)
        object.__setattr__(self, "allowed_studies", normalized)


def is_valid_study_id(study_id: str) -> bool:
    """Return True when a string matches the server's study-ID character set."""
    return bool(study_id and VALID_STUDY_ID_PATTERN.fullmatch(study_id))


def parse_study_scope_from_claims(
    claims: Mapping[str, Any] | None, config: McpConfig
) -> StudyScope:
    """Parse a request's JWT claims into a study authorization scope.

    Auth-disabled mode preserves current development behavior by granting all
    studies. When auth is enabled, missing or empty roles result in no access
    unless ``CBIOPORTAL_DEFAULT_STUDIES`` is explicitly configured.
    """
    if not config.cbioportal_auth_enabled:
        return StudyScope(
            allowed_studies=frozenset(),
            allow_all=True,
            source="auth_disabled",
        )

    default_studies = _parse_default_studies(config.cbioportal_default_studies)

    if not claims:
        return _default_or_empty_scope(
            default_studies, source="default_studies" if default_studies else "no_claims"
        )

    roles = _extract_keycloak_roles(claims, config.cbioportal_keycloak_client_id)
    if config.cbioportal_all_studies_role in roles:
        return StudyScope(
            allowed_studies=frozenset(),
            allow_all=True,
            source="keycloak_role",
        )

    allowed_studies = frozenset(role for role in roles if is_valid_study_id(role))
    if allowed_studies:
        return StudyScope(
            allowed_studies=allowed_studies,
            allow_all=False,
            source="keycloak_role",
        )

    return _default_or_empty_scope(
        default_studies, source="default_studies" if default_studies else "keycloak_role"
    )


def _validate_study_id(study_id: str) -> str:
    if not is_valid_study_id(study_id):
        raise ValueError(
            f"Invalid study_id {study_id!r}. "
            "Study IDs may only contain alphanumeric characters, underscores, and hyphens."
        )
    return study_id


def _extract_keycloak_roles(claims: Mapping[str, Any], client_id: str) -> frozenset[str]:
    resource_access = claims.get("resource_access")
    if not isinstance(resource_access, Mapping):
        return frozenset()

    client_access = resource_access.get(client_id)
    if not isinstance(client_access, Mapping):
        return frozenset()

    roles = client_access.get("roles")
    if not isinstance(roles, Iterable) or isinstance(roles, (str, bytes)):
        return frozenset()

    return frozenset(role for role in roles if isinstance(role, str) and role)


def _parse_default_studies(raw_default_studies: str) -> frozenset[str]:
    studies = frozenset(study.strip() for study in raw_default_studies.split(",") if study.strip())
    for study_id in studies:
        _validate_study_id(study_id)
    return studies


def _default_or_empty_scope(default_studies: frozenset[str], source: str) -> StudyScope:
    return StudyScope(
        allowed_studies=default_studies,
        allow_all=False,
        source=source,
    )
