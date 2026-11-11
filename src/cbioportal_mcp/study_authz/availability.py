"""Restart-scoped, fail-closed admission for study data.

Only startup may complete the checks. All MCP tools default to study data;
resources always contain deployment or study guidance and are gated too.
"""

import logging

from fastmcp.exceptions import PromptError, ResourceError, ToolError
from fastmcp.server.middleware import Middleware

from cbioportal_mcp import rollout
from cbioportal_mcp.redaction import redact

logger = logging.getLogger(__name__)
SAFE_TOOLS = frozenset(
    {"clickhouse_list_tables", "clickhouse_list_table_columns", "search_oncotree"}
)
_reason = "checks_pending"


def reason() -> str:
    if rollout._PIN is None:
        return "checks_pending"
    return _reason if rollout._PIN.filtering == "on" else "filtering_off"


def available() -> bool:
    return reason() in {"ready", "filtering_off"}


def complete(code: str) -> None:
    global _reason
    _reason = code


def require(*, resource: bool = False, prompt: bool = False) -> None:
    """The single admission gate; run outside handlers' exception fallbacks."""
    if available():
        return
    message = f"Study data unavailable: {reason()}"
    logger.warning("%s", redact(message))
    error = PromptError if prompt else ResourceError if resource else ToolError
    raise error(message)


class StudyDataMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        if context.message.name not in SAFE_TOOLS:
            require()
        return await call_next(context)

    async def on_read_resource(self, context, call_next):
        require(resource=True)
        return await call_next(context)

    async def on_get_prompt(self, context, call_next):
        require(prompt=True)
        return await call_next(context)
