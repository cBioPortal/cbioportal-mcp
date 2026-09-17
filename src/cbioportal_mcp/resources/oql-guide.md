# Alteration Queries (OQL) Guide

`oncoprint(oql=)`, `alteration_cooccurrence(tracks=)`, `survival_curve(groups=)`, `alteration_enrichment(group_a=, group_b=)` and `mutation_allele_frequency(alteration=)` accept a documented subset of cBioPortal's Onco Query Language — the same syntax users paste from cbioportal.org. Every payload echoes the parsed query (`query`, `tracks`, `grouping`, `groups`), so quote that back.

## Syntax

| Write | Means |
|---|---|
| `TP53` | all alterations: `MUT FUSION AMP HOMDEL` |
| `TP53: MUT` | mutations (excluding UNCALLED) |
| `KRAS: AMP` / `CDKN2A: HOMDEL` / `ALK: FUSION` | amplification (GISTIC 2) / deep deletion (-2) / structural variant |
| `TP53: MISSENSE` (or `MUT = MISSENSE`) | a mutation class: MISSENSE, NONSENSE, NONSTART, NONSTOP, FRAMESHIFT, INFRAME, SPLICE, TRUNC, PROMOTER |
| `BRAF: V600E`, `BRAF: V600`, `p.Val600Glu` | one protein change / any change at codon 600 / three-letter notation |
| `TP53: MUT = (1-40)` | changes overlapping codons 1-40; `(41-)` from 41 on; `(-40)` up to 40; `(1-40*)` fully contained |
| `EGFR: MUT != T790M MUT != L858R` | every EGFR mutation except T790M and L858R |
| `TP53: MISSENSE MUT != R175H` | missense except R175H |
| `BRCA1: GERMLINE` / `MUT_SOMATIC` / `TRUNC_GERMLINE` | germline / somatic mutations |
| `EGFR: MUT_DRIVER`, `(712-979)_DRIVER` | study-supplied driver annotations only (see below) |
| `["HR repair" BRCA1 BRCA2 PALB2]` | one merged track, altered when any gene is |
| `[SMARCA4: TRUNC; SMARCB1: TRUNC]` | merged track with per-gene commands |

Separate gene lines with `;` or new lines once they carry commands: `TP53: MUT; KRAS: AMP`. A bare protein-change-shaped word on the same line is read as a value (`TP53: MUT A2M` means TP53 A2M).

## Semantics to state when reporting

- Exclusions: every `!=` on a gene line removes those changes. (cbioportal.org honours only one `!=` per line; this server applies all of them.) `oncoprint` returns `exclusions[]` with events and samples removed — quote those counts; `0` means the excluded change was not present.
- Codon ranges use the protein change's first codon and, for spans such as `E746_A750del`, its last codon. Changes with no codon (splice `NA`) are in no range.
- Merged tracks in `alteration_cooccurrence` are tested only on samples profiled for all their genes; in `oncoprint` a sample counts as profiled when any gene was.
- Pathway membership is whatever gene list you pass — state the lists you used.

## Refused (the tool returns an error, never a silently unfiltered result)

- `DRIVER` / `_DRIVER` for studies without `driver_filter` annotations (almost all; OncoKB is not stored). Offer the unfiltered track and say it includes passengers.
- `EXP`, `PROT` (use `survival_curve(group_by_expression=)` for expression groups), `GAIN`, `HETLOSS`, `CNA >= ...`, `DATATYPES`.
