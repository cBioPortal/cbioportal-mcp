# Cross-study meta-analysis — Phase A handoff notes (2026-09-03)

Phase A of `docs/cross-study-meta-analysis-plan.md`: the statistics module, the
`cross_study_alteration_frequency` tool, DB-free tests, and the live reconciliation. Nothing is
committed; everything below is uncommitted on `feature/mcp-apps`.

## What changed

| File | Change |
|---|---|
| `src/cbioportal_mcp/meta_stats.py` | **new** — `wilson_interval`, `pooled_proportion` (DerSimonian–Laird on the logit scale, fixed effect alongside, Q / τ² / I² / p), `heterogeneity`, `homogeneity_test` (k×2 chi-square, Fisher for two small studies), `logit` / `expit`. Pure stdlib; reuses `chi_square_sf` / `normal_ppf` from `survival_stats` and `fisher_exact_two_sided` from `cooccurrence_stats`. |
| `src/cbioportal_mcp/server.py` | **new section** "Cross-study alteration frequency (meta-analysis) app" before `mcp = FastMCP(...)` (constants, validators, OncoTree resolution, the six query builders, `_overlap_pairs`, `_cross_study_design_summary`, `_build_cross_study_payload`) and the `cross_study_alteration_frequency` tool after `alteration_cooccurrence`. One new import line for `meta_stats`. |
| `tests/test_meta_stats.py` | **new** — 28 tests: textbook Wilson values, the two-study TP53 LUAD example, k = 2 closed forms (Q, C, 2×2 chi-square) that bypass the summation code, continuity correction, τ² floor, Fisher fallback. |
| `tests/test_cross_study.py` | **new** — 32 tests with a fake DB dispatching on SQL text: the worked example end to end, patient unit, cohort-key fallbacks, subtype expansion, generic cohort predicate reaching every query, statuses (`not_covered`, `below_min_profiled`, `overlap`), pooling guard, preference union, comma-separated ids, amplification SQL, `uniqExactIf` guards, error contract, registration. |
| `tests/test_cross_study_live.py` | **new** — 5 live smoke tests that run the real tool against ClickHouse; skipped unless `CLICKHOUSE_HOST` is set. TCGA PanCancer counts asserted exactly (frozen dataset), MSK registries by range, plus the overlap guard, preference resolution, the shipped-view reconciliation and the not-covered state. |
| `docs/cross-study-meta-analysis-plan.md` | status line + new "Implementation status" section with the deviations and the live reconciliation table. |

Run: `uv run --python 3.12 --extra dev pytest -q` → 343 passed, 5 skipped (the live file), 2 failed; with the ClickHouse variables exported (loader below) → 348 passed, 2 failed. The 2 failures are the
pre-existing `tests/test_survival_curve.py` ones (`test_small_curves_are_not_binned`,
`test_binned_curve_still_spans_the_full_follow_up`), present on a clean `62518e8`. `ruff` and
`black --check` are clean on the three new files; `server.py` keeps its pre-existing lint debt and
the new code adds none (checked by line range).

## Decisions worth remembering

- **Per-pair overlap exclusion instead of all-or-nothing.** The plan said any shared id withholds
  pooling. Implemented: for each overlapping pair the study with fewer profiled samples gets
  `status: "overlap"` (+ `overlaps_with`) and pooling continues over the rest, which is pairwise
  disjoint by construction. `overlap.excluded` lists the drops; `pooling_blocked` is true only when
  fewer than two studies remain. "All LUAD studies" therefore still gets a pooled estimate when one
  TCGA release sneaks in.
- **`pool=False` only hides the pooled block.** Heterogeneity and the difference test are valid
  without pooling, so they stay.
- **Cohort CTE drives everything.** The counts, overlap and sample-type queries all embed the same
  `cohort` CTE text, so the generic `cohort` predicate cannot narrow the numerator without the
  denominator (the trap `test_cohort_filter.py` exists for). Tested by asserting the CTE body is
  byte-identical across the three queries.
- **`sample_derived` for whole-study cohorts and stable ids.** `patient_stable_id` /
  `sample_stable_id` are exact; the plan's `replaceOne` prefix stripping was a workaround.
