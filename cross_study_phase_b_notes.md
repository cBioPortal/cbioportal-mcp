# Cross-study meta-analysis — Phase B handoff notes (2026-09-03)

Phase B of `docs/cross-study-meta-analysis-plan.md`: the forest-plot widget at
`ui://cbioportal/forest`, wired to `cross_study_alteration_frequency`. Nothing is committed.

## What changed

| File | Change |
|---|---|
| `frontend/forest/` | **new widget project** (gitignored like the other five — the source exists only on this machine): `package.json`, `tsconfig.json`, `vite.config.ts`, `forest.html` (shell + CSS tokens for light/dark), `src/main.ts` (renderer + ext-apps `App` host wiring + preview path), `src/samples.ts` (baked-in **real** payloads for the preview, generated from live results — regenerate rather than hand-edit). `node_modules` and `package-lock.json` were copied from `frontend/cooccurrence` (identical dependency set), so no network was needed. Rebuild: `cd frontend/forest && npm run build`. |
| `src/cbioportal_mcp/resources/widgets/forest.html` | **new built bundle** (418 KB, same size class as the siblings). Self-contained: the only URLs inside are JSON-schema `$id` strings from the SDK and the SVG namespace, nothing is fetched. Rebuilding an untouched tree reproduces it byte for byte. |
| `src/cbioportal_mcp/ui.py` | `FOREST_UI_URI = "ui://cbioportal/forest"`. |
| `src/cbioportal_mcp/server.py` | `forest_widget` resource (`UI_MIME_TYPE`) and `app=ui.app_config(ui.FOREST_UI_URI)` on the tool, whose description now says it renders a forest plot. No CSP (no network). |
| `tests/test_cross_study.py` | 4 UI-wiring tests: app config, bundle loads and carries the bridge + preview payloads, the tool's `_meta["ui"].resourceUri` is the forest resource (via `mcp.get_tool`), and the resource is registered with the MCP-app MIME type (via `mcp.list_resources`). |

Run: `uv run --python 3.12 --extra dev pytest -q` → 347 passed, 5 skipped (live), 2 failed (the
pre-existing `test_survival_curve.py` pair). `ruff` / `black --check` clean on the touched Python.

## The widget

One row per study, sorted included-by-weight, then below-minimum, then overlap, then not-covered:

- square with area ∝ random-effects weight at the study's frequency, whiskers = Wilson 95% CI;
  columns for altered / profiled (on the payload's `unit`), `% [95% CI]`, and weight.
- `below_min_profiled` → hollow square + amber `n < 10` badge; `overlap` → struck-through label,
  faint marker, red `overlap` badge (partner study in the tooltip and warnings); `not_covered` →
  no marker, "0 profiled of N", `not on panel` badge — never a 0%.
- pooled random-effects diamond with CI, fixed-effect dashed diamond as reference, dashed vertical
  line at the pooled value; when pooling is not computed the diamond row says why (overlap or
  fewer than two eligible) and the subtitle says "pooling blocked: overlapping studies".
- x-axis 0–100 unless every interval sits in a band narrower than 45 points, then it zooms to a
  5-point grid around them (the two-study example shows 35–65, the 16-study case stays 0–100).
- statistics block under the plot (pooled + fixed effect, Q / p / I² / τ², χ² or Fisher difference
  test, counting unit), legend, then the payload's `warnings` and `notes` as separate lists.
- hover tooltip per row: both grains, CI, weight, panels, top sample types, exclusion reason.
- host theming through `applyDocumentTheme` / host style variables, same as the siblings.

Offline QA: `forest.html?preview=1` (+ `&case=many|overlap|notcovered|error`, `&theme=dark`).
Rendered in headless Chromium in both themes for all cases on 2026-09-03; one fix pass (badge width,
blocked-pooling reason length, notes heading, label column width, χ² symbol) and re-verified.

## Decisions worth remembering

- **Preview data is real, not synthetic.** `src/samples.ts` holds the four captured payloads
  (worked example, 16-study LUAD set, TCGA-releases overlap, TTN not covered) with provenance
  stripped. The other widgets use synthetic samples; here real ones were available and they
  exercise every row state.
- **Badges are short by design** — `overlap`, `n < 10`, `not on panel` — because the weight column
  is 104 px; the partner study for an overlap is in the tooltip and in `warnings[]`.
- **Warnings are rendered verbatim** from the payload, so the disclosure text (heterogeneity with
  design differences, overlap exclusions, "spans N cancer types") reaches the user without a widget
  rebuild, the same contract the other apps rely on.
- **Registration test uses `mcp.get_tool(...).meta`** — that is where FastMCP 3.3.1 puts the
  `_meta["ui"]` block (`{"ui": {"resourceUri", "visibility", "prefersBorder"}}`); the sibling test
  files only checked `app_config`, so this is the first test that pins the actual linkage.

## Open items

1. **`frontend/` is gitignored**, so `frontend/forest/` (like the other five widget trees) is not
   in git — a fresh clone gets the bundle but cannot rebuild it. Same open call as before.
2. **Live host validation** — render path verified offline; the handshake is the official SDK's.
   Still worth one look in Claude Desktop / a `fastmcp dev apps` harness, as for the other apps.
3. **Phase C** — guide / prompt routing, `MANUAL_TOOL_TESTS.md`, guide-coverage tests, optional SQL
   view. The statistical-tests guide still calls study-vs-study comparison UNCOVERED.
4. The R `metafor` fixture from Phase A is still open.
