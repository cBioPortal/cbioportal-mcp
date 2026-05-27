"""Authentication and authorization helpers for cBioPortal MCP."""

from cbioportal_mcp.authentication.request_context import (
    StudyAuthError,
    StudyAuthMiddleware,
    get_current_study_scope,
    set_current_study_scope,
    study_scope_from_authorization_header,
)
from cbioportal_mcp.authentication.study_scope import (
    StudyScope,
    is_valid_study_id,
    parse_study_scope_from_claims,
)

__all__ = [
    "StudyAuthError",
    "StudyAuthMiddleware",
    "StudyScope",
    "get_current_study_scope",
    "is_valid_study_id",
    "parse_study_scope_from_claims",
    "set_current_study_scope",
    "study_scope_from_authorization_header",
]
