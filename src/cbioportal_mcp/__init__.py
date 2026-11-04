"""cBioPortal MCP Server - A specialized MCP interface for cBioPortal data analysis."""

import os

from clickhouse_connect import common

from cbioportal_mcp.query_concurrency import configure_mcp_clickhouse_workers

__version__ = "0.1.0"

# Before anything in the package imports mcp_clickhouse (see query_concurrency).
MAX_CONCURRENT_QUERIES = configure_mcp_clickhouse_workers()

# Configure custom-setting forwarding before any package consumer creates a client.
if os.getenv("CBIOPORTAL_MCP_STUDY_FILTERING") == "on":
    common.set_setting("invalid_setting_action", "send")
