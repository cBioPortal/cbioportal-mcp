# Treatment Data Query Guide

## Overview

Treatment data in cBioPortal is stored as **clinical events**, separate from clinical attributes. This allows for timeline-based treatment records with start/stop dates.

## Start Here: Treatment Views

For "most common treatments / agents / regimens in study X", use the parameterized views (documented in `cbioportal://clinical-data-guide`, Study-View Chart Counts):

```sql
-- Patients per agent (the portal's Treatment chart), with type/subtype arrays
SELECT * FROM treatment_counts_in_study(study='msk_chord_2024') ORDER BY patients DESC LIMIT 20;

-- Same-day agent combinations (investigational, prior-medication and radiation events excluded)
SELECT * FROM treatment_regimens_in_study(study='msk_chord_2024') ORDER BY patients DESC LIMIT 20;
```

Write event queries (below) only for subgroups, timelines or keys the views do not expose.

## Key Tables

| Table | Description |
|-------|-------------|
| `treatment_event_agents` | One row per Treatment event that has an AGENT: `cancer_study_identifier`, `patient_unique_id`, `clinical_event_id`, `start_date`, `agent`, `treatment_type`, `treatment_subtype`. Type and subtype come from the same event as the agent (TREATMENT_TYPE / Treatment_TYPE and SUBTYPE / TREATMENT_SUBTYPE keys are already merged). Event type matching is already case-insensitive. |
| `clinical_event_derived` | All events: `cancer_study_identifier`, `patient_stable_id`, `event_type`, `start_date`, `stop_date` |
| `clinical_event_data_derived` | Every key/value pair of every event: `cancer_study_identifier`, `patient_unique_id`, `event_type`, `key`, `value`, `start_date`, `stop_date`. It has no event id, so it cannot pair an agent with its subtype; use `treatment_event_agents` for that. |

`start_date` / `stop_date` are days from diagnosis.

## Event Types

Not all studies have all event types. Common ones include:

| Event Type | Description |
|------------|-------------|
| `Treatment` / `TREATMENT` | Drug/therapy administration |
| `SURGERY` | Surgical procedures |
| `Diagnosis` | Diagnosis events |
| `LAB_TEST` | Laboratory results |
| `Sequencing` | Sequencing events |
| `Sample acquisition` | Sample collection |
| `PATHOLOGY` / `Pathology` | Pathology reports |

## Basic Treatment Queries

### List Available Event Types in a Study

```sql
SELECT event_type, COUNT(*) AS event_count
FROM clinical_event_derived
WHERE cancer_study_identifier = 'your_study_id'
GROUP BY event_type
ORDER BY event_count DESC;
```

### List Treatment Data Keys in a Study

```sql
SELECT key, COUNT(*) AS cnt
FROM clinical_event_data_derived
WHERE cancer_study_identifier = 'your_study_id'
  AND event_type IN ('Treatment', 'TREATMENT')
GROUP BY key
ORDER BY cnt DESC;
```

### Get Most Common Treatment Agents

```sql
SELECT agent, COUNT(DISTINCT patient_unique_id) AS patient_count
FROM treatment_event_agents
WHERE cancer_study_identifier = 'your_study_id'
GROUP BY agent
ORDER BY patient_count DESC
LIMIT 20;
```

## Advanced Treatment Queries

### Get Treatment with Multiple Attributes

`treatment_event_agents` already pairs each agent with the type and subtype recorded on the same event:

```sql
SELECT
    treatment_subtype,
    agent,
    COUNT(DISTINCT patient_unique_id) AS patients
FROM treatment_event_agents
WHERE cancer_study_identifier = 'your_study_id'
GROUP BY treatment_subtype, agent
ORDER BY patients DESC;
```

### Treatment Timeline Relative to Sequencing

Find treatments given before or after sequencing:

