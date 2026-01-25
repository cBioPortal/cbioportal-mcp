# Mutation Frequency Analysis Guide

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
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED'
  AND g.hugo_gene_symbol = 'TP53'  -- Replace with actual gene symbol for each gene
  AND stgp.cancer_study_identifier = 'ACTUAL_STUDY_ID';  -- Replace with correct study identifier
```

### WES (Whole Exome Sequencing) Studies
For studies using WES (gene_panel_id = 'WES'), ALL genes are profiled in ALL samples. You can use a simpler approach:

```sql
-- For WES studies: total profiled = total samples with mutation profiling
SELECT COUNT(DISTINCT sample_unique_id) AS numberOfProfiledSamples
FROM sample_to_gene_panel_derived
WHERE alteration_type = 'MUTATION_EXTENDED'
  AND cancer_study_identifier = 'ACTUAL_STUDY_ID';
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
        g.hugo_gene_symbol,
        COUNT(DISTINCT stgp.sample_unique_id) as gene_profiled_samples
    FROM sample_to_gene_panel_derived stgp
    JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
    JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
    JOIN gene g ON gpl.gene_id = g.entrez_gene_id
    WHERE
        stgp.alteration_type = 'MUTATION_EXTENDED'
        AND stgp.cancer_study_identifier = 'msk_chord_2024'
    GROUP BY g.hugo_gene_symbol
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
- **Be efficient**: Minimize database calls where possible

## Copy Number Alteration (CNA) Queries

CNA data uses **numeric values**, not strings:
- `cna_alteration = 2` → Amplification (AMP)
- `cna_alteration = -2` → Homozygous Deletion (HOMDEL)

```sql
-- Get amplified genes
SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) as amp_count
FROM genomic_event_derived
WHERE variant_type = 'cna'
  AND cna_alteration = 2  -- AMP (not 'AMP')
  AND cancer_study_identifier = 'your_study'
GROUP BY hugo_gene_symbol
ORDER BY amp_count DESC;
```

## Key Column Names in genomic_event_derived

| Column | Description |
|--------|-------------|
| `hugo_gene_symbol` | Gene symbol (e.g., TP53, KRAS) |
| `mutation_variant` | Protein change (e.g., R175H, G12D) - NOT `protein_change` |
| `mutation_type` | Mutation type (Missense_Mutation, Nonsense_Mutation, etc.) |
| `mutation_status` | SOMATIC, UNKNOWN, UNCALLED |
| `variant_type` | mutation, cna, structural_variant |
| `cna_alteration` | 2 (AMP) or -2 (HOMDEL) |
| `off_panel` | 0 (on-panel) or 1 (off-panel) |

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
