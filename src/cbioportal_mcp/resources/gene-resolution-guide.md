# Gene Resolution Guide

Use this guide before querying gene expression, mutation, copy-number, methylation, or structural-variant data when the user's gene term may be ambiguous.

## Routing Triggers

Read this guide when the user mentions:

- a gene family shorthand: `CD3`, `HLA`, `KRT`, `MUC`, `MT-`, `IGH`, `IGK`, `IGL`
- a marker name that may refer to multiple genes or proteins
- a gene alias, old symbol, or informal name
- a wildcard-like term such as "all CD3 genes"

These examples are not exhaustive. Apply this guide to any gene term that may resolve to multiple symbols, aliases, paralogs, family members, or marker genes.

## Core Rule

Do not silently aggregate multiple genes when the user names an ambiguous symbol. Either ask for clarification or choose a clearly standard marker and state the choice.

For example, "CD3 expression" can refer to `CD3D`, `CD3E`, or `CD3G`; in many immune-marker contexts `CD3E` is the standard marker, but the agent must not average all CD3 genes unless the user asks for a combined signature.

## Preferred Tool

Call `resolve_gene_symbol(term)` first. Use the returned exact, prefix, and contains matches to decide whether the term is a single HUGO symbol, an ambiguous family/marker, or an unmatched term.

## Gene Discovery Query

If the tool is unavailable or you need to inspect the SQL shape, validate the gene table exists, then search exact symbols first, followed by prefix/alias-like matches:

```sql
SELECT
    hugo_gene_symbol,
    entrez_gene_id
FROM gene
WHERE upper(hugo_gene_symbol) = upper('CD3')
   OR upper(hugo_gene_symbol) LIKE upper('CD3%')
ORDER BY hugo_gene_symbol
LIMIT 50;
```

If aliases are available in this deployment, inspect the relevant alias table before assuming no match. If no alias table exists, state that alias resolution is limited to available gene symbols.

## Answer Pattern

If multiple plausible genes are found:

> "CD3" is ambiguous in cBioPortal gene-symbol terms. I found `CD3D`, `CD3E`, and `CD3G`. Did you mean `CD3E` as a T-cell marker, or should I analyze all three separately?

If the user clearly asks for a combined family/signature:

- report each gene separately by default
- only compute an average/signature if the user explicitly requests it
- state exactly how the combined value was calculated

## Do Not

- Do not average multiple genes into one expression value without explicit permission.
- Do not rewrite an ambiguous symbol to a single gene without telling the user.
- Do not treat a prefix match as a validated gene symbol.
