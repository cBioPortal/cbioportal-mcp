# Mutation Frequency Analysis Guide

## IMPORTANT: Reporting Mutation Frequencies
- **ALWAYS report frequencies as percentages**, not raw counts: `frequency = (altered_samples / total_profiled_samples) × 100`
- For quick frequency lookups, **prefer the TCGA Pan-Cancer Atlas study first**, then offer to expand to other studies
- When reporting across multiple studies, show **ranges** (e.g., "TP53 is mutated in 30–60% of samples") rather than a single average
- **NEVER** sum mutation events across studies to compute an aggregate frequency — this can exceed 100% due to double-counting
- Warn users that samples may overlap across cohorts (e.g., MSK studies may share patients)
- **For "across cancer types" questions**, jump to the [Cross-Cancer-Type Mutation Frequency](#cross-cancer-type-mutation-frequency) section below — there is one correct recipe and several common wrong ones.

## STOP rule: a frequency above 100% means your query is wrong

If your query returns a frequency over 100%, **do not try to debug or explain the data inconsistency to the user**. The cause is always one of these query bugs:

- Summing mutation events instead of `COUNT(DISTINCT sample_unique_id)` for the numerator
- Using a study-wide sample count as the denominator instead of the gene-specific profiled count
- Cross-study aggregation where the same biological sample appears under multiple `sample_unique_id` values (e.g., MSK-IMPACT and MSK-CHORD share patients)
- **Joining the profiled CTE through `gene_panel` / `gene_panel_list` without a WES branch.** `gene_panel_id = 'WES'` is *not* a row in `gene_panel`, so any inner JOIN through that table silently drops WES-sequenced samples from the denominator while the numerator (from `genomic_event_derived`) still counts their mutations. Always union with `mutation_wes_coverage` (or include WES samples some other way) — see the Cross-Cancer-Type recipe below.

Rewrite the query using one of the canonical patterns below (either single-study or the [Cross-Cancer-Type](#cross-cancer-type-mutation-frequency) recipe). Do **not** loop on diagnostic queries trying to attribute the >100% to "data inconsistencies" — there are none.

## Cross-Cancer-Type Mutation Frequency

When the user asks about a gene "across cancer types" or "in different cancers", look up the right cohort in **`cancer_study_query_preferences`** and group by the per-sample `CANCER_TYPE` clinical attribute. Never hand-pick study lists yourself.

### Pick the preference that matches the question

`cancer_study_query_preferences` is a `(preference_name, cancer_study_identifier, notes)` lookup. A preference can resolve to many studies (a cohort) or a single study (a recommended cohort for a specific question type). The set of preferences depends on which SQL files this deployment loaded — discover what's available with:

```sql
SELECT preference_name, COUNT(*) AS studies, any(notes) AS notes
FROM cancer_study_query_preferences
GROUP BY preference_name
ORDER BY preference_name;
```

Preferences shipped with the cBioPortal-public deployment (others may differ):

| `preference_name`           | Studies | When to use |
|-----------------------------|---------|-------------|
| `pan_cancer_tcga`           | 32      | **Default for "across cancer types" questions** — including ones phrased as "all cancer types." TCGA PanCancer Atlas uses one consistent `CANCER_TYPE` label per study with balanced sample sizes (hundreds per type), so each cancer type gets one bucket with a meaningful denominator. Canonical published reference dataset. |
| `large_genomic_cohort`      | 1       | `msk_impact_50k_2026`. Genomic-pattern questions (mutation frequency, co-occurrence) where statistical power matters more than cross-deployment portability. |
| `treatment_outcomes`        | 1       | `msk_chord_2024`. Treatment / outcomes questions — pulls treatment context from `clinical_event_derived` (see `treatment-guide`). |
| `all_studies_non_redundant` | 242     | **Only when the user explicitly asks for broader-than-TCGA coverage or for non-TCGA studies specifically.** Big footgun: `CANCER_TYPE` strings are NOT normalized across studies, so the same disease appears under multiple labels (e.g. "Ovarian Cancer" / "Ovarian Carcinoma" / "Ovarian Epithelial Tumor"; "Lung Adenocarcinoma" from one small specialty study vs "Non-Small Cell Lung Cancer" from TCGA + GENIE). Denominators per row vary by orders of magnitude. Frequencies in small per-label buckets are not representative biology — they're artifacts of how that study chose to label its samples. Always warn the user when reporting from this cohort. |

If a preference is missing from this deployment, the discovery query above will tell you what's available — don't hand-pick study lists; ask the user which cohort they want.

Do **not** combine MSK studies (`msk_impact_*`, `msk_chord_*`, `genie_public`) into one query — their `sample_unique_id`s differ but the underlying patients overlap, which inflates counts. Pick one preference.

### Canonical recipe — parameterized view

The whole recipe is wrapped in a parameterized view. The agent's "canonical" query is one line:

```sql
SELECT *
FROM gene_mutation_frequency_by_cancer_type(
    preference = 'pan_cancer_tcga',  -- default; see preference table above for when to switch
    gene       = 'TP53'
)
ORDER BY frequency_pct DESC;
```

Returns `(cancer_type, altered_samples, profiled_samples, frequency_pct)` for every cancer type with ≥ 50 profiled samples for the gene in the cohort. The recipe works identically whether the preference resolves to 1 study or 242.

The view is defined in `sql/4-mutation-frequency-views.sql` and handles the WES-vs-named-panel split internally (see "Why this works" below). The agent should prefer this view for any "gene X across cancer types in cohort Y" question instead of writing the JOIN chain by hand.

### Variant: a single named study (`gene_mutation_frequency_in_study`)

Use when the user names a specific study by id and that study isn't part of a shipped preference. Returns one row per cancer type with ≥ 50 profiled samples — typically one row for single-cancer-type studies (`brca_metabric`, `lung_msk_2017`, ...) and one row per cancer type for multi-cancer-type studies (`msk_chord_2024`).

```sql
SELECT *
FROM gene_mutation_frequency_in_study(
    study = 'brca_metabric',
    gene  = 'TP53'
)
ORDER BY frequency_pct DESC;
```

Same WES-aware denominator handling as the cohort view.

### Variant: copy-number or structural-variant alterations (`gene_alteration_frequency_by_cancer_type`)

The cohort view above filters to point mutations only. For amplifications, deep deletions, or fusions/SVs, use the generalized view that takes an `alteration` token:

```sql
SELECT *
FROM gene_alteration_frequency_by_cancer_type(
    preference = 'pan_cancer_tcga',
    gene       = 'MYC',
    alteration = 'amplification'         -- or 'deep_deletion', 'structural_variant', 'mutation'
)
ORDER BY frequency_pct DESC;
```

Numerator and denominator both switch on the `alteration` parameter:

| `alteration` token   | Counts (numerator)                              | Profiled denominator (alteration_type)  |
|----------------------|-------------------------------------------------|-----------------------------------------|
| `mutation`           | `variant_type='mutation'` AND `mutation_status != 'UNCALLED'` | `MUTATION_EXTENDED`                     |
| `amplification`      | `variant_type='cna'` AND `cna_alteration = 2`   | `COPY_NUMBER_ALTERATION`                |
| `deep_deletion`      | `variant_type='cna'` AND `cna_alteration = -2`  | `COPY_NUMBER_ALTERATION`                |
| `structural_variant` | `variant_type='structural_variant'`             | `STRUCTURAL_VARIANT`                    |

For `alteration='mutation'` the result equals `gene_mutation_frequency_by_cancer_type` (which is kept as the cleaner shorthand for that case).

### Variant: top-N most-mutated genes in a cohort (`top_mutated_genes_in_cohort`)

When the user asks "what are the most-mutated genes in cohort X" instead of naming a specific gene, use this view. Mirrors cbioportal-backend's `StudyViewMapper.getMutatedGenes` with the same WES-aware per-gene denominator as the gene-frequency view.

```sql
SELECT *
FROM top_mutated_genes_in_cohort(
    preference = 'pan_cancer_tcga',
    top_n      = 20
);
```

Returns `(hugo_gene_symbol, altered_samples, profiled_samples, frequency_pct, total_mutation_events)`, sorted by `altered_samples DESC` then gene symbol ASC (matching the backend's tiebreaker). Per-gene `profiled_samples` correctly reflects which samples were assayed for that gene — for targeted-panel cohorts (`large_genomic_cohort` = msk_impact_50k_2026), the denominator is samples on a panel that includes the gene; for WES cohorts (`pan_cancer_tcga`), every gene gets the same WES-sample denominator.

### Variant: Spearman correlation between two genes

Gene expression / copy-number correlation questions ("are TP53 and MYC expression correlated in METABRIC?") belong in `cbioportal://gene-expression-guide`, which covers the `genetic_alteration_derived` table and the `gene_pair_coexpression(study, gene_a, gene_b, profile_type)` view. Don't try to express expression queries through the mutation-frequency views.

### When to drop down to the expanded CTE form

For variations none of these three views cover (top-N most-mutated genes per cancer type, comparing two specific cancer types, custom alteration filters), drop down to the expanded CTE form below and adapt — but start from this CTE form, not a from-scratch JOIN chain that risks missing the WES branch:

```sql
WITH cohort AS (
    SELECT cancer_study_identifier
    FROM cancer_study_query_preferences
    WHERE preference_name = 'pan_cancer_tcga'
),
sample_cancer_type AS (
    SELECT cd.sample_unique_id, cd.attribute_value AS cancer_type
    FROM clinical_data_derived cd
    JOIN cohort c USING (cancer_study_identifier)
    WHERE cd.attribute_name = 'CANCER_TYPE'  -- or 'CANCER_TYPE_DETAILED'
),
altered AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
    FROM genomic_event_derived ged
    JOIN cohort c USING (cancer_study_identifier)
    JOIN sample_cancer_type sct USING (sample_unique_id)
    WHERE ged.variant_type = 'mutation'
      AND ged.mutation_status != 'UNCALLED'
      AND ged.hugo_gene_symbol = 'TP53'  -- target gene
      AND ged.off_panel = 0
    GROUP BY sct.cancer_type
),
profiled_samples_for_gene AS (
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = 'TP53'  -- same gene as above
    UNION ALL
    SELECT sample_unique_id, cancer_study_identifier
    FROM mutation_wes_coverage
),
profiled AS (
    SELECT sct.cancer_type,
           COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
    FROM profiled_samples_for_gene p
    JOIN cohort c USING (cancer_study_identifier)
    JOIN sample_cancer_type sct USING (sample_unique_id)
    GROUP BY sct.cancer_type
)
SELECT a.cancer_type,
       a.altered_samples,
       p.profiled_samples,
       ROUND(a.altered_samples * 100.0 / NULLIF(p.profiled_samples, 0), 1) AS frequency_pct
FROM altered a
JOIN profiled p USING (cancer_type)
WHERE p.profiled_samples >= 50  -- suppress tiny cancer types
ORDER BY frequency_pct DESC;
```

### Why this works
- **`cancer_study_query_preferences` enforces a non-overlapping cohort.** Every shipped preference resolves to studies with no shared samples, so `COUNT(DISTINCT sample_unique_id)` doesn't double-count.
- **`CANCER_TYPE` from `clinical_data_derived`, not `type_of_cancer_id` from `cancer_study`.** Multi-cancer-type studies (MSK-CHORD, MSK-IMPACT-50k, GENIE) carry `cancer_study.type_of_cancer_id = 'mixed'`; the per-sample diagnosis lives in the clinical data. Using the clinical attribute also works uniformly for single-cancer-type studies in the same cohort.
- **WES-aware profiled denominator.** Samples on a named panel are profiled for the genes listed in `gene_panel_list`; WES samples are profiled for *every* gene. `WES` is not in the `gene_panel` table at all, so the older recipe that joined through `gene_panel` / `gene_panel_list` silently dropped WES rows — producing >100% frequencies wherever WES studies contributed altered samples but no profiled samples. The two views in `sql/4-mutation-frequency-views.sql` (`mutation_panel_gene_coverage` and `mutation_wes_coverage`) encapsulate this split so every gene-frequency query gets the WES branch for free via the `UNION ALL` above.

### When to vary
- **Top-N most-mutated genes per cancer type**: drop the `hugo_gene_symbol = '…'` filter and group by `(cancer_type, hugo_gene_symbol)`. Same CTEs.
- **Comparing two specific cancer types**: filter `sct.cancer_type IN ('Cancer A', 'Cancer B')` after resolving via `search_oncotree`.
- **Treatment-related cross-cancer questions**: switch to `preference_name = 'treatment_outcomes'` and pull treatment context from `clinical_event_derived` (see `treatment-guide`).

### What NOT to do
- **DO NOT** hand-build a `cancer_study_identifier IN ('luad_tcga', 'coadread_tcga', ...)` list — look it up in `cancer_study_query_preferences` so the canonical cohort definition stays consistent across queries.
- **DO NOT** UNION mutation events from many separate studies and then group by `type_of_cancer_id` — frequencies don't compose without per-cancer-type profiling denominators.
- **DO NOT** group by `cancer_study.type_of_cancer_id` for `msk_chord_2024`, `msk_impact_50k_2026`, or GENIE — they're `type_of_cancer_id = 'mixed'`. Use `clinical_data_derived.CANCER_TYPE`.
- **DO NOT** try to debug a >100% result query-by-query. See the STOP rule above.

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
- Filter by study: `cancer_study_identifier = 'your_study_id'`

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
- **Example for MSK-CHORD TP53 (illustrative only)**: sample % = (# Samples / Profiled Samples) × 100. For example, if a gene has 13,105 altered samples out of 25,040 profiled, the sample frequency is (13,105 / 25,040) × 100 ≈ 52.4%. Actual counts will depend on the specific dataset version and filters used.
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
        AND cancer_study_identifier = 'your_study_id'
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
        AND stgp.cancer_study_identifier = 'your_study_id'
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
