# MCP Apps — Test Set & Gap Analysis

Companion to [`mcp-apps-plan.md`](./mcp-apps-plan.md). Where the plan records *what was
built and why*, this records *what researchers actually asked for* and how far the built
apps get. Everything below is derived from real usage, not from speculation about it.

## Provenance and method

- **Source.** Deduplicated prompts from real researcher conversations with the LibreChat
  interface, plus the navigator's own redirects to `cbioportal.org` routes.
- **Corpus size.** 3,303 deduplicated prompts.
- **Redirect counts** are the strongest signal in the corpus: they are the assistant
  explicitly conceding it cannot answer and sending the user to the portal UI. Unlike topic
  labels, they are not a judgement call.
- **Counting caveat.** The topic categories below are **overlapping and non-exhaustive** —
  a prompt may carry more than one label, and prompts outside these nine themes are
  uncounted. The listed counts sum to 1,324, not 3,303. Read them as *relative demand
  between themes*, not as a partition of the corpus.
- **Study IDs** in the test set were checked against the live database. Statistics marked
  *verified live* were produced by running the tool, not estimated.

> **Before publishing derived material from this corpus**, confirm prompts carry no
> researcher identifiers or cohort-identifying detail.

## Demand signal

### Portal redirects, by route

| Route | Redirects | App today |
|---|---:|---|
| `results/oncoprint` | 168 | ✅ `oncoprint` |
| `results/mutations` | 122 | ✅ `mutation_diagram` |
| `results/plots` | 51 | ❌ none |
| `comparison/survival` | 45 | ✅ `survival_curve` |
| `results/cancerTypesSummary` | 44 | ❌ none |
| `comparison/alterations` | 39 | ❌ none |
| `results/comparison` | 33 | ❌ none |
| `comparison/mrna` | 29 | ❌ none |
| `study/plots` | 16 | ❌ none |
| `results/structuralVariants` | 15 | ❌ none |
| `results/mutualExclusivity` | 11 | ✅ `alteration_cooccurrence` |
| `results/coexpression` | 4 | ❌ none |

**Plots, cancer-type summary, alteration enrichment and mRNA comparison together account for
212 redirects — more than survival and mutual exclusivity combined.**

### Topic mix

| Topic | Prompts | App today |
|---|---:|---|
| Group comparison / enrichment | 358 | ❌ none |
| Expression / correlation / methylation | 286 | ❌ none |
| Survival / outcome | 167 | ✅ |
| Treatment / therapy / response | 150 | ❌ none |
| Cross-study / pan-cancer | 87 | ❌ none |
| Protein change / hotspot / domain | 79 | ✅ (partial) |
| OncoPrint / alteration landscape | 61 | ✅ |
| Timeline / longitudinal | 44 | ❌ none |
| Co-occurrence / mutual exclusivity | 42 | ✅ |

**The two largest themes — group comparison and expression — have no app at all.** The four
shipped data apps cover the 3rd, 6th, 7th and 9th ranked themes.

## Coverage — what the apps can and cannot express

Derived from the live tool schemas. The pattern is consistent: the apps are excellent at
*one gene, one study, one grouping*, and cannot express anything researchers build on top
of that.

Legend: ✅ supported · ◐ partial or caller-supplied · ❌ not expressible

| Capability | survival | oncoprint | lollipop | co-occur | charts |
|---|:--:|:--:|:--:|:--:|:--:|
| Single study, single gene | ✅ | ✅ | ✅ | ✅ | ✅ |
| Real statistics returned | ✅ | ❌ | ❌ | ✅ | ❌ |
| Multi-study / pan-cancer cohort | ❌ | ❌ | ❌ | ❌ | ◐ |
| Cohort filter within a study | ❌ | ◐ | ❌ | ❌ | ◐ |
| Multi-gene / co-mutation groups | ❌ | ✅ | ❌ | ✅ | ◐ |
| Protein-change granularity | ❌ | ❌ | ✅ | ❌ | ◐ |
| Protein domain / codon range | ❌ | ❌ | ◐ | ❌ | ❌ |
| Expression as a variable | ❌ | ❌ | ❌ | ❌ | ◐ |
| Driver / OncoKB filtering | ❌ | ❌ | ❌ | ❌ | ❌ |
| Treatment / timeline events | ❌ | ❌ | ❌ | ❌ | ◐ |
| Export underlying data | ❌ | ❌ | ❌ | ❌ | ❌ |
| Deep link back to cbioportal.org | ❌ | ❌ | ❌ | ❌ | ❌ |

