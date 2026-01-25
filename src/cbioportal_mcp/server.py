#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
import sys
from pathlib import Path
from typing import Optional
from fastmcp import FastMCP


from cbioportal_mcp.env import get_mcp_config, TransportType
from cbioportal_mcp.authentication.permissions import ensure_db_permissions

logger = logging.getLogger(__name__)

# Resource directory for markdown guides
RESOURCES_DIR = Path(__file__).parent.parent.parent / "resources"

def _load_resource(filename: str) -> str:
    """Load a resource guide from the resources directory."""
    resource_path = RESOURCES_DIR / filename
    if not resource_path.exists():
        logger.error(f"Resource file not found: {resource_path}")
        return f"Error: Resource file not found: {filename}"
    return resource_path.read_text()

# Create FastMCP instance
mcp = FastMCP(
    name="cBioPortal MCP Server",
    instructions="""
        You are the cBioPortal MCP Server, built on top of the MCP-ClickHouse project.
        Your role is to provide structured, reliable access to cBioPortal cancer genomics data via the ClickHouse database.

        CRITICAL: ALWAYS READ RELEVANT RESOURCES FIRST
        Before answering any complex query, you MUST:
        1. List available MCP resources
        2. Read the relevant resource guide(s) for your query type
        3. Follow the specific patterns and requirements from the resources

        Resource Reading Requirements:
        - For gene mutation frequencies: MUST read cbioportal://mutation-frequency-guide
        - For clinical data queries: MUST read cbioportal://clinical-data-guide
        - For sample filtering: MUST read cbioportal://sample-filtering-guide
        - For avoiding mistakes: MUST read cbioportal://common-pitfalls
        - When unsure about query patterns: Read multiple relevant resources

        Rules and behavior:
        1. Always respond truthfully and rely on the underlying database resources.
        2. If requested data is unavailable or a query cannot be executed, state that clearly; do not guess or fabricate results.
        3. You have tools to:
            - Execute read-only SELECT queries against the ClickHouse database.
            - Explore the database schema, including available tables and columns.
            - Read MCP resources for detailed query guidance.
        4. Only use the database tools when necessary; do not attempt to modify the database (INSERT, UPDATE, DELETE, any DDL SQL statements are forbidden).
        5. When building queries for the user:
            - FIRST: Read relevant MCP resources for query patterns
            - Explore the database tables using the `clickhouse_list_tables` tool.
            - For each table of interest, use the `clickhouse_list_table_columns(table)` tool to inspect available columns and their comments.
            - Consult with the comments associated with tables and columns to determine which should be used in the query.
            - Use only tables and columns that exist in the schema.
            - Ensure queries are syntactically correct.
            - Follow the specific patterns from the MCP resources.
        6. Return results in a structured format (JSON).
        7. If a user asks something outside the database, respond clearly that it cannot be answered via this MCP.

        REMEMBER: Resource consultation is MANDATORY for complex genomic queries. Always read the relevant guides first.

        Maintain a helpful, concise, and professional tone.
    """,
)


def main():
    """Main entry point for the server."""
    logging.basicConfig(level=logging.INFO)
    logger.info("🚀 Starting cBioPortal MCP Server with FastMCP...")

    # Get config
    config = get_mcp_config()

    try:
        ensure_db_permissions(config=config)
    except PermissionError as e:
        logger.critical("❌ ClickHouse permission check failed: %s", e)
        sys.exit(2)

    transport = config.mcp_server_transport

    try:
        # For HTTP and SSE transports, we need to specify host and port
        http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
        if transport in http_transports:
            # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
            # and bind port (defaults to 8000)
            mcp.run(transport=transport, host=config.mcp_bind_host, port=config.mcp_bind_port)
        else:
            # For stdio transport, no host or port is needed
            mcp.run(transport=transport)
    except ValueError as e:
        if "I/O operation on closed file" in str(e):
            # Handle the stdio buffer closed error gracefully
            logger.warning(f"Stdio transport initialization failed: {e}")
            logger.info("This may happen during subprocess cleanup. Server completed successfully.")
        else:
            # Re-raise other ValueError exceptions
            raise
    except Exception as e:
        logger.error(f"Failed to start MCP server: {e}")
        raise

def _mutation_frequency_guide_text() -> str:
    return _load_resource("mutation-frequency-guide.md")

def _clinical_data_guide_text() -> str:
    return _load_resource("clinical-data-guide.md")

def _sample_filtering_guide_text() -> str:
    return _load_resource("sample-filtering-guide.md")

def _common_pitfalls_guide_text() -> str:
    return _load_resource("common-pitfalls.md")

# --- MCP resources (decorator registers them) --------------------------------
@mcp.resource("cbioportal://mutation-frequency-guide")
def mutation_frequency_guide() -> str:
    return _mutation_frequency_guide_text()

