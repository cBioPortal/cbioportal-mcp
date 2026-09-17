# HIGH_STRESS_TEST — execution results

Executed 2026-09-13 against the live LLM-prepped clone (`cbioportal_public_librechat_blue`,
ClickHouse Cloud), branch `feature-mcp-apps`.

**Result: 1 / 14 passed before the changes, 14 / 14 after** (tool level — see *Method*).
Re-run the after state with:

```bash
# first export CLICKHOUSE_HOST/PORT/USER/PASSWORD/SECURE/DATABASE
# (loader snippet for ~/Desktop/.clickhouse.txt: cross_study_phase_a_notes.md)
uv run --python 3.12 --extra dev pytest tests/test_high_stress_live.py -q   # 14 passed
```

## Method

Each T# was executed as the tool calls a model makes for that question, directly against
live data through the server's tool functions (several through `mcp.call_tool`, so FastMCP's
argument validation is exercised too). Two kinds of call per test:

- the **naive call** — what the current tool surface let a model do (e.g. passing
  `pan_cancer_tcga` as a `study_id`, KRAS-mutant vs wild-type instead of the co-mutation
  comparison, co-occurrence with no stratification);
- the **capability call** — the analysis the question actually asks for.

Grading applies the rubric's bar ("does not silently return a wrong or misleading answer") at
the **tool layer**:

- **FAIL**: the request cannot be expressed and nothing in the output stops a silent
  substitution, or the tool's own output is misleading.
- **PASS**: the tool computes the requested analysis and echoes its definitions, or refuses
  the unsupported part with an explicit error or warning.

**Not done here:** a blind model-in-the-loop run (a fresh assistant given only the question) in
Claude Desktop / LibreChat. The guidance now routes each shape to the tool and forbids claiming
steps the payload does not show, but that behaviour still needs a live-host run. Note for whoever
grades that run: several *Expected behavior* lines in `HIGH_STRESS_TEST.md` were written for the
old limitations ("states it cannot construct…"). Where the analysis is now expressible, the
correct response runs it and quotes the payload's definitions. A refusal is still correct only for
DRIVER / OncoKB filtering, violin or jitter plots, and whole-genome ploidy.

## Results

