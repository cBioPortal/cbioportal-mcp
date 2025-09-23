#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
from typing import Optional
from fastmcp import FastMCP 

from mcp_clickhouse.mcp_server import run_select_query

from cbioportal_mcp.env import get_mcp_config, TransportType

logger = logging.getLogger(__name__)

# Create FastMCP instance
mcp = FastMCP(
    name="cBioPortal MCP Server",
    instructions="""
        You are the cBioPortal MCP Server, built on top of the MCP-ClickHouse project.
        Your role is to provide structured, reliable access to cBioPortal cancer genomics data via the ClickHouse database.

        Rules and behavior:
        1. Always respond truthfully and rely on the underlying database resources.
        2. If requested data is unavailable or a query cannot be executed, state that clearly; do not guess or fabricate results.
        3. You have tools to:
        - Execute read-only SELECT queries against the ClickHouse database.
        - Explore the database schema, including available tables and columns.
        4. Only use the database tools when necessary; do not attempt to modify the database (INSERT, UPDATE, DELETE, any DDL SQL statements are forbidden).
        5. When building queries for the user:
            - Ensure queries are syntactically correct.
            - Use only tables and columns that exist in the schema (use respective tools for exploration).
            - Consult with the comments associated with tables and columns to determine which should be used in the query.
        6. Return results in a structured format (JSON) including any relevant metadata (row counts, success status, messages).
        7. If a user asks something outside the database, respond clearly that it cannot be answered via this MCP.

        Maintain a helpful, concise, and professional tone.
    """
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
    
    try:
        if not query.strip().upper().startswith("SELECT"):
            logger.warning(f"clickhouse_run_select_query called with non select query: {query}. Skipping the query.")
            return {
                "success": False,
                "message": "Only SELECT queries are allowed.",
                "data": None
            }
        logger.debug("clickhouse_run_select_query: delegate the query to run_select_query tool of ClickHouse MCP")
        result = run_select_query(query)
        result = {
            "success": True,
            "message": "Query executed successfully",
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 0
        }
        logger.debug(f"clickhouse_run_select_query returns {result}")
        return result
    except Exception as e:
        original_error_message = str(e)
        logger.error(f"clickhouse_run_select_query: {original_error_message}")
        return {
            "success": False,
            "message": f"Error executing query: {original_error_message}",
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
        original_error_message = str(e)
        logger.error(f"clickhouse_list_tables: {original_error_message}")
        return {
            "success": False,
            "message": f"Error listing tables: {original_error_message}",
            "data": None
        }


if __name__ == "__main__":
    main()