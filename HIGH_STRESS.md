# cBioPortal MCP Apps — STRESS Test Questions

Extracted from *cBioPortal MCP Apps — Test Set & Gap Analysis*. These are the questions
flagged as **STRESS**: designed to expose a specific limitation and fail by design.

---

## survival_curve

1. **Perform the survival analysis for breast cancer in MSK-CHORD.**
   Fails by design — `msk_chord_2024` is pan-cancer with 25k samples and the tool has no
   cohort filter, so it silently answers for all cancer types.

2. **For the TCGA pan-cancer cohort, what is the survival difference between patients
   with mutations in both TP53 and KRAS versus patients with only a KRAS mutation?**
   Fails by design — `group_by_gene` is a single gene; co-mutation groups aren't expressible.

3. **Do lung adenocarcinoma patients with high EGFR mRNA expression (top quartile)
   have different survival than those with low expression?**
   Fails by design — no expression-based grouping exists. This shape recurs constantly
   (e.g. "survival curves by FOXP1 expression levels").

4. **Can we look at patient survival for patients with a mutation in TP53 codons 1–40
   versus the rest of the protein, and compare it to patients without a TP53 mutation?**
   Fails by design — three groups defined by codon range; nothing in the schema reaches
   protein-change granularity.

---

## alteration_cooccurrence

5. **I want to evaluate pathway-level mutual exclusivity in pancreatic adenocarcinoma
   — cell cycle checkpoints versus homologous recombination repair.**
   Fails by design — genes cannot be merged into pathway tracks; every pair is gene × gene.

6. **When observing mutual exclusivity, did you normalise for tumour type? Re-run
   including tumour type as a confounder and report the p-values.**
   Fails by design, and matters — a real user caught the assistant reporting pan-cancer
   exclusivity confounded by tissue type. The app has no stratification.

7. **Find genes frequently mutated only in TP53 wild-type tumours but rarely in
   TP53-mutant tumours, implicating synthetic lethal interactions.**
   Genome-wide discovery rather than a fixed gene list — currently requires hand-written SQL.

---

## oncoprint

8. **Create an OncoPrint in TCGA PanCancer Atlas for SMARCA4, SMARCB1 and ARID1A with a
   merged track for all three, a merged track for truncating driver mutations, and a
   merged track for missense drivers.**
   Fails by design — no merged tracks, no driver filter. The same user later hand-pasted
   working OQL syntax as a workaround.

9. **I want an OncoPrint for colon cancer with all mutations in EGFR except T790M and
   L858R.**
   Fails by design — negative protein-change filters aren't expressible.

10. **Is there a way to reduce the fine granularity of the mutation types listed —
    missense, missense of unknown significance, splice, truncating?**
    A legibility complaint inherited from the portal's own OncoPrint (8+ legend classes).

---

## mutation_diagram

11. **I want to query mutations in EGFR that fall specifically within its tyrosine
    kinase domain.**
    Fails by design — Pfam domains are fetched by the widget for rendering only; you
    cannot filter or count by domain.

12. **Show me the frequency of the different codons that produce BRAF V600E in the
    TCGA melanoma study.**
    Fails by design — the app is keyed on protein change; nucleotide-level detail isn't
    surfaced. (Related, equally unanswerable: "find cases where a point mutation changes
    a GAG codon to GAA in EGFR.")

13. **Show me a lollipop for BAP1 across the TCGA PanCancer cohort, then regenerate it
    on the non-redundant dataset.**
    Fails by design — one `study_id`, no multi-study cohort. The most common structural
    failure across all four data apps.

---

## bar_chart · pie_chart · line_chart

14. **Plot a histogram of allele frequency for all TP53 missense mutations in diploid
    TCGA samples, with mean and median marked.**
    Fails by design — no distribution chart, no reference lines. Same failure for "violin
    plot overlaid with jittered dots, separated by cancer type," which a user asked for
    verbatim.

---

### Summary

| App | # STRESS questions | Common root cause |
|---|---|---|
| survival_curve | 4 | No cohort filter, no multi-gene/expression grouping, no protein-change granularity |
| alteration_cooccurrence | 3 | No pathway merging, no stratification/confounders, no genome-wide discovery |
| oncoprint | 3 | No merged tracks, no negative filters, no legend granularity control |
| mutation_diagram | 3 | No domain-level filtering, no nucleotide detail, no multi-study cohorts |
| bar/pie/line chart | 1 | No distribution/histogram chart type |

**14 STRESS questions total**, each chosen because it fails in a specific, instructive way.
