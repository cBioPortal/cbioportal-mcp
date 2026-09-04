-- ============================================================================
-- Precomputed sample_count for cancer_study
-- ============================================================================
-- list_studies() (see server.py) used to compute this on every call with a
-- live COUNT(DISTINCT) across two joins (cancer_study -> patient -> sample).
-- The MCP server caches that result in-process, but the cache refresh itself
-- still paid for the join+aggregate every cycle. Since sample/patient data
-- only changes via this daily clone job, there's no reason to recompute the
-- count more often than once per clone: precompute it here, once, right
-- after clone+load, same as the OncoTree denormalization in
-- 2-add-oncotree-fields.sql.
--
-- After this runs, list_studies() reads sample_count directly off
-- cancer_study with no joins and no aggregation.
--
-- ClickHouse mutations don't support correlated subqueries (a subquery
-- referencing the outer table's row), so the per-study count is computed
-- into a Join-engine table first and pulled in with joinGet(), which
-- ClickHouse evaluates per-row instead of correlating. Join(ANY, LEFT, ...)
-- makes joinGet() return the column default (0) for a study with no
-- patients, matching the original query's LEFT JOIN semantics.
-- ============================================================================

ALTER TABLE cancer_study ADD COLUMN IF NOT EXISTS sample_count UInt32 DEFAULT 0;

ALTER TABLE cancer_study MODIFY COLUMN sample_count UInt32
  COMMENT 'Distinct sample count for this study, precomputed at LLM-prep time from patient/sample. Avoids a live COUNT(DISTINCT) join at query time; only as fresh as the last daily clone.';

DROP TABLE IF EXISTS study_sample_counts_derived;

CREATE TABLE study_sample_counts_derived
(
    cancer_study_id UInt32,
    sample_count UInt32
)
ENGINE = Join(ANY, LEFT, cancer_study_id);

INSERT INTO study_sample_counts_derived
SELECT
    p.cancer_study_id AS cancer_study_id,
    COUNT(DISTINCT s.internal_id) AS sample_count
FROM patient p
LEFT JOIN sample s ON s.patient_id = p.internal_id
GROUP BY p.cancer_study_id;

-- mutations_sync = 2 forces this mutation to complete (all replicas) before
-- the query returns, instead of the default async background execution --
-- required here since the very next statement drops the derived table this
-- mutation reads from via joinGet().
ALTER TABLE cancer_study
  UPDATE sample_count = joinGet('study_sample_counts_derived', 'sample_count', cancer_study_id)
  WHERE 1
  SETTINGS mutations_sync = 2;

DROP TABLE IF EXISTS study_sample_counts_derived;