- **ClickHouse traps, now guarded:** `LEFT JOIN` yields `''` not `NULL` (so `uniqExactIf(col,
  joined != '')`), and `uniq()` is approximate (`uniqExact` everywhere). A test asserts the SQL
  shape.
- **`unit="sample"` default**, both grains always returned; the tool description tells the model
  when to pass `unit="patient"`.
- **Tool registered without `app=`** — Phase B adds `ui.FOREST_UI_URI` + the widget resource; when it
  does, also add the `_meta["ui"]` linkage assertion to `tests/test_cross_study.py` like
  `test_cooccurrence.py` has.

## Live reconciliation — how it was done and what it showed

Done twice, with identical results. First, before credentials were available, the tool's *own* SQL
was captured with a fake DB, executed verbatim through the connected cBioPortal MCP, and the rows
replayed through the real builder. Then, with the credentials from `~/Desktop/.clickhouse.txt`, the
real tool ran end to end against ClickHouse Cloud (`cbioportal_public_librechat_blue`, ClickHouse
26.4, 545 studies): every one of the eight payload blocks compared below (`studies`, `pooled`,
`heterogeneity`, `difference_test`, `overlap`, `studies_without_cohort`, `warnings`, `notes`) was
byte-identical between the two runs for all seven scenarios. Each call took 3–7 s including five or
six round trips to ClickHouse Cloud.

Loading the credential file: its values are quoted and some lines carry inline `## comments`, so a
plain `source` breaks the password. This works:

```bash
eval "$(python3 - <<'EOF'
import re, shlex
for line in open('/home/luke/Desktop/.clickhouse.txt'):
    if '=' in line and not line.lstrip().startswith('#'):
        k, v = line.split('=', 1)
        v = re.split(r'\s+#', v.strip(), maxsplit=1)[0].strip().strip('"').strip("'")
        print(f'export {k.strip()}={shlex.quote(v)}')
EOF
)"
uv run --python 3.12 --extra dev pytest tests/test_cross_study_live.py -q
```

| Scenario | Result |
|---|---|
| S1 `studies=[msk_chord_2024, luad_tcga_pan_can_atlas_2018]`, `cancer_type="LUAD"` | 2,695 / 5,957 = 45.2% [44.0, 46.5] and 295 / 566 = 52.1% [48.0, 56.2]; pooled RE 48.4% [41.7, 55.1] (FE 45.8%), Q = 9.805, I² = 89.8%, chi-square p = 0.0017; weights 54.2 / 45.8; no shared ids; 6 queries in provenance |
| S2 `studies=[msk_chord_2024]`, `cohort={"CANCER_TYPE": ["Non-Small Cell Lung Cancer"]}` | 4,005 / 7,809 = 51.3% — identical to `gene_mutation_frequency_in_study`'s NSCLC row |
| S3 `preference="pan_cancer_tcga"`, `cancer_type="LUAD"` | 32 members resolved; one row (`luad_tcga_pan_can_atlas_2018`, 295 / 566), 31 in `studies_without_cohort` with reason "no samples matched ONCOTREE_CODE in ['LUAD']" |
| S4 `studies=[luad_tcga, luad_tcga_pan_can_atlas_2018]` | 564 shared patients and samples; `luad_tcga` has only 230 profiled of 586 cohort samples (106 mutated) and is marked `overlap`; `pooling_blocked: true` |
| S5 `studies=[msk_chord_2024, msk_impact_50k_2026]` | within the LUAD cohorts: 4,622 shared patients / 4,559 samples (the study-wide figure is 19,567); MSK-CHORD marked `overlap`, MSK-IMPACT-50k kept (6,254 profiled, 3,093 mutated = 49.5%); `pooling_blocked: true` |
| S6 the 16 LUAD-bearing members of `all_studies_non_redundant`, passed as explicit ids | 16 rows, no shared ids; `lung_smc_2016` (7 profiled) `below_min_profiled`; pooled over 15: 46.9% [41.2, 52.7], I² = 89.2%, chi-square p = 5.7e-25; the heterogeneity warning is capped at six named designs "and 9 more" |
| S7 `studies=[msk_chord_2024]`, no cancer type | 13,124 / 25,040 samples (13,105 / 24,950 patients); warning "No cancer-type filter: msk_chord_2024 spans 5 cancer types (25040 samples)" |

