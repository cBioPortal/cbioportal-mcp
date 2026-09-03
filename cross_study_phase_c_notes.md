# Cross-study meta-analysis — Phase C handoff notes (2026-09-03)

Phase C of `docs/cross-study-meta-analysis-plan.md`: routing the model to
`cross_study_alteration_frequency` through the guides, the system prompt, the manual test suite and
the guide-coverage tests, plus the optional raw-SQL view. Nothing is committed.

## What changed

| File | Change |
|---|---|
| `resources/statistical-tests-guide.md` | Routing-table row for **study-vs-study** frequency (per-study Wilson CIs, DerSimonian–Laird pooled estimate, Q / I² / τ², chi-square or Fisher difference test). The "Scope limit" paragraph now says what is covered (one gene across studies) and what is not (two cohorts *inside* one study). Rule 1 gets the cross-study bullet; the old "uncovered" example — which taught a two-study 2×2 with a study-wide denominator — is replaced by a covered example that calls the tool and an uncovered within-study example whose SQL uses the panel ∪ WES denominator and the `!= ''` guards (verified live on `msk_chord_2024`: Primary 8,104 / 7,824, Metastasis 4,890 / 3,988). New approved template with the worked LUAD numbers; two new forbidden shapes (a `SUM/SUM` across studies; an untested "differ significantly"); one new "refusing what the server can compute" shape; two pitfall bullets. |
| `resources/mutation-frequency-guide.md` | Top-level bullet routing "across named studies" to the tool; a lead-in on the `gene_mutation_frequency_in_studies` variant pointing at the tool; a new **"Across named studies (`cross_study_alteration_frequency`)"** section describing the call and how to report each payload block (rows are the headline, `not_covered` is never 0%, pooled is not `SUM/SUM`, `difference_test.p_value` is quoted not computed, overlap semantics, warnings carried over, "TCGA" = PanCancer Atlas); a "DO NOT combine per-study frequencies by hand" bullet. |
| `resources/system-prompt.md` | Routing sub-bullet under mutation-frequency questions ("across studies X and Y", "compare study A with study B", "in all lung adenocarcinoma studies", "TCGA vs MSK" → `search_oncotree` → `list_studies` → the tool); the tool added to the statistical routing list, the hard-rule paragraph and rule 8; "within-study" qualifier on the cohort-vs-cohort handoff. Every phrase the existing prompt tests assert is untouched. |
| `resources/study-resolution-guide.md` | New routing trigger and a **"Same Cohort, Several Releases"** section (TCGA Firehose / pub / PanCancer / GDC; MSK-CHORD inside MSK-IMPACT-50k) with the pick rule and the overlap-guard explanation. Kept under the 500-word cap the existing test enforces (494). |
| `resources/common-pitfalls.md` | Pitfall 19 "these have tools" gains the cross-study forest plot; best practice 22; a checklist line. |
| `tests/MANUAL_TOOL_TESTS.md` | 18 tools; new **5.2** (the target prompt, with pass/fail criteria and the 2026-09-03 numbers) and **7.11** (two TCGA releases → overlap, no pooled number); coverage map updated. |
| `tests/test_guide_layer_issue_coverage.py` | `test_guides_and_prompt_route_cross_study_questions_to_the_tool`: every routed document names the tool; the study-vs-study row, the random-effects wording, the `SUM/SUM` forbidden shape and the within-study scope note are present; the new mutation-guide section, the releases section and the `*_tcga_pan_can_atlas_2018` rule exist. |
| `sql/6-cross-study-views.sql` (+ `sql/README.md`, `AGENTS.md`) | **Optional raw-SQL parity**: parameterized view `gene_alteration_counts_per_study(studies, gene, alteration, oncotree_codes)` returning the per-study cohort / profiled / altered counts on both grains with the panel ∪ WES denominator (`oncotree_codes = []` takes each study whole). The tool does **not** depend on it. |

Run: `uv run --python 3.12 --extra dev pytest -q` → 348 passed, 5 skipped (live tests without
credentials), 2 failed (the pre-existing `test_survival_curve.py` pair).

## Verification

- **Guide-coverage tests** pass, including the pre-existing ones that pin exact prompt phrases and
  the 500-word cap on the targeted guides.
- **The SQL view body** was executed live with literal parameters (the read-only `llm_user` cannot
  `CREATE VIEW`, so the view itself is not created here — the daily clone applies `sql/`):
  LUAD/TP53 → 5,957 / 2,695 and 566 / 295; `oncotree_codes = []` on MSK-CHORD → 25,040 / 13,124;
  MYC amplification LUAD → 511 / 43 and 5,957 / 310. All equal to the tool's numbers.
- **Not done here: the live-host manual test (5.2 / 7.11).** It needs a chat host connected to a
  server running this branch; the remote cBioPortal MCP this session can reach is the previous
  deployment without the tool. That is the plan's "done when" for Phase C and is the one step left
  for you: deploy (or `fastmcp dev apps src/cbioportal_mcp/server.py` with a host) and paste 5.2.

## Decisions worth remembering

- **The old contingency-table example was wrong twice**: it was the exact shape the tool now covers,
  and its SQL divided by all samples in the study (the >100% trap). Rather than delete it, the
  guide now shows the covered call *and* a correct within-study example, so the model still has a
  hand-off recipe for the shape that stays uncovered.
- **"Within one study" is the scope boundary** everywhere the guides talk about cohort-vs-cohort:
  study-vs-study is the tool's job, two clinical cohorts inside one study still hands off.
- **The prompt tells the model what "TCGA" means** (the PanCancer Atlas release) at the point of
  routing, and the study-resolution guide backs it with the releases table, because the overlap
  guard only helps if the model passes both releases — it cannot help if it silently picks
  `luad_tcga`.
- **The SQL view mirrors the tool's query, not the guide's CTE form**, so the two cannot drift on
  denominator rules; the plan's optional signature was kept (`studies, gene, alteration,
  oncotree_codes`).

## Open items

1. Live-host manual test 5.2 / 7.11 (above).
2. R `metafor` fixture (Phase A), `frontend/` gitignore (Phase B), live-host widget check (Phase B).
3. The view in `sql/6-cross-study-views.sql` will only exist after the next clone applies it;
   until then the guide's raw-SQL cross-check has to inline the query.
