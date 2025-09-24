#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import logging
import sys
from typing import Optional
from fastmcp import FastMCP


from cbioportal_mcp.env import get_mcp_config, TransportType
from cbioportal_mcp.authentication.permissions import ensure_db_permissions

logger = logging.getLogger(__name__)

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
    return """# Mutation Frequency Analysis Guide

## Overview
For accurate gene mutation frequency calculations, you must use gene-specific profiling denominators, not study-wide sample counts.

## Key Tables & Relationships

**mutation** → **mutation_event** → **gene** (via entrez_gene_id)
**mutation** → **sample** → **patient** (via internal_id fields)

### CRITICAL: Use Derived Tables
**Don't join raw tables manually.** Use these pre-computed views:

**genomic_event_derived**: Pre-joined mutation + sample + gene data
- Contains: sample_unique_id, hugo_gene_symbol, mutation_type, mutation_status, variant_type
- Filter: `variant_type = 'mutation'` for mutations
- Filter by study: `cancer_study_identifier = 'msk_chord_2024'`

**sample_to_gene_panel_derived**: Gene-specific profiling coverage data
- Use for calculating gene-specific profiled sample denominators
- Critical for accurate frequency calculations per gene

## Critical Principles

### CRITICAL: Mutation Frequency Calculation Workflow
**For identifying most frequently mutated genes:**

1. **Step 1**: Use `genomic_event_derived` table ONLY for getting altered counts per gene
2. **Step 2**: Use `sample_to_gene_panel_derived` tables for gene-specific profiling denominators
3. **Step 3**: Calculate gene-specific frequencies using gene-specific denominators
4. **Step 4**: For EACH gene in results, run separate profiling queries

### Key Rules:
- **DO NOT use genomic_event_derived for total sample counts** - this gives study-wide counts, not gene-specific
- Report **sample frequencies only** for accurate, memory-efficient analysis
- **Each gene has different profiling coverage** - denominators vary by gene
- Sample frequency: numberOfAlteredSamplesOnPanel / gene_specific_profiled_samples
- **CRITICAL: Each gene will have different profiling coverage** (e.g., TP53 might be profiled in 25,040 samples, MUC16 in 23,000)

## Recommended Query Pattern

### Step 1: Get Altered Sample Counts per Gene
```sql
SELECT
    hugo_gene_symbol AS hugoGeneSymbol,
    entrez_gene_id AS entrezGeneId,
    COUNT(DISTINCT sample_unique_id) AS numberOfAlteredSamples,
    COUNT(DISTINCT CASE WHEN off_panel = 0 THEN sample_unique_id END) AS numberOfAlteredSamplesOnPanel,
    COUNT(*) AS totalMutationEvents
FROM genomic_event_derived
WHERE
    variant_type = 'mutation'
    AND mutation_status != 'UNCALLED'
    -- [Additional filters for specific studies/genes as needed]
GROUP BY entrez_gene_id, hugo_gene_symbol
ORDER BY numberOfAlteredSamplesOnPanel DESC;
```

## CRITICAL: Gene-Specific Profiling Denominators

### Step 2: Calculate Gene-Specific Profiled Samples (REQUIRED FOR EACH GENE)
**WORKFLOW REQUIREMENT:** For EACH gene in your results, run separate profiling queries:

```sql
-- REQUIRED: Gene-specific profiled samples (DO THIS FOR EACH GENE)
SELECT COUNT(DISTINCT stgp.sample_unique_id) AS numberOfProfiledSamples
FROM sample_to_gene_panel_derived stgp
JOIN gene_panel_to_gene_derived gptg ON stgp.gene_panel_id = gptg.gene_panel_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED'
  AND gptg.gene = 'TP53'  -- Replace with actual gene symbol for each gene
  AND stgp.cancer_study_identifier = 'ACTUAL_STUDY_ID';  -- Replace with correct study identifier
```

### Step 3: Calculate Frequencies
**Table Format Requirements:**
| Gene | # Mutations | # Samples | Profiled Samples | Sample % |

**Column Meanings:**
- **# Mutations** = totalMutationEvents (total mutation events)
- **# Samples** = numberOfAlteredSamplesOnPanel (altered samples, off_panel = 0)
- **Profiled Samples** = gene-specific profiled samples from Step 2
- **Sample %** = (# Samples / Profiled Samples) × 100

## WORKFLOW REQUIREMENTS

### Critical Requirements for Accurate Analysis:
1. **Exclude UNCALLED mutations**: `mutation_status != 'UNCALLED'`
2. **Use numberOfAlteredSamplesOnPanel**: Filter `off_panel = 0` for accurate sample frequency calculations
3. **For EACH gene in results**: Run separate profiling queries using `sample_to_gene_panel_derived`
4. **DO NOT use study-wide counts**: from genomic_event_derived for denominators
5. **Include denominator columns**: showing gene-specific profiled samples per row
6. **Replace gene symbols**: Replace 'TP53' in profiling queries with actual gene symbol for each gene

### Expected Results Format:
- **Example for MSK-CHORD TP53**: expect ~25,040 profiled samples, giving 13,105/25,040 = 52.4%
- **Each gene varies**: TP53, MUC16, TTN, etc. will have different profiling coverage

## Complete Analysis Example

### Method 1: Automated Query (If Supported)
```sql
-- Complete mutation frequency analysis with gene-specific denominators
WITH mutation_counts AS (
    SELECT
        hugo_gene_symbol,
        entrez_gene_id,
        COUNT(DISTINCT sample_unique_id) AS numberOfAlteredSamples,
        COUNT(DISTINCT CASE WHEN off_panel = 0 THEN sample_unique_id END) AS numberOfAlteredSamplesOnPanel,
        COUNT(*) AS totalMutationEvents
    FROM genomic_event_derived
    WHERE
        variant_type = 'mutation'
        AND mutation_status != 'UNCALLED'
        AND cancer_study_identifier = 'msk_chord_2024'
    GROUP BY entrez_gene_id, hugo_gene_symbol
),
-- Gene-specific profiled samples
profiled_counts AS (
    SELECT
        gptg.gene as hugo_gene_symbol,
        COUNT(DISTINCT stgp.sample_unique_id) as gene_profiled_samples
    FROM sample_to_gene_panel_derived stgp
    JOIN gene_panel_to_gene_derived gptg ON stgp.gene_panel_id = gptg.gene_panel_id
    WHERE
        stgp.alteration_type = 'MUTATION_EXTENDED'
        AND stgp.cancer_study_identifier = 'msk_chord_2024'
    GROUP BY gptg.gene
)
-- Calculate frequencies with proper denominators
SELECT
    m.hugo_gene_symbol,
    m.totalMutationEvents as mutations,
    m.numberOfAlteredSamplesOnPanel as altered_samples,
    p.gene_profiled_samples as profiled_samples,
    ROUND((m.numberOfAlteredSamplesOnPanel * 100.0) / p.gene_profiled_samples, 1) as sample_frequency_percent
FROM mutation_counts m
JOIN profiled_counts p ON m.hugo_gene_symbol = p.hugo_gene_symbol
ORDER BY sample_frequency_percent DESC;
```

### Method 2: Manual Per-Gene Queries (More Reliable)
**Step 1**: Get top mutated genes
**Step 2**: For each gene, run individual profiling query
**Step 3**: Combine results manually with proper denominators

## Important Notes

- **Off-panel filtering**: Use `off_panel = 0` to exclude mutations not covered by the gene panel
- **Mutation status filtering**: Exclude `UNCALLED` mutations for accurate counts
- **Study-specific analysis**: Always filter by specific cancer study for consistent results
- **Memory efficiency**: Sample-level analysis is more memory-efficient than patient-level for large datasets
- **Always use fully qualified table names**: Include database prefix in queries
- **Be efficient**: Minimize database calls where possible

## Common Mistakes to Avoid

### ❌ DON'T: Filter mutation_status = 'SOMATIC'
Include ALL statuses ('SOMATIC', 'UNKNOWN', etc.) - use `!= 'UNCALLED'` instead

### ❌ DON'T: Use study-wide totals for gene frequencies
```sql
-- WRONG: This gives incorrect frequencies
SELECT hugo_gene_symbol,
       COUNT(*) as mutations,
       (SELECT COUNT(DISTINCT sample_unique_id) FROM genomic_event_derived
        WHERE cancer_study_identifier = 'study') as total_samples
```

### ✅ DO: Use gene-specific profiling denominators
Each gene has different profiling coverage - always use gene-specific denominators from `sample_to_gene_panel_derived`.

## Schema Relationships

**Key Relationships:**
- cancer_study.cancer_study_identifier → identifies the study
- cancer_study.cancer_study_id → patient.cancer_study_id → clinical_patient (via patient.internal_id)
- cancer_study.cancer_study_id → patient.cancer_study_id → sample.patient_id → clinical_sample (via sample.internal_id)

**Always filter by study**: JOIN with cancer_study WHERE cancer_study_identifier = 'your_study_id'
"""

