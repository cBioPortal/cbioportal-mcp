# cBioPortal MCP Server

> **WARNING ⚠️: This is still under construction**

A specialized Model Context Protocol (MCP) interface for cBioPortal cancer genomics data analysis. This server provides both cBioPortal-specific tools for common queries and fallback support for raw ClickHouse SQL queries.

## Features

- **cBioPortal-Specific Tools**: Pre-built functions for common cancer genomics queries
- **Fallback Support**: Raw SQL access via mcp-clickhouse integration
- **Rich System Prompts**: Guidance on cBioPortal schema and best practices
- **Hybrid Architecture**: Combines optimized queries with flexible SQL fallback

## Installation

```bash
# Navigate to the project directory
cd cbioportal-mcp

# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
# venv\Scripts\activate

# Upgrade pip
pip install --upgrade pip

# Install the package in development mode
pip install -e .

# Or install with development dependencies
pip install -e ".[dev]"
```

## Configuration

Set the same environment variables used by mcp-clickhouse:

```bash
export CLICKHOUSE_HOST=your-clickhouse-host
export CLICKHOUSE_PORT=9000
export CLICKHOUSE_USER=your-username
export CLICKHOUSE_PASSWORD=your-password
export CLICKHOUSE_DATABASE=your-cbioportal-database  # e.g., cgds_public_2025_06_24
export CLICKHOUSE_SECURE=false  # or true for secure connections
```

## Available Tools

### Study & Metadata Tools
- `get_cancer_studies` - List available cancer studies with metadata
- `get_molecular_profiles` - List molecular data profiles for studies
- `get_clinical_attributes` - Available clinical attributes and descriptions

### Clinical Data Tools
- `get_clinical_data_counts` - Clinical attribute value distributions
- `get_sample_clinical_data` - Sample-level clinical data
- `get_patient_clinical_data` - Patient-level clinical data

### Genomic Analysis Tools
- `get_mutation_counts` - Mutation statistics for specific genes
- `get_cna_counts` - Copy number alteration frequencies
- `get_mutated_genes` - Gene-level mutation statistics
- `get_gene_mutations` - Simplified gene mutation lookup

### Sample & Patient Tools
- `get_filtered_samples` - Sample filtering with complex criteria
- `get_sample_count` - Count of samples matching filters

### Fallback Tools (from mcp-clickhouse)
- `clickhouse_run_select_query` - Execute any ClickHouse SQL query
- `clickhouse_list_databases` - Explore database structure
- `clickhouse_list_tables` - List tables in database

## Usage Examples

### Basic Usage
```python
# The server can be used with any MCP client
# Example using Claude Desktop or other MCP-compatible clients

# List available studies
get_cancer_studies()

# Get mutation data for TP53
get_mutation_counts({"hugo_gene_symbol": "TP53"})

# Get clinical data distributions
get_clinical_data_counts({
    "sample_attribute_ids": ["CANCER_TYPE", "SAMPLE_TYPE"],
    "patient_attribute_ids": ["AGE", "SEX"]
})
```

### Advanced SQL Queries
```python
# When specialized tools don't fit, use raw SQL
clickhouse_run_select_query({
    "query": """
    SELECT 
        cancer_study_identifier,
        COUNT(DISTINCT sample_unique_id) as sample_count
    FROM sample_derived 
    GROUP BY cancer_study_identifier 
    ORDER BY sample_count DESC
    """
})
```

## Architecture

```
cbioportal-mcp/
├── src/cbioportal_mcp/
│   ├── server.py              # Main MCP server
│   ├── backend_queries/       # Queries ported from cBioPortal backend
│   │   ├── cancer_studies.py  # Study metadata queries
│   │   ├── clinical_data.py   # Clinical data queries
│   │   └── genomic_data.py    # Genomic alteration queries
│   ├── mcp_queries/           # MCP convenience queries
│   │   └── shortcuts.py       # Common analysis patterns
│   └── prompts/
│       └── cbioportal_prompt.py  # System guidance
```

## Development

### Running the Server
```bash
# For development
python -m cbioportal_mcp.server

# Or using the installed script
cbioportal-mcp
```

### Running Tests
```bash
pip install -e ".[dev]"
pytest
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

MIT License - see LICENSE file for details.

## Related Projects

- [cBioPortal](https://github.com/cBioPortal/cbioportal) - The main cBioPortal platform
- [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) - ClickHouse MCP server
- [Model Context Protocol](https://github.com/anthropics/mcp) - MCP specification