Notes on the ◐ cells:

- **oncoprint / cohort filter** — `clinical_tracks` are *displayed*, not filtered, sorted or
  grouped on.
- **lollipop / protein domain** — Pfam domains are fetched by the widget from Genome Nexus
  for rendering only; they cannot be filtered or counted on.
- **charts** — the chart tools take data the caller already computed, so the capability
  depends entirely on what the model managed to query first.

## Test set

Questions taken from real conversations, ordered from clean happy path to the ones that
break the app. **STRESS** marks a question that exposes a specific limitation — run it
expecting failure and observe *how* it fails.

### `survival_curve` — works, log-rank p returned
*167 outcome prompts · 45 redirects to `comparison/survival`*

1. Do patients with PIK3CA mutations have different overall survival compared to PIK3CA
   wild-type in breast cancer?
   - *Verified live* on `brca_tcga_pan_can_atlas_2018`: 347 vs 737 patients, medians
     129.1 / 130.2 mo, log-rank p = 0.690. A **true null** — it tests whether the model
     over-claims a difference that isn't there.
2. What is the median survival time in the Intrahepatic Cholangiocarcinoma study from MSK?
   - Tests the hard rule that median OS comes from Kaplan–Meier, not `AVG()`.
     Study is `ihch_msk_2021`.
3. In the Pediatric Neuroblastoma study from TARGET, what is the survival difference between
   patients older than four at diagnosis and the younger ones?
   - Continuous clinical attribute needing a cut point; `group_by_clinical` takes an
     attribute name, not a threshold.
4. **STRESS** Perform the survival analysis for breast cancer in MSK-CHORD.
   - *Fails silently by design.* `msk_chord_2024` is pan-cancer with 25k samples and the tool
     has no cohort filter, so it answers for **all** cancer types and reports a real log-rank
     p for a cohort nobody asked about. This request recurs throughout the history.
5. **STRESS** For the TCGA pan-cancer cohort, what is the survival difference between patients
   with mutations in both TP53 and KRAS versus patients with only a KRAS mutation?
   - `group_by_gene` is a single gene; co-mutation groups are not expressible.
6. **STRESS** Do lung adenocarcinoma patients with high EGFR mRNA expression (top quartile)
   have different survival than those with low expression?
   - No expression-based grouping. This shape recurs constantly — survival by FOXP1
     expression, KM by OCT4 expression, survival by IMPDH2 expression in colorectal.
7. **STRESS** Patient survival for a mutation in TP53 codons 1–40 versus the rest of the
   protein, compared to patients without a TP53 mutation.
   - Three groups defined by codon range. Nothing in the schema reaches protein-change
     granularity for grouping.

### `alteration_cooccurrence` — works, Fisher + BH q
*42 prompts · strongest data-to-app fit in the corpus*

1. Are mutations in IDH1, EGFR and TP53 mutually exclusive in LGG?
   - *Verified live* on `lgg_tcga_pan_can_atlas_2018`: IDH1–EGFR mutually exclusive,
     log2 OR 5.98, q = 9.1e−31; TP53–ATRX co-occurring, q = 5.0e−67. Textbook result.
2. Are mutations in CDKN2A, CDK4 and RB1 mutually exclusive in glioblastoma patients?
   - Pathway-level exclusivity within `gbm_tcga_pan_can_atlas_2018`.
3. Is there mutual exclusivity between BRAF and CDKN2A in melanoma?
   - `skcm_tcga_pan_can_atlas_2018`. No study named — tests study resolution into the app.
