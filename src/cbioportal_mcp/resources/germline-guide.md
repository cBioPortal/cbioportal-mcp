Germline Variant Guide
======================

Overview
--------
cBioPortal stores both somatic AND germline variant data. Many cBioPortal features work identically for both variant types. This guide explains how to query germline variants and what to be aware of.

How Germline Data is Stored
---------------------------

### genomic_event_derived table (preferred for most queries)
- Column: `mutation_status` (LowCardinality String)
- Values: `'Germline'`, `'Somatic'`, `'LOH'`, `'NA'`, or empty string
- Filter for germline mutations: `WHERE variant_type = 'mutation' AND mutation_status = 'Germline'`
- Filter for somatic mutations: `WHERE variant_type = 'mutation' AND mutation_status = 'Somatic'`

### mutation_derived table
- Column: `mutationStatus` (Nullable String)
- Values: `'Germline'`, `'Somatic'`, `'LOH'`, or NULL
- Filter: `WHERE mutationStatus = 'Germline'`

### Structural variants
- Structural variants use `mutation_status` in `genomic_event_derived` (mapped from `sv_status`)
- Default is `'SOMATIC'`; germline SVs have value `'GERMLINE'`

Identifying Studies with Germline Data
--------------------------------------
Not all studies include germline data. Always check before querying:

```sql
-- Find studies containing germline mutations
SELECT cancer_study_identifier, COUNT(*) as germline_count
FROM genomic_event_derived
WHERE variant_type = 'mutation'
  AND mutation_status = 'Germline'
GROUP BY cancer_study_identifier
ORDER BY germline_count DESC
```

Common Query Patterns
---------------------

### Count germline vs somatic mutations per gene in a study
```sql
SELECT hugo_gene_symbol, mutation_status, COUNT(*) as count
FROM genomic_event_derived
WHERE cancer_study_identifier = '{study_id}'
  AND variant_type = 'mutation'
  AND mutation_status IN ('Germline', 'Somatic')
GROUP BY hugo_gene_symbol, mutation_status
ORDER BY count DESC
LIMIT 20
```

### Find patients with germline mutations in a specific gene
```sql
SELECT DISTINCT patient_unique_id, sample_unique_id, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE hugo_gene_symbol = '{GENE}'
  AND mutation_status = 'Germline'
  AND cancer_study_identifier = '{study_id}'
  AND variant_type = 'mutation'
```

### Germline mutation frequency with proper denominator
```sql
SELECT
  hugo_gene_symbol,
  COUNT(DISTINCT CASE WHEN mutation_status = 'Germline' THEN sample_unique_id END) AS germline_altered,
  COUNT(DISTINCT sample_unique_id) AS total_profiled,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN mutation_status = 'Germline' THEN sample_unique_id END) / COUNT(DISTINCT sample_unique_id), 2) AS germline_pct
FROM genomic_event_derived
WHERE cancer_study_identifier = '{study_id}'
  AND variant_type = 'mutation'
  AND hugo_gene_symbol = '{GENE}'
  AND off_panel = FALSE
GROUP BY hugo_gene_symbol
```

Important Caveats
-----------------

1. **Not all studies include germline data.** Many studies filter out germline variants during data processing. Always verify a study has germline data before querying.

2. **mutation_status can be NULL or empty.** Some older studies do not annotate mutation status. NULL/empty does NOT mean somatic — it means unknown. Do not assume unclassified variants are somatic.

3. **Privacy considerations.** Germline data may be more sensitive than somatic data. Some public cBioPortal instances may exclude germline variants.

4. **Variant classification.** Germline variants may include pathogenic, likely pathogenic, VUS, etc. cBioPortal does not store ACMG classification directly in standard columns.

5. **Default behavior without filtering.** If a query does not filter by `mutation_status`, results will include BOTH somatic and germline variants (plus LOH and unknown). Always note this to users.

Features That Work for Both Variant Types
-----------------------------------------
- Mutation frequency queries (with mutation_status filter)
- Protein domain analysis (mutation_variant, proteinChange, proteinPosStart/End)
- Co-occurrence analysis (with appropriate filtering)
- Clinical correlation (joining with clinical_data_derived)
- Gene panel coverage checks (sample_to_gene_panel_derived)
- cBioPortal web interface visualization

Features Primarily Designed for Somatic Variants
------------------------------------------------
- Driver annotations (`driver_filter`, `driver_tiers_filter`) — typically annotated for somatic variants only
- OncoKB annotations — designed for somatic variant classification
- Mutual exclusivity analysis — typically applied to somatic alterations
- TMB (Tumor Mutational Burden) calculations — count somatic mutations only