@mcp.resource("cbioportal://clinical-data-guide")
def clinical_data_guide() -> str:
    return _clinical_data_guide_text()

@mcp.resource("cbioportal://sample-filtering-guide")
def sample_filtering_guide() -> str:
    return _sample_filtering_guide_text()

@mcp.resource("cbioportal://common-pitfalls")
def common_pitfalls_guide() -> str:
    return _common_pitfalls_guide_text()


@mcp.tool(
    description="""
    Execute a ClickHouse SQL SELECT query.

    For complex analysis patterns, consult these query guides:
    - cbioportal://mutation-frequency-guide - Gene mutation frequency calculations with proper denominators
    - cbioportal://clinical-data-guide - Patient vs sample-level clinical data queries
    - cbioportal://sample-filtering-guide - Study and sample type filtering strategies
    - cbioportal://common-pitfalls - Common query mistakes and how to avoid them

    Returns:
        - On success: an object with a single field "rows" containing an array of result rows.
        - On failure: an object with a single field "error_message" containing a string describing the error.
"""
)
def clickhouse_run_select_query(query: str) -> dict[str, list[dict] | str]:
    try:
        result = run_select_query(query)
        logger.debug(f"clickhouse_run_select_query returns {result}")
        return {"rows": result}
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_run_select_query: {error_message}")
        return {"error_message": error_message}


@mcp.tool(
    description="""
    Retrieve a list of all tables in the current database.

    Returns:
        - On success: an object with a single field "tables" containing an array of objects with the following fields:
            - name: Table name.
            - primary_key: Name of the table primary key column(s), if defined.
            - total_rows: Number of rows in the table.
            - comment: Table description, if available.
        - On failure: an object with a single field "error_message" containing a string describing the error.
"""
)
def clickhouse_list_tables() -> dict[str, list[dict] | str]:
    logger.info(f"clickhouse_list_tables: called")

    try:
        query = "SELECT name, primary_key, total_rows, comment FROM system.tables WHERE database = currentDatabase()"
        result = run_select_query(query)
        logger.debug(f"clickhouse_list_tables result: {result}")
        return {"tables": result}
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_list_tables: {error_message}")
        return {"error_message": error_message}


@mcp.tool(
    description="""
    Retrieve a list of all columns for the table in the current database.

    Returns:
        - On success: an object with a single field "columns" containing an array of objects with the following fields:
            - name: Column name.
            - type: ClickHouse data type of the column.
            - comment: Column description, if available.
        - On failure: an object with a single field "error_message" containing a string describing the error.
"""
)
def clickhouse_list_table_columns(table: str) -> dict[str, list[dict] | str]:
    logger.info(f"clickhouse_list_table_columns: called")

    try:
        if any(char in table for char in ['"', "'", " "]):
            raise ValueError(f"Invalid table name: {table}")
        # FIXME be aware of sql injections! sanitize the table better
        query = f"SELECT name, type, comment FROM system.columns WHERE table='{table}' and database = currentDatabase()"
        result = run_select_query(query)
        logger.debug(f"clickhouse_list_table_columns result: {result}")
        return {"columns": result}
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


# Resource Access Tools for AI Agents
@mcp.tool()
def list_mcp_resources() -> list[dict]:
    """List all available MCP resources with their URIs and descriptions.

    Call this tool first to see what resource guides are available before answering complex queries.
    """
    return [
        {
            "uri": "cbioportal://mutation-frequency-guide",
            "description": "Comprehensive guide for calculating gene mutation frequencies with gene-specific profiling denominators"
        },
        {
            "uri": "cbioportal://clinical-data-guide",
            "description": "Guide for querying clinical data including patient vs sample level considerations"
        },
        {
            "uri": "cbioportal://sample-filtering-guide",
            "description": "Guide for filtering samples and studies in cBioPortal queries"
        },
        {
            "uri": "cbioportal://common-pitfalls",
            "description": "Guide to avoid common mistakes when querying cBioPortal data"
        }
    ]


@mcp.tool()
def read_mcp_resource(uri: str) -> str:
    """Read the content of a specific MCP resource by URI.

    Use this after calling list_mcp_resources() to read the detailed content of resource guides.

    Args:
        uri: The resource URI (e.g., "cbioportal://mutation-frequency-guide")
    """
    # Resource content mapping
    resources = {
        "cbioportal://mutation-frequency-guide": _mutation_frequency_guide_text(),
        "cbioportal://clinical-data-guide": _clinical_data_guide_text(),
        "cbioportal://sample-filtering-guide": _sample_filtering_guide_text(),
        "cbioportal://common-pitfalls": _common_pitfalls_guide_text()
    }

    if uri not in resources:
        available = list(resources.keys())
        return f"Resource not found: {uri}. Available resources: {available}"

    return resources[uri]


if __name__ == "__main__":
    main()