4. What are the most co-mutated genes in EGFR-mutant NSCLC?
   - No gene list supplied. Tests whether the app picks genes itself.
5. **STRESS** Pathway-level mutual exclusivity in pancreatic adenocarcinoma — cell cycle
   checkpoints versus homologous recombination repair.
   - Genes cannot be merged into pathway tracks; every pair is gene × gene.
6. **STRESS** When observing mutual exclusivity, did you normalise for tumour type? Re-run
   including tumour type as a confounder and report the p-values.
   - *This one matters.* A real user caught the assistant reporting pan-cancer exclusivity
     confounded by tissue. The app has no stratification, so it can reproduce exactly that
     error on a mixed cohort.
7. **STRESS** Find genes frequently mutated only in TP53 wild-type tumours but rarely in
   TP53-mutant tumours, implicating synthetic lethal interactions.
   - Genome-wide discovery rather than a fixed gene list. Currently requires hand-written SQL.

### `oncoprint` — works, truncates at 500 columns
*61 prompts · 168 redirects to `results/oncoprint`*

1. Show me an OncoPrint for TP53, KRAS and EGFR in lung adenocarcinoma.
   - The canonical request, asked dozens of times including in German and French.
2. OncoPrint of KRAS, STK11, KEAP1 and TP53 stratified by smoking status in lung
   adenocarcinoma, oncogenic somatic alterations only, excluding unknown smoking status.
   - *Verified live* on `msk_chord_2024`: clinical tracks are accepted but "stratified by" is
     not — tracks are displayed, not sorted or grouped on. Returns
     `Showing 500 of 25040 profiled samples`.
3. OncoPrint of the top 10 most mutated genes in this study, then filter to just breast
   cancers and do the same.
   - Two gaps at once: gene auto-selection, and mid-conversation cohort refinement.
4. **STRESS** OncoPrint in TCGA PanCancer for SMARCA4, SMARCB1 and ARID1A with a merged track
   for all three, a merged track for truncating drivers, and one for missense drivers.
   - No merged tracks, no driver filter. The same user later hand-pasted working OQL
     (`EGFR: MUT = (712-979)_DRIVER; …`) — a clear signal that **OQL passthrough** is the
     feature they wanted.
5. **STRESS** OncoPrint for colon cancer with all mutations in EGFR except T790M and L858R.
   - Negative protein-change filters are not expressible.
6. **STRESS** Is there a way to reduce the fine granularity of the mutation types listed —
   missense, missense of unknown significance, splice, truncating?
   - A legibility complaint about the portal's own OncoPrint that the app inherits. Check what
     the legend does with 8+ classes.

### `mutation_diagram` — works, caps at 400 protein changes
*79 protein-change prompts · 122 redirects to `results/mutations`*

1. Rank the top 10 TP53 hotspot mutations in TCGA PanCancer from most to least frequent, with
   counts and percentages.
   - *Verified live* on `msk_impact_2017`: R175H 209, R248Q 152, R273H 137, R273C 125, with
     the warning `1136 distinct protein changes found; showing the 400 most recurrent`. The
     lollipop and the ranked table are the same query — check the model reports both.
2. Lollipop with all samples containing any of these SEPHS1 mutations: p.Arg371Trp,
   p.Arg371Gln, p.Arg371Gly.
   - Three-letter amino-acid notation and an obscure gene. Tests the position parser and gene
     resolution together.
3. Give me a list of POLE hotspot mutations in endometrial cancer.
   - `ucec_tcga_pan_can_atlas_2018`. The exonuclease-domain hotspots are the clinically
     meaningful ones — tests whether domain context appears at all.
4. **STRESS** Query mutations in EGFR that fall specifically within its tyrosine kinase domain.
   - Pfam domains are render-only; you cannot filter or count by domain.
5. **STRESS** Frequency of the different codons that produce BRAF V600E in TCGA melanoma.
   - Keyed on protein change; nucleotide detail is not surfaced. Equally unanswerable: "cases
     where a point mutation changes a GAG codon to GAA in EGFR".
