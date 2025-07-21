#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
import os
from typing import Optional, List, Dict, Any

from fastmcp import FastMCP

# Import fallback tools from mcp-clickhouse
try:
    import mcp_clickhouse
    from mcp_clickhouse.clickhouse_client import ClickHouseClient
    HAS_MCP_CLICKHOUSE = True
    logging.info("mcp-clickhouse available")
except ImportError:
    HAS_MCP_CLICKHOUSE = False
    logging.warning("mcp-clickhouse not available, fallback tools disabled")

from cbioportal_mcp.backend_queries.cancer_studies import execute_cancer_study_query
from cbioportal_mcp.backend_queries.clinical_data import execute_clinical_data_query
from cbioportal_mcp.backend_queries.genomic_data import execute_genomic_data_query

logger = logging.getLogger(__name__)

# Create FastMCP instance
mcp = FastMCP("cBioPortal MCP Server")

# Initialize ClickHouse client if available
clickhouse_client = None
if HAS_MCP_CLICKHOUSE:
    try:
        clickhouse_client = ClickHouseClient(
            host=os.environ.get('CLICKHOUSE_HOST', 'localhost'),
            port=int(os.environ.get('CLICKHOUSE_PORT', '9000')),
            user=os.environ.get('CLICKHOUSE_USER', 'default'),
            password=os.environ.get('CLICKHOUSE_PASSWORD', ''),
            database=os.environ.get('CLICKHOUSE_DATABASE', 'default'),
            secure=os.environ.get('CLICKHOUSE_SECURE', 'false').lower() == 'true'
        )
        logger.info("ClickHouse client initialized successfully")
    except Exception as e:
        logger.warning(f"Could not initialize ClickHouse client: {e}")
        HAS_MCP_CLICKHOUSE = False

# Debug environment variables
logger.info("Environment variables:")
for key in ['CLICKHOUSE_HOST', 'CLICKHOUSE_PORT', 'CLICKHOUSE_USER', 'CLICKHOUSE_PASSWORD', 'CLICKHOUSE_DATABASE']:
    value = os.environ.get(key, 'NOT_SET')
    # Don't log password in full
    if key == 'CLICKHOUSE_PASSWORD' and value != 'NOT_SET':
        value = '***SET***'
    logger.info(f"  {key}: {value}")


@mcp.tool()
def get_cancer_studies(keyword: str = "") -> str:
    """
    List available cancer studies with metadata.
    
    Args:
        keyword: Optional keyword to filter studies by name or identifier
    
    Returns:
        String containing formatted list of cancer studies
    """
    logger.info(f"get_cancer_studies called with keyword: '{keyword}'")
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return mock data for testing
        logger.info("Using fallback mode for get_cancer_studies")
        return """Cancer Studies (Test Data - ClickHouse not connected):

• TCGA Lung Adenocarcinoma (luad_tcga)
  Type: Lung Adenocarcinoma
  Description: TCGA lung adenocarcinoma data

• MSK-IMPACT Clinical Sequencing Cohort (msk_impact_2017)
  Type: Mixed Cancer Types
  Description: Clinical sequencing of cancer patients

• TCGA Breast Invasive Carcinoma (brca_tcga)
  Type: Breast Invasive Carcinoma
  Description: TCGA breast cancer data

⚠️  This is test data. To see real data, configure ClickHouse connection.
"""
    
    # TODO: Implement real ClickHouse query when client is working
    try:
        if keyword:
            # Use search functionality
            result = execute_cancer_study_query(
                clickhouse_client,
                "search",
                keyword=keyword
            )
        else:
            # Get summary of all studies
            result = execute_cancer_study_query(
                clickhouse_client,
                "summary"
            )
        
        if result["success"]:
            data = result["data"]
            if isinstance(data, list) and data:
                # Format the response nicely
                studies_text = "Cancer Studies:\n\n"
                for study in data[:20]:  # Limit to first 20 studies
                    studies_text += f"• {study.get('name', 'N/A')} ({study.get('cancerStudyIdentifier', 'N/A')})\n"
                    studies_text += f"  Type: {study.get('type_of_cancer_name', 'N/A')}\n"
                    if study.get('description'):
                        desc = study['description'][:100] + "..." if len(study['description']) > 100 else study['description']
                        studies_text += f"  Description: {desc}\n"
                    studies_text += "\n"
                
                studies_text += f"\nTotal studies found: {len(data)}"
                return studies_text
            else:
                return "No cancer studies found"
        else:
            return f"Error retrieving cancer studies: {result['error']}"
            
    except Exception as e:
        logger.exception("Error in get_cancer_studies")
        return f"Error retrieving cancer studies: {str(e)}"


