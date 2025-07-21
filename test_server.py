#!/usr/bin/env python3
"""Simple test script to verify the cBioPortal MCP server structure."""

import asyncio
import logging
import sys
from pathlib import Path

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from cbioportal_mcp.server import cBioPortalMCPServer

async def test_server_initialization():
    """Test that the server can be initialized."""
    
    logging.basicConfig(level=logging.INFO)
    
    try:
        print("Testing cBioPortal MCP Server initialization...")
        
        # Create server instance
        server = cBioPortalMCPServer()
        print("✓ Server initialized successfully")
        
        # Check that the server has the expected attributes
        print("\nTesting server capabilities...")
        print(f"✓ ClickHouse client available: {server.clickhouse_client is not None}")
        print(f"✓ ClickHouse server available: {server.clickhouse_server is not None}")
        
        # Test that imports work
        print("\nTesting backend query imports...")
        from cbioportal_mcp.backend_queries.cancer_studies import CancerStudyQueries
        from cbioportal_mcp.backend_queries.clinical_data import ClinicalDataQueries
        from cbioportal_mcp.backend_queries.genomic_data import GenomicDataQueries
        from cbioportal_mcp.mcp_queries.shortcuts import MCPShortcuts
        from cbioportal_mcp.prompts.cbioportal_prompt import CBIOPORTAL_SYSTEM_PROMPT
        
        print("✓ All backend query modules imported successfully")
        
        # Test SQL generation
        print("\nTesting SQL query generation...")
        cancer_sql = CancerStudyQueries.get_cancer_studies_metadata_summary()
        print(f"✓ Cancer study query generated ({len(cancer_sql)} characters)")
        
        clinical_sql = ClinicalDataQueries.get_clinical_data_by_attribute("CANCER_TYPE")
        print(f"✓ Clinical data query generated ({len(clinical_sql)} characters)")
        
        genomic_sql = GenomicDataQueries.get_mutation_counts("TP53")
        print(f"✓ Genomic data query generated ({len(genomic_sql)} characters)")
        
        shortcut_sql = MCPShortcuts.get_study_overview("test_study")
        print(f"✓ Shortcut query generated ({len(shortcut_sql)} characters)")
        
        # Test system prompt
        print(f"\nSystem prompt loaded: {len(CBIOPORTAL_SYSTEM_PROMPT)} characters")
        
        print("\n🎉 Basic server structure test passed!")
        
        return True
        
    except Exception as e:
        print(f"❌ Server test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = asyncio.run(test_server_initialization())
    sys.exit(0 if success else 1)