def _clinical_data_guide_text() -> str:
    return """# Clinical Data Query Guide

## Overview
Clinical data in cBioPortal is stored at both patient and sample levels. Understanding the distinction is crucial for accurate analysis.

## Data Organization

### Patient-Level vs Sample-Level Data
- **Patient-level**: Demographics, overall survival, disease stage (stored once per patient)
- **Sample-level**: Sample type, sequencing platform, purity (can have multiple per patient)

### Key Tables
- `clinical_patient`: Patient-level clinical attributes
- `clinical_sample`: Sample-level clinical attributes
- `clinical_data_derived`: Pre-joined view combining both levels
- `clinical_attribute_meta`: Metadata about available clinical attributes

## Recommended Approach: Use clinical_data_derived

The `clinical_data_derived` table is pre-joined and optimized for most queries:

```sql
-- Get clinical data for specific attributes
SELECT
    sample_unique_id,
    patient_unique_id,
    attribute_name,
    attribute_value
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name IN ('CANCER_TYPE', 'SAMPLE_TYPE', 'TMB_NONSYNONYMOUS');
```

## Clinical Attribute Discovery

### Use clinical_attribute_meta for Discovering Available Attributes
**Always start here** to see what clinical attributes are available for a specific study:

```sql
-- Discover available clinical attributes for a study
SELECT
    attr_id,
    description,
    patient_attribute,
    cancer_study_id
FROM clinical_attribute_meta
WHERE
    cancer_study_id = (
        SELECT cancer_study_id
        FROM cancer_study
        WHERE cancer_study_identifier = 'msk_chord_2024'
    )
ORDER BY patient_attribute, attr_id;
```

**Key Fields:**
- **attr_id**: matches attr_id in clinical_sample/clinical_patient tables
- **description**: human-readable description of the attribute
- **patient_attribute**: true = patient attribute, false = sample attribute
- **cancer_study_id**: links to cancer_study table (filter by study)

## Common Clinical Attributes

### Sample-Level Attributes:
- `SAMPLE_TYPE`: Primary, Metastasis, Recurrence, etc.
- `SEQUENCING_CENTER`: Where sequencing was performed
- `TUMOR_PURITY`: Estimated tumor cell percentage
- `PLATFORM`: Sequencing platform used
- `TMB_NONSYNONYMOUS`: Tumor mutational burden
- `MUTATION_COUNT`: Total mutation count per sample

### Patient-Level Attributes:
- `CANCER_TYPE`: Broad cancer category
- `CANCER_TYPE_DETAILED`: Specific cancer subtype
- `SEX`: Patient gender
- `AGE`: Age at diagnosis
- `OVERALL_SURVIVAL_MONTHS`: Overall survival time
- `OVERALL_SURVIVAL_STATUS`: Alive/Deceased status

### Cancer Type Selection Guidance:
**CANCER_TYPE vs CANCER_TYPE_DETAILED**: Choose based on question specificity
- **CANCER_TYPE**: broader categories like 'Non-Small Cell Lung Cancer', 'Breast Cancer'
- **CANCER_TYPE_DETAILED**: specific subtypes like 'Spindle Cell Carcinoma of the Lung', 'Invasive Ductal Carcinoma'
- **Decision**: Match the attribute to the level of detail requested in the question
- **When unsure**: start with CANCER_TYPE for broader matching

## Query Patterns

### 1. Filter Samples by Clinical Criteria

```sql
-- Get samples with specific clinical characteristics
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND (
        (attribute_name = 'CANCER_TYPE' AND attribute_value = 'Breast Cancer')
        OR (attribute_name = 'SAMPLE_TYPE' AND attribute_value = 'Primary')
    );
```

### 2. Aggregate Clinical Data

```sql
-- Count samples by cancer type
SELECT
    attribute_value as cancer_type,
    COUNT(DISTINCT sample_unique_id) as sample_count,
    COUNT(DISTINCT patient_unique_id) as patient_count
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'CANCER_TYPE'
GROUP BY attribute_value
ORDER BY sample_count DESC;
```

### 3. Patient Demographics Analysis

```sql
-- Get patient demographics summary
WITH patient_data AS (
    SELECT DISTINCT
        patient_unique_id,
        CASE WHEN attribute_name = 'SEX' THEN attribute_value END as sex,
        CASE WHEN attribute_name = 'AGE' THEN CAST(attribute_value AS Float64) END as age
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'msk_chord_2024'
        AND attribute_name IN ('SEX', 'AGE')
)
SELECT
    sex,
    COUNT(*) as patient_count,
    AVG(age) as avg_age,
    MIN(age) as min_age,
    MAX(age) as max_age
FROM patient_data
WHERE sex IS NOT NULL AND age IS NOT NULL
GROUP BY sex;
```

## Raw Table Queries (Advanced)

If you need to use raw clinical tables instead of the derived view:

### Patient-Level Query:
```sql
-- Query patient-level data directly
SELECT
    cp.patient_id,
    cp.attr_id,
    cp.attr_value
FROM cancer_study cs
JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
JOIN clinical_patient cp ON p.internal_id = cp.patient_id
WHERE
    cs.cancer_study_identifier = 'msk_chord_2024'
    AND cp.attr_id = 'CANCER_TYPE';
```

### Sample-Level Query:
```sql
-- Query sample-level data directly
SELECT
    cs_sample.internal_id as sample_id,
    cs_sample.attr_id,
    cs_sample.attr_value
FROM cancer_study cs
JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
JOIN sample s ON p.internal_id = s.patient_id
JOIN clinical_sample cs_sample ON s.internal_id = cs_sample.internal_id
WHERE
    cs.cancer_study_identifier = 'msk_chord_2024'
    AND cs_sample.attr_id = 'SAMPLE_TYPE';
```

## Best Practices

1. **Use clinical_data_derived when possible** - it's pre-optimized and easier to work with
2. **Check attribute availability first** - use clinical_attribute_meta to see what's available
3. **Handle missing values** - clinical data can have NULL or empty values
4. **Distinguish patient vs sample level** - know whether you need patient or sample-level aggregation
5. **Filter by study** - always specify cancer_study_identifier for consistent results
"""

