#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
from typing import Optional
from fastmcp import FastMCP 


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

@mcp.tool(description="""
    Execute a ClickHouse SQL SELECT query.

    Returns:
        - On success: an object with a single field "rows" containing an array of result rows.
        - On failure: an object with a single field "error_message" containing a string describing the error.
""")
def clickhouse_run_select_query(query: str) -> dict[str, list[dict] | str]:
    try:
        result = run_select_query(query)
        logger.debug(f"clickhouse_run_select_query returns {result}")
        return {"rows": result}
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_run_select_query: {error_message}")
        return {"error_message": error_message}

@mcp.tool(description="""
    Retrieve a list of all tables in the current database.

    Returns:
        - On success: an object with a single field "tables" containing an array of objects with the following fields:
            - name: Table name.
            - primary_key: Name of the table primary key column(s), if defined.
            - total_rows: Number of rows in the table.
            - comment: Table description, if available.
        - On failure: an object with a single field "error_message" containing a string describing the error.
""")
def clickhouse_list_tables() -> dict[str, list[dict] | str]:
    logger.info(f"clickhouse_list_tables: called")
    
    try:
        query = "SELECT name, primary_key, total_rows, comment FROM system.tables WHERE database = currentDatabase()"
        result = run_select_query(query)
        logger.debug(f"clickhouse_list_tables result: {result}")
        return { "tables": result }
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_list_tables: {error_message}")
        return {"error_message": error_message}

@mcp.tool(description="""
    Retrieve a list of all columns for the table in the current database.

    Returns:
        - On success: an object with a single field "columns" containing an array of objects with the following fields:
            - name: Column name.
            - type: ClickHouse data type of the column.
            - comment: Column description, if available.
        - On failure: an object with a single field "error_message" containing a string describing the error.
""")
def clickhouse_list_table_columns(table: str) -> dict[str, list[dict] | str]:
    logger.info(f"clickhouse_list_table_columns: called")

    try:
        if any(char in table for char in ['"', "'", " "]):
            raise ValueError(f"Invalid table name: {table}")
        # FIXME be aware of sql injections! sanitize the table better
        query = f"SELECT name, type, comment FROM system.columns WHERE table='{table}' and database = currentDatabase()"
        result = run_select_query(query)
        logger.debug(f"clickhouse_list_table_columns result: {result}")
        return { "columns": result }
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_list_table_columns: {error_message}")
        return {"error_message": error_message}

def run_select_query(query: str) -> list[dict]:
    """
    Execute arbitrary ClickHouse SQL SELECT query.

    Returns:
        list: A list of rows, where each row is a dictionary with column names as keys and corresponding values.
    """
    from mcp_clickhouse.mcp_server import run_select_query
    if not query.strip().upper().startswith("SELECT"):
        raise ValueError(f"Non select queries are forbidden: '{query}'. Skipping the query.")
    logger.debug("run_select_query: delegate the query to run_select_query tool of ClickHouse MCP")
    ch_query_result = run_select_query(query)
    result = zip_select_query_result(ch_query_result)
    return result

def zip_select_query_result(ch_query_result) -> list[dict]:
    """
    Join columns and corresponding row values into dictionaries skipping dictionary entries if value is emtpy or None
    """
    columns = ch_query_result["columns"]
    rows = ch_query_result["rows"]
    result = []
    for row in rows:
        result.append({k: v for k, v in zip(columns, row) if v not in ("", None)})
    return result

if __name__ == "__main__":
    main()