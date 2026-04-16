# Common Query Pitfalls Guide

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
     WHERE cancer_study_identifier = 'your_study_id') as total_samples
FROM genomic_event_derived
WHERE variant_type = 'mutation' AND cancer_study_identifier = 'your_study_id'
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
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED'
  AND g.hugo_gene_symbol = 'TP53'  -- Replace with each gene from Step 1
  AND stgp.cancer_study_identifier = 'your_study_id';

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
WHERE variant_type = 'mutation' AND cancer_study_identifier = 'your_study_id'
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
WHERE cancer_study_identifier = 'your_study_id'
UNION ALL
SELECT
    'Sample Level' as analysis_level,
    COUNT(DISTINCT sample_unique_id) as count
FROM clinical_data_derived
WHERE cancer_study_identifier = 'your_study_id';
```

### 5b. 🚨 SAMPLE TYPE FILTERING ERRORS

#### ❌ WRONG: Using `sample.sample_type` column instead of clinical attribute
```sql
-- INCORRECT - sample.sample_type is NOT the primary/metastatic distinction!
SELECT COUNT(*) as primary_samples
FROM sample s
JOIN patient p ON s.patient_id = p.internal_id
WHERE p.cancer_study_id = 791
  AND s.sample_type = 'Primary';  -- WRONG COLUMN!
-- Returns 0 because sample_type contains "Primary Solid Tumor" for ALL samples
```
**Problem**: The `sample.sample_type` column contains general classifications like "Primary Solid Tumor" (for all tumor samples). It does NOT distinguish between primary vs metastatic samples!

#### ❌ WRONG: Assuming all samples are "Primary"
```sql
-- INCORRECT - This counts ALL samples, not just primary samples!
SELECT COUNT(DISTINCT sample_unique_id) as primary_samples
FROM clinical_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024';
-- Returns 25,040 (total samples), not 15,928 (primary samples)
```
**Problem**: Studies often have multiple sample types (Primary, Metastasis, Recurrence, etc.). Never assume all samples are primary!

#### ✅ CORRECT: Use clinical_data_derived with SAMPLE_TYPE attribute
```sql
-- CORRECT - Filter using SAMPLE_TYPE attribute in clinical_data_derived
SELECT COUNT(DISTINCT sample_unique_id) as primary_samples
FROM clinical_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024'
  AND attribute_name = 'SAMPLE_TYPE'
  AND attribute_value = 'Primary';
-- Returns 15,928 (actual primary samples)
```

#### ✅ CORRECT: First check sample type distribution
```sql
-- ALWAYS check what sample types exist in a study first
SELECT
    attribute_value as sample_type,
    COUNT(DISTINCT sample_unique_id) as sample_count
FROM clinical_data_derived
WHERE cancer_study_identifier = 'your_study_id'
  AND attribute_name = 'SAMPLE_TYPE'
GROUP BY attribute_value
ORDER BY sample_count DESC;
```

**Critical Distinction:**
| Column/Attribute | Location | Contains | Use For |
|------------------|----------|----------|---------|
| `sample.sample_type` | sample table | General class (e.g., "Primary Solid Tumor") | NOT for primary/metastatic queries |
| `SAMPLE_TYPE` attribute | clinical_data_derived | Specific type (Primary, Metastasis, etc.) | Primary vs metastatic filtering |

**Common SAMPLE_TYPE Values (clinical_data_derived):**
| Sample Type | Description |
|-------------|-------------|
| `Primary` | Primary tumor samples |
| `Metastasis` | Metastatic tumor samples |
| `Local Recurrence` | Locally recurrent tumors |
| `Unknown` | Sample type not specified |

**Key Rule**: When asked about "primary samples", "metastatic samples", etc., ALWAYS use `clinical_data_derived` with `attribute_name = 'SAMPLE_TYPE'`. NEVER use the `sample.sample_type` column!

### 5c. 🚨 FABRICATING OncoKB / DRIVER MUTATION ANNOTATIONS

#### ❌ Wrong: Claiming mutations are OncoKB-annotated drivers without querying driver data
```sql
-- INCORRECT - assuming all truncating mutations are "OncoKB drivers"
SELECT hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = 'coadread_mskcc_2017'
    AND hugo_gene_symbol = 'BRAF'
    AND variant_type = 'mutation'