def _sample_filtering_guide_text() -> str:
    return """# Sample and Study Filtering Guide

## Overview
Proper filtering is essential for meaningful cBioPortal analysis. This guide covers filtering by studies, sample types, and other criteria.

## Study-Level Filtering

### 1. Always Filter by Study
Every query should specify a study to ensure consistent results:

```sql
-- Always include study filtering
SELECT *
FROM your_table
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    -- Additional filters...
```

### 2. Find Available Studies

```sql
-- Discover available studies
SELECT
    cancer_study_identifier,
    name,
    description,
    type_of_cancer_id
FROM cancer_study
ORDER BY cancer_study_identifier;
```

### 3. Study Information

```sql
-- Get detailed study information
SELECT
    cs.cancer_study_identifier,
    cs.name as study_name,
    cs.description,
    COUNT(DISTINCT p.internal_id) as patient_count,
    COUNT(DISTINCT s.internal_id) as sample_count
FROM cancer_study cs
LEFT JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
LEFT JOIN sample s ON p.internal_id = s.patient_id
WHERE
    cs.cancer_study_identifier = 'msk_chord_2024'
GROUP BY cs.cancer_study_identifier, cs.name, cs.description;
```

## Sample Type Filtering

### 1. Common Sample Types
- **Primary**: Primary tumor samples
- **Metastasis**: Metastatic tumor samples
- **Recurrence**: Recurrent tumor samples
- **Blood**: Blood/liquid biopsy samples
- **Normal**: Normal tissue samples

### 2. Filter by Sample Type

```sql
-- Filter samples by type using clinical_data_derived
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'SAMPLE_TYPE'
    AND attribute_value = 'Primary';
```

### 3. Sample Type Distribution

```sql
-- See sample type distribution in a study
SELECT
    attribute_value as sample_type,
    COUNT(DISTINCT sample_unique_id) as sample_count,
    COUNT(DISTINCT patient_unique_id) as patient_count
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'SAMPLE_TYPE'
GROUP BY attribute_value
ORDER BY sample_count DESC;
```

## Cancer Type Filtering

### 1. Broad vs Detailed Cancer Types
- **CANCER_TYPE**: Broad categories (e.g., "Breast Cancer", "Lung Cancer")
- **CANCER_TYPE_DETAILED**: Specific subtypes (e.g., "Invasive Ductal Carcinoma", "Adenocarcinoma")

### 2. Filter by Cancer Type

```sql
-- Filter by broad cancer type
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'CANCER_TYPE'
    AND attribute_value = 'Breast Cancer';
```

### 3. Cancer Type Hierarchy

```sql
-- See cancer type breakdown
SELECT
    broad.attribute_value as broad_cancer_type,
    detailed.attribute_value as detailed_cancer_type,
    COUNT(DISTINCT broad.sample_unique_id) as sample_count
FROM clinical_data_derived broad
JOIN clinical_data_derived detailed
    ON broad.sample_unique_id = detailed.sample_unique_id
WHERE
    broad.cancer_study_identifier = 'msk_chord_2024'
    AND broad.attribute_name = 'CANCER_TYPE'
    AND detailed.attribute_name = 'CANCER_TYPE_DETAILED'
GROUP BY broad.attribute_value, detailed.attribute_value
ORDER BY broad.attribute_value, sample_count DESC;
```

## Multi-Criteria Filtering

### 1. Combine Multiple Filters

```sql
-- Filter by multiple criteria
WITH filtered_samples AS (
    SELECT DISTINCT sample_unique_id, patient_unique_id
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'msk_chord_2024'
        AND (
            (attribute_name = 'SAMPLE_TYPE' AND attribute_value = 'Primary')
            OR (attribute_name = 'CANCER_TYPE' AND attribute_value = 'Breast Cancer')
        )
    GROUP BY sample_unique_id, patient_unique_id
    HAVING COUNT(DISTINCT attribute_name) = 2  -- Must match both criteria
)
SELECT COUNT(*) as filtered_sample_count
FROM filtered_samples;
```

### 2. Age and Gender Filtering

```sql
-- Filter by patient demographics
WITH patient_filters AS (
    SELECT DISTINCT
        patient_unique_id,
        MAX(CASE WHEN attribute_name = 'SEX' THEN attribute_value END) as sex,
        MAX(CASE WHEN attribute_name = 'AGE' THEN CAST(attribute_value AS Float64) END) as age
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'msk_chord_2024'
        AND attribute_name IN ('SEX', 'AGE')
    GROUP BY patient_unique_id
)
SELECT
    patient_unique_id,
    sex,
    age
FROM patient_filters
WHERE
    sex = 'Female'
    AND age BETWEEN 40 AND 70;
```

## Quality Filtering

### 1. Sequencing Quality Filters

```sql
-- Filter by sequencing platform or quality metrics
SELECT DISTINCT
    sample_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'PLATFORM'
    AND attribute_value LIKE '%Illumina%';
```

### 2. Tumor Purity Filtering

```sql
-- Filter by tumor purity
SELECT DISTINCT
    sample_unique_id,
    CAST(attribute_value AS Float64) as tumor_purity
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND attribute_name = 'TUMOR_PURITY'
    AND CAST(attribute_value AS Float64) >= 0.3;  -- >= 30% purity
```

## Advanced Filtering Patterns

### 1. Exclude Certain Samples

```sql
-- Exclude specific sample types
SELECT DISTINCT sample_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'msk_chord_2024'
    AND sample_unique_id NOT IN (
        SELECT sample_unique_id
        FROM clinical_data_derived
        WHERE
            cancer_study_identifier = 'msk_chord_2024'
            AND attribute_name = 'SAMPLE_TYPE'
            AND attribute_value IN ('Normal', 'Blood')
    );
```

### 2. Filter by Data Availability

```sql
-- Only include samples with mutation data
SELECT DISTINCT
    cd.sample_unique_id,
    cd.patient_unique_id
FROM clinical_data_derived cd
WHERE
    cd.cancer_study_identifier = 'msk_chord_2024'
    AND EXISTS (
        SELECT 1
        FROM genomic_event_derived ged
        WHERE
            ged.sample_unique_id = cd.sample_unique_id
            AND ged.variant_type = 'mutation'
    );
```

## Best Practices

1. **Always specify study identifier** - Never query across all studies unless intended
2. **Check available values first** - Use DISTINCT queries to see available filter options
3. **Use clinical_data_derived for filtering** - It's optimized and easier than joining raw tables
4. **Handle NULL values** - Clinical data may have missing values
5. **Document your filters** - Complex filters should be well-commented
6. **Test filter logic** - Verify your filters return expected sample counts
7. **Consider sample vs patient level** - Know whether you're filtering samples or patients
"""