@mcp.tool()
def get_clinical_data_counts(
    sample_attribute_ids: Optional[List[str]] = None,
    patient_attribute_ids: Optional[List[str]] = None
) -> str:
    """
    Get clinical attribute value distributions.
    
    Args:
        sample_attribute_ids: List of sample-level clinical attribute IDs
        patient_attribute_ids: List of patient-level clinical attribute IDs
    
    Returns:
        String containing formatted clinical data counts
    """
    logger.info(f"get_clinical_data_counts called with sample_attrs: {sample_attribute_ids}, patient_attrs: {patient_attribute_ids}")
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return mock data for testing
        logger.info("Using fallback mode for get_clinical_data_counts")
        return """Clinical Data Counts (Test Data):

CANCER_TYPE:
  Lung Adenocarcinoma: 230
  Breast Cancer: 180
  Glioblastoma: 150
  NA: 25

SEX:
  Male: 315
  Female: 270

AGE:
  <50: 120
  50-70: 340
  >70: 125

⚠️  This is test data. To see real data, configure ClickHouse connection.
"""
    
    # TODO: Implement real ClickHouse query when client is working
    try:
        result = execute_clinical_data_query(
            clickhouse_client,
            "clinical_data_counts",
            sample_attribute_ids=sample_attribute_ids or [],
            patient_attribute_ids=patient_attribute_ids or []
        )
        
        if result["success"]:
            data = result["data"]
            if isinstance(data, list) and data:
                # Format the response nicely
                counts_text = "Clinical Data Counts:\n\n"
                current_attr = None
                
                for item in data:
                    attr_id = item.get('attributeId', 'N/A')
                    value = item.get('value', 'N/A') 
                    count = item.get('count', 0)
                    
                    if attr_id != current_attr:
                        counts_text += f"\n{attr_id}:\n"
                        current_attr = attr_id
                    
                    counts_text += f"  {value}: {count}\n"
                
                return counts_text
            else:
                return "No clinical data counts found"
        else:
            return f"Error retrieving clinical data counts: {result['error']}"
            
    except Exception as e:
        logger.exception("Error in get_clinical_data_counts")
        return f"Error retrieving clinical data counts: {str(e)}"


@mcp.tool()
def get_mutation_counts(hugo_gene_symbol: str) -> str:
    """
    Get mutation statistics for a specific gene.
    
    Args:
        hugo_gene_symbol: Gene symbol (e.g., TP53, EGFR)
    
    Returns:
        String containing formatted mutation statistics
    """
    logger.info(f"get_mutation_counts called for gene: {hugo_gene_symbol}")
    
    if not hugo_gene_symbol:
        return "Error: hugo_gene_symbol parameter is required"
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return mock data for testing
        logger.info(f"Using fallback mode for get_mutation_counts: {hugo_gene_symbol}")
        return f"""Mutation Counts for {hugo_gene_symbol} (Test Data):

• Samples with mutations: 45
• Samples without mutations: 155
• Total profiled samples: 200
• Mutation rate: 22.5%

⚠️  This is test data. To see real data, configure ClickHouse connection.
"""
    
    # TODO: Implement real ClickHouse query when client is working
    try:
        result = execute_genomic_data_query(
            clickhouse_client,
            "mutation_counts",
            hugo_gene_symbol=hugo_gene_symbol
        )
        
        if result["success"]:
            data = result["data"]
            if isinstance(data, list) and data:
                mutation_data = data[0]  # Should be single row result
                mutated = mutation_data.get('mutatedCount', 0)
                not_mutated = mutation_data.get('notMutatedCount', 0)
                profiled = mutation_data.get('profiledCount', 0)
                
                counts_text = f"Mutation Counts for {hugo_gene_symbol}:\n\n"
                counts_text += f"• Samples with mutations: {mutated}\n"
                counts_text += f"• Samples without mutations: {not_mutated}\n"
                counts_text += f"• Total profiled samples: {profiled}\n"
                
                if profiled > 0:
                    mutation_rate = (mutated / profiled) * 100
                    counts_text += f"• Mutation rate: {mutation_rate:.1f}%\n"
                
                return counts_text
            else:
                return f"No mutation data found for gene {hugo_gene_symbol}"
        else:
            return f"Error retrieving mutation counts: {result['error']}"
            
    except Exception as e:
        logger.exception("Error in get_mutation_counts")
        return f"Error retrieving mutation counts: {str(e)}"


