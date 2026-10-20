# Gene Expression Analysis Guide

This guide covers continuous-value genomic data: gene **expression**, **copy number** values, **methylation**, and related profile types. Mutation/CNA/SV *frequency* analysis lives in `cbioportal://mutation-frequency-guide`.

## Where this data lives

Continuous per-sample-per-gene values are stored in `genetic_alteration_derived`:

| Column | Description |
|---|---|
| `sample_unique_id` | `<study>_<sample_stable_id>` |
| `cancer_study_identifier` | study scope |
| `hugo_gene_symbol` | gene |
| `profile_type` | which assay/normalization (see below) |
| `alteration_value` | the actual value — stored as Nullable(String); cast with `toFloat64OrNull` |

`alteration_value` is a string because the same column hosts many different value scales. The `''` and `'NA'` sentinels mean "missing"; always filter them out and use `toFloat64OrNull(alteration_value) IS NOT NULL` for downstream math.

## Discovering profile types for a study

Different studies expose different profile types depending on what assays were run and how the data was normalized. Always check what a specific study supports before picking one:

```sql
SELECT DISTINCT profile_type
FROM genetic_alteration_derived
WHERE cancer_study_identifier = 'brca_metabric'
ORDER BY profile_type;
```

Common values across the public portal:

| Family | Profile types |
|---|---|
| mRNA expression | `rna_seq_v2_mrna`, `rna_seq_v2_mrna_median_Zscores`, `rna_seq_v2_mrna_median_all_sample_Zscores` (TCGA PanCancer Atlas), `mrna`, `mrna_median_Zscores`, `mrna_seq_v2_rsem`, `mrna_seq_v2_rsem_Zscores`, `mrna_seq_cpm`, `mrna_seq_fpkm`, `mrna_U133`, `mrna_outliers` |
| Copy number (continuous) | `cna`, `linear_CNA`, `log2CNA`, `cna_consensus`, `cna_rae`, `gistic` |
| Methylation | `methylation_hm27`, `methylation_hm450`, `methylation_epic`, `methylation_promoters_rrbs` |
| miRNA | `mirna`, `mirna_median_Zscores` |
| Protein | `protein_quantification`, `protein_level`, `RPPA` |

**Z-score vs raw choice.** When the user asks "is X correlated with Y", either works for Spearman (rank-based) — Pearson would care. Default to the non-Z-score variant if both exist, and call out which one in the response.

## Canonical recipe — Spearman correlation between two genes

```sql
SELECT *
FROM gene_pair_coexpression(
    study        = 'brca_metabric',
    gene_a       = 'TP53',
    gene_b       = 'MYC',
    profile_type = 'mrna'
);
```

Returns one row: `(gene_a, gene_b, profile_type, spearman_correlation, num_samples)`.

- `spearman_correlation` in [−1, 1]; `NULL` when fewer than 3 valid paired samples.
- Mirrors cbioportal-backend's `ClickhouseCoExpressionMapper.getCoExpressions`, simplified to a pair lookup (the backend computes one ref gene vs ALL other genes for the coexpression page; here the agent asks about a specific pair).

### Verified examples

| Study | gene_a | gene_b | profile_type | spearman | n |
|---|---|---|---|---|---|
| `brca_metabric` | TP53 | MYC | `mrna` | 0.118 | 1980 |
| `brca_metabric` | ESR1 | PGR | `mrna` | 0.487 | 1980 |

ESR1↔PGR is the textbook breast-cancer estrogen-receptor coregulation; the 0.49 is a useful "this is what a real signal looks like" reference.

## When NOT to use `gene_pair_coexpression`

- **Mutation–mutation co-occurrence.** Spearman on essentially-binary inputs (mutated vs not) returns near-zero regardless of true co-occurrence. The agent should compute a 2×2 sample contingency table (gene_a mutated × gene_b mutated) and run Fisher's exact / log odds-ratio. See `cbioportal://statistical-tests-guide` for the decision matrix and `cBioPortal/cbioportal-navigator#43` for the real-world failure mode.
- **Survival vs gene expression.** Spearman correlates two continuous variables; survival analysis (Cox PH, Kaplan-Meier) is a different shape. Use `clinical_data_derived` + `clinical_event_derived` and surface the data; let the user run the survival test in R/Python/cBioPortal's Comparison tab.
- **Mutation count vs expression** (TMB ↔ a gene). Spearman is fine if both axes are continuous — compute TMB per sample (e.g. mutation count in `genomic_event_derived`) and join with the gene's expression. The pair-view doesn't cover this; the agent should write the CTE directly.

