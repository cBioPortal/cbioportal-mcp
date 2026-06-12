# MCP UI Apps Plan — cBioPortal

> **Status:** Phase 1 (Foundation + Kaplan–Meier) **implemented 2026-06-04**. Created 2026-06-01.
> **Phase 2 (OncoPrint) implemented 2026-06-11** — `oncoprint` tool + `ui://cbioportal/oncoprint`
> widget (custom inline SVG, full fidelity: alteration matrix + clinical tracks + panel-coverage
> "not profiled" cells).
> **Widget host bridge rewritten 2026-06-08:** the hand-rolled defensive postMessage bridge did not
> implement the MCP Apps `2026-01-26` View↔Host handshake (wrong `protocolVersion`, no
> `ui/notifications/initialized`), so hosts never delivered the tool result and the widget hung on
> "Loading…". It was replaced by the official [`@modelcontextprotocol/ext-apps`](https://www.npmjs.com/package/@modelcontextprotocol/ext-apps)
> `App` SDK, bundled with Vite into the single self-contained `survival.html`.
> **Direction locked:** Interactive UI apps (MCP Apps / `mcp-ui` extension), prioritizing
> **mutation landscape** and **survival analysis**.

## Implementation status

- **Phase 1 — DONE.** `survival_curve` tool + `ui://cbioportal/survival` widget, backed by a
  pure-Python Kaplan–Meier + log-rank module (`survival_stats.py`). See "Key finding" below.
- **Phase 2 — DONE (2026-06-11).** `oncoprint` tool + `ui://cbioportal/oncoprint` widget. Delivered:
  server-side data shaping in `server.py` (`_build_oncoprint_payload` + `_fetch_oncoprint_events`,
  `_fetch_profiled_samples`, `_memo_sort`, `_fetch_clinical_tracks`, `_oncoprint_gene_stats`),
  `ui.oncoprint_app_config()`, the Vite widget `frontend/oncoprint/` → built
  `resources/widgets/oncoprint.html`, and `tests/test_oncoprint.py`. Custom inline SVG (no
  `oncoprintjs`), full fidelity (matrix + clinical tracks + panel-coverage gray cells via
  `mutation_panel_gene_coverage` ∪ `mutation_wes_coverage`). Sample columns capped at 200 (altered
  first); per-gene `%` computed over the full profiled set. Note: per-gene coverage uses
  MUTATION_EXTENDED profiling; CNA/SV-only panels remain a TODO.
- Phases 3–4 (lollipop, co-occurrence) — not started.

> **fastmcp ↔ mcp_clickhouse import fix (Phase 2):** `import cbioportal_mcp.server` was failing
> because the pinned `fastmcp==3.3.1` (required for MCP Apps) rejects the `dependencies=` kwarg that
> `mcp_clickhouse==0.1.11` passes at import — and no published `mcp_clickhouse` supports fastmcp 3.x
> (0.4.0 still requires `fastmcp<3`), so upgrading is not an option. **Fixed** by
> `src/cbioportal_mcp/_compat.py` (`patch_fastmcp_removed_kwargs`), applied at the top of the package
> `__init__`, which strips the removed kwarg from `FastMCP(...)` before `mcp_clickhouse` is imported.
> Safe because the server only uses mcp_clickhouse's plain query helpers
> (`execute_query`/`run_select_query`), which never touch its FastMCP instance. The DB-free tests
> exercise this real import path (no stub). Note: `server.py` uses `importlib.resources.abc`, so
> **Python ≥3.11 is required** (the repo `.venv` was 3.10; use 3.11+/3.12).

### Key finding — FastMCP ships native MCP Apps support

`fastmcp==3.3.1` (now pinned) implements the **MCP Apps extension** (`io.modelcontextprotocol/ui`)
directly: a tool declares `app=AppConfig(resource_uri="ui://…")` and the host renders that `ui://`
HTML resource in a sandboxed iframe, passing the tool's structured result to the widget. This made
most of the "decisions to lock" moot and **eliminated the heavy build surface** the draft assumed:

- **Self-contained HTML, but now Vite-built (updated 2026-06-08)** — the inline-SVG render is still
  hand-written, but the host bridge is the official ext-apps `App` SDK, so the widget is built from
  `frontend/survival/` by Vite (`vite-plugin-singlefile`) into one self-contained HTML file (SDK + deps
  inlined). Shipped via the existing `resources/` force-include (`resources/widgets/survival.html`,
  committed). A dev-time Node toolchain is required to rebuild the widget; the Python runtime/wheel
  stays pure-Python. (This supersedes the original "no esbuild/Node" goal — see decision #5.)
- **No `lifelines`/`scipy`** — KM and log-rank (incl. the chi-square p-value) are pure standard library.
- **No `prefab_ui`** — we use the custom `ui://` HTML resource path, not Prefab components.
- **Host (decision #1):** standardized on the native MCP Apps extension. The widget uses the official
  `@modelcontextprotocol/ext-apps` `App` SDK (handshake `protocolVersion 2026-01-26`:
  `ui/initialize` → `ui/notifications/initialized` → render `ui/notifications/tool-result`'s
  `structuredContent`). Render path validated via headless preview; recommend a final check in
  Claude Desktop / `fastmcp dev apps`.

## Context

`cbioportal-mcp` is a FastMCP server wrapping `mcp-clickhouse` that exposes cancer-genomics
data from a read-only (SELECT-only) ClickHouse database, plus a set of guide resources.

This plan covers adding **interactive UI apps**: tools that return embedded HTML/iframe
widgets which render inside the host (Claude/ChatGPT), built on the MCP Apps (SEP-1865) /
`mcp-ui` extension. The goal is to bring cBioPortal's signature visualizations (OncoPrint,
Kaplan–Meier curves, mutation lollipop plots) into the MCP surface.

### Existing surface to build on

- **Query tools:** `clickhouse_run_select_query`, `clickhouse_list_tables`, `clickhouse_list_table_columns`
- **Discovery tools:** `list_studies`, `search_oncotree`, `get_study_guide`, `list_study_guides`, `list_guides`, `read_guide`
- **Resources:** 8 `cbioportal://` guides (mutation-frequency, clinical-data, sample-filtering, expression, statistical-tests, treatment, FAQ, pitfalls)
- **Clean Python helper:** `run_select_query()` returns `list[dict]` — UI tools reuse this directly
- **Packaging hook:** `pyproject.toml` already force-includes `src/cbioportal_mcp/resources` into the wheel (line ~44) — reuse for built widget assets
- **Safety patterns already present:** SELECT-only DB user, `MAX_LIST_LIMIT` payload clamping, input validators (`_validate_study_id`, `_validate_table_name`, etc.)

### Relevant data model (confirmed)

- `genomic_event_derived` — `sample_unique_id`, `hugo_gene_symbol`, `variant_type`
  (`'mutation'` | `'cna'` | `'structural_variant'`), `mutation_type`, `mutation_variant`,
  `cna_alteration`, `mutation_status`
- `clinical_data_derived` — `sample_unique_id`, `attribute_name`, `attribute_value`
  (e.g. `OS_MONTHS`, `OS_STATUS`, `SUBTYPE`, `ER_STATUS`)
- `cancer_study` — `cancer_study_identifier`, `name`, `description`, `type_of_cancer_id`
- Materialized views: `gene_mutation_frequency_by_cancer_type`, `top_mutated_genes_in_cohort`,
  `gene_pair_coexpression`, coverage views

## How a UI app works in this server

Each app is **three pieces**, mirroring existing patterns:

1. **A `ui://` resource** — `@mcp.resource("ui://cbioportal/<app>")` returning `text/html`
   (a self-contained widget bundle), exactly like the existing `cbioportal://` guide resources.
2. **A tool** — e.g. `oncoprint(study_id, genes=[...])` that fetches/shapes data via the
   existing `run_select_query()`, returns structured JSON **and** references the UI template
   via `_meta` (MCP Apps / `mcp-ui` `UIResource`).
3. **The widget JS** — renders the data in the host's sandboxed iframe and calls back into
   tools via `postMessage` for interactivity (add a gene, drill into a sample).

## Shared foundation (build once, before any app)

| Piece | What it is |
|---|---|
| **Widget scaffold** | One HTML host template + JS bridge (`@mcp-ui/client` / MCP Apps SDK): host theming (light/dark), loading/empty/error states, a typed data-contract convention. |
| **Build + packaging** | An esbuild/vite step bundling each widget to static assets, shipped via the existing `resources/` force-include mechanism in `pyproject.toml`. |
| **Python UI helper** | A small `_ui_resource(...)` emitter so every tool returns the `UIResource`/`_meta` consistently, reusing `run_select_query()`. |

## Recommended app catalog (mutation landscape + survival)

| App | `ui://` | Tool (sketch) | Data | Viz | Effort |
|---|---|---|---|---|---|
| **Kaplan–Meier survival** ⭐ start here | `…/survival` | `survival_curve(study_id, endpoint=OS\|PFS\|DFS, group_by)` | `clinical_data_derived` (OS_MONTHS/OS_STATUS) + `genomic_event_derived` for alteration grouping | Plotly/D3 line chart + at-risk table + censor ticks | Med |
| **OncoPrint** 🏁 flagship | `…/oncoprint` | `oncoprint(study_id, genes[], clinical_tracks=[]?)` | `genomic_event_derived` (variant_type, mutation_type, cna_alteration) + `clinical_data_derived` tracks | `oncoprintjs` (cBioPortal's own lib) or custom SVG | High |
| **Mutation lollipop** | `…/lollipop` | `mutation_diagram(study_id, gene)` | per-gene `genomic_event_derived` + Pfam domains | `react-mutation-mapper` (fetches domains itself) | Med-High |
| **Co-occurrence / mutual exclusivity** *(stretch)* | `…/cooccurrence` | `alteration_cooccurrence(study_id, genes[])` | pairwise `genomic_event_derived` + Fisher exact (scipy, server-side) | D3 heatmap | Med |

**Headline use case:** the survival app's `group_by = alteration_status(gene)` answers
*"does mutating TP53 change survival?"* — tying both priorities into one widget. That's why it leads.

## Suggested phasing

- **Phase 1 — Foundation + Kaplan–Meier. ✅ DONE (2026-06-04).** Simplest rendering (a line chart)
  and self-contained server-side stats, so it de-risks the whole MCP-UI pipeline end-to-end while
  immediately demonstrating the mutation↔survival tie-in. Delivered: `survival_curve` tool,
  `ui://cbioportal/survival` widget, `survival_stats.py`, `ui.py`, and tests in `tests/`.
- **Phase 2 — OncoPrint. ✅ DONE (2026-06-11).** The signature visualization; established the matrix
  + clinical-track data contract. Viz-library decision resolved in favor of custom inline SVG
  (mirroring Phase 1), not `oncoprintjs`.
- **Phase 3 — Lollipop.** Reuses gene-level fetch; introduces the external-annotation question.
- **Phase 4 (stretch) — Co-occurrence.**

## Decisions to lock before building

1. **Target host.** ✅ *Resolved:* standardized on the native MCP Apps extension
   (`io.modelcontextprotocol/ui`) that `fastmcp==3.3.1` implements. The widget bridge is host-defensive
   (OpenAI Apps SDK + ext-apps postMessage + injection). Still TODO: validate against a live
   Claude/ChatGPT host.
2. **Patient vs. sample grain** — a classic cBioPortal trap: **survival is per-patient,
   alterations per-sample.** The KM tool must join through sample→patient (see
   `cbioportal://clinical-data-guide`).
3. **Data availability** — confirm `genomic_event_derived` has a **protein-change/position**
   column for lollipops (else annotate from genomic coords via Genome Nexus), and the exact
   survival attribute names (OS/PFS/DFS) in `clinical_data_derived`.
4. **Viz fidelity vs. effort** — reuse cBioPortal's own `oncoprintjs`/`react-mutation-mapper`
   (canonical, heavier React integration) vs. lightweight D3/Plotly. Current lean: Plotly/D3 for
   KM + co-occurrence, the cBioPortal libs for OncoPrint/lollipop.
5. **New build surface** — *Updated 2026-06-08:* stats stay pure standard library (no
   `lifelines`/`scipy`). The widget originally avoided Node/esbuild, but the hand-rolled host bridge
   didn't conform to the MCP Apps handshake, so we adopted the official `@modelcontextprotocol/ext-apps`
   `App` SDK and a **dev-time Vite build** (`frontend/survival/`) that bundles it into one
   self-contained `survival.html`. The built artifact is committed and shipped via `resources/`
   force-include, so the **Python runtime/wheel remains pure-Python**; only rebuilding the widget needs
   Node. `fastmcp` stays **pinned to `==3.3.1`** (its `ui://`/`_meta`/`app=` support confirmed).
6. **Iframe external calls** — lollipop domain data means iframe → Genome Nexus; decide
   allow-direct vs. proxy-through-a-tool (CSP).

## Next steps (when we return)

1. Lock decision #1 (target host).
2. Turn **Phase 1 (Foundation + Kaplan–Meier)** into a concrete implementation plan.
3. Confirm decisions #2–#3 against the live schema before writing the survival tool.
