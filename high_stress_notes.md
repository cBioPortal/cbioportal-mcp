# HIGH_STRESS fixes — handoff notes (2026-09-13)

Task: run `HIGH_STRESS_TEST.md` (T1–T14), then build whatever makes each failing test pass. Before:
T1 passed, T2–T14 failed. After: all 14 pass at the tool level. Per-test evidence is in
`HIGH_STRESS_TEST_RESULTS.md`. Nothing is committed; everything is uncommitted on `feature-mcp-apps`.

## What changed

| File | Change |
|---|---|
| `src/cbioportal_mcp/alteration_query.py` | **new** — parser and evaluator for a documented subset of OQL: merged tracks `["label" A B]`, `MUT/AMP/HOMDEL/FUSION`, mutation classes (MISSENSE … TRUNC, PROMOTER), protein changes (incl. `p.Val600Glu`), codon ranges `(1-40)` / `(41-)` / `(a-b*)`, `MUT != X` exclusions, GERMLINE / SOMATIC / DRIVER modifiers. Each query evaluates two ways that must agree: in Python on fetched rows, or compiled to a ClickHouse predicate for genome-wide queries. Unsupported constructs raise instead of being dropped. |
| `src/cbioportal_mcp/distribution_stats.py` | **new** — type-7 quantiles, `describe`, numpy-compatible `histogram` (FD / Sturges bins), quantile splits (median, tertiles, quartiles, top vs bottom quartile, top quartile vs rest). |
| `src/cbioportal_mcp/cooccurrence_stats.py` | stratified 2×2×K tests: exact conditional test (Fisher generalised to strata; linear-space convolution), CMH chi-square with continuity correction, Mantel–Haenszel odds ratio, Mantel–Fleiss criterion, `stratified_association_test` routing between them. |
| `src/cbioportal_mcp/survival_stats.py` | `stratified_logrank_test`; log-rank rewritten to O(T·k·log n) (44× faster, identical results on 400 random trials); **bug fix** below. |
| `src/cbioportal_mcp/server.py` | study scopes (`StudyScope`, `studies=` / `preference=` on every data app); shared OQL event fetch + driver check; `survival_curve(groups=, group_by_expression=, stratify_by=)`; `oncoprint(oql=, mutation_classes=)`; `alteration_cooccurrence(tracks=, stratify_by=)`; `mutation_diagram(domain=, protein_range=)` (Pfam from Genome Nexus, fetched server-side); **new tools** `alteration_enrichment`, `nucleotide_variants`, `mutation_allele_frequency`, `histogram_chart` (+ `ui://cbioportal/histogram` resource); `cbioportal://oql-guide` registered. 4,407 → 7,358 lines. |
| `src/cbioportal_mcp/ui.py` | `HISTOGRAM_UI_URI`. |
| `resources/oql-guide.md` | **new** — syntax table, semantics to state when reporting, refused constructs. |
| `resources/system-prompt.md` | routing for the new parameters and tools; hard rule "never claim an analysis step the payload does not show"; "Data Apps — What They Can Express" table + "Still not available". |
| `resources/statistical-tests-guide.md` | routing rows (custom / expression groups, stratified log-rank, adjusted co-occurrence, enrichment, distributions); "Confounding: pooled cancer types" section; template for "was this adjusted for tumour type?". |
| `resources/common-pitfalls.md` | #19 capability list updated; new #21 (claiming an adjustment / filter / merge the tool did not apply) and #22 (study set passed as a study id); checklist items. |
| `resources/widgets/*.html` | rebuilt: charts (new `histogram` kind with reference lines + stats caption), OncoPrint (long merged-track labels, collapsed legend, Query / Exclusions captions, scope title), survival (custom / expression / stratified labels), co-occurrence (pathway labels, stratified subtitle, label clipping fix), lollipop (shaded region band + filter note). Sources are in `frontend/` (gitignored, this machine only). |
| `tests/_fakedb.py` | **new** — shared in-memory fake ClickHouse, dispatching on the first `FROM` table. |
| `tests/test_alteration_query.py` | **new**, 95 — parser, evaluation, and Python-vs-SQL parity through sqlite3 over 23 queries. |
| `tests/test_stratified_stats.py` | **new**, 26 — R `mantelhaen.test` Rabbits example (CMH X² = 3.9286, p = 0.04747; exact S = 16, p = 0.03994; MH OR = 7), stratified log-rank properties (K = 1 equals unstratified, O−E balances when the last patient dies alone, group-order invariance), quantiles / histograms. |
| `tests/test_study_scope.py`, `test_survival_groups.py`, `test_oql_apps.py`, `test_new_analysis_tools.py` | **new**, 19 / 24 / 11 / 32 — scopes, custom and expression groups, OQL tracks in the three apps, enrichment / domain / nucleotide / VAF / histogram. |
| `tests/test_stress_guidance.py` | **new**, 5 — guidance routes each STRESS shape and keeps the "don't claim unapplied steps" rules. |
| `tests/test_high_stress_live.py` | **new**, 14 live — T1–T14 with exact TCGA numbers; skipped without `CLICKHOUSE_HOST`. |
| `tests/MANUAL_TOOL_TESTS.md`, `docs/mcp-apps-gap-analysis.md` | 22 tools / 12 guides; new §8 STRESS table; "Status after the STRESS fixes" section. |