6. **STRESS** Lollipop for BAP1 across the TCGA PanCancer cohort, then regenerate on the
   non-redundant dataset.
   - One `study_id`, no multi-study cohort. **The most common structural failure across all
     four data apps.**

### `bar_chart` / `pie_chart` / `line_chart` — untested in the wild
*554 turns answered with a markdown table instead*

These take data the model has already computed, so the interesting failure is not rendering —
it is **whether the model reaches for them at all** instead of emitting another wall of pipes.
Every question below produced a large markdown table in the real transcript.

1. Which cancer types show the highest frequency of BRAF V600E across all TCGA PanCancer Atlas
   studies?
   - A 33-row frequency table. Prime horizontal bar chart; also the shape behind the 44
     `results/cancerTypesSummary` redirects.
2. What treatment did most patients receive in MSK-CHORD? Categorise and display in a visual
   data representation using graphs.
   - The user asked for a chart explicitly and got a table. Direct regression test.
3. Histogram of C228T mutations in the TERT promoter across cancer types.
   - Two problems: "histogram" is not one of the three chart types, and TERT promoter
     mutations are non-coding — check the model doesn't quietly return zero.
4. Cancer type frequency in the MSK-IMPACT 50k cohort.
   - Long-tailed — tests whether the model bins the tail or renders 60 unreadable pie slices.
5. Break down how many patients have a BRAF mutation by tumour origin, then by race and sex.
   - Grouped or stacked bar. Tests the multi-series path.
6. **STRESS** Histogram of allele frequency for all TP53 missense mutations in diploid TCGA
   samples, with mean and median marked.
   - No distribution chart, no reference lines. Same for "violin plot overlaid with jittered
     dots, separated by cancer type", asked verbatim.

## Roadmap

Ranked by demand in the transcript against implementation cost. The first two are catch-up;
the rest are places the conversational surface can do something `cbioportal.org` structurally
cannot.

### P1 — A cohort filter on every app

One optional `cohort` argument — clinical attribute predicates, sample type, treatment
exposure — applied uniformly across all four data apps. The single highest-leverage change: it
converts pan-cancer registries like MSK-CHORD and MSK-IMPACT 50k from unusable-by-default into
the most valuable studies in the catalogue, and unblocks the "treatment-naive only", "MSI-high
only", "breast cancer within MSK-CHORD" requests that recur throughout the history.

This is a **correctness fix, not a feature**: today the request is not refused, it is answered
for the wrong cohort with a real statistic attached.

> **evidence** · survival on `msk_chord_2024` silently spans 24k patients across all cancer types

Implementation notes:
- Apply the predicate **before** the profiled-sample intersection, or the numerator is
  filtered while the denominator is not.
- `CANCER_TYPE` is sample-grain in `clinical_data_derived` while survival is patient-grain —
  reuse the ambiguous-patient exclusion already in `_clinical_patient_values`, or multi-primary
  patients are silently dropped.

### P1 — An expression app

286 prompts touch expression, correlation or methylation, and there is no app for any of it —
the largest single gap. Minimum viable version: a two-variable scatter with Spearman ρ (gene vs
gene, expression vs CNA, methylation vs expression), plus a box or violin plot of one gene
split by a categorical group. Wiring expression tertiles into `survival_curve` as a grouping
mode covers a second recurring shape at almost no extra cost.

> **evidence** · 51 redirects to `results/plots` · 29 to `comparison/mrna` · 4 to `results/coexpression`

### P2 — A group-comparison app with a volcano plot

Group comparison is the largest topic in the corpus at 358 prompts, and the navigator was asked
to route to the portal's comparison tab 321 times. The co-occurrence app **already contains the
machinery** — 2×2 tables, `fisher_exact_two_sided`, `benjamini_hochberg` in
`cooccurrence_stats.py` — applied to gene pairs. Applied instead to cohort A vs cohort B across
all genes, it produces the alteration-enrichment volcano researchers keep asking for, and it
inherits the statistical rigour the guides insist on rather than handing the user off to R.