```sql
WITH seq_dates AS (
    SELECT concat(cancer_study_identifier, '_', patient_stable_id) AS patient_unique_id,
           MIN(start_date) AS seq_date
    FROM clinical_event_derived
    WHERE cancer_study_identifier = 'your_study_id'
      AND event_type = 'Sequencing'
    GROUP BY patient_unique_id
)
SELECT
    CASE
        WHEN tx.start_date < seq.seq_date THEN 'Before Sequencing'
        ELSE 'After Sequencing'
    END AS timing,
    tx.agent AS treatment,
    COUNT(DISTINCT tx.patient_unique_id) AS patients
FROM treatment_event_agents tx
JOIN seq_dates seq ON tx.patient_unique_id = seq.patient_unique_id
WHERE tx.cancer_study_identifier = 'your_study_id'
GROUP BY timing, treatment
ORDER BY timing, patients DESC;
```

### Link Treatment to Genomic Alterations

Find patients with a specific mutation and their treatments:

```sql
WITH mutated_patients AS (
    SELECT DISTINCT patient_unique_id
    FROM genomic_event_derived
    WHERE cancer_study_identifier = 'your_study_id'
      AND hugo_gene_symbol = 'EGFR'
      AND variant_type = 'mutation'
)
SELECT
    tx.agent AS treatment,
    COUNT(DISTINCT tx.patient_unique_id) AS treated_patients
FROM treatment_event_agents tx
WHERE tx.cancer_study_identifier = 'your_study_id'
  AND tx.patient_unique_id IN (SELECT patient_unique_id FROM mutated_patients)
GROUP BY tx.agent
ORDER BY treated_patients DESC;
```

## Study-Specific Treatment Data

Treatment data availability and structure varies significantly by study. Check the study-specific guide for:
- Available treatment keys
- Treatment subtype classifications
- Study-specific caveats

Example: MSK-CHORD has detailed treatment data including:
- AGENT, SUBTYPE, TREATMENT_TYPE
- Investigational drug flags
- Prior medications tracking

Most TCGA studies have limited or no treatment data.

## Important Caveats

### Missing Treatment Data Limitation

Treatment data in cBioPortal has a critical limitation:
- **We cannot distinguish** between "treatment was never collected" vs "patient did not receive treatment"
- This means **percentages are unreliable** and should NOT be calculated
- **Always report raw counts**: "6,319 patients received Fluorouracil" NOT "25% of patients received Fluorouracil"
- Do NOT calculate percentages of total patients for treatment data

### Query Pattern Guidelines

**For broad questions** like "What treatments did patients receive?":
1. First summarize by **treatment category (SUBTYPE)**: Chemo, Immuno, Targeted, etc.
2. Then list **top agents within each category**

```sql
-- Step 1: Summarize by category
SELECT
    treatment_subtype AS treatment_category,
    COUNT(DISTINCT patient_unique_id) AS patient_count
FROM treatment_event_agents
WHERE cancer_study_identifier = 'your_study_id'
  AND treatment_subtype != ''
GROUP BY treatment_subtype
ORDER BY patient_count DESC;

-- Step 2: Top agents per category
SELECT
    treatment_subtype AS category,
    agent,
    COUNT(DISTINCT patient_unique_id) AS patients
FROM treatment_event_agents
WHERE cancer_study_identifier = 'your_study_id'
  AND treatment_subtype != ''
GROUP BY treatment_subtype, agent
ORDER BY category, patients DESC;
```

**For specific questions** like "Most common treatment?":
- Query by AGENT key directly
- Report patient count, NOT percentage

**For treatment type questions** like "Most common chemotherapy?":
- Filter by `SUBTYPE = 'Chemo'`
- Then report by AGENT

## Other Notes

1. **Event type casing**: Some studies use `Treatment`, others use `TREATMENT`. `treatment_event_agents` already covers both; on `clinical_event_derived` / `clinical_event_data_derived` query for both
2. **Date interpretation**: start_date/stop_date are typically days from diagnosis
3. **Not all studies have treatment data**: TCGA studies often lack detailed treatment info
4. **Key availability varies**: Always discover available keys before assuming data exists
5. **Patient vs event counts**: One patient can have many treatment events; use COUNT(DISTINCT patient_unique_id) for patient counts
