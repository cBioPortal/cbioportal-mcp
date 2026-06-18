"""cBioPortal MCP Server - A specialized MCP interface for cBioPortal data analysis."""

# Apply third-party compatibility shims before any submodule (e.g. the query
# layer) imports mcp_clickhouse, which is otherwise incompatible with the pinned
# fastmcp 3.x at import time. See _compat for details.
from cbioportal_mcp._compat import patch_fastmcp_removed_kwargs

patch_fastmcp_removed_kwargs()

__version__ = "0.1.0"
