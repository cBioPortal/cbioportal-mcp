"""Authorized ClickHouse query execution using per-query settings."""

from __future__ import annotations

import concurrent.futures
import logging
import re

from fastmcp.exceptions import ToolError
from mcp_clickhouse.mcp_server import (
    QUERY_EXECUTOR,
    SELECT_QUERY_TIMEOUT_SECS,
    create_clickhouse_client,
    get_readonly_setting,
)

from cbioportal_mcp.authentication.request_context import get_current_study_scope
from cbioportal_mcp.authentication.study_scope import StudyScope
from cbioportal_mcp.env import McpConfig, get_mcp_config

logger = logging.getLogger(__name__)

CLICKHOUSE_SETTING_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_clickhouse_auth_settings(scope: StudyScope, config: McpConfig) -> dict[str, str]:
    """Build ClickHouse custom settings for row-policy study authorization."""
    allowed_setting = _validate_setting_name(config.cbioportal_clickhouse_allowed_studies_setting)
    allow_all_setting = _validate_setting_name(config.cbioportal_clickhouse_allow_all_setting)

    return {
        allowed_setting: ",".join(sorted(scope.allowed_studies)),
        allow_all_setting: "1" if scope.allow_all else "0",
    }


def execute_authorized_select_query(
    query: str,
    scope: StudyScope | None = None,
    config: McpConfig | None = None,
) -> dict:
    """Execute a ClickHouse SELECT query with request-local auth settings.

    Authorization is passed as native per-query settings. This avoids mutable
    session state and avoids rewriting arbitrary SQL.
    """
    config = config or get_mcp_config()
    scope = scope or get_current_study_scope(config)

    future = QUERY_EXECUTOR.submit(_execute_query_with_scope, query, scope, config)
    try:
        return future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise ToolError(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds") from exc


def _execute_query_with_scope(query: str, scope: StudyScope, config: McpConfig) -> dict:
    client = create_clickhouse_client()
    settings = _build_query_settings(client, scope, config)

    try:
        result = client.query(query, settings=settings)
    except Exception as exc:
        logger.error("Authorized ClickHouse query failed: %s", exc)
        raise ToolError(f"Query execution failed: {str(exc)}") from exc

    logger.info(
        "Authorized ClickHouse query returned %d rows with auth source=%s "
        "allow_all=%s study_count=%d",
        len(result.result_rows),
        scope.source,
        scope.allow_all,
        len(scope.allowed_studies),
    )
    return {"columns": result.column_names, "rows": result.result_rows}


def _build_query_settings(client, scope: StudyScope, config: McpConfig) -> dict[str, str]:
    settings = {"readonly": get_readonly_setting(client)}
    if config.cbioportal_auth_enabled:
        settings.update(build_clickhouse_auth_settings(scope, config))
    return settings


def _validate_setting_name(setting_name: str) -> str:
    if not CLICKHOUSE_SETTING_NAME_PATTERN.fullmatch(setting_name):
        raise ValueError(
            f"Invalid ClickHouse setting name {setting_name!r}. "
            "Setting names may only contain letters, numbers, and underscores, "
            "and may not start with a number."
        )
    return setting_name
