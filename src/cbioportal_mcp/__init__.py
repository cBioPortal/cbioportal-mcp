"""cBioPortal MCP Server - A specialized MCP interface for cBioPortal data analysis."""

from cbioportal_mcp.query_concurrency import configure_mcp_clickhouse_workers

__version__ = "0.1.0"

# Before anything in the package imports mcp_clickhouse (see query_concurrency).
configure_mcp_clickhouse_workers()
