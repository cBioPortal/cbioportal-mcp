Statistical Test Selection Guide
================================

Purpose
-------
This guide ensures the correct statistical test is selected before performing any group comparison, matching cBioPortal's own Group Comparison defaults.

HARD RULES — NEVER FABRICATE A STATISTIC
----------------------------------------
ClickHouse cannot run statistical tests. The agent therefore must NEVER produce a derived statistic that is not a literal column value from a SQL result. Specifically:

1. **Never invent a p-value.** Not "p < 0.001", not "p ≈ 0.05", not any p-value. If the user asks "what is the p-value?", the answer is *"I can't compute that — here is the 2x2 contingency table (or group statistics). Run it in cBioPortal's Group Comparison tab, in R with `fisher.test(...)` / `wilcox.test(...)`, or in Python with `scipy.stats.fisher_exact(...)` / `mannwhitneyu(...)`."*
2. **Never claim mutual exclusivity (or co-occurrence) from a contingency table alone.** A 2x2 table is not a test. The shape "altered/not altered × group A/group B" needs Fisher's exact + a defined direction (odds ratio < 1 with significant p). Without that test, the agent presents the table and stops.
3. **Never report a "median" that came from `AVG(...)` or any non-median aggregate.** "Median" and "mean" are different statistics; for skewed clinical distributions (especially survival) they differ substantially. Use ClickHouse's `quantile(0.5)(...)` for actual median, and label arithmetic averages as "mean", never "median".
4. **Never report a hazard ratio, odds ratio, risk ratio, or relative risk** that wasn't computed by an external tool. These require regression / model fitting that ClickHouse does not do.
5. **Never report median overall survival from `AVG(OS_MONTHS)` or even `quantile(0.5)(OS_MONTHS)`.** Median OS requires Kaplan-Meier estimation, which handles censoring (`OS_STATUS = 0:LIVING` means the event hasn't happened yet). Naive medians/means over `OS_MONTHS` ignore censoring and are systematically wrong. The correct handoff: return the raw `(OS_MONTHS, OS_STATUS)` pairs (or descriptive counts: N events, N censored, follow-up range) and tell the user to run KM in R (`survival::survfit`) or Python (`lifelines.KaplanMeierFitter`), or use cBioPortal's Survival comparison.

If asked to do any of (1)–(5), respond with the appropriate handoff template from the "Approved Response Templates" section below — do not produce the number.

Ambiguous-Term Clarification (before any analysis)
--------------------------------------------------
If the user's question uses an ambiguous term, ask before computing anything. Examples:

- **"aggressive"** → could mean shorter overall survival, higher metastasis rate, higher grade/stage, higher TMB, or specific molecular features. Ask which definition before any query. Do NOT silently equate "aggressive" with "lower OS".
- **"better outcome" / "worse outcome"** → same problem. Ask: OS? PFS? DFS? Response rate?
- **"more common"** → frequency among what denominator? Profiled samples for that gene? All samples in the study? Patients (one count per patient) or samples (multiple per patient)?

State the chosen interpretation explicitly before computing, so the user can correct you before you run the query.

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

Approved Response Templates
---------------------------

### When asked for a p-value
> "ClickHouse can't compute statistical tests. Here is the 2x2 contingency table:
>
> | | altered | unaltered |
> |---|---|---|
> | Group A | a | b |
> | Group B | c | d |
>
> The appropriate test is **Fisher's exact (two-tailed)** because [reason]. You can compute the p-value in:
> - **cBioPortal's Group Comparison tab** (recommended — uses the same defaults)
> - **R:** `fisher.test(matrix(c(a, b, c, d), nrow=2))`
> - **Python:** `from scipy.stats import fisher_exact; fisher_exact([[a, b], [c, d]])`"

### When asked about mutual exclusivity / co-occurrence
> "I can show the contingency counts, but mutual exclusivity requires Fisher's exact (with odds ratio < 1) which I can't compute. Here are the counts: [table]. Run them through cBioPortal's Mutual Exclusivity tab or the R/Python snippets above. Looking at the raw counts alone — without the test — would be guessing."

### When asked for median overall survival
> "Median OS requires Kaplan-Meier estimation because survival data is censored — patients still alive at last follow-up have not yet experienced the event, and a naive `AVG()` or `quantile(0.5)` over `OS_MONTHS` ignores that. Here is the summary for your cohort:
> - N total patients: ...
> - N events (OS_STATUS = 1:DECEASED): ...
> - N censored (OS_STATUS = 0:LIVING): ...
> - Follow-up range: min ... – max ... months
>
> Run KM in R (`survival::survfit(Surv(OS_MONTHS, OS_STATUS==\"1:DECEASED\") ~ group, data=...)`), Python (`lifelines.KaplanMeierFitter`), or cBioPortal's Survival comparison."

### When asked to compare "aggressiveness" / "outcome" between cohorts
> "'Aggressive' could mean shorter OS, higher metastasis rate, higher grade/stage, higher TMB, or specific molecular features. Which would you like to compare? I'll pull the raw values and tell you which test applies."

### Forbidden Shapes (do not produce these outputs)

- ❌ "Median overall survival is 24.3 months." (where the number came from `AVG()` or even raw `quantile`)
- ❌ "These mutations are mutually exclusive (p < 0.001)." (no test was run)
- ❌ "KRAS G12C is more aggressive than G12D (median OS 18 vs 25 months)." (KM not run, "aggressive" not clarified)
- ❌ "Hazard ratio for EGFR-mutant vs wild-type LUAD is 0.67."  (regression not run)
- ❌ "The p-value is approximately 0.03." (no test was run)
- ❌ "Based on the contingency table, there is significant enrichment." (no test was run)

Common Pitfalls
---------------
- Do NOT use chi-squared for 2x2 tables with small expected cell counts — use Fisher's exact.
- Do NOT use a t-test for clinical attributes like age or tumor stage — use Wilcoxon (non-parametric).
- Do NOT compare alteration frequencies without accounting for gene panel coverage. Use profiled sample count as the denominator, not total sample count.
- Do NOT run statistical tests on a single group — comparisons require at least 2 groups.
- Do NOT conflate mutation frequency with functional significance. "Frequently mutated" does NOT mean "driver" or "actionable."
- Do NOT present p-values from multiple comparisons without noting the need for multiple testing correction.