No existing test file was edited, and all of them still pass (apart from the two pre-existing failures). A single `study_id` still filters with `cancer_study_identifier = 'x'`. Payloads gained keys (`scope`, `stratification`, …) and warnings; some queries changed shape (e.g. the lollipop query now also selects `cancer_study_identifier` for the per-study breakdown).

## Verification

- Offline: `.venv/bin/python -m pytest -q -p no:cacheprovider` → **560 passed, 19 skipped, 2 failed**.
  The 2 failures are the pre-existing `tests/test_survival_curve.py` ones
  (`test_small_curves_are_not_binned`, `test_binned_curve_still_spans_the_full_follow_up`), untouched.
  212 of the passes are the new tests. The other 348 are the unchanged pre-existing suite.
- Live, with the ClickHouse variables exported (use the loader in `cross_study_phase_a_notes.md`):
  `uv run --python 3.12 --extra dev pytest tests/test_high_stress_live.py tests/test_cross_study_live.py -q`
  → **19 passed in 76 s**.
- ruff: new modules and tests clean. `server.py` went from 47 to 50 findings. All 3 new ones are E402
  for the new imports, placed after the existing compat-shim import block like the 21 already there.
  E501 count unchanged.
- Widgets: the charts bundle was first confirmed to rebuild byte-identically from unchanged sources.
  Every changed widget was then screenshot-checked in headless Chromium with a captured live payload
  injected (T2, T3, T5, T6, T8, T9, T11, T13, T14).

## Pre-existing log-rank bug (fixed)

`logrank_test` skipped the expected count at event times with exactly one subject at risk
(`if n <= 1 or d == 0: …observed only…; continue`). That subject's group got the observed event but not
its expected 1, so its O−E was inflated by 1. Now expected accrues at every event time and only the
variance term (which is 0 there) is skipped. Unstratified effect: only when the longest follow-up ends
in an event. The TCGA spot checks were unchanged (BRCA TP53 χ² 0.1017, p 0.749794). It mattered for the
stratified test, where small strata hit it constantly: before the fix, Σ(O−E) ≠ 0 and the χ² depended
on group order.

## Decisions worth remembering

- **`MUT != X` semantics differ from the portal, on purpose.** cbioportal.org evaluates each `!=`
  independently, so `EGFR: MUT != T790M MUT != L858R` re-admits what the first removed (its docs say
  `!=` works for one event). Here every `!=` on a line is a veto, and `!=` admits other mutations only
  when the line has no positive mutation command. The difference is documented in the module docstring
  and the OQL guide. OncoPrint reports `exclusions[]` with events / samples removed, so a no-op
  exclusion is visible (CRC: 0 and 0, with a warning).
- **DRIVER is refused, not approximated.** The DB only stores study-supplied `driver_filter`, which
  4 studies have (`mds_iwg_2022`, `msk_ch_2020`, `msk_ch_2023`, `msk_ch_ped_2021`). OncoKB / hotspot
  calls are computed by the portal at query time and are not stored. The check refuses if **any**
  study in scope lacks annotations for the requested variant types; mixed scopes are refused too.
  EXP / PROT / GAIN / HETLOSS / `CNA >=` / DATATYPES also raise.
