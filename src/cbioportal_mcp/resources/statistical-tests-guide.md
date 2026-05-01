Statistical Test Selection Guide
================================

Purpose
-------
This guide ensures the correct statistical test is selected before performing any group comparison, matching cBioPortal's own Group Comparison defaults.

MANDATORY: Pre-Analysis Checklist
----------------------------------
Before running any statistical comparison, determine:
1. How many groups are being compared? (2 vs. 3+)
2. What is the data type? (categorical/binary vs. continuous/numeric)
3. What is the sample size per group?
4. Are there confounders (mixed studies, different gene panels)?

Decision Matrix
---------------

### Alteration Data (mutated vs. not mutated, altered vs. not altered)

| Groups | Test | Notes |
|--------|------|-------|
| 2 | Fisher's exact test (two-tailed) | Standard for 2x2 contingency tables. Use altered vs. not-altered counts per group. |
| 3+ | Chi-squared test | For larger contingency tables. Requires expected cell counts >= 5; note limitation if not met. |

### Clinical Numeric Data (age, OS_MONTHS, tumor size, TMB, etc.)

| Groups | Test | Notes |
|--------|------|-------|
| 2 | Wilcoxon rank-sum test (Mann-Whitney U) | Non-parametric. No normality assumption. Preferred for clinical data which often has skewed distributions. |
| 3+ | Kruskal-Wallis test | Non-parametric extension of Wilcoxon for 3+ groups. |

### Clinical Categorical Data (stage, grade, sample type, etc.)

| Groups | Test | Notes |
|--------|------|-------|
| Any | Chi-squared test | Used regardless of group count. Tests independence between group membership and category. |

### Expression / Genomic Continuous Data (mRNA expression, protein levels, methylation)

| Groups | Test | Notes |
|--------|------|-------|
| 2 | Student's t-test | Parametric. Assumes approximate normality (usually valid after log transformation). |
| 3+ | One-way ANOVA | Parametric extension of t-test for 3+ groups. |

Why These Defaults?
-------------------
- **Clinical data → non-parametric** (Wilcoxon, Kruskal-Wallis): Clinical attributes often have skewed distributions, outliers, ordinal scales, or small sample sizes where normality cannot be assumed.
- **Expression data → parametric** (t-test, ANOVA): Expression values are typically continuous and approximately normally distributed after log transformation, making parametric tests more powerful.
- **Alteration data → exact/chi-squared** (Fisher's, Chi-squared): Alteration status is binary (altered/not altered), creating contingency tables. Fisher's exact is preferred for 2x2 tables, especially with small counts.

Data Transformations
--------------------
- **RNA-seq expression**: Apply `log2(value + 1)` transformation before comparison.
- **Other expression data** (microarray, RPPA): Use values as provided.
- **Clinical numeric data**: Use raw values (no transformation).

Multiple Testing Correction
----------------------------
When comparing many genes or attributes simultaneously:
- Apply **Benjamini-Hochberg FDR correction** (q-value)
- Report both raw p-value and adjusted q-value
- A typical significance threshold is q < 0.05

Sample Size Warnings
--------------------
- **Fisher's exact test**: Valid for any sample size (exact test).
- **Chi-squared test**: Warn if any expected cell count < 5 (test may be unreliable).
- **Student's t-test / ANOVA**: Warn if any group has fewer than 3 samples.
- **Wilcoxon / Kruskal-Wallis**: Warn if any group has fewer than 5 samples.

Computing Statistics with ClickHouse
-------------------------------------
ClickHouse does NOT have built-in statistical test functions (no Fisher's exact, no Wilcoxon, no t-test). The correct workflow is:

1. **Query the data** from ClickHouse to build the contingency table or extract group values.
2. **State which test is appropriate** and why (referencing this decision matrix).
3. **Present the summary data**: contingency table for categorical data, or descriptive statistics (mean, median, N) for continuous data.
4. **Recommend** the user run the actual test in:
   - **cBioPortal's Group Comparison tab** (built-in, uses these same test defaults)
   - **R** (fisher.test, wilcox.test, t.test, kruskal.test, chisq.test)
   - **Python** (scipy.stats: fisher_exact, mannwhitneyu, ttest_ind, kruskal, chi2_contingency)

### Example: Building a Contingency Table for Fisher's Exact Test
```sql
-- Compare TP53 mutation frequency between two cancer types
SELECT
  cancer_study_identifier,
  COUNT(DISTINCT CASE WHEN hugo_gene_symbol = 'TP53' AND variant_type = 'mutation'
    AND mutation_status != 'GERMLINE' THEN sample_unique_id END) AS altered,
  COUNT(DISTINCT sample_unique_id) - COUNT(DISTINCT CASE WHEN hugo_gene_symbol = 'TP53'
    AND variant_type = 'mutation' AND mutation_status != 'GERMLINE' THEN sample_unique_id END) AS unaltered
FROM genomic_event_derived
WHERE cancer_study_identifier IN ('{study_1}', '{study_2}')
  AND off_panel = FALSE
GROUP BY cancer_study_identifier
```

Then state: "This is a 2-group alteration comparison. The appropriate test is Fisher's exact test (two-tailed). Here is the 2x2 contingency table — you can compute the p-value in cBioPortal's Group Comparison tab, or in R with `fisher.test(matrix(c(...), nrow=2))`."

Common Pitfalls
---------------
- Do NOT use chi-squared for 2x2 tables with small expected cell counts — use Fisher's exact.
- Do NOT use a t-test for clinical attributes like age or tumor stage — use Wilcoxon (non-parametric).
- Do NOT compare alteration frequencies without accounting for gene panel coverage. Use profiled sample count as the denominator, not total sample count.
- Do NOT run statistical tests on a single group — comparisons require at least 2 groups.
- Do NOT conflate mutation frequency with functional significance. "Frequently mutated" does NOT mean "driver" or "actionable."
- Do NOT present p-values from multiple comparisons without noting the need for multiple testing correction.
