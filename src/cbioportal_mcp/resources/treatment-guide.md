# Treatment Data Query Guide

## Overview

Treatment data in cBioPortal is stored in **clinical event tables**, separate from clinical attributes. This allows for timeline-based treatment records with start/stop dates.

## Key Tables

| Table | Description |
|-------|-------------|
| `clinical_event` | Event records with patient_id, event_type, start_date, stop_date |
| `clinical_event_data` | Key-value pairs linked to each clinical_event_id |

## Schema

```
clinical_event
├── clinical_event_id (PK)
├── patient_id (FK → patient.internal_id)
├── event_type (Treatment, TREATMENT, Diagnosis, SURGERY, etc.)
├── start_date (days from diagnosis)
└── stop_date (days from diagnosis)

clinical_event_data
├── clinical_event_id (FK)
├── key (AGENT, SUBTYPE, etc.)
└── value
```

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
SELECT DISTINCT ce.event_type, COUNT(*) as event_count
FROM clinical_event ce
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'your_study_id'
GROUP BY ce.event_type
ORDER BY event_count DESC;
```

### List Treatment Data Keys in a Study

```sql
SELECT DISTINCT ced.key, COUNT(*) as cnt
FROM clinical_event ce
JOIN clinical_event_data ced ON ce.clinical_event_id = ced.clinical_event_id
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'your_study_id'
  AND ce.event_type IN ('Treatment', 'TREATMENT')
GROUP BY ced.key
ORDER BY cnt DESC;
```

### Get Most Common Treatment Agents

```sql
SELECT 
    ced.value as agent,
    COUNT(DISTINCT ce.patient_id) as patient_count
FROM clinical_event ce
JOIN clinical_event_data ced ON ce.clinical_event_id = ced.clinical_event_id
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'your_study_id'
  AND ce.event_type IN ('Treatment', 'TREATMENT')
  AND ced.key = 'AGENT'
GROUP BY ced.value
ORDER BY patient_count DESC
LIMIT 20;
```

## Advanced Treatment Queries

### Get Treatment with Multiple Attributes

Treatment events often have multiple key-value pairs. To get agent + subtype together:

```sql
SELECT 
    agent.value as agent,
    subtype.value as treatment_subtype,
    COUNT(DISTINCT ce.patient_id) as patients
FROM clinical_event ce
JOIN clinical_event_data agent ON ce.clinical_event_id = agent.clinical_event_id AND agent.key = 'AGENT'
LEFT JOIN clinical_event_data subtype ON ce.clinical_event_id = subtype.clinical_event_id AND subtype.key = 'SUBTYPE'
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'your_study_id'
  AND ce.event_type IN ('Treatment', 'TREATMENT')
GROUP BY agent.value, subtype.value
ORDER BY patients DESC;
```

### Treatment Timeline Relative to Sequencing

Find treatments given before or after sequencing:

```sql
WITH seq_dates AS (
    SELECT ce.patient_id, MIN(ce.start_date) as seq_date
    FROM clinical_event ce
    JOIN patient p ON ce.patient_id = p.internal_id
    JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
    WHERE cs.cancer_study_identifier = 'your_study_id'
      AND ce.event_type = 'Sequencing'
    GROUP BY ce.patient_id
)
SELECT 
    CASE 
        WHEN tx.start_date < seq.seq_date THEN 'Before Sequencing'
        ELSE 'After Sequencing'
    END as timing,
    agent.value as treatment,
    COUNT(DISTINCT tx.patient_id) as patients
FROM clinical_event tx
JOIN clinical_event_data agent ON tx.clinical_event_id = agent.clinical_event_id AND agent.key = 'AGENT'
JOIN seq_dates seq ON tx.patient_id = seq.patient_id
JOIN patient p ON tx.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'your_study_id'
  AND tx.event_type IN ('Treatment', 'TREATMENT')
GROUP BY timing, agent.value
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
    agent.value as treatment,
    COUNT(DISTINCT ce.patient_id) as treated_patients
FROM clinical_event ce
JOIN clinical_event_data agent ON ce.clinical_event_id = agent.clinical_event_id AND agent.key = 'AGENT'
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
JOIN mutated_patients mp ON CONCAT(cs.cancer_study_identifier, '_', p.stable_id) = mp.patient_unique_id
WHERE cs.cancer_study_identifier = 'your_study_id'
  AND ce.event_type IN ('Treatment', 'TREATMENT')
GROUP BY agent.value
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

## Notes & Caveats

1. **Event type casing**: Some studies use `Treatment`, others use `TREATMENT` — query for both
2. **Date interpretation**: start_date/stop_date are typically days from diagnosis
3. **Not all studies have treatment data**: TCGA studies often lack detailed treatment info
4. **Key availability varies**: Always discover available keys before assuming data exists
5. **Patient vs event counts**: One patient can have many treatment events; use COUNT(DISTINCT patient_id) for patient counts