> **evidence** · 39 redirects to `comparison/alterations` · 33 to `results/comparison`

### P2 — Cross-study cohorts

Every data app takes exactly one `study_id`. Meanwhile researchers ask to rank cancer types by
BRAF V600E frequency, compare BAP1 across TCGA and the non-redundant MSK set, and aggregate
across all DLBCL studies. On `cbioportal.org` this means building a virtual study by hand.

The data layer is already there: `gene_mutation_frequency_in_studies` takes
`{studies:Array(String)}` and handles the panel ∪ WES denominator correctly, and
`cancer_study_query_preferences` ships named sets like `pan_cancer_tcga`. The blocker is
`study_id: str` and `_validate_study_id` at the tool boundary.

Pool with care: MSK-CHORD is metastatic clinical sequencing, TCGA is primary and
treatment-naive. Report per-study rates with a pooled estimate and heterogeneity — and an
explicit "not covered by this study's panel" state rather than a silent zero.

> **evidence** · 87 cross-study prompts · `survival_curve` errors on a comma-separated study list

### P2 — Protein-change and domain predicates

Let alteration filters reach below the gene: specific protein changes, codon ranges, Pfam
domains, and negation. This turns three currently impossible transcript questions into
one-liners — TP53 codons 1–40 versus the rest, EGFR kinase-domain drivers only, all EGFR
mutations except T790M and L858R — and it is the shared prerequisite for treating hotspots as
first-class cohort definitions across survival, oncoprint and co-occurrence.

> **evidence** · 79 protein-change prompts · one user hand-wrote OQL range syntax to work around it

### P3 — A treatment timeline / swimmer app

MSK-CHORD's timeline data is the portal's least visually accessible asset, and 150 treatment
prompts plus 44 timeline prompts show sustained interest — sequencing relative to therapy,
response after a named drug, molecular tumour board case-finding. A swimmer plot anchored at
time of sequencing, with treatment bars and sample marks.

> **evidence** · 150 treatment prompts · 44 timeline prompts

## Relationship to the other test material

| Document | Instrument | Question it answers |
|---|---|---|
| [`tests/MANUAL_TOOL_TESTS.md`](../tests/MANUAL_TOOL_TESTS.md) | Coverage / regression suite | Does each of the 17 tools fire with correct behaviour? |
| This document | Gap analysis on real demand | What do researchers ask, and how far do the apps get? |

Both are single-arm. A **comparative** evaluation is the natural next step, and the test set
above is the input to it:

- **Arm 0** — no MCP server. Measures hallucination rate on real questions.
- **Arm 1** — MCP with raw SQL tools and guides only. The server before this branch.
- **Arm 2** — MCP with the domain tools and apps.

Arm 1 vs Arm 2 is the question this branch exists to answer. Note the likely result is **not**
a clean sweep: raw SQL is more expressive, so Arm 1 should win several of the STRESS questions
(the synthetic-lethal discovery question is explicitly noted above as "currently requires
hand-written SQL"). The expected shape is *apps win on the routine majority and eliminate a
class of denominator errors; raw SQL wins on the expressive tail; the crossover points are
exactly the roadmap items above.*

Suggested scoring dimensions, in rough order of signal per unit of grading effort:

1. **Cohort correctness** — did it answer for the cohort that was asked for? MSK-CHORD gives a
   natural test; this is its own failure class, distinct from a wrong number.
2. **Denominator correctness** — profiled samples vs. all samples, patient vs. sample grain.
3. **Statistic provenance** — was a p-value produced, and was it computed or fabricated?
4. **Over-claiming on nulls** — the PIK3CA item (p = 0.690) is the seed; add two or three more
   deliberate nulls.
5. Turns to answer, and refusal rate.

Blind the grader to arm identity — tool-call traces leak it — and use deterministic checks
against the verified figures above for numeric items, reserving judgement calls for
interpretation quality.
