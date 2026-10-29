Germline Variant Guide
======================

Overview
--------
cBioPortal stores both somatic AND germline variant data. Many cBioPortal features work identically for both variant types. This guide explains how to query germline variants and what to be aware of.

How Germline Data is Stored
---------------------------

### mutation_status is free text — always compare case-insensitively
Each study loads its own spelling. Germline calls appear as `'Germline'`, `'GERMLINE'` and `'germline'`; somatic calls as `'Somatic'`, `'SOMATIC'` and `'somatic'`; unannotated mutations as `'NA'`, `'.'`, `'Unknown'`, `'UNKNOWN'`, `'__UNKNOWN__'` and others. Matching one spelling silently drops whole studies (`mutation_status = 'Germline'` misses `all_stjude_2013`, `aml_stjude_2024` and `pog570_bcgsc_2020`).

- **Germline:** `upper(mutation_status) = 'GERMLINE'`
- **Somatic:** `upper(mutation_status) = 'SOMATIC'` — but only when the user asks for somatic-only. Many studies label their somatic calls `'NA'` or `'UNKNOWN'`, so for ordinary mutation questions follow common-pitfalls #3 and exclude only `'UNCALLED'`.
- When unsure, list the values first: `SELECT mutation_status, count() FROM genomic_event_derived WHERE cancer_study_identifier = '{study_id}' AND variant_type = 'mutation' GROUP BY mutation_status`

### Where the column lives
- `genomic_event_derived.mutation_status` (preferred): mutations, and structural variants (from `sv_status`: `'SOMATIC'`, `'Somatic'`, `'GERMLINE'`)
- `mutation_derived.mutationStatus`: the same values for mutations

Identifying Studies with Germline Data
--------------------------------------
Not all studies include germline data. Always check before querying:

```sql
-- Find studies containing germline mutations
SELECT cancer_study_identifier, COUNT(*) as germline_count
FROM genomic_event_derived
WHERE variant_type = 'mutation'
  AND upper(mutation_status) = 'GERMLINE'
GROUP BY cancer_study_identifier
ORDER BY germline_count DESC
```

Common Query Patterns
---------------------

### Count germline vs somatic mutations per gene in a study
```sql
SELECT hugo_gene_symbol, upper(mutation_status) AS status, COUNT(*) as count
FROM genomic_event_derived
WHERE cancer_study_identifier = '{study_id}'
  AND variant_type = 'mutation'
  AND upper(mutation_status) IN ('GERMLINE', 'SOMATIC')
GROUP BY hugo_gene_symbol, status
ORDER BY count DESC
LIMIT 20
```

### Find patients with germline mutations in a specific gene
```sql
SELECT DISTINCT patient_unique_id, sample_unique_id, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE hugo_gene_symbol = '{GENE}'
  AND upper(mutation_status) = 'GERMLINE'
  AND cancer_study_identifier = '{study_id}'
  AND variant_type = 'mutation'
```

### Germline mutation frequency
The numerator is samples with a germline mutation in the gene; the denominator is samples **profiled** for the gene — not samples that happen to have a mutation in it. Take the denominator from mutation-frequency-guide Step 2 (or its WES variant).

```sql
-- Numerator
SELECT COUNT(DISTINCT sample_unique_id) AS germline_altered
FROM genomic_event_derived
WHERE cancer_study_identifier = '{study_id}'
  AND variant_type = 'mutation'
  AND hugo_gene_symbol = '{GENE}'
  AND upper(mutation_status) = 'GERMLINE'
  AND off_panel = FALSE;

-- Denominator: mutation-frequency-guide, "Gene-Specific Profiling Denominators"
-- germline % = germline_altered / numberOfProfiledSamples * 100
```

Important Caveats
-----------------

1. **Not all studies include germline data.** Many studies filter out germline variants during data processing. Always verify a study has germline data before querying.

2. **Many studies don't annotate mutation status.** Their mutations carry `'NA'`, `'.'`, `'UNKNOWN'` or similar. That means unknown, not somatic and not germline.

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
