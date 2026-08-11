Statistical Test Selection Guide
================================

Purpose
-------
This guide ensures the correct statistical test is selected before performing any group comparison, matching cBioPortal's own Group Comparison defaults.

HARD RULES — NEVER FABRICATE A STATISTIC
----------------------------------------
ClickHouse cannot run statistical tests — but **this MCP server can**, for the cases in the routing table below. So the rule is not "never produce a statistic", it is:

> **Never compute a statistic yourself. Call the tool that computes it. Hand off to cBioPortal Group Comparison / R / Python only when no tool covers the case.**

A number you derived by hand, estimated, or recalled is a fabrication whether or not a tool exists for it. Only two sources are legitimate: a literal column value from a SQL result, or a field from one of these tool payloads.

### Tool routing table — which tool computes which statistic

| Statistic | Tool | Payload field |
|---|---|---|
| Kaplan-Meier curve; median survival (censoring-aware) | `survival_curve(study_id, endpoint, group_by_gene=... \| group_by_clinical=...)` | `groups[].curve`, `groups[].median_survival` |
| Log-rank test (2+ groups): chi-square, df, p-value | `survival_curve` (same call) | `stats.p_value`, `stats.chi_square`, `stats.df` |
| **Gene-pair** co-occurrence / mutual exclusivity: two-sided Fisher's exact p, log2 odds ratio, Benjamini-Hochberg q | `alteration_cooccurrence(study_id, genes=[...])` | `pairs[].p_value`, `pairs[].log2_odds_ratio`, `pairs[].q_value`, `pairs[].tendency` |
| Alteration frequency with panel-aware (profiled) denominators | `oncoprint(study_id, genes=[...])` | `gene_stats[]` |

`survival_curve` supports endpoints OS, PFS, DFS, DSS, and splits the cohort either by gene-alteration status or by a clinical attribute. `median_survival` is `null` when the median was not reached — report that as "not reached", never substitute a mean.

**Scope limit on `alteration_cooccurrence`:** it tests **gene A vs gene B within one study cohort**. It does *not* test "cohort A vs cohort B" alteration enrichment. There is no tool for that comparison — it still takes the handoff in rule 1.

### The rules

1. **Never invent a p-value.** Not "p < 0.001", not "p ≈ 0.05", not any p-value. Route it instead:
   - survival difference between groups → call `survival_curve` and report `stats.p_value` (log-rank), with the per-group N and event counts.
   - gene-pair co-occurrence / mutual exclusivity → call `alteration_cooccurrence` and report `pairs[].p_value` and `pairs[].q_value`.
   - **anything else** — cohort A vs cohort B enrichment, Wilcoxon / Mann-Whitney, t-test, ANOVA, Kruskal-Wallis, general chi-squared — no tool computes it. Answer: *"I can't compute that here — here is the 2x2 contingency table (or group statistics). Run it in cBioPortal's Group Comparison tab, in R with `fisher.test(...)` / `wilcox.test(...)`, or in Python with `scipy.stats.fisher_exact(...)` / `mannwhitneyu(...)`."*
