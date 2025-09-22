#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
from typing import Optional
from fastmcp import FastMCP 

from mcp_clickhouse.mcp_server import run_select_query
from cbioportal_mcp.prompts.cbioportal_prompt import CBIOPORTAL_SYSTEM_PROMPT

from .env import get_mcp_config, TransportType

logger = logging.getLogger(__name__)

# Create FastMCP instance
mcp = FastMCP(
    name="cBioPortal MCP Server",
    instructions=CBIOPORTAL_SYSTEM_PROMPT
)

def main():
    """Main entry point for the server."""
    logging.basicConfig(level=logging.INFO)
    logger.info("🚀 Starting cBioPortal MCP Server with FastMCP...")

    # Get config
    config = get_mcp_config()
    transport = config.mcp_server_transport

    # For HTTP and SSE transports, we need to specify host and port
    http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
    if transport in http_transports:
        # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
        # and bind port (defaults to 8000)
        mcp.run(transport=transport, host=config.mcp_bind_host, port=config.mcp_bind_port)
    else:
        # For stdio transport, no host or port is needed
        mcp.run(transport=transport)

@mcp.tool()
def clickhouse_run_select_query(query: str) -> dict:
    """
    Execute any ClickHouse SQL query.
    
    Args:
        query: The SQL query to execute
    
    Returns:
        Dictionary containing query results
    """
    logger.info(f"clickhouse_run_select_query called with query: {query}")
    
    try:
        if not query.strip().upper().startswith("SELECT"):
            return {
                "success": False,
                "message": "Only SELECT queries are allowed.",
                "data": None
            }
        result = run_select_query(query)
        return {
            "success": True,
            "message": "Query executed successfully",
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 0
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error executing query: {str(e)}",
            "data": None
        }


@mcp.tool()
def clickhouse_list_tables(database: Optional[str] = None) -> dict:
    """
    List tables in a specific database or the current database.
    
    Args:
        database: Optional database name. If not provided, uses the current database.
    
    Returns:
        Dictionary containing list of tables
    """
    logger.info(f"clickhouse_list_tables called with database: {database}")
    
    try:
        if database:
            query = f"SHOW TABLES FROM {database}"
        else:
            query = "SHOW TABLES"
        result = run_select_query(query)
        return {
            "success": True,
            "message": f"Successfully retrieved tables from {'specified database' if database else 'current database'}",
            "data": result,
            "tables": [row[0] for row in result] if isinstance(result, list) else []
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error listing tables: {str(e)}",
            "data": None
        }


if __name__ == "__main__":
    main()