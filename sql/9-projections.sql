-- ============================================================================
-- Projections for alteration-frequency and co-occurrence query shapes
-- ============================================================================
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
-- Agent frequency / co-occurrence queries (and every recipe view in
-- sql/4-mutation-frequency-views.sql) filter on study + hugo_gene_symbol +
-- variant_type and never on genetic_profile_stable_id, so the primary key
-- can't prune: every study-scoped gene lookup reads the whole table. The
-- profiling denominator on sample_to_gene_panel_derived has the same problem
-- (no study in the key).
--
-- The projections below give ClickHouse alternate sort orders to pick from.
-- The optimizer chooses a projection per query only when it reads fewer
-- marks than the base table and the projection holds every column the query
-- touches; otherwise it silently falls back to the base table. Results are
-- identical either way.
--
--   ged_by_study_gene   study-first: "gene X in study S" (gene_*_in_study,
--                       gene_*_in_studies, two-gene co-occurrence in S).
--   ged_by_gene_study   gene-first: cross-study "gene X across cancer types
--                       in cohort Y" (gene_*_by_cancer_type). Those views
--                       restrict the study via JOIN cohort, which does not
--                       reach the primary-key index, so only a gene-leading
--                       key prunes them.
--   stgp_by_study       study-first profiling denominator.
--
-- The genomic_event_derived projections are deliberately NARROW: they omit
-- the wide free-text columns (mutation_variant, driver_*_annotation,
-- cna_cytoband, sv_event_info) that frequency / co-occurrence queries never
-- read. A query that selects one of those columns uses the base table.
--
-- Both tables are plain MergeTree (not Replacing/Collapsing), so
-- deduplicate_merge_projection_mode does not apply. No file in sql/ runs
-- lightweight DELETE/UPDATE against these tables (that would need
-- lightweight_mutation_projection_mode on tables with projections).
--
-- Re-apply: the daily clone rebuilds every table via CLONE AS and then runs
-- every sql/*.sql, so projections are recreated on each clone. Re-running
-- this file on a DB that already has them is safe: ADD ... IF NOT EXISTS is a
-- no-op and MATERIALIZE rewrites the projection parts (same result, costs a
-- rebuild). mutations_sync = 2 makes each MATERIALIZE block until every
-- replica finishes, so the clone job does not hand the DB to the MCP server
-- with half-built projections.
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
            patient_unique_id,
            hugo_gene_symbol,
            entrez_gene_id,
            gene_panel_stable_id,
            cancer_study_identifier,
            genetic_profile_stable_id,
            variant_type,
            mutation_type,
            mutation_status,
            driver_filter,
            driver_tiers_filter,
            cna_alteration,
            off_panel
        ORDER BY (cancer_study_identifier, hugo_gene_symbol, variant_type, sample_unique_id)
    );

ALTER TABLE genomic_event_derived
    MATERIALIZE PROJECTION ged_by_study_gene
    SETTINGS mutations_sync = 2;

-- ----------------------------------------------------------------------------
-- genomic_event_derived: gene-first (cross-study / cohort queries)
-- ----------------------------------------------------------------------------
-- Same columns as ged_by_study_gene minus patient_unique_id. Cross-study
-- frequency is counted per sample, and once rows are sorted gene-first the
-- two unique-id strings stop compressing well (together ~80% of this
-- projection's size), so dropping the patient id cuts its size ~40%.
-- Patient-level cross-study queries use the base table.
ALTER TABLE genomic_event_derived
    ADD PROJECTION IF NOT EXISTS ged_by_gene_study
    (
        SELECT
            sample_unique_id,
            hugo_gene_symbol,
            entrez_gene_id,
            gene_panel_stable_id,
            cancer_study_identifier,
            genetic_profile_stable_id,
            variant_type,
            mutation_type,
            mutation_status,
            driver_filter,
            driver_tiers_filter,
            cna_alteration,
            off_panel
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