- **Profiled denominators differ by app, deliberately.** For `alteration_cooccurrence`, a merged
  track is tested only on samples profiled for all of its genes (complete case, clean 2×2). OncoPrint
  counts a sample as profiled if any gene was (matches the portal's display). For survival,
  `unaltered` requires a sample profiled for every gene of the track.
- **Custom survival groups are disjoint:** patients matching several definitions are excluded from
  all (`n_overlapping_excluded`), and patients matching none are counted in `n_unassigned`.
- **Expression groups:** patient value = mean over samples. Quantile cut-offs are computed **per
  study** (`cutoffs_within: "study"`), so pooled studies with different normalisation are not split
  on batch. The default profile is `rna_seq_v2_mrna`.
- **Stratification methods.** Survival uses a stratified log-rank (O−E and variance summed within
  strata). Co-occurrence and enrichment build one 2×2 per stratum and drop strata with N < 2. They
  use the exact conditional test when one stratum is informative or the Mantel–Fleiss criterion is
  < 5, else CMH with continuity correction; the effect size is the MH odds ratio (Haldane fallback).
  The crude result is always kept (`crude`, `stats_unstratified`). An unstratified run over several
  cancer types carries `stratification: null` and a "NOT adjusted" warning.
- **Study scopes.** `studies=` and `preference=` are resolved up front, so an unknown id or set name
  is an error, never a smaller cohort. `preference` compiles to an `IN (subquery)` on
  `cancer_study_query_preferences`. A single `study_id` is checked lazily, only when a result comes
  back empty, so normal calls pay no extra round trip. That empty-result check turns a study-set name
  passed as `study_id` into an error naming `preference=`. Patient ids shared between studies are
  warned about, not deduplicated.
- **`alteration_enrichment`** uses three queries (group sizes, altered per gene[/stratum], profiled
  per gene/group[/stratum]); a genome-wide TCGA run takes about 11 s. The genes that define the groups
  are excluded from testing, and BH runs over every tested gene. A warning fires when one group
  carries at least 1.2× more altered genes per sample (hypermutation / passenger bias). Crude
  percentages and the adjusted OR can point in different directions (KRAS: 8.8% vs 6.4% crude, but
  the adjusted OR favours wild-type); the notes say so. There is no widget.
- **Lollipop domain filter** adds a server-side network dependency: Genome Nexus Pfam for the
  canonical transcript, with a 10 s timeout and a cache. Only the widget used to fetch it.
  `protein_range=` is the offline fallback, and the error message says so.
- **VAF** = t_alt / (t_alt + t_ref) from `mutation`. `copy_number="diploid"` means the gene's
  discrete GISTIC call is 0 in that sample, not whole-genome ploidy; the payload's definition says so
  and the exclusions are counted.
- **`histogram_chart`** computes bins and statistics from raw values (≤ 100k). `bar_chart` is
  unchanged; the guidance routes histograms away from it.

## Open items

1. **Restart the local MCP server** to expose the 22 tools to the "cBioPortal Local" connector. It
   runs in tmux session `cbioportal`, window 1 "MCP Server" (`python -m cbioportal_mcp.server`,
   started 17:41, before these changes). I did not touch it. Rebuild and redeploy the Docker image for
   the "cBioPortal MCP" connector.
2. **Model-in-the-loop run** of the `HIGH_STRESS_TEST.md` prompts in Claude Desktop / LibreChat.
   Several "Expected behavior" lines there describe the old refusals. Now the right response runs the
   analysis and quotes the payload's definitions; a refusal is still right only for DRIVER / OncoKB,
   violin / jitter plots and whole-genome ploidy. The file is yours and was left unedited.
3. **Still not built:** OncoKB driver calls, OQL EXP / PROT, hazard ratios (Cox), violin / box /
   volcano plots, widgets for `alteration_enrichment` and `nucleotide_variants`.
4. `frontend/` is still gitignored, so widget sources (including the new `charts/src/histogram.ts`)
   exist only on this machine.
5. `server.py` is now 7.4k lines. The OQL-driven apps (enrichment, nucleotide / VAF) would split
   cleanly into their own modules.