## Expanded CTE form for variations the view doesn't cover

If you need to filter samples (e.g. restrict to a subtype), correlate with a non-gene attribute (TMB, age), or correlate against multiple genes at once, drop down to the CTE form:

```sql
WITH a AS (
    SELECT sample_unique_id, toFloat64OrNull(alteration_value) AS v
    FROM genetic_alteration_derived
    WHERE cancer_study_identifier = 'brca_metabric'
      AND profile_type = 'mrna'
      AND hugo_gene_symbol = 'TP53'
      AND alteration_value NOT IN ('', 'NA')
      AND toFloat64OrNull(alteration_value) IS NOT NULL
),
b AS (
    SELECT sample_unique_id, toFloat64OrNull(alteration_value) AS v
    FROM genetic_alteration_derived
    WHERE cancer_study_identifier = 'brca_metabric'
      AND profile_type = 'mrna'
      AND hugo_gene_symbol = 'MYC'
      AND alteration_value NOT IN ('', 'NA')
      AND toFloat64OrNull(alteration_value) IS NOT NULL
)
SELECT rankCorr(a.v, b.v) AS spearman_correlation,
       count() AS num_samples
FROM a INNER JOIN b USING (sample_unique_id);
```

The view's job is to encode this shape once and force the `alteration_value NOT IN ('', 'NA')` + `toFloat64OrNull(...) IS NOT NULL` filters so a forgotten one doesn't silently corrupt the correlation.

## Cross-study correlation

`genetic_alteration_derived.profile_type` values are study-scoped — `mrna` in METABRIC isn't directly comparable to `mrna` in TCGA-BRCA because the underlying assays and normalizations differ. To pool across studies, use a Z-scored profile type that exists in both (`mrna_median_Zscores` is the most common) and document the caveat in the response. Or stay within a single study.

## Expression across cancer types

Z-scores are computed within each study, so every study is centered on its own reference: they rank samples inside a study but erase differences between cancer types. Do not compare cancer types on Z-scores.

For "gene X expression across cancer types", use the TCGA PanCancer Atlas studies (`pan_cancer_tcga` preference) and the non-Z-score `rna_seq_v2_mrna` profile (RSEM, batch-normalized across the PanCancer Atlas studies, so values are comparable between them), grouped by `CANCER_TYPE`. Report an upper percentile next to the median: genes driven by amplification in a subset of tumors stand out in the tail, not the median.

```sql
WITH cohort AS (
    SELECT cancer_study_identifier FROM cancer_study_query_preferences WHERE preference_name = 'pan_cancer_tcga'
),
ct AS (
    SELECT sample_unique_id, attribute_value AS cancer_type
    FROM clinical_data_derived
    WHERE attribute_name = 'CANCER_TYPE' AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
)
SELECT ct.cancer_type,
       count() AS samples,
       round(median(log2(toFloat64OrNull(g.alteration_value) + 1)), 2) AS median_log2_rsem,
       round(quantile(0.9)(log2(toFloat64OrNull(g.alteration_value) + 1)), 2) AS p90_log2_rsem
FROM genetic_alteration_derived g
JOIN ct USING (sample_unique_id)
WHERE g.cancer_study_identifier IN (SELECT cancer_study_identifier FROM cohort)
  AND g.hugo_gene_symbol = 'ERBB2'
  AND g.profile_type = 'rna_seq_v2_mrna'
  AND g.alteration_value NOT IN ('', 'NA')
  AND toFloat64OrNull(g.alteration_value) IS NOT NULL
GROUP BY ct.cancer_type
ORDER BY p90_log2_rsem DESC;
-- ERBB2: Breast 1,082 samples, median 12.78, p90 14.75; Bladder median 12.88, p90 14.28; Esophagogastric p90 13.75.
-- By median alone bladder edges out breast; the HER2-amplified tail puts breast on top.
```

Point to cBioPortal Plots for the full distribution. Outside TCGA PanCancer Atlas, raw values from different studies are not comparable either; say so rather than pooling them.
