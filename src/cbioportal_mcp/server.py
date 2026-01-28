#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
import re
import sys
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Optional
from fastmcp import FastMCP


from cbioportal_mcp.env import get_mcp_config, TransportType
from cbioportal_mcp.authentication.permissions import ensure_db_permissions

logger = logging.getLogger(__name__)

# Regex pattern for valid cBioPortal study identifiers
# Allows alphanumeric characters, underscores, and hyphens
VALID_STUDY_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')

def _validate_study_id(study_id: str) -> str:
    """Validate and sanitize a study ID to prevent SQL injection.
    
    Args:
        study_id: The study identifier to validate
        
    Returns:
        The validated study_id if valid
        
    Raises:
        ValueError: If study_id contains invalid characters
    """
    if not study_id:
        raise ValueError("study_id cannot be empty")
    if not VALID_STUDY_ID_PATTERN.match(study_id):
        raise ValueError(
            f"Invalid study_id '{study_id}'. "
            "Study IDs may only contain alphanumeric characters, underscores, and hyphens."
        )
    return study_id

def _sanitize_search_term(search: str) -> str:
    """Sanitize a search term by escaping SQL special characters.
    
    Args:
        search: The search term to sanitize
        
    Returns:
        The sanitized search term safe for use in LIKE clauses
    """
    if not search:
        return ""
    # Escape single quotes by doubling them (SQL standard)
    # Also escape % and _ which are LIKE wildcards
    sanitized = search.replace("'", "''")
    sanitized = sanitized.replace("%", "\\%")
    sanitized = sanitized.replace("_", "\\_")
    return sanitized

# Resource loading using importlib.resources for proper package support
def _get_resources_path() -> Path:
    """Get the resources directory path, supporting both installed packages and dev mode."""
    try:
        # Python 3.9+ approach using importlib.resources.files
        return importlib_resources.files("cbioportal_mcp") / "resources"
    except (TypeError, AttributeError):
        # Fallback for older Python or if package isn't installed
        return Path(__file__).parent / "resources"

def _load_resource(filename: str) -> str:
    """Load a resource guide from the resources directory."""
    try:
        resources_path = _get_resources_path()
        resource_file = resources_path / filename
        # Use read_text() which works for both Traversable and Path
        return resource_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error(f"Resource file not found: {filename}")
        return f"Error: Resource file not found: {filename}"
    except Exception as e:
        logger.error(f"Error loading resource {filename}: {e}")
        return f"Error: Could not load resource: {filename}"

def _load_study_guide(study_id: str) -> str | None:
    """Load a study guide from the study-guides directory if it exists."""
    try:
        resources_path = _get_resources_path()
        study_file = resources_path / "study-guides" / f"{study_id}.md"
        return study_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Error loading study guide for {study_id}: {e}")
        return None

def _list_available_study_guides() -> list[str]:
    """List all available pre-generated study guides."""
    try:
        resources_path = _get_resources_path()
        study_guides_path = resources_path / "study-guides"
        # For Traversable (importlib.resources), iterate contents
        # For Path, use glob
        if hasattr(study_guides_path, 'iterdir'):
            # It's a Path-like object
            return [f.stem for f in study_guides_path.iterdir() 
                    if f.name.endswith('.md') and not f.name.startswith('_')]
        else:
            # It's a Traversable from importlib.resources
            return [f.name.removesuffix('.md') for f in study_guides_path.iterdir() 
                    if f.name.endswith('.md') and not f.name.startswith('_')]
    except Exception as e:
        logger.error(f"Error listing study guides: {e}")
        return []

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

def _treatment_guide_text() -> str:
    return _load_resource("treatment-guide.md")

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