2. **Never claim mutual exclusivity (or co-occurrence) from a contingency table alone.** A 2x2 table is not a test. Call `alteration_cooccurrence`, which runs the two-sided Fisher's exact test and returns the direction (`tendency`, plus the sign of `log2_odds_ratio`) and the BH-corrected q-value. Do not hand-roll the test in SQL, and do not eyeball the counts. If the pair is outside that tool's scope, present the table and stop.
3. **Never report a "median" that came from `AVG(...)` or any non-median aggregate.** "Median" and "mean" are different statistics; for skewed clinical distributions (especially survival) they differ substantially. Use ClickHouse's `quantile(0.5)(...)` for an actual median of a non-censored attribute, and label arithmetic averages as "mean", never "median". For survival specifically, neither aggregate is valid — see rule 5.
4. **Never report a hazard ratio, risk ratio, or relative risk.** These require regression / model fitting that neither ClickHouse nor this server does — there is no tool, so the answer is a refusal plus a handoff, every time. The one odds ratio you may report is `pairs[].log2_odds_ratio` from `alteration_cooccurrence`, and only as that tool returned it.
5. **Never report median overall survival from `AVG(OS_MONTHS)` or even `quantile(0.5)(OS_MONTHS)`.** Median OS requires Kaplan-Meier estimation, which handles censoring (`OS_STATUS = 0:LIVING` means the event hasn't happened yet). Naive medians/means over `OS_MONTHS` ignore censoring and are systematically wrong. **First option: call `survival_curve`** and report `groups[].median_survival` — that is the KM estimate. Only if the study or endpoint isn't supported, fall back to returning the raw `(OS_MONTHS, OS_STATUS)` pairs (or descriptive counts: N events, N censored, follow-up range) and telling the user to run KM in R (`survival::survfit`) or Python (`lifelines.KaplanMeierFitter`), or use cBioPortal's Survival comparison.

If a tool covers the request, call it — refusing a statistic the server can compute is as wrong as fabricating one. If none does, respond with the appropriate handoff template from the "Approved Response Templates" section below and do not produce the number.

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

Computing Statistics: Tools First, Handoff Second
-------------------------------------------------
ClickHouse does NOT have built-in statistical test functions (no Fisher's exact, no Wilcoxon, no t-test). Never try to express one in SQL. The workflow is:

1. **Check the tool routing table** at the top of this guide. If a tool computes the statistic, call it and report the values from its payload — naming the tool and the test it ran. That is the end of the workflow.
2. If no tool covers it, **query the data** from ClickHouse to build the contingency table or extract group values.
3. **State which test is appropriate** and why (referencing this decision matrix).
4. **Present the summary data**: contingency table for categorical data, or descriptive statistics (mean, median, N) for continuous data.
5. **Recommend** the user run the actual test in:
   - **cBioPortal's Group Comparison tab** (built-in, uses these same test defaults)
   - **R** (fisher.test, wilcox.test, t.test, kruskal.test, chisq.test)
   - **Python** (scipy.stats: fisher_exact, mannwhitneyu, ttest_ind, kruskal, chi2_contingency)

Steps 2-5 apply to the tests with no tool behind them: two-cohort alteration enrichment, Wilcoxon / Mann-Whitney, Kruskal-Wallis, t-test, ANOVA, general chi-squared, and any regression-based measure (hazard ratio, relative risk).

### Example: Building a Contingency Table for Fisher's Exact Test

This is the **uncovered** shape — one gene compared across two cohorts. No tool computes it, so it takes the handoff. (The covered shape is two genes within one cohort: call `alteration_cooccurrence` instead of building this table by hand.)

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

Pick by whether a tool covers the request. Check the routing table first.

### When asked for a p-value on a survival difference — COVERED, call the tool
> "I ran the Kaplan-Meier analysis with `survival_curve` (endpoint OS, split by TP53 alteration status). The **log-rank test** gives **p = [stats.p_value]** (chi-square [stats.chi_square], df [stats.df]).
>
> | Group | N patients | Events | Median OS |
> |---|---|---|---|
> | TP53-altered | ... | ... | ... months |
> | TP53 wild-type | ... | ... | ... months |
>
> Median is the Kaplan-Meier estimate, which accounts for censoring. 'Not reached' means the curve never crossed 50%."

### When asked about mutual exclusivity / co-occurrence — COVERED, call the tool
> "I ran `alteration_cooccurrence` on [genes] in [study]. For TP53 / RB1: **[tendency]**, two-sided Fisher's exact **p = [pairs[].p_value]**, BH-corrected **q = [pairs[].q_value]**, log2 odds ratio [pairs[].log2_odds_ratio].
>
> | | RB1 altered | RB1 not altered |
> |---|---|---|
> | TP53 altered | n_both | n_a_only |
> | TP53 not altered | n_b_only | n_neither |
>
> Counts are over the [n_profiled] samples profiled for both genes. q-values are corrected across all tested pairs."

### When asked for median overall survival — COVERED, call the tool
> "Median OS requires Kaplan-Meier estimation because survival data is censored — patients still alive at last follow-up have not yet experienced the event, and a naive `AVG()` or `quantile(0.5)` over `OS_MONTHS` ignores that. I ran `survival_curve` instead: **median OS = [groups[].median_survival] months** (KM estimate; N = ..., events = ..., censored = ...)."

Only if the study or endpoint isn't supported by `survival_curve`, fall back to:

> "Here is the summary for your cohort:
> - N total patients: ...
> - N events (OS_STATUS = 1:DECEASED): ...
> - N censored (OS_STATUS = 0:LIVING): ...
> - Follow-up range: min ... – max ... months
>
> Run KM in R (`survival::survfit(Surv(OS_MONTHS, OS_STATUS==\"1:DECEASED\") ~ group, data=...)`), Python (`lifelines.KaplanMeierFitter`), or cBioPortal's Survival comparison."

### When asked for a p-value no tool computes — UNCOVERED, hand off
Two-cohort alteration enrichment, Wilcoxon / Mann-Whitney, t-test, ANOVA, Kruskal-Wallis, general chi-squared:

> "I can't compute that test here. Here is the 2x2 contingency table:
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

### When asked for a hazard ratio — UNCOVERED, refuse and hand off
> "I can't give you a hazard ratio — that needs Cox proportional-hazards regression, which neither ClickHouse nor this server runs, and I won't estimate one from the curves. What I can give you is the Kaplan-Meier comparison from `survival_curve`: log-rank p = [...], median OS per group [...]. For the HR, fit the model in R (`survival::coxph(Surv(OS_MONTHS, event) ~ group)`) or Python (`lifelines.CoxPHFitter`)."

### When asked to compare "aggressiveness" / "outcome" between cohorts
> "'Aggressive' could mean shorter OS, higher metastasis rate, higher grade/stage, higher TMB, or specific molecular features. Which would you like to compare? I'll pull the raw values and tell you which test applies."

### Forbidden Shapes (do not produce these outputs)

- ❌ "Median overall survival is 24.3 months." (where the number came from `AVG()` or even raw `quantile` — not from `survival_curve`)
- ❌ "These mutations are mutually exclusive (p < 0.001)." (no `alteration_cooccurrence` call behind the p)
- ❌ "KRAS G12C is more aggressive than G12D (median OS 18 vs 25 months)." (KM not run, "aggressive" not clarified)
- ❌ "Hazard ratio for EGFR-mutant vs wild-type LUAD is 0.67."  (regression not run — and no tool runs it)
- ❌ "The p-value is approximately 0.03." (no test was run)
- ❌ "Based on the contingency table, there is significant enrichment." (no test was run)

### Also forbidden: refusing what the server can compute

- ❌ "I can't compute a p-value for that survival difference — run it in R." (`survival_curve` returns the log-rank p; call it)
- ❌ "Mutual exclusivity needs Fisher's exact, which I can't run — try the portal's Mutual Exclusivity tab." (`alteration_cooccurrence` runs it)
- ❌ "Median OS needs Kaplan-Meier, so I can only give you the raw `(OS_MONTHS, OS_STATUS)` pairs." (`survival_curve` computes the KM median)

Sending a researcher to scipy for a statistic this server computes is a failure, not caution.

Common Pitfalls
---------------
- Do NOT hand-roll a test the server already runs. No Fisher's exact expressed in SQL, no KM assembled from `quantile()`, no p-value approximated from a chi-square you computed by hand. Call `alteration_cooccurrence` / `survival_curve`.
- Do NOT stretch a tool past its scope. `alteration_cooccurrence` tests gene-pairs within one cohort; it is not a cohort-vs-cohort enrichment test, and its q-values are corrected only across the pairs it tested.
- Do NOT use chi-squared for 2x2 tables with small expected cell counts — use Fisher's exact.
- Do NOT use a t-test for clinical attributes like age or tumor stage — use Wilcoxon (non-parametric).
- Do NOT compare alteration frequencies without accounting for gene panel coverage. Use profiled sample count as the denominator, not total sample count.
- Do NOT run statistical tests on a single group — comparisons require at least 2 groups.
- Do NOT conflate mutation frequency with functional significance. "Frequently mutated" does NOT mean "driver" or "actionable."
- Do NOT present p-values from multiple comparisons without noting the need for multiple testing correction.
