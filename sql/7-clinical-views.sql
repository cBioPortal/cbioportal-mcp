-- ============================================================================
-- Study-view chart views (treatments, categorical clinical attributes)
-- ============================================================================
-- Parameterized views that reproduce the counts shown by cBioPortal's
-- study-view charts for one study, so the agent does not have to re-derive
-- the portal's counting and NA rules:
--   - treatment_counts_in_study    — "Treatment (patient)" chart
--   - treatment_regimens_in_study  — same-day agent combinations
--   - clinical_attribute_counts    — categorical clinical attribute chart
--
-- Like the portal, all three count only patients / samples present in
-- sample_derived (patients without samples are not in the study view).
--
-- The agent-facing docs are at `cbioportal://clinical-data-guide`.
-- ============================================================================

-- ============================================================================
-- treatment_counts_in_study — Treatment (patient) chart
-- ============================================================================
-- Distinct patients per treatment AGENT, from Treatment events
-- (event_type matched case-insensitively). Matches the portal's
-- getPatientTreatments count for every agent.
--
-- Each agent row also lists the treatment types / subtypes recorded on its
-- events. Studies use different keys: TREATMENT_TYPE or Treatment_TYPE for
-- the type, SUBTYPE or TREATMENT_SUBTYPE for the subtype (MSK-CHORD puts
-- Chemo / Targeted / Immuno / Investigational ... in SUBTYPE; TCGA puts
-- Chemotherapy / Radiation Therapy ... in TREATMENT_TYPE). An agent can
-- carry several types in one study, hence the arrays.
--
-- pct_of_treated_patients is relative to patients with at least one AGENT
-- event (the portal's treatment chart total), not all study patients:
-- a patient without treatment events may simply not have had treatment
-- data collected.
--
-- Parameters:
--   study  — cancer_study.cancer_study_identifier (e.g. 'msk_chord_2024')
--
-- Usage:
--   SELECT * FROM treatment_counts_in_study(study='msk_chord_2024')
--   ORDER BY patients DESC LIMIT 20;
--
-- Columns: agent, treatment_types, treatment_subtypes, patients,
--          treated_patients, pct_of_treated_patients
-- ============================================================================

DROP VIEW IF EXISTS treatment_counts_in_study;

CREATE VIEW treatment_counts_in_study AS
WITH study_patients AS (
    SELECT DISTINCT patient_unique_id
    FROM sample_derived
    WHERE cancer_study_identifier = {study:String}
),
treatment_events AS (
    SELECT
        ce.clinical_event_id,
        concat(cs.cancer_study_identifier, '_', p.stable_id) AS patient_unique_id,
        maxIf(ced.value, ced.key = 'AGENT') AS agent,
        maxIf(ced.value, ced.key IN ('TREATMENT_TYPE', 'Treatment_TYPE')) AS treatment_type,
        maxIf(ced.value, ced.key IN ('SUBTYPE', 'TREATMENT_SUBTYPE')) AS treatment_subtype
    FROM clinical_event ce
    JOIN patient p ON ce.patient_id = p.internal_id
    JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
    JOIN clinical_event_data ced ON ce.clinical_event_id = ced.clinical_event_id
    WHERE cs.cancer_study_identifier = {study:String}
      AND lower(ce.event_type) = 'treatment'
    GROUP BY ce.clinical_event_id, patient_unique_id
    HAVING agent != ''
),
treated AS (
    SELECT *
    FROM treatment_events
    WHERE patient_unique_id IN (SELECT patient_unique_id FROM study_patients)
),
treated_total AS (
    SELECT count(DISTINCT patient_unique_id) AS n FROM treated
)
SELECT
    agent,
    arraySort(groupUniqArrayIf(treatment_type, treatment_type != '')) AS treatment_types,
    arraySort(groupUniqArrayIf(treatment_subtype, treatment_subtype != '')) AS treatment_subtypes,
    count(DISTINCT patient_unique_id) AS patients,
    any(tt.n) AS treated_patients,
    ROUND(count(DISTINCT patient_unique_id) * 100.0 / NULLIF(any(tt.n), 0), 1) AS pct_of_treated_patients
FROM treated
CROSS JOIN treated_total tt
GROUP BY agent;

-- ============================================================================
-- treatment_regimens_in_study — same-day agent combinations
-- ============================================================================
-- Groups each patient's systemic AGENT events by start_date: all agents
-- started on the same day form one regimen (e.g. CARBOPLATIN + PEMETREXED).
-- Counts distinct patients per regimen; a patient who received several
-- regimens is counted under each.
--
-- Excluded: agent INVESTIGATIONAL, events whose subtype is Investigational
-- or Prior Medications to MSK, and events whose type or subtype mentions
-- radiation.
--
-- Only meaningful when events carry real start dates. Studies where every
-- start_date is 0 (e.g. pog570_bcgsc_2020) collapse into one "regimen" per
-- patient.
--
-- Parameters:
--   study  — cancer_study.cancer_study_identifier
--
-- Usage:
--   SELECT * FROM treatment_regimens_in_study(study='msk_chord_2024')
--   ORDER BY patients DESC LIMIT 20;
--
-- Columns: regimen, n_agents, patients, treated_patients,
--          pct_of_treated_patients
-- ============================================================================

DROP VIEW IF EXISTS treatment_regimens_in_study;

CREATE VIEW treatment_regimens_in_study AS
WITH study_patients AS (
    SELECT DISTINCT patient_unique_id
    FROM sample_derived
    WHERE cancer_study_identifier = {study:String}
),
treatment_events AS (
    SELECT
        ce.clinical_event_id,
        concat(cs.cancer_study_identifier, '_', p.stable_id) AS patient_unique_id,
        any(ce.start_date) AS start_date,
        maxIf(ced.value, ced.key = 'AGENT') AS agent,
        maxIf(ced.value, ced.key IN ('TREATMENT_TYPE', 'Treatment_TYPE')) AS treatment_type,
        maxIf(ced.value, ced.key IN ('SUBTYPE', 'TREATMENT_SUBTYPE')) AS treatment_subtype
    FROM clinical_event ce
    JOIN patient p ON ce.patient_id = p.internal_id
    JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
    JOIN clinical_event_data ced ON ce.clinical_event_id = ced.clinical_event_id
    WHERE cs.cancer_study_identifier = {study:String}
      AND lower(ce.event_type) = 'treatment'
    GROUP BY ce.clinical_event_id, patient_unique_id
    HAVING agent != ''
),
systemic_events AS (
    SELECT patient_unique_id, start_date, agent
    FROM treatment_events
    WHERE patient_unique_id IN (SELECT patient_unique_id FROM study_patients)
      AND upperUTF8(agent) != 'INVESTIGATIONAL'
      AND lowerUTF8(treatment_subtype) NOT IN ('investigational', 'prior medications to msk')
      AND positionCaseInsensitiveUTF8(treatment_subtype, 'radiation') = 0
      AND positionCaseInsensitiveUTF8(treatment_type, 'radiation') = 0
),
regimen_starts AS (
    SELECT
        patient_unique_id,
        start_date,
        arraySort(groupUniqArray(agent)) AS agents
    FROM systemic_events
    GROUP BY patient_unique_id, start_date
),
treated_total AS (
    SELECT count(DISTINCT patient_unique_id) AS n FROM systemic_events
)
SELECT
    arrayStringConcat(agents, ' + ') AS regimen,
    length(agents) AS n_agents,
    count(DISTINCT patient_unique_id) AS patients,
    any(tt.n) AS treated_patients,
    ROUND(count(DISTINCT patient_unique_id) * 100.0 / NULLIF(any(tt.n), 0), 1) AS pct_of_treated_patients
FROM regimen_starts
CROSS JOIN treated_total tt
GROUP BY agents;

-- ============================================================================
-- clinical_attribute_counts — categorical clinical attribute chart
-- ============================================================================
-- Counts per value of one clinical attribute, at the attribute's level
-- (clinical_attribute_meta.patient_attribute): distinct patients for a
-- patient attribute, distinct samples for a sample attribute.
--
-- NA handling follows the portal: values '', 'NA', 'NAN', 'N/A'
-- (case-insensitive) are not counted as values, and an 'NA' row holds every
-- study patient / sample without a real value. Other placeholders such as
-- 'Unknown' stay as their own value, as in the portal. Values are grouped
-- exactly as stored (case-sensitive, untrimmed).
--
-- Returns no rows when the attribute does not exist in the study.
-- attribute is case-sensitive (e.g. 'SAMPLE_TYPE', 'SEX').
--
-- Parameters:
--   study      — cancer_study.cancer_study_identifier
--   attribute  — clinical_attribute_meta.attr_id
--
-- Usage:
--   SELECT * FROM clinical_attribute_counts(
--       study='brca_tcga_pan_can_atlas_2018', attribute='SUBTYPE'
--   ) ORDER BY count DESC;
--
-- Columns: value, count, pct_of_study, level ('patient' or 'sample')
-- ============================================================================

DROP VIEW IF EXISTS clinical_attribute_counts;

CREATE VIEW clinical_attribute_counts AS
WITH attribute_level AS (
    SELECT max(m.patient_attribute) AS is_patient, count() AS found
    FROM clinical_attribute_meta m
    JOIN cancer_study cs ON m.cancer_study_id = cs.cancer_study_id
    WHERE cs.cancer_study_identifier = {study:String}
      AND m.attr_id = {attribute:String}
),
study_entities AS (
    SELECT DISTINCT
        if((SELECT is_patient FROM attribute_level) = 1, patient_unique_id, sample_unique_id) AS entity_id
    FROM sample_derived
    WHERE cancer_study_identifier = {study:String}
),
total AS (
    SELECT count() AS n FROM study_entities
),
valued AS (
    SELECT
        cd.attribute_value AS value,
        count(DISTINCT if((SELECT is_patient FROM attribute_level) = 1,
                          cd.patient_unique_id, cd.sample_unique_id)) AS count
    FROM clinical_data_derived cd
    WHERE cd.cancer_study_identifier = {study:String}
      AND cd.attribute_name = {attribute:String}
      AND NOT (cd.attribute_value = ''
               OR upperUTF8(cd.attribute_value) IN ('NA', 'NAN', 'N/A'))
      AND if((SELECT is_patient FROM attribute_level) = 1,
             cd.patient_unique_id, cd.sample_unique_id)
          IN (SELECT entity_id FROM study_entities)
    GROUP BY cd.attribute_value
),
with_na AS (
    SELECT value, toInt64(count) AS count FROM valued
    UNION ALL
    SELECT 'NA' AS value, toInt64((SELECT n FROM total)) - toInt64((SELECT sum(count) FROM valued)) AS count
)
SELECT
    value,
    count,
    ROUND(count * 100.0 / NULLIF((SELECT n FROM total), 0), 1) AS pct_of_study,
    if((SELECT is_patient FROM attribute_level) = 1, 'patient', 'sample') AS level
FROM with_na
WHERE count > 0
  AND (SELECT found FROM attribute_level) > 0;