@mcp.tool()
def get_gene_mutations(
    hugo_gene_symbol: str,
    study_ids: Optional[List[str]] = None
) -> str:
    """
    Get detailed mutation information for a gene.
    
    Args:
        hugo_gene_symbol: Gene symbol to query
        study_ids: Optional study identifiers to filter by
    
    Returns:
        String containing detailed gene mutations
    """
    logger.info(f"get_gene_mutations called for gene: {hugo_gene_symbol}, studies: {study_ids}")
    
    if not hugo_gene_symbol:
        return "Error: hugo_gene_symbol parameter is required"
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return mock data for testing
        logger.info(f"Using fallback mode for get_gene_mutations: {hugo_gene_symbol}")
        return f"""Mutations in {hugo_gene_symbol} (Test Data):

Study: luad_tcga
Mutations found: 15
  • Missense_Mutation (p.R273H)
  • Nonsense_Mutation (p.Q331*)
  • Frame_Shift_Del (p.P152fs)
  • Splice_Site (c.672+1G>T)
  • Missense_Mutation (p.G245S)

Study: brca_tcga
Mutations found: 8
  • Missense_Mutation (p.R175H)
  • Nonsense_Mutation (p.R213*)
  • Frame_Shift_Ins (p.L194fs)
  ... and 5 more mutations

Total mutations across all studies: 23

⚠️  This is test data. To see real data, configure ClickHouse connection.
"""
    
    # TODO: Implement real ClickHouse query when client is working
    try:
        result = execute_genomic_data_query(
            clickhouse_client,
            "gene_mutations",
            hugo_gene_symbol=hugo_gene_symbol,
            study_ids=study_ids
        )
        
        if result["success"]:
            data = result["data"]
            if isinstance(data, list) and data:
                mutations_text = f"Mutations in {hugo_gene_symbol}:\n\n"
                
                # Group by study
                studies = {}
                for mutation in data:
                    study_id = mutation.get('studyId', 'Unknown')
                    if study_id not in studies:
                        studies[study_id] = []
                    studies[study_id].append(mutation)
                
                for study_id, mutations in studies.items():
                    mutations_text += f"Study: {study_id}\n"
                    mutations_text += f"Mutations found: {len(mutations)}\n"
                    
                    # Show first few mutations with details
                    for i, mut in enumerate(mutations[:5]):
                        mutation_type = mut.get('mutationType', 'N/A')
                        protein_change = mut.get('proteinChange', 'N/A')
                        mutations_text += f"  • {mutation_type}"
                        if protein_change and protein_change != 'N/A':
                            mutations_text += f" ({protein_change})"
                        mutations_text += "\n"
                    
                    if len(mutations) > 5:
                        mutations_text += f"  ... and {len(mutations) - 5} more mutations\n"
                    mutations_text += "\n"
                
                mutations_text += f"Total mutations across all studies: {len(data)}"
                return mutations_text
            else:
                return f"No mutations found for gene {hugo_gene_symbol}"
        else:
            return f"Error retrieving gene mutations: {result['error']}"
            
    except Exception as e:
        logger.exception("Error in get_gene_mutations")
        return f"Error retrieving gene mutations: {str(e)}"


