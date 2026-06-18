"""Compatibility shims for third-party libraries.

``mcp_clickhouse`` (pinned ``==0.1.11``) constructs ``FastMCP(dependencies=[...])``
at import time. ``fastmcp`` 3.x (pinned ``==3.3.1`` for MCP Apps / ``ui://``
support) removed the ``dependencies`` kwarg and raises ``TypeError`` for it, so
merely importing ``mcp_clickhouse`` — which this server does for its query
helpers — fails. No published ``mcp_clickhouse`` release supports fastmcp 3.x
(0.4.0 still requires ``fastmcp<3``), so we patch ``FastMCP`` to drop the kwargs
fastmcp 3.x removed, before ``mcp_clickhouse`` is imported.

This is safe for our use: we only call ``mcp_clickhouse``'s plain query functions
(``execute_query`` / ``run_select_query``), which talk to ClickHouse directly and
never touch the ``FastMCP`` instance whose construction we are fixing up. Our own
``FastMCP(...)`` call does not pass these kwargs, so the patch is a no-op for it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Kwargs accepted by older FastMCP (<= 2.x) that 3.x removed; mcp_clickhouse
# 0.1.11 still passes ``dependencies`` (used only by the old ``fastmcp install``).
_REMOVED_FASTMCP_KWARGS = ("dependencies",)

_patched = False


def patch_fastmcp_removed_kwargs() -> None:
    """Make ``FastMCP(...)`` tolerate kwargs removed in fastmcp 3.x. Idempotent."""
    global _patched
    if _patched:
        return
    try:
        import fastmcp
    except Exception:  # pragma: no cover - fastmcp is a hard dependency
        return

    original_init = fastmcp.FastMCP.__init__

    def _init(self, *args, **kwargs):
        for dead in _REMOVED_FASTMCP_KWARGS:
            if kwargs.pop(dead, None) is not None:
                logger.debug("Dropped FastMCP kwarg %r removed in fastmcp 3.x", dead)
        return original_init(self, *args, **kwargs)

    fastmcp.FastMCP.__init__ = _init
    _patched = True
