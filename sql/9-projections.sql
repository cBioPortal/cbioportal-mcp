-- ============================================================================
-- Projections for alteration-frequency and co-occurrence query shapes
-- ============================================================================
-- REQUIRES optimize_use_implicit_projections = 0 for every user that queries
-- this database (see sql/README.md, "Projections"). With ClickHouse's
-- default (1), a bare count() whose WHERE is answered by the base primary
-- key over-counts once these projections exist: the implicit exact-count
-- path counts the granules the base key proves fully matching, and the
-- normal-projection read of the remaining rows counts them again. Seen on
-- 24.8.14, 26.2.19, 26.8.12 and 26.9.3 (e.g. 36384 instead of 20000). The MCP
-- refuses to start against a database that has projections unless its
-- ClickHouse user has that setting pinned so a query can't turn it back on
-- (cbioportal_mcp.authentication.permissions).
--
-- The derived tables are sorted for the cBioPortal backend's access pattern,
-- not the agent's:
--
--   genomic_event_derived        ORDER BY (genetic_profile_stable_id,
--                                          cancer_study_identifier,
--                                          variant_type, entrez_gene_id,
--                                          hugo_gene_symbol, sample_unique_id)
--   sample_to_gene_panel_derived ORDER BY (gene_panel_id, alteration_type,
--                                          genetic_profile_id,
--                                          sample_unique_id)
--
-- Study- and gene-scoped agent queries and most recipe views in
-- sql/4-mutation-frequency-views.sql filter on study + hugo_gene_symbol +
-- variant_type and never on genetic_profile_stable_id, so the primary key
-- prunes poorly and a study-scoped gene lookup reads most granules. The
-- profiling denominator on sample_to_gene_panel_derived has the same problem
-- (no study in the key). Cohort-wide recipes with no gene filter (for example
-- top_mutated_genes_in_cohort) don't benefit.
--
-- The projections below give ClickHouse alternate sort orders to pick from.
-- The optimizer uses one for a query only when it holds every column the
-- query reads and would read fewer marks than the base table; otherwise the
-- query reads the base table.
--
--   ged_by_study_gene   study-first: "gene X in study S" (gene_*_in_study,
--                       gene_*_in_studies, top_mutated_genes_in_study,
--                       co_altered_genes_in_study, top_sv_genes_in_study,
--                       two-gene co-occurrence in S).
--   ged_by_gene_study   gene-first: cross-study "gene X across cancer types
--                       in cohort Y" (gene_*_by_cancer_type). Those views
--                       restrict the study via JOIN cohort, which does not
--                       reach the primary-key index, so only a gene-leading
--                       key prunes them.
--   stgp_by_study       study-first profiling denominator.
--
-- The genomic_event_derived projections hold only the seven columns the
-- recipe views filter or count on. cna_alteration is the one column beyond
-- the six every mutation recipe reads: gene_alteration_frequency_by_cancer_type
-- filters on it for amplification / deep_deletion, and it is a
-- Nullable(Int8), so it costs about a byte per row. Everything else
-- (patient_unique_id, mutation_variant, mutation_type, cna_cytoband,
-- annotations, ...) reads the base table: gene_mutation_variants_in_study and
-- top_cna_genes_in_study, and patient-level agent queries, get no speedup
-- but return the same rows.
--
-- Re-apply: the daily clone rebuilds every table via CLONE AS from the
-- production database (which has no projections) and then runs every
-- sql/*.sql, so this file recreates the projections on each clone.
-- Re-running it on a database that already has them is a no-op:
-- ADD ... IF NOT EXISTS skips, and MATERIALIZE on parts that already carry
-- the projection rewrites no rows. mutations_sync = 2 makes each MATERIALIZE
-- wait until every replica finishes, so the clone job does not hand the
-- database to the MCP server with half-built projections.
--
-- Verify / drop: see sql/README.md ("Projections").
-- ============================================================================

-- ----------------------------------------------------------------------------
-- genomic_event_derived: study-first
-- ----------------------------------------------------------------------------
ALTER TABLE genomic_event_derived
    ADD PROJECTION IF NOT EXISTS ged_by_study_gene
    (
        SELECT
            sample_unique_id,
            cancer_study_identifier,
            hugo_gene_symbol,
            variant_type,
            mutation_status,
            off_panel,
            cna_alteration
        ORDER BY (cancer_study_identifier, hugo_gene_symbol, variant_type, sample_unique_id)
    );

ALTER TABLE genomic_event_derived
    MATERIALIZE PROJECTION ged_by_study_gene
    SETTINGS mutations_sync = 2;

-- ----------------------------------------------------------------------------
-- genomic_event_derived: gene-first (cross-study / cohort queries)
-- ----------------------------------------------------------------------------
ALTER TABLE genomic_event_derived
    ADD PROJECTION IF NOT EXISTS ged_by_gene_study
    (
        SELECT
            sample_unique_id,
            cancer_study_identifier,
            hugo_gene_symbol,
            variant_type,
            mutation_status,
            off_panel,
            cna_alteration
        ORDER BY (hugo_gene_symbol, variant_type, cancer_study_identifier, sample_unique_id)
    );

ALTER TABLE genomic_event_derived
    MATERIALIZE PROJECTION ged_by_gene_study
    SETTINGS mutations_sync = 2;

-- ----------------------------------------------------------------------------
-- sample_to_gene_panel_derived: study-first
-- ----------------------------------------------------------------------------
-- The table already carries cancer_study_identifier (it just isn't in the
-- sort key), so no derived column is needed. All five columns are narrow, so
-- the projection holds the full row.
ALTER TABLE sample_to_gene_panel_derived
    ADD PROJECTION IF NOT EXISTS stgp_by_study
    (
        SELECT *
        ORDER BY (cancer_study_identifier, alteration_type, gene_panel_id, sample_unique_id)
    );

ALTER TABLE sample_to_gene_panel_derived
    MATERIALIZE PROJECTION stgp_by_study
    SETTINGS mutations_sync = 2;