@mcp.resource("cbioportal://treatment-guide")
def treatment_guide() -> str:
    return _treatment_guide_text()


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
    
    Note: CTEs (WITH ... AS) are supported. Query validation is handled at the
    database level via read-only user permissions (see authentication/permissions.py).

    Returns:
        list: A list of rows, where each row is a dictionary with column names as keys and corresponding values.
    """
    from mcp_clickhouse.mcp_server import run_select_query

    # DB-level read-only permissions (enforced on startup) prevent non-SELECT queries,
    # so we don't need application-level query filtering. This allows CTEs (WITH ... AS).
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
    
    Note: For study-specific guides, use the `get_study_guide(study_id)` tool instead.
    Use `list_studies(search)` to find available studies.
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
        },
        {
            "uri": "cbioportal://treatment-guide",
            "description": "Guide for querying treatment/clinical event data including drug agents, timelines, and linking to genomic data"
        },
        {
            "uri": "cbioportal://study-guide/{study_id}",
            "description": "Dynamic study-specific guide - use get_study_guide(study_id) tool to generate"
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
        "cbioportal://common-pitfalls": _common_pitfalls_guide_text(),
        "cbioportal://treatment-guide": _treatment_guide_text()
    }

    if uri not in resources:
        available = list(resources.keys())
        return f"Resource not found: {uri}. Available resources: {available}"

    return resources[uri]


@mcp.tool()
def get_study_guide(study_id: str) -> str:
    """Get a guide for a specific cBioPortal study.
    
    First checks for a pre-generated guide in resources/study-guides/{study_id}.md.
    If not found, dynamically generates one by querying the database.
    
    Pre-generated guides may include curated notes and tips specific to each study.
    
    Args:
        study_id: The cancer study identifier (e.g., "msk_chord_2024", "brca_tcga_pan_can_atlas_2018")
    
    Returns:
        A markdown-formatted guide specific to the requested study
    """
    # Validate study_id to prevent SQL injection
    try:
        study_id = _validate_study_id(study_id)
    except ValueError as e:
        return f"Error: {str(e)}"
    
    # First, check for a pre-generated guide file
    static_guide = _load_study_guide(study_id)
    if static_guide:
        logger.info(f"Loaded static study guide for {study_id}")
        return static_guide
    
    # Fall back to dynamic generation
    logger.info(f"Generating dynamic study guide for {study_id}")
    try:
        guide_sections = []
        
        # 1. Basic study info
        study_info = run_select_query(f"""
            SELECT 
                cancer_study_identifier,
                name,
                description,
                type_of_cancer_id
            FROM cancer_study 
            WHERE cancer_study_identifier = '{study_id}'
        """)
        
        if not study_info:
            return f"Study '{study_id}' not found. Use clickhouse_list_tables or query cancer_study table to find valid study identifiers."
        
        info = study_info[0]
        guide_sections.append(f"""# Study Guide: {info.get('name', study_id)}

**Study ID:** `{study_id}`
**Cancer Type:** {info.get('type_of_cancer_id', 'N/A')}
**Description:** {info.get('description', 'N/A')}
""")
        
        # 2. Patient and sample counts
        counts = run_select_query(f"""
            SELECT 
                COUNT(DISTINCT patient_unique_id) as patient_count,
                COUNT(DISTINCT sample_unique_id) as sample_count
            FROM clinical_data_derived 
            WHERE cancer_study_identifier = '{study_id}'
        """)
        if counts:
            c = counts[0]
            guide_sections.append(f"""## Cohort Statistics
- **Patients:** {c.get('patient_count', 'N/A'):,}
- **Samples:** {c.get('sample_count', 'N/A'):,}
""")
        
        # 3. Available data types
        profiles = run_select_query(f"""
            SELECT DISTINCT 
                gp.genetic_alteration_type,
                gp.datatype,
                gp.name
            FROM genetic_profile gp
            JOIN cancer_study cs ON gp.cancer_study_id = cs.cancer_study_id
            WHERE cs.cancer_study_identifier = '{study_id}'
        """)
        if profiles:
            guide_sections.append("## Available Data Types\n")
            for p in profiles:
                guide_sections.append(f"- **{p.get('genetic_alteration_type', 'Unknown')}**: {p.get('name', 'N/A')}")
            guide_sections.append("")
        
        # 4. Gene panels used
        panels = run_select_query(f"""
            SELECT DISTINCT gene_panel_id, COUNT(DISTINCT sample_unique_id) as sample_count
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier = '{study_id}'
            GROUP BY gene_panel_id
            ORDER BY sample_count DESC
            LIMIT 10
        """)
        if panels:
            guide_sections.append("## Gene Panels\n")
            for p in panels:
                panel_id = p.get('gene_panel_id', 'Unknown')
                count = p.get('sample_count', 0)
                if panel_id == 'WES':
                    guide_sections.append(f"- **{panel_id}** (Whole Exome): {count:,} samples — all genes profiled")
                else:
                    guide_sections.append(f"- **{panel_id}**: {count:,} samples")
            guide_sections.append("")
        
        # 5. Clinical attributes available
        attrs = run_select_query(f"""
            SELECT DISTINCT attribute_name, COUNT(DISTINCT sample_unique_id) as coverage
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
            GROUP BY attribute_name
            ORDER BY coverage DESC
            LIMIT 20
        """)
        if attrs:
            guide_sections.append("## Available Clinical Attributes\n")
            guide_sections.append("| Attribute | Samples with Data |")
            guide_sections.append("|-----------|------------------|")
            for a in attrs:
                guide_sections.append(f"| {a.get('attribute_name', 'Unknown')} | {a.get('coverage', 0):,} |")
            guide_sections.append("")
        
        # 6. Top mutated genes (if mutation data exists)
        top_genes = run_select_query(f"""
            SELECT 
                hugo_gene_symbol,
                COUNT(DISTINCT sample_unique_id) as altered_samples
            FROM genomic_event_derived
            WHERE cancer_study_identifier = '{study_id}'
                AND variant_type = 'mutation'
                AND mutation_status != 'UNCALLED'
            GROUP BY hugo_gene_symbol
            ORDER BY altered_samples DESC
            LIMIT 10
        """)
        if top_genes:
            guide_sections.append("## Top Mutated Genes\n")
            guide_sections.append("| Gene | Altered Samples |")
            guide_sections.append("|------|----------------|")
            for g in top_genes:
                guide_sections.append(f"| {g.get('hugo_gene_symbol', 'Unknown')} | {g.get('altered_samples', 0):,} |")
            guide_sections.append("")
        
        # 7. Sample type distribution
        sample_types = run_select_query(f"""
            SELECT attribute_value as sample_type, COUNT(DISTINCT sample_unique_id) as count
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
                AND attribute_name = 'SAMPLE_TYPE'
            GROUP BY attribute_value
            ORDER BY count DESC
        """)
        if sample_types:
            guide_sections.append("## Sample Types\n")
            for st in sample_types:
                guide_sections.append(f"- **{st.get('sample_type', 'Unknown')}**: {st.get('count', 0):,} samples")
            guide_sections.append("")
        
        # 8. Query tips for this study
        guide_sections.append(f"""## Query Tips for {study_id}