def main():
    """Main entry point for the server."""
    logging.basicConfig(level=logging.INFO)
    logger.info("🚀 Starting cBioPortal MCP Server with FastMCP...")
    
    # Run the FastMCP server
    mcp.run()


@mcp.tool()
async def clickhouse_run_select_query(query: str) -> Dict[str, Any]:
    """
    Execute any ClickHouse SQL query.
    
    Args:
        query: The SQL query to execute
    
    Returns:
        Dictionary containing query results
    """
    logger.info(f"clickhouse_run_select_query called with query: {query}")
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return error message
        logger.warning("ClickHouse not available for clickhouse_run_select_query")
        return {
            "success": False,
            "message": "ClickHouse client not available. Please check your configuration.",
            "data": None
        }
    
    try:
        # Check if query is a SELECT query (basic protection)
        if not query.strip().upper().startswith("SELECT"):
            return {
                "success": False,
                "message": "Only SELECT queries are allowed.",
                "data": None
            }
        
        # Execute the query
        result = await clickhouse_client.execute_query(query)
        
        return {
            "success": True,
            "message": "Query executed successfully",
            "data": result,
            "row_count": len(result) if isinstance(result, list) else 0
        }
        
    except Exception as e:
        logger.exception(f"Error in clickhouse_run_select_query: {str(e)}")
        return {
            "success": False,
            "message": f"Error executing query: {str(e)}",
            "data": None
        }


@mcp.tool()
async def clickhouse_list_databases() -> Dict[str, Any]:
    """
    List available databases in ClickHouse.
    
    Returns:
        Dictionary containing list of databases
    """
    logger.info("clickhouse_list_databases called")
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return error message
        logger.warning("ClickHouse not available for clickhouse_list_databases")
        return {
            "success": False,
            "message": "ClickHouse client not available. Please check your configuration.",
            "data": None
        }
    
    try:
        # Simple query to list databases
        result = await clickhouse_client.execute_query("SHOW DATABASES")
        
        return {
            "success": True,
            "message": "Successfully retrieved databases",
            "data": result,
            "databases": [row[0] for row in result] if isinstance(result, list) else []
        }
        
    except Exception as e:
        logger.exception(f"Error in clickhouse_list_databases: {str(e)}")
        return {
            "success": False,
            "message": f"Error listing databases: {str(e)}",
            "data": None
        }


@mcp.tool()
async def clickhouse_list_tables(database: Optional[str] = None) -> Dict[str, Any]:
    """
    List tables in a specific database or the current database.
    
    Args:
        database: Optional database name. If not provided, uses the current database.
    
    Returns:
        Dictionary containing list of tables
    """
    logger.info(f"clickhouse_list_tables called with database: {database}")
    
    if not HAS_MCP_CLICKHOUSE or not clickhouse_client:
        # Fallback mode - return error message
        logger.warning("ClickHouse not available for clickhouse_list_tables")
        return {
            "success": False,
            "message": "ClickHouse client not available. Please check your configuration.",
            "data": None
        }
    
    try:
        # Build query based on whether database was provided
        if database:
            query = f"SHOW TABLES FROM {database}"
        else:
            query = "SHOW TABLES"
        
        # Execute query
        result = await clickhouse_client.execute_query(query)
        
        return {
            "success": True,
            "message": f"Successfully retrieved tables from {'specified database' if database else 'current database'}",
            "data": result,
            "tables": [row[0] for row in result] if isinstance(result, list) else []
        }
        
    except Exception as e:
        logger.exception(f"Error in clickhouse_list_tables: {str(e)}")
        return {
            "success": False,
            "message": f"Error listing tables: {str(e)}",
            "data": None
        }


if __name__ == "__main__":
    main()