| # | Before | After | Evidence (after, live) |
|---|---|---|---|
| T1 | **PASS** — `cohort` filter existed; unfiltered run warns *"spans 25040 samples across 5 cancer types"* | **PASS** | `cohort={"CANCER_TYPE": ["Breast Cancer"]}` → 5,344 patients (90 multi-primary excluded and reported) |
| T2 | **FAIL** — `survival_curve('pan_cancer_tcga')` → *"No OS survival data found"* (misleading); only one-gene altered vs wild-type expressible, no flag | **PASS** | `groups` TP53+KRAS (331) vs KRAS-only (418) across TCGA: crude log-rank p = 0.025, **stratified by CANCER_TYPE p = 0.69**. Study-set as `study_id` → error naming `preference=` |
| T3 | **FAIL** — no expression grouping; nearest call (EGFR amplification) returns a real p = 0.003 that could be passed off | **PASS** | `group_by_expression` EGFR RSEM top vs bottom quartile in LUAD: 125 vs 126 patients, cut-offs Q1 536 / Q3 1772, p = 0.30 |
| T4 | **FAIL** — codon ranges and 3 arms not expressible | **PASS** | TCGA PanCancer, three `groups`: codons 1–40: 56, codons 41+: 3,715, wild-type: 6,509; 17 overlapping patients excluded and reported; stratified p = 0.035 |
| T5 | **FAIL** — gene×gene only (66 pairs), no pathway statistic | **PASS** | merged tracks in PAAD: cell-cycle 95/179 (53.1%), HR repair 14/179 (7.8%); pair log2 OR 0.67, p = 0.42 (no exclusivity) |
| T6 | **FAIL** — no stratification; pan-cancer KRAS–APC p = 2e-68 with only a cohort-size note | **PASS** | unstratified payload: `stratification: null` + *"NOT adjusted for tumour type"* warning. `stratify_by="CANCER_TYPE"` (58 strata): TP53–APC p 1.5e-66 → 2.7e-6; **TP53–KRAS flips** from co-occurrence to mutual exclusivity; crude kept under `crude` |
| T7 | **FAIL** — `genes=None` silently tests the 8 most altered genes, looks like a discovery result | **PASS** | `alteration_enrichment("TP53: MUT", preference="pan_cancer_tcga", stratify_by="CANCER_TYPE", direction="B")`: 3,839 mutant vs 6,604 wild-type samples, 18,865 genes tested, 173 significant (15 enriched in wild-type: ARID1A, CTNNB1, PTEN, CIC, …), burden warning (1.49×), "not synthetic lethality" note. Naive call now warns it is not a genome-wide search |
| T8 | **FAIL** — `oncoprint('pan_cancer_tcga')` → *"No samples found"*; no merged or driver tracks | **PASS** | DRIVER tracks → explicit error (only 4 studies carry annotations). Without DRIVER: All three 1,505/10,443 (14.4%), Truncating 700 (6.7%), Missense 582 (5.6%) |
| T9 | **FAIL** — exclusion not expressible | **PASS** | `EGFR: MUT != T790M MUT != L858R` in CRC: 15/534 altered, `exclusions` report 0 events removed each (+ warning). In LUAD the same query removes 2 and 23 events |
| T10 | **FAIL** — no granularity option, legend classes undocumented | **PASS** | `mutation_classes="collapsed"` (documented) → single *Mutation* class and legend entry |
| T11 | **FAIL** — Pfam domains render-only | **PASS** | `domain="tyrosine kinase"` → PF07714 codons 713–965 on ENST00000275493: 60 of 70 mutated LUAD samples (85.7%), 25 changes |
| T12 | **FAIL** — protein-level only | **PASS** | BRAF V600E in TCGA SKCM: **158/158 GTG>GAG** (chr7:140453136 A>T, GRCh37). EGFR GAG>GAA → *synonymous (silent)…filtered upstream, not absent* |
| T13 | **FAIL** — `mutation_diagram('pan_cancer_tcga','BAP1')` → *"No mutations found"* (a false zero) | **PASS** | study-set as `study_id` → error. `preference="pan_cancer_tcga"`: 241 samples (by-study breakdown); `all_studies_non_redundant` (241 studies): 2,441 samples, 968 shared patient ids warned |
| T14 | **FAIL** — `bar_chart` titled "Histogram… mean/median marked" accepted with no statistics | **PASS** | `mutation_allele_frequency("TP53: MISSENSE", preference="pan_cancer_tcga", copy_number="diploid")`: 953 mutations / 838 samples, mean 0.4715, median 0.4259 drawn as lines; "diploid" defined as gene-level GISTIC 0, not ploidy; exclusions counted |

## What changed (summary)

- **Multi-study scopes** on every data app (`studies` / `preference`), with a precise error when a
  study-set name is passed as `study_id`, and a warning when studies share patients.
- **OQL alteration queries** (`alteration_query.py`): merged tracks, mutation classes, protein
  changes, codon ranges, `!=` exclusions, germline/somatic, DRIVER. Used by `oncoprint(oql=)`,
  `alteration_cooccurrence(tracks=)`, `survival_curve(groups=)`, `alteration_enrichment`,
  `mutation_allele_frequency`.
- **Stratification**: stratified log-rank (`survival_curve(stratify_by=)`), exact conditional /
  CMH tests with Mantel-Haenszel odds ratios (`alteration_cooccurrence` and
  `alteration_enrichment`). Unstratified comparisons on mixed cohorts now say they are
  unadjusted.
- **Expression groups** for survival; **domain / codon-range filters** for the lollipop.
- **New tools**: `alteration_enrichment`, `nucleotide_variants`, `mutation_allele_frequency`,
  `histogram_chart` (22 tools, up from 18).
- **Guidance**: capability table and "never claim a step the payload does not show" hard rule in
  the system prompt, a confounding section in the statistical-tests guide, pitfalls #21–22, and a
  new `cbioportal://oql-guide`.
- **Widgets rebuilt**: histogram kind (charts), merged-track labels + collapsed legend (OncoPrint),
  custom / expression / stratified labels (survival), pathway labels + stratified caption
  (co-occurrence), region band (lollipop).

Details, design decisions and verification: `high_stress_notes.md`.