-- Then claiming: "These are all OncoKB-annotated driver mutations"
```
**Problem**: Not all mutations in a gene are drivers. OncoKB driver annotations are separate from mutation occurrence data. Truncating mutations, missense mutations, etc. are NOT automatically "drivers" — they must be specifically annotated by OncoKB.

#### ✅ Correct: Check for driver annotation columns first
```sql
-- Step 1: Check if driver annotation columns exist
-- Use clickhouse_list_table_columns('genomic_event_derived')
-- and look for columns with "driver" in the name

-- Step 2: If driver columns exist, use them to filter
SELECT hugo_gene_symbol, mutation_variant, driver_filter
FROM genomic_event_derived
WHERE cancer_study_identifier = 'coadread_mskcc_2017'
    AND hugo_gene_symbol = 'BRAF'
    AND variant_type = 'mutation'
    AND driver_filter IS NOT NULL;

-- Step 3: If driver columns do NOT exist, inform the user:
-- "Driver mutation annotations are not available in the current database.
--  Use the cBioPortal web interface with OQL DRIVER syntax (e.g., BRAF: MUT_DRIVER)"
```

**OQL DRIVER syntax (for reference — used in cBioPortal web UI, not SQL):**
- `TP53: DRIVER` — all OncoKB-annotated driver alterations (mutations, fusions, CNAs)
- `BRAF: MUT_DRIVER` — OncoKB-annotated driver mutations in BRAF
- `BRAF: AMP_DRIVER` — OncoKB-annotated driver amplifications
- `BRAF: HOMDEL_DRIVER` — OncoKB-annotated driver deletions

**Key rules:**
1. NEVER claim a mutation is an "OncoKB driver" without confirming from driver annotation data
2. Always check for driver columns before answering driver mutation questions
3. If driver data is unavailable, say so and suggest the web UI with OQL DRIVER syntax
4. "Frequently mutated" does NOT mean "oncogenic" or "driver"

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
WHERE g.cancer_study_identifier = 'your_study_id'
  AND c.cancer_study_identifier = 'your_study_id';
```

#### ✅ Correct: Proper join condition
```sql
-- CORRECT - Join on sample ID
SELECT *
FROM genomic_event_derived g
JOIN clinical_data_derived c ON g.sample_unique_id = c.sample_unique_id
WHERE g.cancer_study_identifier = 'your_study_id';
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

## CNA and Column Name Pitfalls

### 11. 🚨 CNA VALUES ARE NUMERIC, NOT STRINGS

#### ❌ Wrong: Using string values for CNA
```sql
-- INCORRECT - cna_alteration is numeric!
SELECT * FROM genomic_event_derived
WHERE cna_alteration = 'AMP';
```

#### ✅ Correct: Use numeric values
```sql
-- CORRECT - Use numeric codes
SELECT * FROM genomic_event_derived
WHERE cna_alteration = 2;   -- 2 = Amplification
-- OR
WHERE cna_alteration = -2;  -- -2 = Homozygous Deletion
```

### 12. 🚨 WRONG COLUMN NAMES

#### ❌ Wrong: Using non-existent column names
```sql
-- INCORRECT - 'protein_change' doesn't exist
SELECT protein_change FROM genomic_event_derived;
```

#### ✅ Correct: Use actual column names
```sql
-- CORRECT - Use 'mutation_variant' for protein changes
SELECT mutation_variant FROM genomic_event_derived;
```

**Key column reference:**
| Wrong | Correct |
|-------|---------|
| `protein_change` | `mutation_variant` |
| `amino_acid_change` | `mutation_variant` |

## ClickHouse-Specific Pitfalls

### 13. 🚨 CTE COLUMN REFERENCE ISSUES

ClickHouse has limitations with CTEs - you can't always reference CTE column aliases in outer expressions.

#### ❌ Wrong: Referencing CTE aliases in calculations
```sql
-- MAY FAIL - CTE alias reference issues
WITH stats AS (
    SELECT patient_id, MAX(CASE WHEN gene='TP53' THEN 1 ELSE 0 END) as tp53
    FROM mutations GROUP BY patient_id
)
SELECT SUM(tp53) FROM stats;  -- 'tp53' may not be recognized
```

#### ✅ Correct: Use subquery instead
```sql
-- CORRECT - Use subquery for complex aggregations
SELECT SUM(tp53) FROM (
    SELECT patient_id, MAX(CASE WHEN gene='TP53' THEN 1 ELSE 0 END) as tp53
    FROM mutations GROUP BY patient_id
);
```

### 14. 🚨 PREMATURELY DECLARING SOMETHING OUT OF SCOPE

#### ❌ Wrong: Immediately saying something is out of scope without checking
```
User: "Can you show me the Minerva viewer for the OHSU HTAN sample?"
Bot: "This is outside the scope of cBioPortal data. Please visit the HTAN Data Portal."
```

#### ✅ Correct: Search for relevant data before declaring out of scope
```sql
-- Step 1: Search for relevant studies
SELECT cancer_study_identifier, name FROM cancer_study
WHERE name ILIKE '%HTAN%' OR cancer_study_identifier ILIKE '%htan%';

