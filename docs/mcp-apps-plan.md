# MCP UI Apps Plan — cBioPortal

> **Status:** Draft / parked for later. Created 2026-06-01.
> **Direction locked:** Interactive UI apps (MCP Apps / `mcp-ui` extension), prioritizing
> **mutation landscape** and **survival analysis**.

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

- **Phase 1 — Foundation + Kaplan–Meier.** Simplest rendering (a line chart) and self-contained
  server-side stats, so it de-risks the whole MCP-UI pipeline end-to-end while immediately
  demonstrating the mutation↔survival tie-in.
- **Phase 2 — OncoPrint.** The signature visualization; establishes the matrix + clinical-track
  data contract and the heavier-viz-library decision.
- **Phase 3 — Lollipop.** Reuses gene-level fetch; introduces the external-annotation question.
- **Phase 4 (stretch) — Co-occurrence.**

## Decisions to lock before building

1. **Target host.** Claude (desktop/web), ChatGPT (Apps SDK), or any `mcp-ui` host? The render
   bridge differs — lean toward standardizing on `ui://` + `mcp-ui` for breadth and confirm the
   primary client. **(Lock this first — it shapes the scaffold.)**
2. **Patient vs. sample grain** — a classic cBioPortal trap: **survival is per-patient,
   alterations per-sample.** The KM tool must join through sample→patient (see
   `cbioportal://clinical-data-guide`).
3. **Data availability** — confirm `genomic_event_derived` has a **protein-change/position**
   column for lollipops (else annotate from genomic coords via Genome Nexus), and the exact
   survival attribute names (OS/PFS/DFS) in `clinical_data_derived`.
4. **Viz fidelity vs. effort** — reuse cBioPortal's own `oncoprintjs`/`react-mutation-mapper`
   (canonical, heavier React integration) vs. lightweight D3/Plotly. Current lean: Plotly/D3 for
   KM + co-occurrence, the cBioPortal libs for OncoPrint/lollipop.
5. **New build surface** — adds `lifelines`/`scipy` (Python stats) and a Node/esbuild toolchain
   to a currently pure-Python repo. Needs an explicit yes, plus **pinning `fastmcp`** (currently
   unpinned) once its `ui://`/`_meta` support is confirmed.
6. **Iframe external calls** — lollipop domain data means iframe → Genome Nexus; decide
   allow-direct vs. proxy-through-a-tool (CSP).

## Next steps (when we return)

1. Lock decision #1 (target host).
2. Turn **Phase 1 (Foundation + Kaplan–Meier)** into a concrete implementation plan.
3. Confirm decisions #2–#3 against the live schema before writing the survival tool.