```sql
-- Get all samples in this study
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = '{study_id}';

-- Get mutations for a specific gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = '{study_id}'
    AND hugo_gene_symbol = 'TP53'
    AND variant_type = 'mutation';

-- Get clinical data for specific attributes
SELECT sample_unique_id, attribute_name, attribute_value
FROM clinical_data_derived
WHERE cancer_study_identifier = '{study_id}'
    AND attribute_name IN ('CANCER_TYPE', 'SAMPLE_TYPE', 'OS_MONTHS');
```
""")
        
        return "\n".join(guide_sections)
        
    except Exception as e:
        logger.error(f"get_study_guide error: {e}")
        return f"Error generating study guide for '{study_id}': {str(e)}"


# Maximum allowed limit for list queries to prevent expensive unbounded queries
MAX_LIST_LIMIT = 100

@mcp.tool()
def list_studies(search: str = None, limit: int = 20) -> list[dict]:
    """List available cBioPortal studies.
    
    Studies with pre-generated guides (in resources/study-guides/) will have has_guide=True.
    
    Args:
        search: Optional search term to filter studies by name or identifier
        limit: Maximum number of studies to return (default 20, max 100)
    
    Returns:
        List of studies with their identifiers, names, sample counts, and guide availability
    """
    available_guides = set(_list_available_study_guides())
    
    # Clamp limit to safe bounds
    safe_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    
    try:
        if search:
            # Sanitize search term to prevent SQL injection
            safe_search = _sanitize_search_term(search)
            query = f"""
                SELECT 
                    cs.cancer_study_identifier,
                    cs.name,
                    cs.type_of_cancer_id,
                    COUNT(DISTINCT cd.sample_unique_id) as sample_count
                FROM cancer_study cs
                LEFT JOIN clinical_data_derived cd ON cs.cancer_study_identifier = cd.cancer_study_identifier
                WHERE cs.cancer_study_identifier LIKE '%{safe_search}%' 
                    OR cs.name LIKE '%{safe_search}%'
                    OR cs.type_of_cancer_id LIKE '%{safe_search}%'
                GROUP BY cs.cancer_study_identifier, cs.name, cs.type_of_cancer_id
                ORDER BY sample_count DESC
                LIMIT {safe_limit}
            """
        else:
            query = f"""
                SELECT 
                    cs.cancer_study_identifier,
                    cs.name,
                    cs.type_of_cancer_id,
                    COUNT(DISTINCT cd.sample_unique_id) as sample_count
                FROM cancer_study cs
                LEFT JOIN clinical_data_derived cd ON cs.cancer_study_identifier = cd.cancer_study_identifier
                GROUP BY cs.cancer_study_identifier, cs.name, cs.type_of_cancer_id
                ORDER BY sample_count DESC
                LIMIT {safe_limit}
            """
        
        results = run_select_query(query)
        
        # Add has_guide field
        for study in results:
            study_id = study.get('cancer_study_identifier', '')
            study['has_guide'] = study_id in available_guides
        
        return results
        
    except Exception as e:
        logger.error(f"list_studies error: {e}")
        return [{"error": str(e)}]


@mcp.tool()
def list_study_guides() -> list[str]:
    """List all studies that have pre-generated guides available.
    
    Returns:
        List of study identifiers that have curated guides in resources/study-guides/
    """
    return _list_available_study_guides()


if __name__ == "__main__":
    main()
