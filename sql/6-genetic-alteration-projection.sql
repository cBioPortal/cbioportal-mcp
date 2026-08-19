-- ============================================================================
-- Alternative-order projection for genetic_alteration_derived
-- ============================================================================
-- genetic_alteration_derived (10.29B rows / 28.1 GiB) is sorted by
-- (cancer_study_identifier, hugo_gene_symbol, profile_type, sample_unique_id).
-- Queries that filter on profile_type without constraining an earlier
-- sort-key column (e.g. "which samples have an mrna_median_Zscores profile")
-- force a full-table scan even for a single-value equality check, because
-- profile_type is only the third sort-key column.
--
-- A five-day trace of the llm_user ClickHouse account found nine such
-- queries responsible for 293s/week (49.3% of total execution time) --
-- see the P99 query-optimization report:
-- https://gist.github.com/alisman/9cda4965ba6b20ed1abcb3e432a6d1c2
--
-- A projection stores a second physical copy of the table sorted by an
-- alternative key; ClickHouse picks whichever ordering serves the query.
--
-- Trade-offs -- read before applying:
--   - Roughly doubles on-disk size for this table (~28 GiB -> ~56 GiB).
--   - MATERIALIZE PROJECTION is a heavy one-time mutation over 10.29B rows;
--     run it during a low-traffic window.
--   - sql/README.md notes these files re-apply on EVERY daily clone (the
--     CronJob clones a fresh DB, then re-runs sql/*.sql against it). That
--     means this materialization is NOT one-time in production -- it re-runs
--     in full on every daily clone. Confirm the daily cron window can absorb
--     a 10.29B-row MATERIALIZE PROJECTION before merging this; if it can't,
--     this needs a different mechanism (e.g. only materializing when the
--     projection doesn't already exist on an incrementally-updated clone)
--     rather than being applied as-is here.
-- ============================================================================

ALTER TABLE genetic_alteration_derived
ADD PROJECTION IF NOT EXISTS by_profile_type (
    SELECT *
    ORDER BY (profile_type, hugo_gene_symbol, cancer_study_identifier, sample_unique_id)
);

ALTER TABLE genetic_alteration_derived MATERIALIZE PROJECTION by_profile_type;
