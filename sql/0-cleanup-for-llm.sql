-- ============================================================================
-- cBioPortal Database Cleanup for LLM Agents
-- ============================================================================
-- Run this script AFTER duplicating/copying a cBioPortal database to create
-- an LLM-friendly version. These changes remove confusing columns that cause
-- agents to write incorrect queries.
--
-- WARNING: This modifies the database schema! Only run on a COPY of your data.
-- ============================================================================

-- ============================================================================
-- 1. Remove sample.sample_type column
-- ============================================================================
-- PROBLEM: The sample.sample_type column contains generic values like
-- "Primary Solid Tumor" for ALL tumor samples. LLM agents confuse this with
-- the actual primary/metastatic distinction stored in clinical_data_derived
-- as attribute_name='SAMPLE_TYPE' (with values: Primary, Metastasis, etc.)
--
-- EXAMPLE OF CONFUSION:
--   Q: "How many primary samples are in MSK-CHORD?"
--   Wrong query: SELECT COUNT(*) FROM sample WHERE sample_type LIKE '%Primary%'
--                Returns 25,040 (ALL samples)
--   Correct query: SELECT COUNT(*) FROM clinical_data_derived
--                  WHERE attribute_name='SAMPLE_TYPE' AND attribute_value='Primary'
--                  Returns 15,928 (actual primary samples)
--
-- SOLUTION: Remove the column to prevent agents from using it incorrectly.
-- ============================================================================

ALTER TABLE sample DROP COLUMN IF EXISTS sample_type;

-- ============================================================================
-- 2. Future cleanup items (add as needed)
-- ============================================================================
-- Add more cleanup statements here as we identify confusing schema elements.
-- Each should include:
--   - Description of the problem
--   - Example of the confusion it causes
--   - Why removing/changing it helps LLM agents

-- Example template:
-- -- PROBLEM: [Column/table] causes [specific confusion]
-- -- EXAMPLE: [Show wrong vs correct query]
-- -- SOLUTION: [What this command does]
-- ALTER TABLE xxx DROP COLUMN IF EXISTS yyy;