-- Step 2: Check resource definitions for relevant resources
SELECT resource_id, display_name, description, resource_type
FROM resource_definition
WHERE display_name ILIKE '%Minerva%' OR description ILIKE '%Minerva%';

-- Step 3: Check sample-level resource links for matching studies
SELECT rs.url, rd.display_name as resource_name, s.stable_id as sample_id
FROM resource_sample rs
JOIN resource_definition rd ON rs.resource_id = rd.resource_id
JOIN sample s ON rs.internal_id = s.internal_id
JOIN patient p ON s.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier ILIKE '%htan%';
```

**Key rule:** cBioPortal stores external resource links in `resource_sample`, `resource_patient`, `resource_study`, and `resource_definition` tables. Always check these before saying something is out of scope.

### 15. 🚨 HALLUCINATED TABLES OR COLUMNS

#### ❌ Wrong: Querying tables or columns that don't exist
```sql
-- INCORRECT - assumes an 'oncokb_annotations' table exists
SELECT * FROM oncokb_annotations WHERE gene = 'BRAF';

-- INCORRECT - assumes a 'tumor_grade' column exists
SELECT tumor_grade FROM clinical_data_derived WHERE cancer_study_identifier = 'brca_tcga';
```

#### ✅ Correct: Always verify schema before querying
```sql
-- Step 1: Verify the table exists
-- Use clickhouse_list_tables tool first

-- Step 2: Verify columns exist
-- Use clickhouse_list_table_columns(table) tool first

-- Step 3: Only then build your query using confirmed tables and columns
SELECT attribute_value FROM clinical_data_derived
WHERE attribute_name = 'TUMOR_GRADE' AND cancer_study_identifier = 'brca_tcga';
```

**Key rule:** Never assume a table or column exists. Always check with `clickhouse_list_tables` and `clickhouse_list_table_columns` first.

## Best Practices Summary

1. **Always use gene-specific denominators** for mutation frequencies
2. **Filter out off-panel mutations** with `off_panel = 0`
3. **Exclude uncalled mutations** with `mutation_status != 'UNCALLED'`
4. **Always specify study identifier** for consistent results
5. **Keep patient vs sample aggregation separate**
6. **Filter by SAMPLE_TYPE** when asked about specific sample types (primary, metastatic, etc.)
7. **Cast clinical values to appropriate data types**
8. **Handle NULL values explicitly**
9. **Use proper join keys and conditions**
10. **Avoid cartesian products**
11. **Use derived tables when possible** for better performance
12. **Use numeric values for CNA** (2=AMP, -2=HOMDEL)
13. **Use correct column names** (`mutation_variant` not `protein_change`)
14. **Use subqueries instead of CTEs** for complex aggregations in ClickHouse
15. **Check resource tables before saying "out of scope"** — `resource_sample`, `resource_patient`, `resource_study`, `resource_definition` may have external links
16. **Always verify schema** — never assume tables or columns exist without checking first
17. **Never fabricate OncoKB/driver annotations** — check for driver columns first
18. **Check resource tables before saying "out of scope"** — `resource_sample`, `resource_patient`, `resource_study`, `resource_definition` may have external links

## Validation Checklist

Before trusting your results, ask:
- [ ] Did I filter by specific study?
- [ ] Did I exclude off-panel and uncalled mutations?
- [ ] Are my denominators gene-specific (not study-wide)?
- [ ] Am I comparing the right data types?
- [ ] Did I handle NULL values appropriately?
- [ ] Do my join conditions make biological sense?
- [ ] Are my sample counts reasonable for the study?
- [ ] Did I use numeric values for CNA alterations (not strings)?
- [ ] Am I using the correct column names (mutation_variant, not protein_change)?
- [ ] When asked about specific sample types (primary, metastatic, etc.), did I filter by SAMPLE_TYPE?
- [ ] Did I avoid fabricating OncoKB/driver annotations without checking driver columns first?
- [ ] Before saying "out of scope", did I check `resource_sample`, `resource_patient`, `resource_study`, and `resource_definition` for external links?
- [ ] Did I verify all tables and columns exist before querying them?
