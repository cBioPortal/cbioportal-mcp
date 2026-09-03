# Cross-Study Meta-Analysis Plan — cBioPortal MCP

> **Status:** proposed 2026-09-03. **Phase A implemented 2026-09-03** (stats module, tool, SQL, overlap guard, DB-free tests, live reconciliation) and **Phase B implemented 2026-09-03** (`ui://cbioportal/forest` widget, tool linked) and **Phase C implemented 2026-09-03** (guide / prompt routing, manual tests, coverage tests, optional SQL view) — see "Implementation status" below. Remaining: the live-host manual test. Branch `feature/mcp-apps`.
> **Target prompt:** *"What's the TP53 mutation frequency in lung adenocarcinoma across MSK-CHORD and TCGA?"*
> **Deliverable:** one new data app — the `cross_study_alteration_frequency` tool plus a
> `ui://cbioportal/forest` forest-plot widget — backed by a pure-Python `meta_stats.py`, with guide /
> prompt routing and DB-free tests. It follows the three-piece + Vite pattern of the four shipped apps
> and implements the P2 "Cross-study cohorts" item from `docs/mcp-apps-gap-analysis.md`.

All numbers below were verified against the live public deployment on 2026-09-03.

## Implementation status

**Phase A — done (2026-09-03).** `src/cbioportal_mcp/meta_stats.py`, the
`cross_study_alteration_frequency` tool and its helpers in `server.py` (section "Cross-study
alteration frequency (meta-analysis) app"), `tests/test_meta_stats.py` (28 tests) and
`tests/test_cross_study.py` (32 tests). Handoff note: `cross_study_phase_a_notes.md`. Where the
implementation deviates from the sections below, the implementation wins:

- **Overlap guard is per pair, not all-or-nothing.** The smaller (fewer profiled) study of each
  overlapping pair gets `status: "overlap"` plus `overlaps_with`, and pooling proceeds over the
  remaining pairwise-disjoint studies. `overlap.excluded` lists the dropped ids;
  `overlap.pooling_blocked` is true only when fewer than two studies survive.
- **`pool=False` withholds only the `pooled` block.** `heterogeneity` and `difference_test` are
  still computed whenever two or more studies are eligible; a note says why weights are absent.
- **Whole-study cohorts and the overlap probe use `sample_derived`** (`sample_stable_id` /
  `patient_stable_id`), not prefix stripping of the unique ids.
- **Six queries, not four:** resolution, attribute availability, counts, overlap (skipped for a
  single study), panels, sample-type mix. All recorded in `provenance`.
- **`min_profiled` must be at least 1**; the tool is registered without a `ui://` app until Phase B
  ships the widget.
- **R fixture still to add**: no R in the development environment; the `metafor` snippet is in the
  Phase A notes for whoever has one.

**Phase B — done (2026-09-03).** `frontend/forest/` (gitignored source, Vite + ext-apps `App`
SDK, custom inline SVG) → `resources/widgets/forest.html`; `ui.FOREST_UI_URI`; the `forest_widget`
resource and `app=ui.app_config(ui.FOREST_UI_URI)` on the tool; four UI-wiring tests including one
that pins the tool's `_meta["ui"].resourceUri` through `mcp.get_tool`. Handoff note:
`cross_study_phase_b_notes.md`. Deviations from §6: badges are the short tokens `overlap`,
`n < 10`, `not on panel` (the partner study lives in the tooltip and warnings); the preview path
bakes in four *real* captured payloads (`?case=example|many|overlap|notcovered`, plus `error`)
rather than synthetic data; the fixed-effect estimate is drawn as a dashed reference diamond under
the random-effects one. Rendered and checked in headless Chromium in both themes.

**Phase C — done (2026-09-03).** Routing edits in `statistical-tests-guide.md` (study-vs-study row,
scope limits, covered/uncovered examples, template, forbidden shapes), `mutation-frequency-guide.md`
("Across named studies" section), `system-prompt.md`, `study-resolution-guide.md` ("Same Cohort,
Several Releases"), `common-pitfalls.md`; manual tests 5.2 and 7.11; a guide-coverage test; and the
optional `sql/6-cross-study-views.sql` (`gene_alteration_counts_per_study`), whose body was verified
live to reproduce the tool's counts. Handoff note: `cross_study_phase_c_notes.md`. **Still open:** the
live-host manual test (needs a host connected to a server running this branch), the R fixture, and
the `frontend/` gitignore decision.

Live reconciliation (§8 checklist), executed twice on 2026-09-03 — by replaying the tool's own SQL
through the connected MCP, then directly against ClickHouse Cloud with the real tool — with
byte-identical payloads. `tests/test_cross_study_live.py` keeps five of these runnable (skipped
without `CLICKHOUSE_HOST`). A live-only run over `all_studies_non_redundant` also confirmed the
`CANCER_TYPE_DETAILED` fallback: `pan_origimed_2020` (no `ONCOTREE_CODE`) contributes 1,572 LUAD
samples that an OncoTree-only query misses.

| Check | Result |
|---|---|
| TCGA LUAD + MSK-CHORD, TP53, `LUAD` | 295 / 566 = 52.1% and 2,695 / 5,957 = 45.2%; pooled 48.4% [41.7, 55.1], I² 89.8%, chi-square p = 0.0017; no overlap ✅ |
| MSK-CHORD, `cohort={"CANCER_TYPE": ["Non-Small Cell Lung Cancer"]}` | 4,005 / 7,809 = 51.3%, equal to the view's NSCLC row ✅ |
| `preference="pan_cancer_tcga"`, `LUAD` | one row (`luad_tcga_pan_can_atlas_2018`), 31 in `studies_without_cohort` ✅ |
| `luad_tcga` + `luad_tcga_pan_can_atlas_2018` | 564 shared patients; `luad_tcga` (106 / 230 profiled) marked `overlap`, pooling blocked ✅ |
| `msk_chord_2024` + `msk_impact_50k_2026`, `LUAD` | 4,622 shared patients *within the LUAD cohorts* (the 19,567 figure in §4.4 is study-wide); MSK-CHORD marked `overlap`, pooling blocked ✅ |
| the 16 LUAD-bearing non-redundant studies (explicit ids) | 16 rows, no overlap, `lung_smc_2016` below `min_profiled`, pooled over 15: 46.9% [41.2, 52.7], I² 89.2% ✅ |
| `msk_chord_2024` alone, no `cancer_type` | 13,124 / 25,040 = 52.4% with the warning "spans 5 cancer types" ✅ |

## 1. Why the server cannot answer this today

1. **Every data app takes exactly one `study_id`.** `_validate_study_id` rejects anything else, and
   `survival_curve` errors on a comma-separated list (gap analysis: 87 cross-study prompts hit this).
2. **The SQL recipes bucket on the broad `CANCER_TYPE` only.** In `msk_chord_2024`, "Lung
   Adenocarcinoma" lives in `CANCER_TYPE_DETAILED` / `ONCOTREE_CODE = LUAD` — 5,957 of the 7,809 samples
   whose `CANCER_TYPE` is "Non-Small Cell Lung Cancer". `gene_mutation_frequency_in_study(study =
   'msk_chord_2024', gene = 'TP53')` therefore returns the NSCLC bucket, 4,005 / 7,809 = 51.3%, which is a
   confident answer for the wrong cohort (squamous, carcinoid and NSCLC-NOS included). Nothing in the
   server can narrow it to LUAD.
3. **`gene_mutation_frequency_in_studies` merges the studies into one bucket** — no per-study rows — and
   has no overlap protection ("you are responsible for non-overlap").
4. **The statistical-tests guide declares cohort-vs-cohort comparison UNCOVERED**, so even after two
   correct per-study queries the model must hand the comparison off to R. The mutation-frequency guide
   (rightly) forbids summing across studies. Net effect: today's best answer is two numbers, no
   comparison, no pooled estimate.

What the correct answer looks like (prototype query from §4, run today):

| Study | LUAD cohort | Profiled for TP53 | TP53-mutated | Frequency | Wilson 95% CI |
|---|---:|---:|---:|---:|---|
| `luad_tcga_pan_can_atlas_2018` (WES) | 566 samples | 566 | 295 | 52.1% | 48.0–56.2 |
| `msk_chord_2024` (IMPACT 341/410/468/505) | 5,957 samples | 5,957 | 2,695 | 45.2% | 44.0–46.5 |

Patient-level counts are identical here (one sample per patient in both LUAD cohorts); they diverge in
e.g. `msk_impact_50k_2026` (6,254 LUAD samples from 5,224 patients), which is why the tool returns both.

## 2. Design principles

These come straight from the repo's existing rules (`AGENTS.md`, the guides, the cohort-filter and
provenance work) and decide most of the design below.

- **Per-study first, pooled second, never summed.** The headline is one row per study with its own
  panel-aware denominator. A pooled value is a *random-effects meta-analytic proportion* with a CI and
  heterogeneity statistics, disclosed as such — not `SUM(altered) / SUM(profiled)`.
- **The cohort is always disclosed.** Same reasoning as `_cohort_block`: an answer for the wrong cohort
  is only invisible while the payload declines to say which cohort it used. Every study row states the
  resolution key it matched on (`ONCOTREE_CODE`, `CANCER_TYPE_DETAILED`, or whole-study).
- **Denominators are per study, per gene, panel ∪ WES.** Reuse the `mutation_panel_gene_coverage` +
  `mutation_wes_coverage` views (the >100% trap is already solved there). A study whose panels do not
  include the gene is an explicit "not covered" row, never a silent zero.
- **Refuse to pool overlapping studies.** MSK-CHORD shares 19,567 patients with MSK-IMPACT-50k; the four
  TCGA LUAD versions share 227–566 patients pairwise. Overlap is detected from the data (§4.4), per-study
  rows are still returned, and only the pooled block is withheld.
- **Statistics come from the tool, never the model.** The hard rules in `statistical-tests-guide` mean
  the tool must compute the CIs, the pooled estimate, heterogeneity and the difference test itself, in
  pure stdlib Python like `survival_stats.py` / `cooccurrence_stats.py` (no scipy).
- **Context efficiency.** One grouped SQL query returns *k* rows of counts. No sample-ID sets cross into
  Python (unlike the single-study cohort helpers, which pull ID sets — fine for one study, not for 241).
- **Provenance.** Wrap the build in `_with_provenance`; the SQL that produced the numbers ships with them.
- **Same app pattern.** Tool + `ui://` resource + Vite-built self-contained widget, error contract
  `{"error": …, "kind": …}`, DB-free tests that monkeypatch `server.run_select_query`.

## 3. Tool contract

```python
cross_study_alteration_frequency(
    gene: str,                                   # HUGO symbol, validated by _validate_gene_symbol
    studies: list[str] | None = None,            # explicit study ids (≥ 1), validated one by one
    preference: str | None = None,               # named cohort from cancer_study_query_preferences
                                                 #   (e.g. "pan_cancer_tcga"); unioned with `studies`
    cancer_type: str | list[str] | None = None,  # OncoTree code(s), e.g. "LUAD"; resolved per sample
    include_subtypes: bool = True,               # expand codes to OncoTree descendants (oncotree.json)
    cohort: dict[str, list[str]] | None = None,  # same v1 predicate as the other apps, applied inside
                                                 #   every study and ANDed with cancer_type
    alteration: str = "mutation",                # mutation | amplification | deep_deletion | structural_variant
    unit: str = "sample",                        # sample | patient — which grain drives stats + chart
    min_profiled: int = 10,                      # rows below this are shown but not pooled/tested
    pool: bool = True,                           # compute the pooled estimate when it is safe to
) -> dict
```

Decisions baked into the signature:

- **Studies come in as ids, resolved by the model** with `list_studies` / `search_oncotree`, exactly as
  today. The tool does not guess what "TCGA" means — but its overlap guard catches the classic mistake of
  passing two TCGA versions of the same cohort.
- **`cancer_type` is an OncoTree code**, not free text. `ONCOTREE_CODE` is present as a per-sample
  attribute in 518 of the 545 studies with clinical data, with no partially-annotated study, so it is the
  one cross-study key that actually harmonises. Fallback chain per study (§4.2): `ONCOTREE_CODE` →
  `CANCER_TYPE_DETAILED` equals the OncoTree *name* → study-level `cancer_study.type_of_cancer_id`.
- **`include_subtypes=True`** so `cancer_type="NSCLC"` expands to LUAD, LUSC, LUAS, … from the bundled
  `oncotree.json` (`parent` links; `_build_hierarchy_path` already walks them upward). `LUAD` has no
  children, so the example is unaffected.
- **Single `alteration` token** in v1, mirroring `gene_alteration_frequency_by_cancer_type`. "Any of
  several alteration types" needs a multi-profile denominator convention and is deferred (§10).
- **`unit="sample"` by default**, because that is what cBioPortal's own study view, the shipped SQL views
  and the other apps report — so the raw-SQL path reconciles by default. Both sample and patient counts
  are always in the payload; the tool description tells the model to pass `unit="patient"` for
  prevalence / "fraction of patients" wording, per the counting-unit rule in the mutation-frequency guide.
- **`min_profiled=10`** rather than the views' 50: a meta-analysis weights small studies down anyway, so
  they are shown with a flag and excluded from pooling/tests rather than hidden.

### Payload

```jsonc
{
  "kind": "cross_study_frequency",
  "gene": "TP53", "alteration": "mutation", "unit": "sample",
  "cancer_type": {"requested": ["LUAD"], "codes": ["LUAD"], "names": ["Lung Adenocarcinoma"]},
  "filter": null,                                  // the generic cohort predicate, echoed back
  "studies": [
    {
      "study_id": "luad_tcga_pan_can_atlas_2018", "name": "Lung Adenocarcinoma (TCGA, PanCancer Atlas)",
      "cohort_key": "ONCOTREE_CODE",               // how the cancer-type filter was matched in this study
      "samples":  {"cohort": 566,  "profiled": 566,  "altered": 295},
      "patients": {"cohort": 566,  "profiled": 566,  "altered": 295},
      "frequency_pct": 52.1, "ci95": [48.0, 56.2],  // on `unit`
      "weight_pct": 45.8,                          // random-effects weight; null when not pooled
      "panels": ["WES"],
      "status": "included"                         // included | below_min_profiled | not_covered | overlap
    },
    { "study_id": "msk_chord_2024", "...": "...", "frequency_pct": 45.2, "ci95": [44.0, 46.5],
      "weight_pct": 54.2, "panels": ["IMPACT468", "IMPACT505", "IMPACT410", "IMPACT341"] }
  ],
  "studies_without_cohort": [],                    // requested studies with zero matching samples
  "pooled": {
    "method": "random_effects_dersimonian_laird", "k": 2,
    "frequency_pct": 48.4, "ci95": [41.7, 55.1],
    "fixed_effect_pct": 45.8, "fixed_effect_ci95": [44.6, 47.1],
    "n_altered": 2990, "n_profiled": 6523          // crude totals, labelled as such, never the headline
  },
  "heterogeneity": {"q": 9.81, "df": 1, "p_value": 0.0017, "i2_pct": 89.8, "tau2": 0.034},
  "difference_test": {"test": "chi_square_homogeneity", "statistic": 9.85, "df": 1, "p_value": 0.0017},
  "overlap": {"checked": true, "pairs": [], "pooling_blocked": false},
  "warnings": ["High heterogeneity (I² = 90%): …design differences…"],
  "notes": ["Counting unit: samples. …", "Pooled estimate is a random-effects meta-analytic proportion, not a sum."],
  "provenance": {"queries": ["…"], "server_version": "0.1.0"}
}
```

`pooled`, `heterogeneity` and `difference_test` are `null` (with a note) when fewer than two studies
survive `min_profiled`, when `pool=False`, or when overlap was detected. Error returns keep the shape:
`{"error": …, "kind": "cross_study_frequency", "gene": …, "studies": []}`.

## 4. Data layer

Four small queries, all issued through `_query` so they are recorded in provenance. No new DDL is
required: everything builds on the views `sql/4-mutation-frequency-views.sql` already ships, so the tool
works the moment the server deploys (the other four apps have the same dependency). A companion
parameterized view for raw-SQL users is an optional Phase C item.

### 4.1 Study resolution (1 query)

`SELECT cancer_study_identifier, name, type_of_cancer_id FROM cancer_study WHERE cancer_study_identifier
IN (…)` ∪ the members of `preference`. Unknown ids → error listing them (do not silently drop). Also
fetch, per study, whether `ONCOTREE_CODE` / `CANCER_TYPE_DETAILED` rows exist (one `uniqExactIf` per
attribute, grouped by study) to pick the cohort key.

### 4.2 Cancer-type resolution (Python, no query)

Validate every code against `oncotree.json` (error with `search_oncotree` suggestions if unknown), expand
descendants when `include_subtypes`, collect the OncoTree names of the expanded set. Per study, choose:

| Study has… | Cohort predicate | `cohort_key` |
|---|---|---|
| per-sample `ONCOTREE_CODE` (518 / 545 studies) | `attribute_name = 'ONCOTREE_CODE' AND upper(attribute_value) IN (codes)` | `ONCOTREE_CODE` |
| only `CANCER_TYPE_DETAILED` (12 studies) | `attribute_name = 'CANCER_TYPE_DETAILED' AND upper(attribute_value) IN (upper(names))` | `CANCER_TYPE_DETAILED` |
| neither (15 studies) and `type_of_cancer_id` ∈ codes | whole study | `STUDY_TYPE` |
| neither and no match | listed in `studies_without_cohort` with reason | — |

With no `cancer_type` the cohort is the whole study; reuse the `_study_cancer_type_spread` idea (batched
into one grouped query) to warn per study when it spans more than one cancer type — the MSK-CHORD
"answered for 24k patients" failure must not come back through this tool.

### 4.3 Counts (1 grouped query — the core)

Prototype, verified today to reproduce the existing view exactly (295 / 566 for TCGA LUAD):

```sql
WITH cohort AS (                                   -- one UNION ALL branch per cohort_key group (§4.2)
    SELECT cancer_study_identifier, sample_unique_id, patient_unique_id
    FROM clinical_data_derived
    WHERE cancer_study_identifier IN ('msk_chord_2024', 'luad_tcga_pan_can_atlas_2018')
      AND attribute_name = 'ONCOTREE_CODE'
      AND upper(attribute_value) IN ('LUAD')
      -- AND sample_unique_id IN (SELECT sample_unique_id FROM clinical_data_derived WHERE … ) for `cohort`
),
profiled AS (                                      -- panel ∪ WES, per alteration token
    SELECT sample_unique_id FROM mutation_panel_gene_coverage
    WHERE hugo_gene_symbol = 'TP53' AND cancer_study_identifier IN (…)
    UNION ALL
    SELECT sample_unique_id FROM mutation_wes_coverage WHERE cancer_study_identifier IN (…)
),
altered AS (
    SELECT DISTINCT sample_unique_id FROM genomic_event_derived
    WHERE cancer_study_identifier IN (…) AND hugo_gene_symbol = 'TP53'
      AND variant_type = 'mutation' AND mutation_status != 'UNCALLED' AND off_panel = 0
)
SELECT c.cancer_study_identifier AS study,
       uniqExact(c.sample_unique_id)                                                   AS cohort_samples,
       uniqExactIf(c.sample_unique_id,  p.sample_unique_id != '')                      AS profiled_samples,
       uniqExactIf(c.sample_unique_id,  p.sample_unique_id != '' AND a.sample_unique_id != '') AS altered_samples,
       uniqExact(c.patient_unique_id)                                                  AS cohort_patients,
       uniqExactIf(c.patient_unique_id, p.sample_unique_id != '')                      AS profiled_patients,
       uniqExactIf(c.patient_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '') AS altered_patients
FROM cohort c
LEFT JOIN profiled p USING (sample_unique_id)
LEFT JOIN altered  a USING (sample_unique_id)
GROUP BY study
```

Two ClickHouse traps found while prototyping, both worth a test:

- `LEFT JOIN` fills non-matches with `''`, not `NULL`, so `COUNT(DISTINCT CASE WHEN … THEN a.sample_unique_id END)`
  counts the empty string as one extra distinct value (the prototype's first run reported 296 instead
  of 295). Use `uniqExactIf(col, joined_col != '')`.
- `uniq()` is approximate; use `uniqExact` / `uniqExactIf` (what `COUNT(DISTINCT)` maps to).

For `amplification` / `deep_deletion` / `structural_variant`, `profiled` switches to the
`sample_to_gene_panel_derived` panel ∪ WES branch with the matching `alteration_type`, exactly as
`gene_alteration_frequency_by_cancer_type` does; `altered` uses `ALTERATION_CONFIGS[…]["event_filter"]`.

`altered` is intersected with `profiled` (the `p.sample_unique_id != ''` guard) so an off-panel-but-called
event can never push a study over 100%. `profiled = 0` with `cohort > 0` is the "gene not covered by this
study's panels" state. The 241-study `all_studies_non_redundant` version of this query returned promptly
today (16 studies carry LUAD samples), so a `preference` of any shipped size is fine.

### 4.4 Overlap probe (1 query, restricted to the cohort)

Sample and patient ids are study-prefixed (`<study>_<stable_id>`); stripping the prefix recovers the
institutional id, which is stable across MSK releases (`P-0001234`) and TCGA versions (`TCGA-05-4244`):

```sql
SELECT studies, count() AS shared_ids
FROM (
    SELECT pid, arraySort(groupUniqArray(cancer_study_identifier)) AS studies
    FROM (SELECT replaceOne(patient_unique_id, concat(cancer_study_identifier, '_'), '') AS pid,
                 cancer_study_identifier
          FROM cohort)                              -- same cohort CTE as §4.3
    GROUP BY pid
    HAVING length(studies) > 1
)
GROUP BY studies
```

Run once for patients and once for samples. Verified behaviour: `msk_chord_2024` ↔ `msk_impact_50k_2026`
share 19,411 samples / 19,567 patients; `luad_tcga` ↔ `luad_tcga_pan_can_atlas_2018` share 564 patients;
the 16 LUAD-bearing members of `all_studies_non_redundant` share 0. Any shared id blocks pooling and
marks the smaller study's row `status: "overlap"`; per-study numbers stay. This is a heuristic — generic
ids like `1`, `2` could collide between unrelated studies (false positive → over-cautious), and re-identified
samples escape it (false negative) — so the payload also notes whether every requested study is in
cBioPortal's curated `all_studies_non_redundant` set, which stays the authoritative vetting.

### 4.5 Panels + sample-type mix (1 query, for the notes)

`groupUniqArray(gene_panel_id)` per study from `sample_to_gene_panel_derived`, and the `SAMPLE_TYPE`
distribution of the cohort. Today's LUAD cohorts: TCGA is 566 Primary; MSK-CHORD is 3,715 Primary /
2,133 Metastasis / 80 Unknown / 29 Local Recurrence. This is what makes the heterogeneity warning concrete
instead of generic.

## 5. Statistics — `src/cbioportal_mcp/meta_stats.py`

Pure stdlib, dependency-free, reusing `survival_stats.chi_square_sf` / `normal_ppf` and
`cooccurrence_stats.fisher_exact_two_sided`.

| Function | Formula | Used for |
|---|---|---|
| `wilson_interval(a, n, z=1.96)` | centre `(p + z²/2n) / (1 + z²/n)`, half-width `z·√(p(1−p)/n + z²/4n²) / (1 + z²/n)` | per-study CI (never the Wald interval — it breaks at 0/100%) |
| `pooled_proportion(studies)` | logit scale: `yᵢ = ln(aᵢ/(nᵢ−aᵢ))`, `vᵢ = 1/aᵢ + 1/(nᵢ−aᵢ)` (½ continuity correction when `aᵢ ∈ {0, nᵢ}`); fixed effect `ȳ = Σwᵢyᵢ/Σwᵢ`, `wᵢ = 1/vᵢ`; `Q = Σwᵢ(yᵢ−ȳ)²`, `df = k−1`, `C = Σwᵢ − Σwᵢ²/Σwᵢ`, `τ² = max(0, (Q−df)/C)`; random effects `wᵢ* = 1/(vᵢ+τ²)`, `ȳ* = Σwᵢ*yᵢ/Σwᵢ*`, `SE = √(1/Σwᵢ*)`; back-transform with expit | `pooled` block + per-study `weight_pct` |
| `heterogeneity(Q, df)` | `I² = max(0, (Q−df)/Q)·100`, `p = chi_square_sf(Q, df)` | `heterogeneity` block |
| `homogeneity_test(studies)` | k×2 chi-square against the crude pooled proportion, `df = k−1`; if k = 2 and any expected cell < 5, `fisher_exact_two_sided` instead | `difference_test` ("is the frequency different between these studies?") |

DerSimonian–Laird on the logit scale is the standard default for proportions (what R's
`meta::metaprop(sm = "PLOGIT")` and `metafor::rma(measure = "PLO")` do); it is a few dozen lines and has
closed-form everything, which matters because nothing here may iterate to convergence in a tool call.

Worked numbers for the example (already computed with the repo's own `chi_square_sf`, so the test fixture
is ready): fixed 45.8% [44.6, 47.1]; random 48.4% [41.7, 55.1]; Q = 9.805, df = 1, p = 0.0017; I² = 89.8%;
τ² = 0.034; RE weights 45.8 / 54.2; chi-square homogeneity 9.853, p = 0.0017; Fisher p = 0.0020.

Guardrails the module enforces rather than the caller: `k < 2` → no pooling; a study with `n = 0` is
rejected before it reaches the formulas; identical inputs give `Q = 0`, `τ² = 0`, RE == FE (a test).

Add a `tests/test_meta_stats.py` fixture computed in R (`metafor::rma(ai=…, ni=…, measure="PLO",
method="DL")`) for a 5-study case so the implementation is checked against an external reference, not
just against itself — the same standard `survival_stats` met with the Freireich dataset.

## 6. Widget — forest plot (`ui://cbioportal/forest`)

Same wiring as the other four: `ui.FOREST_UI_URI`, `ui.app_config(ui.FOREST_UI_URI)` (no CSP — fully
self-contained), `@mcp.resource` serving `resources/widgets/forest.html`, built by Vite +
`vite-plugin-singlefile` from a new `frontend/forest/` project using the ext-apps `App` SDK
(`app.ontoolresult` → render). Custom inline SVG, no charting library.

Layout (one row per study, sorted by weight desc):

```
Study                                 altered/profiled   %  [95% CI]           weight
TCGA LUAD PanCan 2018 (WES)              295/566        52.1 [48.0, 56.2]     45.8%   ──■──
MSK-CHORD 2024 (IMPACT)                 2695/5957       45.2 [44.0, 46.5]     54.2%     ▪
──────────────────────────────────────────────────────────────────────────────────────
Random effects (DL)                     2990/6523*      48.4 [41.7, 55.1]              ◆
Heterogeneity: Q = 9.81 (df 1), p = 0.0017, I² = 90%      * crude totals, not the estimate
```

- Square area ∝ random-effects weight; whiskers = Wilson CI; diamond = pooled RE estimate with CI;
  dashed vertical line at the pooled value; x-axis 0–100% (auto-zoomed when all CIs sit in a narrow band).
- Row states: `not_covered` → grey row, "gene not on this study's panels" badge, no marker;
  `below_min_profiled` → hollow marker + "n < 10" badge, not in pooling; `overlap` → struck-through with
  "overlaps with <study>" badge; when pooling is blocked the diamond row is replaced by the reason.
- Footer carries the counting unit and the difference-test line; `warnings[]` renders exactly as the other
  widgets do (the disclosure text flows through without a widget rebuild).
- Host light/dark theming, hover tooltip with the raw counts, `?preview=1` bakes in the LUAD example for
  headless-Chromium QA in both themes.

Reminder from the earlier widget work: `frontend/` is gitignored, so the widget *source* only exists on
the developer's machine while the built bundle is committed. `frontend/forest/` will be the sixth such
tree; whether to un-ignore them is still an open call.

## 7. Guides, prompt and routing

Following the fix hierarchy in `AGENTS.md` (data → prompt → guide):

1. **`statistical-tests-guide.md`** — add a routing-table row: *cohort-vs-cohort / study-vs-study
   alteration frequency: per-study Wilson CIs, random-effects pooled proportion, Q / I² / τ², chi-square
   (or Fisher) difference test → `cross_study_alteration_frequency`*. Rewrite the "Scope limit" paragraph
   and the "Example: Building a Contingency Table" section, which today teach the model to hand-build the
   two-study 2×2 and hand off — that shape is now COVERED. Add the pooled-estimate wording to the Approved
   Response Templates and "a pooled frequency that is `SUM/SUM`" to the Forbidden Shapes.
2. **`mutation-frequency-guide.md`** — new subsection under Cross-Cancer-Type: *"Across named studies /
   all studies of one cancer type"* → call the tool; keep the "never sum across studies" rule and explain
   that the pooled value is a meta-analytic estimate. Point the `gene_mutation_frequency_in_studies`
   variant at the tool as the safer default.
3. **`system-prompt.md`** — one routing bullet: *"across studies X and Y", "compare study A with study B",
   "in all lung adenocarcinoma studies", "TCGA vs MSK" → `cross_study_alteration_frequency` after
   resolving the cancer type with `search_oncotree` and the studies with `list_studies`*. Note that "TCGA"
   for one disease resolves to the `*_tcga_pan_can_atlas_2018` study by default (the guide's existing
   preference) and that the four TCGA versions of a cohort overlap.
4. **`study-resolution-guide.md`** — a short "same cohort, several releases" paragraph (TCGA Firehose /
   PanCan / GDC / publication; MSK-CHORD ⊂ MSK-IMPACT-50k) so the model picks one release on purpose.
5. **`tests/MANUAL_TOOL_TESTS.md`** — new section with the target prompt and pass criteria (§8).

## 8. Tests and verification

DB-free unit tests, in the house style (`tests/test_cooccurrence.py` / `test_cohort_filter.py`: fake
`run_select_query` dispatching on SQL text):

- `tests/test_meta_stats.py` — Wilson against known intervals; DL pooling against the §5 numbers and an R
  fixture; `a = 0` / `a = n` continuity correction; identical studies → `Q = 0`, RE == FE; `k = 1` → no
  pooling; chi-square vs Fisher selection at expected cell < 5.
- `tests/test_cross_study.py` — per-study rows with the right counts on both grains; `unit` switch changes
  which grain drives `frequency_pct` / stats; cohort-key fallback (ONCOTREE_CODE → CANCER_TYPE_DETAILED →
  whole study) is reflected in `cohort_key`; `include_subtypes` expands NSCLC and rejects unknown codes
  with suggestions; unknown study ids error rather than vanish; `not_covered` when profiled = 0;
  `below_min_profiled` excluded from pooling; an overlap row blocks pooling and is disclosed; `preference`
  membership is unioned with `studies`; the generic `cohort` predicate narrows both numerator and
  denominator (the trap `test_cohort_filter.py` exists for); error contract and `provenance` present on
  every return path; the emitted SQL uses `uniqExactIf` guards (regression for the `''` trap).
- Extend `tests/test_guide_layer_issue_coverage.py` so the three guides and the system prompt mention the
  tool.

Live reconciliation checklist (run once against the deployment before calling Phase A done):

| Check | Expected |
|---|---|
| TCGA LUAD, TP53, `cancer_type="LUAD"` | 295 / 566 = 52.1%, equal to `gene_mutation_frequency_in_study` |
| MSK-CHORD, TP53, `cohort={"CANCER_TYPE": ["Non-Small Cell Lung Cancer"]}` | 4,005 / 7,809 = 51.3%, equal to the view's NSCLC row |
| `preference="pan_cancer_tcga"`, `cancer_type="LUAD"` | exactly one row: `luad_tcga_pan_can_atlas_2018`; 31 studies in `studies_without_cohort` |
| `studies=["luad_tcga", "luad_tcga_pan_can_atlas_2018"]` | overlap pair with 564 shared patients, pooling blocked |
| `studies=["msk_chord_2024", "msk_impact_50k_2026"]` | 19,567 shared patients, pooling blocked |
| `preference="all_studies_non_redundant"`, `cancer_type="LUAD"` | 16 rows, no overlap, pooled block present |
| `studies=["msk_chord_2024"]`, no `cancer_type` | warning that the study spans many cancer types |

Manual test (for `MANUAL_TOOL_TESTS.md`): the target prompt should cascade `search_oncotree` (LUAD) →
`list_studies` (msk_chord_2024, luad_tcga_pan_can_atlas_2018) → guide read → the tool → an answer that
gives both per-study rates with counts, the difference-test p-value from the payload, the pooled estimate
labelled as random-effects with its CI and I², and the design caveat. Fail if it sums, if it reports the
NSCLC bucket, or if it hands the comparison off to R.

## 9. Worked example — the target prompt end to end

1. `search_oncotree("lung adenocarcinoma")` → `LUAD` (hierarchy LUNG > NSCLC > LUAD).
2. `list_studies("MSK-CHORD")` → `msk_chord_2024`; "TCGA" for LUAD → `luad_tcga_pan_can_atlas_2018` (the
   guide's default release; `luad_tcga`, `luad_tcga_gdc`, `luad_tcga_pub` are other releases of the same
   patients).
3. `cross_study_alteration_frequency(gene="TP53", studies=["msk_chord_2024",
   "luad_tcga_pan_can_atlas_2018"], cancer_type="LUAD")`.
4. Payload → forest plot, and an answer of this shape:

> TP53 is mutated in **52.1% (295 / 566)** of TCGA PanCancer LUAD samples and **45.2% (2,695 / 5,957)** of
> MSK-CHORD LUAD samples (counting unit: samples; denominators are samples profiled for TP53 — WES in TCGA,
> IMPACT panels in MSK-CHORD). The difference is statistically significant (chi-square homogeneity test,
> p = 0.0017). A random-effects pooled estimate is **48.4% (95% CI 41.7–55.1)**, but heterogeneity is high
> (I² = 90%), which is expected: MSK-CHORD is clinical panel sequencing that includes 2,133 metastatic
> samples, while TCGA is exome sequencing of primary, treatment-naive tumours — so the per-study rates are
> the better headline. Sample IDs are study-prefixed and the two cohorts share no patients.

## 10. Phases

| Phase | Scope | Done when |
|---|---|---|
| **A — core** | `meta_stats.py` + tests; tool, SQL (§4), cancer-type resolution, overlap guard, payload, `_with_provenance`; `tests/test_cross_study.py`; live reconciliation checklist | suite green (currently 283 passed, 2 pre-existing `test_survival_curve` failures); the checklist in §8 matches |
| **B — widget** | `frontend/forest/` → `resources/widgets/forest.html`; `ui.FOREST_UI_URI`; `?preview=1`; both themes verified in headless Chromium; `test_cross_study.py` asserts the `ui://` linkage like `test_cooccurrence.py` does | renders the LUAD example offline; tool `_meta["ui"]` points at the resource |
| **C — routing** | guide / prompt edits (§7), `MANUAL_TOOL_TESTS.md`, guide-coverage tests; optional `sql/6-cross-study-views.sql` with `gene_alteration_counts_per_study(studies, gene, alteration, oncotree_codes)` for raw-SQL parity | the target prompt passes the manual test in a live host |
| **Later** | multi-gene (`genes` list ≤ `MAX_ANALYSIS_GENES`, one forest panel per gene); "any alteration" multi-profile denominators; pairwise BH-corrected Fisher for k > 2; `studies` on `survival_curve` with study-stratified log-rank, reusing the same study-set resolver and overlap guard | — |

Each phase is one handoff note (`cross_study_phase_<x>_notes.md`), no commits unless asked.

## 11. Risks, open questions, non-goals

- **Design heterogeneity is the real story, not a nuisance.** Panel vs WES, metastatic vs primary,
  clinical vs research calling pipelines. The tool cannot correct for this; it can only make it visible
  (panels, sample-type mix, I²) and keep the per-study rows as the headline. The warning text should say
  *why* the studies differ, not just that I² is high.
- **Overlap detection is a heuristic** (§4.4). Curated non-redundant membership is reported alongside it.
- **Study-level fallback** (`STUDY_TYPE`) treats a whole single-cancer-type study as the cohort; for a
  study typed `luad` that is right, but mixed studies with no per-sample code (15 of 545) simply cannot be
  filtered and are reported as such.
- **Very small cohorts** produce wide Wilson intervals and near-zero weight; `min_profiled` keeps them
  out of the pooling but they still appear, so nothing is hidden.
- **Naming.** `cross_study_alteration_frequency` is descriptive next to `alteration_cooccurrence`; the
  widget is `forest`. Rename if a shorter name reads better in the tool list.
- **Not in scope:** cross-study survival or expression (needs different statistics), driver/OncoKB
  filtering, deep links back to cbioportal.org, virtual-study creation.
