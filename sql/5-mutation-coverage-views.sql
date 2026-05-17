-- ============================================================================
-- mutation_panel_gene_coverage + mutation_wes_coverage
-- ============================================================================
-- The canonical "is this sample profiled for gene G?" query has to handle
-- two cases:
--
--   1. The sample is on a NAMED gene panel (MSK-IMPACT, METABRIC_173,
--      ARCHER-SOLID, ...) and the panel's `gene_panel_list` entries do
--      or don't include G.
--   2. The sample is sequenced via WES (`gene_panel_id = 'WES'`), in
--      which case ALL genes are considered profiled. WES is NOT in the
--      `gene_panel` table at all, so any JOIN through `gene_panel` /
--      `gene_panel_list` silently drops every WES row — this is the bug
--      that produced 172% TP53 frequencies in cross-cancer-type queries
--      before the views below existed.
--
-- These two views together let queries answer "samples profiled for
-- mutations in gene G" with one UNION ALL instead of re-deriving the
-- WES branch every time:
--
--   SELECT sample_unique_id, cancer_study_identifier
--   FROM mutation_panel_gene_coverage WHERE hugo_gene_symbol = 'TP53'
--   UNION ALL
--   SELECT sample_unique_id, cancer_study_identifier
--   FROM mutation_wes_coverage;
--
-- See cbioportal://mutation-frequency-guide for the full canonical
-- frequency-by-cancer-type recipe.
--
-- Refreshed (DROP + CREATE) on every clone. Views are cheap — they hold
-- no data, just the query definition.
-- ============================================================================

DROP VIEW IF EXISTS mutation_panel_gene_coverage;

CREATE VIEW mutation_panel_gene_coverage AS
SELECT
    stgp.sample_unique_id,
    stgp.cancer_study_identifier,
    g.hugo_gene_symbol,
    stgp.gene_panel_id
FROM sample_to_gene_panel_derived stgp
JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
JOIN gene g ON gpl.gene_id = g.entrez_gene_id
WHERE stgp.alteration_type = 'MUTATION_EXTENDED';

DROP VIEW IF EXISTS mutation_wes_coverage;

CREATE VIEW mutation_wes_coverage AS
SELECT
    sample_unique_id,
    cancer_study_identifier
FROM sample_to_gene_panel_derived
WHERE alteration_type = 'MUTATION_EXTENDED'
  AND gene_panel_id = 'WES';