def _common_pitfalls_guide_text() -> str:
    return """# Common Query Pitfalls Guide

## Overview
This guide highlights frequent mistakes when analyzing cBioPortal data and provides solutions to avoid them.

## Critical Pitfalls

### 1. 🚨 CRITICAL MUTATION FREQUENCY ERRORS

#### ❌ WRONG: Using study-wide totals for gene frequencies
```sql
-- INCORRECT - This gives wrong frequencies!
SELECT
    hugo_gene_symbol,
    COUNT(DISTINCT sample_unique_id) as altered_samples,
    (SELECT COUNT(DISTINCT sample_unique_id)
     FROM genomic_event_derived
     WHERE cancer_study_identifier = 'msk_chord_2024') as total_samples
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND cancer_study_identifier = 'msk_chord_2024'
GROUP BY hugo_gene_symbol;
```
**Problem**: Different genes have different profiling coverage - you can't use study-wide totals!

#### ❌ WRONG: Not using gene-specific profiling denominators
```sql
-- INCORRECT - Missing gene-specific denominators
SELECT
    hugo_gene_symbol,
    COUNT(DISTINCT sample_unique_id) as altered_samples
FROM genomic_event_derived
WHERE variant_type = 'mutation'
GROUP BY hugo_gene_symbol;
-- Missing: WHERE ARE THE DENOMINATORS FOR EACH GENE?
```

#### ❌ WRONG: Skipping individual gene profiling queries
**Problem**: Failing to run separate profiling queries for EACH gene in results.
**Each gene has different coverage**: TP53 might be profiled in 25,040 samples, MUC16 in 23,000, etc.

#### ✅ CORRECT: Complete gene-specific workflow
```sql
-- STEP 1: Get altered counts per gene
SELECT
    hugo_gene_symbol,
    entrez_gene_id,
    COUNT(DISTINCT CASE WHEN off_panel = 0 THEN sample_unique_id END) AS numberOfAlteredSamplesOnPanel,
    COUNT(*) AS totalMutationEvents
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND mutation_status != 'UNCALLED'
GROUP BY entrez_gene_id, hugo_gene_symbol
ORDER BY numberOfAlteredSamplesOnPanel DESC;

-- STEP 2: FOR EACH GENE, run this profiling query:
SELECT COUNT(DISTINCT stgp.sample_unique_id) AS numberOfProfiledSamples
FROM sample_to_gene_panel_derived stgp
JOIN gene_panel_to_gene_derived gptg ON stgp.gene_panel_id = gptg.gene_panel_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED'
  AND gptg.gene = 'TP53'  -- Replace with each gene from Step 1
  AND stgp.cancer_study_identifier = 'msk_chord_2024';

-- STEP 3: Calculate frequency = numberOfAlteredSamplesOnPanel / numberOfProfiledSamples * 100
```

#### 🚨 WORKFLOW REQUIREMENTS VIOLATIONS:
- **Missing denominator columns**: Must show gene-specific profiled samples per row
- **Wrong table format**: Should be | Gene | # Mutations | # Samples | Profiled Samples | Sample % |
- **Not replacing gene symbols**: Must replace 'TP53' with actual gene for each query
- **Using study totals**: Never use genomic_event_derived for total sample counts

### 2. 🚨 OFF-PANEL MUTATION INCLUSION

#### ❌ Wrong: Including off-panel mutations
```sql
-- INCORRECT - Includes mutations outside gene panels
SELECT COUNT(*) FROM genomic_event_derived WHERE variant_type = 'mutation';
```

#### ✅ Correct: Filter out off-panel mutations
```sql
-- CORRECT - Only on-panel mutations
SELECT COUNT(*)
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND off_panel = 0;
```

### 3. 🚨 MUTATION STATUS FILTERING ERRORS

#### ❌ WRONG: Filtering mutation_status = 'SOMATIC'
```sql
-- INCORRECT - Too restrictive filtering
SELECT COUNT(*)
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND mutation_status = 'SOMATIC';
```
**Problem**: This excludes valid mutations with status 'UNKNOWN' and other non-SOMATIC statuses!

#### ❌ WRONG: Including uncalled mutations
```sql
-- INCORRECT - Includes uncalled/uncertain mutations
SELECT COUNT(*) FROM genomic_event_derived WHERE variant_type = 'mutation';
```

#### ✅ CORRECT: Exclude UNCALLED only, include ALL other statuses
```sql
-- CORRECT - Include SOMATIC, UNKNOWN, etc. but exclude UNCALLED
SELECT COUNT(*)
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND mutation_status != 'UNCALLED';
```
**Key**: Include ALL statuses ('SOMATIC', 'UNKNOWN', etc.) except 'UNCALLED'

### 4. 🚨 MISSING STUDY FILTERS

#### ❌ Wrong: Querying across all studies
```sql
-- INCORRECT - Results mix different studies/platforms
SELECT hugo_gene_symbol, COUNT(*) as mutations
FROM genomic_event_derived
WHERE variant_type = 'mutation'
GROUP BY hugo_gene_symbol;
```

#### ✅ Correct: Always filter by study
```sql
-- CORRECT - Study-specific analysis
SELECT hugo_gene_symbol, COUNT(*) as mutations
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND cancer_study_identifier = 'msk_chord_2024'
GROUP BY hugo_gene_symbol;
```

### 5. 🚨 PATIENT VS SAMPLE CONFUSION

#### ❌ Wrong: Mixing patient and sample counts
```sql
-- INCORRECT - Mixing levels of aggregation
SELECT
    COUNT(DISTINCT patient_unique_id) as patients,
    COUNT(DISTINCT sample_unique_id) as samples,
    -- This calculation is meaningless!
    (COUNT(DISTINCT patient_unique_id) / COUNT(DISTINCT sample_unique_id)) as ratio
FROM clinical_data_derived;
```

#### ✅ Correct: Keep aggregation levels separate
```sql
-- CORRECT - Separate patient and sample analysis
SELECT
    'Patient Level' as analysis_level,
    COUNT(DISTINCT patient_unique_id) as count
FROM clinical_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024'
UNION ALL
SELECT
    'Sample Level' as analysis_level,
    COUNT(DISTINCT sample_unique_id) as count
FROM clinical_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024';
```

## Data Type Pitfalls

### 6. 🚨 STRING VS NUMERIC COMPARISONS

#### ❌ Wrong: Treating numbers as strings
```sql
-- INCORRECT - String comparison gives wrong results
SELECT * FROM clinical_data_derived
WHERE attribute_name = 'AGE' AND attribute_value > '50';  -- String comparison!
```

#### ✅ Correct: Cast to appropriate type
```sql
-- CORRECT - Numeric comparison
SELECT * FROM clinical_data_derived
WHERE attribute_name = 'AGE' AND CAST(attribute_value AS Float64) > 50;
```

### 7. 🚨 NULL VALUE HANDLING

#### ❌ Wrong: Ignoring NULL values
```sql
-- INCORRECT - NULLs are ignored in calculations
SELECT AVG(CAST(attribute_value AS Float64)) as avg_age
FROM clinical_data_derived
WHERE attribute_name = 'AGE';
```

#### ✅ Correct: Explicit NULL handling
```sql
-- CORRECT - Handle NULLs explicitly
SELECT
    AVG(CAST(attribute_value AS Float64)) as avg_age,
    COUNT(*) as total_records,
    COUNT(CASE WHEN attribute_value IS NOT NULL THEN 1 END) as non_null_records
FROM clinical_data_derived
WHERE attribute_name = 'AGE';
```

## Join Pitfalls

### 8. 🚨 INCORRECT JOINS

#### ❌ Wrong: Joining on wrong keys
```sql
-- INCORRECT - Wrong join relationship
SELECT *
FROM patient p
JOIN sample s ON p.cancer_study_id = s.internal_id;  -- Wrong keys!
```

#### ✅ Correct: Use proper join keys
```sql
-- CORRECT - Proper foreign key relationship
SELECT *
FROM patient p
JOIN sample s ON p.internal_id = s.patient_id;
```

### 9. 🚨 CARTESIAN PRODUCTS

#### ❌ Wrong: Missing join conditions
```sql
-- INCORRECT - Creates cartesian product
SELECT *
FROM genomic_event_derived g, clinical_data_derived c
WHERE g.cancer_study_identifier = 'msk_chord_2024'
  AND c.cancer_study_identifier = 'msk_chord_2024';
```

#### ✅ Correct: Proper join condition
```sql
-- CORRECT - Join on sample ID
SELECT *
FROM genomic_event_derived g
JOIN clinical_data_derived c ON g.sample_unique_id = c.sample_unique_id
WHERE g.cancer_study_identifier = 'msk_chord_2024';
```

## Performance Pitfalls

### 10. 🚨 INEFFICIENT QUERIES

#### ❌ Wrong: Multiple subqueries
```sql
-- INCORRECT - Inefficient repeated subqueries
SELECT
    hugo_gene_symbol,
    (SELECT COUNT(*) FROM genomic_event_derived WHERE hugo_gene_symbol = g.hugo_gene_symbol) as total
FROM genomic_event_derived g;
```

#### ✅ Correct: Use aggregation
```sql
-- CORRECT - Single aggregation query
SELECT
    hugo_gene_symbol,
    COUNT(*) as total
FROM genomic_event_derived
GROUP BY hugo_gene_symbol;
```

## Best Practices Summary

1. **Always use gene-specific denominators** for mutation frequencies
2. **Filter out off-panel mutations** with `off_panel = 0`
3. **Exclude uncalled mutations** with `mutation_status != 'UNCALLED'`
4. **Always specify study identifier** for consistent results
5. **Keep patient vs sample aggregation separate**
6. **Cast clinical values to appropriate data types**
7. **Handle NULL values explicitly**
8. **Use proper join keys and conditions**
9. **Avoid cartesian products**
10. **Use derived tables when possible** for better performance

## Validation Checklist

Before trusting your results, ask:
- [ ] Did I filter by specific study?
- [ ] Did I exclude off-panel and uncalled mutations?
- [ ] Are my denominators gene-specific (not study-wide)?
- [ ] Am I comparing the right data types?
- [ ] Did I handle NULL values appropriately?
- [ ] Do my join conditions make biological sense?
- [ ] Are my sample counts reasonable for the study?
"""

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