Live-only runs (not reproducible through the MCP replay), all through the real tool:

| Run | Result |
|---|---|
| L1 `unit="patient"`, MSK-IMPACT-50k + TCGA, LUAD | 2,616 / 5,224 patients = 50.1% vs 295 / 566 = 52.1%; pooled 50.3% [49.0, 51.6], I² = 0%, p = 0.36 — at patient grain the two agree |
| L2 KRAS, `cancer_type="NSCLC"` (expands to 18 codes), MSK-CHORD + TCGA LUAD + TCGA LUSC | 2,140 / 7,483 = 28.6%, 168 / 566 = 29.7%, 7 / 484 = 1.4%; I² = 97.4% — the squamous row is the expected biology, and the warning says so by design |
| L3 MYC `alteration="amplification"`, TCGA LUAD + MSK-CHORD | 43 / 511 = 8.4% (511 CNA-profiled of 566) vs 310 / 5,957 = 5.2%; the notes state the COPY_NUMBER_ALTERATION denominator |
| L4 EGFR, LUAD + `cohort={"SAMPLE_TYPE": ["Primary"]}` | 1,072 / 3,715 = 28.9% (MSK-CHORD primaries) vs 70 / 566 = 12.4% |
| L5 `preference="all_studies_non_redundant"`, LUAD | 241 members resolved in 3.7 s; 17 rows, 16 pooled (46.8% [42.3, 51.3], I² = 88.4%); 224 in `studies_without_cohort` with per-study reasons |
| L6 TTN (not on IMPACT panels), MSK-CHORD + TCGA | MSK-CHORD row `not_covered` (5,957 cohort, 0 profiled, frequency null) with the warning; TCGA 272 / 566 = 48.1% |

**The fallback chain earned its keep in L5:** `pan_origimed_2020` has no `ONCOTREE_CODE` attribute,
so it was matched on `CANCER_TYPE_DETAILED = 'Lung Adenocarcinoma'` (`cohort_key:
"CANCER_TYPE_DETAILED"`) — 1,572 LUAD samples, 774 TP53-mutated (49.2%) — a study the earlier
OncoTree-only prototype silently missed. Ten other members have neither per-sample attribute and are
reported with their study-level type ("… study's own cancer type 'prostate' is not among the
requested codes"), never answered whole.

Two things the live run taught that the plan did not know: `luad_tcga` (Firehose legacy) carries
mutation data for only 230 of its 586 LUAD samples, so per-study profiled denominators matter even
between two TCGA releases; and MSK-CHORD's `CANCER_TYPE` has 5 distinct values (it is a five-cancer
cohort), so the "spans N cancer types" warning reads 5, not the 40+ the gap analysis assumed.

## Open items

1. **R reference fixture** (plan §5). No R here. Someone with `metafor` can run
   ```r
   library(metafor)
   dat <- data.frame(ai = c(295, 2695, 0, 53, 12), ni = c(566, 5957, 20, 103, 17))
   res <- rma(measure = "PLO", ai = ai, ni = ni, data = dat, method = "DL")
   print(res); predict(res, transf = transf.ilogit)
   ```
   and paste `tau2`, `QE`, `I2` and the back-transformed estimate / CI into a
   `test_pooled_matches_metafor` in `tests/test_meta_stats.py` (tolerance 1e-3). Note `metafor` adds
   0.5 to *all* cells of a study only when needed for that study, same as `_logit_effect`.
2. **Phase B** — `frontend/forest/` → `resources/widgets/forest.html`, `ui.FOREST_UI_URI`, and
   `app=ui.app_config(ui.FOREST_UI_URI)` on the tool. `frontend/` is still gitignored (sources of
   the other five widgets live only on this machine).
3. **Phase C** — guide / prompt routing edits (`statistical-tests-guide` still calls study-vs-study
   comparison UNCOVERED), `MANUAL_TOOL_TESTS.md` section, guide-coverage tests, optional
   `sql/6-cross-study-views.sql`.
4. The two pre-existing `test_survival_curve.py` failures are untouched.
5. `~/Desktop/.clickhouse.txt` is the only place the credentials exist; nothing in the repo or the
   shell profile reads it, so `tests/test_cross_study_live.py` skips unless you export the variables.
