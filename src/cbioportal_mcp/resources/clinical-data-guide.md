# Clinical Data Query Guide

## Overview
Clinical data in cBioPortal is stored at both patient and sample levels. Understanding the distinction is crucial for accurate analysis.

## Data Organization

### Patient-Level vs Sample-Level Data
- **Patient-level**: Demographics, overall survival, disease stage (stored once per patient)
- **Sample-level**: Sample type, sequencing platform, purity (can have multiple per patient)

### Key Tables
- `clinical_patient`: Patient-level clinical attributes
- `clinical_sample`: Sample-level clinical attributes
- `clinical_data_derived`: Pre-joined view combining both levels
- `clinical_attribute_meta`: Metadata about available clinical attributes

## Recommended Approach: Use clinical_data_derived

The `clinical_data_derived` table is pre-joined and optimized for most queries:

```sql
-- Get clinical data for specific attributes
SELECT
    sample_unique_id,
    patient_unique_id,
    attribute_name,
    attribute_value
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name IN ('CANCER_TYPE', 'SAMPLE_TYPE', 'TMB_NONSYNONYMOUS');
```

## Clinical Attribute Discovery

### Use clinical_attribute_meta for Discovering Available Attributes
**Always start here** to see what clinical attributes are available for a specific study:

```sql
-- Discover available clinical attributes for a study
SELECT
    attr_id,
    description,
    patient_attribute,
    cancer_study_id
FROM clinical_attribute_meta
WHERE
    cancer_study_id = (
        SELECT cancer_study_id
        FROM cancer_study
        WHERE cancer_study_identifier = 'your_study_id'
    )
ORDER BY patient_attribute, attr_id;
```

**Key Fields:**
- **attr_id**: matches attr_id in clinical_sample/clinical_patient tables
- **description**: human-readable description of the attribute
- **patient_attribute**: true = patient attribute, false = sample attribute
- **cancer_study_id**: links to cancer_study table (filter by study)

## Attribute Semantics and Matching

### Case-Insensitive Matching for Attribute Values

Clinical values are free text across studies and may differ only by case. For example, a controlled-looking value such as germline mutation status may appear as `GERMLINE`, `Germline`, or another case variant.

When filtering `clinical_data_derived.attribute_value`, use case-insensitive matching unless you have already profiled the exact values in the target study:

```sql
-- Correct: case-insensitive clinical value filter
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = 'your_study_id'
  AND attribute_name = 'MUTATION_STATUS'
  AND upper(attribute_value) = 'GERMLINE';
```

Do not write `attribute_value = 'GERMLINE'` without first checking all distinct values for that attribute in the study.

### Query the Requested Attribute, Not a Proxy

Do not infer one clinical attribute from a related subtype or marker. Query the actual requested attribute when it exists.

Examples:

- HER2-negative breast cancer: search for `HER2_STATUS`, `HER2`, IHC, or FISH attributes. Do not infer HER2 status from Luminal A/B subtype.
- ER/PR status: query ER/PR clinical attributes directly. Do not infer from molecular subtype alone.
- PD-L1 status: query PD-L1 attributes directly. Do not infer from cancer type or immune subtype.

Discovery pattern:

```sql
SELECT DISTINCT
    attribute_name,
    attribute_value
FROM clinical_data_derived
WHERE cancer_study_identifier = 'brca_tcga_pan_can_atlas_2018'
  AND (
      upper(attribute_name) LIKE '%HER2%'
      OR upper(attribute_name) LIKE '%ER_STATUS%'
      OR upper(attribute_name) LIKE '%PR_STATUS%'
      OR upper(attribute_name) LIKE '%IHC%'
      OR upper(attribute_name) LIKE '%FISH%'
  )
ORDER BY attribute_name, attribute_value
LIMIT 200;
```

If the requested attribute is absent, say it is absent and offer the closest available attribute as a proxy only with an explicit caveat.

## Finding Studies by Cancer Type

When searching for studies about a specific cancer type (e.g., "glioblastoma studies"), **always start with `search_oncotree()`** to resolve the correct OncoTree codes.

### Search Strategy (OncoTree-First)
1. **Call `search_oncotree(search_term)`** to get the correct OncoTree codes, including deprecated code mappings
2. Use the returned codes to filter `cancer_study.type_of_cancer_id`
3. Also search `cancer_study.name` and `cancer_study_identifier` for broader matches

### Example: "How many patients have ALL?"

`search_oncotree("ALL")` returns:
- **BLL** (B-Lymphoblastic Leukemia/Lymphoma) — `replacedCodes: ["ALL"]`, score 90
- **TLL** (T-Lymphoblastic Leukemia/Lymphoma) — `replacedCodes: ["ALL"]`, score 90

"ALL" is a **deprecated** OncoTree code, now split into BLL and TLL. Query using the current codes:
```sql
SELECT COUNT(DISTINCT patient_unique_id) as patient_count
FROM clinical_data_derived
WHERE cancer_study_identifier IN (
    SELECT cancer_study_identifier FROM cancer_study
    WHERE type_of_cancer_id IN ('bll', 'tll')
);
```

### Example: Finding Glioblastoma Studies

`search_oncotree("glioblastoma")` returns GB (Glioblastoma, IDH-Wildtype) with `replacedCodes: ["GBM"]`.

```sql
-- Use OncoTree codes from search_oncotree results
SELECT cancer_study_identifier, name, type_of_cancer_id
FROM cancer_study
WHERE type_of_cancer_id IN ('gb', 'difg', 'gnos', 'lggnos', 'hggnos')
   OR LOWER(name) LIKE '%glioblastoma%'
   OR LOWER(name) LIKE '%gbm%'
ORDER BY name;
```

### Important Notes
- **Never use `LIKE '%abbreviation%'`** for short cancer type codes — "ALL" matches "metALLic", etc.
- Always resolve through `search_oncotree()` first to find the correct `type_of_cancer_id` values
- Deprecated codes (like ALL, GBM, DLBCL) appear in the `replacedCodes` field of their successor entries
- `type_of_cancer_id` values in the database are **lowercase** (e.g., `bll`, `gb`, `luad`)

## Common Clinical Attributes

### Sample-Level Attributes:
- `SAMPLE_TYPE`: Primary, Metastasis, Recurrence, etc.
- `SEQUENCING_CENTER`: Where sequencing was performed
- `TUMOR_PURITY`: Estimated tumor cell percentage
- `PLATFORM`: Sequencing platform used
- `TMB_NONSYNONYMOUS`: Tumor mutational burden
- `MUTATION_COUNT`: Total mutation count per sample

### Patient-Level Attributes:
- `CANCER_TYPE`: Broad cancer category
- `CANCER_TYPE_DETAILED`: Specific cancer subtype
- `SEX`: Patient gender
- `AGE`: Age at diagnosis — can be floored or capped for de-identification (see "Age statistics" below)
- `OS_MONTHS`: Overall survival time in months
- `OS_STATUS`: Overall survival status (0:LIVING, 1:DECEASED or similar)

### Comparing a Clinical Metric Across Cancer Types (e.g. TMB)

- **One consistently processed cohort.** Never average an attribute across hundreds of heterogeneous studies — pipelines, panels and units differ. Use `cancer_study_query_preferences` `'pan_cancer_tcga'` and group by study (or per-sample `CANCER_TYPE`).
- **Report mean AND median.** TMB is skewed by hypermutators, so the rankings disagree (pan_cancer_tcga: highest mean is UCEC 35.7; highest median is SKCM 14.9, then LUSC 7.7, LUAD 6.7, BLCA 5.8). Name the attribute you used.
- **TMB = `TMB_NONSYNONYMOUS`** (mutations/Mb, parse with `toFloat64OrNull`). Never `COUNT(*)` of mutation rows or `MUTATION_COUNT` as a TMB proxy.

```sql
SELECT cancer_study_identifier AS study,
       count(v) AS n,
       round(avg(v), 2) AS mean_tmb,
       round(quantile(0.5)(v), 2) AS median_tmb
FROM (
    SELECT cancer_study_identifier, toFloat64OrNull(attribute_value) AS v
    FROM clinical_data_derived
    WHERE attribute_name = 'TMB_NONSYNONYMOUS'
      AND cancer_study_identifier IN (SELECT cancer_study_identifier FROM cancer_study_query_preferences
                                      WHERE preference_name = 'pan_cancer_tcga')
)
GROUP BY study
ORDER BY median_tmb DESC;
```

## Survival Analysis Queries

Survival data is stored as clinical attributes. Common patterns:

```sql
-- Get survival data for a study
SELECT 
    patient_unique_id,
    MAX(CASE WHEN attribute_name = 'OS_MONTHS' THEN toFloat64OrNull(attribute_value) END) as os_months,
    MAX(CASE WHEN attribute_name = 'OS_STATUS' THEN attribute_value END) as os_status
FROM clinical_data_derived
WHERE cancer_study_identifier = 'your_study'
GROUP BY patient_unique_id;
```

**Never report a median or average of `OS_MONTHS` (or any `*_MONTHS`) as "median survival".** Survival data is censored (`0:LIVING` patients have not had the event yet), so `median()`, `quantile(0.5)`, and `AVG()` over `OS_MONTHS` are wrong. Median OS, log-rank p-values, and hazard ratios require Kaplan-Meier / Cox — hand off to cBioPortal Group Comparison → Survival (link via the navigator) or R (`survival::survfit`) / Python (`lifelines`). See the HARD RULES in `cbioportal://statistical-tests-guide`. If fewer than half of a group's patients have an event, the KM median is likely **not reached** — say so; never substitute a raw median.

What ClickHouse can give is a descriptive per-group summary (one row per patient; patients without OS are excluded):

```sql
-- Describe survival data per group (e.g., mutated vs wild-type) — no median
WITH mut AS (
    SELECT DISTINCT patient_unique_id FROM genomic_event_derived
    WHERE cancer_study_identifier = 'your_study' AND hugo_gene_symbol = 'TP53' AND variant_type = 'mutation'
),
os AS (
    SELECT patient_unique_id,
        maxIf(toFloat64OrNull(attribute_value), attribute_name = 'OS_MONTHS') AS os_months,
        maxIf(attribute_value, attribute_name = 'OS_STATUS') AS os_status
    FROM clinical_data_derived
    WHERE cancer_study_identifier = 'your_study' AND attribute_name IN ('OS_MONTHS', 'OS_STATUS')
    GROUP BY patient_unique_id
)
SELECT
    if(patient_unique_id IN (SELECT patient_unique_id FROM mut), 'Mutated', 'Wild-type') AS group_name,
    count() AS n_patients,
    countIf(startsWith(os_status, '1')) AS n_events,
    countIf(startsWith(os_status, '0')) AS n_censored,
    min(os_months) AS min_followup_months,
    max(os_months) AS max_followup_months
FROM os
WHERE os_months IS NOT NULL AND os_status != ''
GROUP BY group_name;
```

### Cancer Type Selection Guidance:
**CANCER_TYPE vs CANCER_TYPE_DETAILED**: Choose based on question specificity
- **CANCER_TYPE**: broader categories like 'Non-Small Cell Lung Cancer', 'Breast Cancer'
- **CANCER_TYPE_DETAILED**: specific subtypes like 'Spindle Cell Carcinoma of the Lung', 'Invasive Ductal Carcinoma'
- **Decision**: Match the attribute to the level of detail requested in the question
- **When unsure**: start with CANCER_TYPE for broader matching

## Study-View Chart Counts (views)

To reproduce a cBioPortal study-view pie/bar chart for one study, use these parameterized views instead of hand-writing the aggregation. They apply the portal's counting unit, NA rules and "patients with samples only" scope.

### Categorical attribute: `clinical_attribute_counts(study, attribute)`

```sql
SELECT * FROM clinical_attribute_counts(study='msk_chord_2024', attribute='SAMPLE_TYPE')
ORDER BY count DESC;
-- Primary 15,928 | Metastasis 8,878 | Unknown 136 | Local Recurrence 98  (level = sample)
```

- `value` — attribute value as stored; `'NA'` row = study patients/samples with no value or a value of `''`, `NA`, `NAN`, `N/A` (portal rule). `Unknown` stays its own value.
- `count` — distinct patients for a patient attribute, distinct samples for a sample attribute (`level` says which, from `clinical_attribute_meta.patient_attribute`).
- `pct_of_study` — `count` / all patients (or samples) in the study.
- `attribute` is the exact `attr_id` (case-sensitive). No rows = attribute not in this study.
- Patients without samples are not counted, as in the portal: `os_target_gdc` has `SEX` for 383 patients but only 153 have samples, so the view returns Male 87, Female 66.

### Treatments: `treatment_counts_in_study(study)` and `treatment_regimens_in_study(study)`

```sql
SELECT agent, treatment_subtypes, patients
FROM treatment_counts_in_study(study='msk_chord_2024')
ORDER BY patients DESC LIMIT 10;
-- FLUOROURACIL ['Chemo'] 6,319 | LEUCOVORIN 5,573 | OXALIPLATIN 5,489 | ...
```

- One row per `AGENT` = the portal's Treatment (patient) chart. `patients` = distinct patients who received the agent.
- `treatment_types` / `treatment_subtypes` — arrays of the type/subtype values on that agent's events (keys differ by study: MSK-CHORD uses `SUBTYPE` = Chemo, Targeted, Immuno, Investigational…; TCGA uses `TREATMENT_TYPE` = Chemotherapy, Radiation Therapy…).
- `pct_of_treated_patients` — share of patients with any treatment event. Never divide by all study patients (see `cbioportal://treatment-guide`).
- For systemic therapy only, drop investigational and radiation rows: `WHERE agent != 'INVESTIGATIONAL' AND NOT arrayExists(t -> t ILIKE '%radiation%' OR t = 'Investigational', arrayConcat(treatment_types, treatment_subtypes))`. In TCGA studies radiation is recorded as an agent (e.g. `Radiation 1` in `brca_tcga_pan_can_atlas_2018`).
- `treatment_regimens_in_study` groups agents a patient started on the same day into one regimen (`CARBOPLATIN + PEMETREXED`), already excluding investigational, prior-medication and radiation events. A patient is counted under every regimen they received. Needs real start dates: studies where all `start_date` = 0 collapse into one regimen per patient.

The views are study-wide. For a subgroup (e.g. one cancer type in a multi-cancer study), filter the event table directly:

```sql
SELECT value AS agent, count(DISTINCT patient_unique_id) AS patients
FROM clinical_event_data_derived
WHERE cancer_study_identifier = 'msk_chord_2024'
  AND lower(event_type) = 'treatment'
  AND key = 'AGENT'
  AND patient_unique_id IN (
      SELECT patient_unique_id FROM clinical_data_derived
      WHERE cancer_study_identifier = 'msk_chord_2024'
        AND attribute_name = 'CANCER_TYPE'
        AND attribute_value = 'Non-Small Cell Lung Cancer')
GROUP BY agent
ORDER BY patients DESC
LIMIT 10;
-- CARBOPLATIN 3,371 | PEMETREXED 3,347 | PEMBROLIZUMAB 1,569 | INVESTIGATIONAL 1,479 | ...
```

## Query Patterns

### 1. Filter Samples by Clinical Criteria

```sql
-- Get samples with specific clinical characteristics
SELECT DISTINCT
    sample_unique_id,
    patient_unique_id
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND (
        (attribute_name = 'CANCER_TYPE' AND attribute_value = 'Breast Cancer')
        OR (attribute_name = 'SAMPLE_TYPE' AND attribute_value = 'Primary')
    );
```

### 2. Aggregate Clinical Data

```sql
-- Count samples by cancer type
SELECT
    attribute_value as cancer_type,
    COUNT(DISTINCT sample_unique_id) as sample_count,
    COUNT(DISTINCT patient_unique_id) as patient_count
FROM clinical_data_derived
WHERE
    cancer_study_identifier = 'your_study_id'
    AND attribute_name = 'CANCER_TYPE'
GROUP BY attribute_value
ORDER BY sample_count DESC;
```

### 3. Patient Demographics Analysis

```sql
-- Get patient demographics summary
WITH patient_data AS (
    SELECT DISTINCT
        patient_unique_id,
        anyIf(attribute_value, attribute_name = 'SEX') as sex,
        anyIf(toFloat64OrNull(attribute_value), attribute_name = 'AGE') as age
    FROM clinical_data_derived
    WHERE
        cancer_study_identifier = 'your_study_id'
        AND attribute_name IN ('SEX', 'AGE')
    GROUP BY patient_unique_id
)
SELECT
    sex,
    COUNT(*) as patient_count,
    AVG(age) as avg_age,
    MIN(age) as min_age,
    MAX(age) as max_age
FROM patient_data
WHERE sex IS NOT NULL AND age IS NOT NULL
GROUP BY sex;
```

### Age statistics: check for a floor or cap first

Some studies floor or cap `AGE` for de-identification — e.g. every child recorded as 18, or everyone over 89 as 89. A median or mean over such a column is wrong. Before reporting age statistics, check how many patients sit exactly at the minimum or maximum:

```sql
SELECT
    arrayMin(ages) AS min_age, arrayMax(ages) AS max_age,
    countEqual(ages, min_age) AS at_min, countEqual(ages, max_age) AS at_max, length(ages) AS patients
FROM (
    SELECT groupArray(toFloat64OrNull(attribute_value)) AS ages
    FROM clinical_data_derived
    WHERE cancer_study_identifier = 'your_study_id' AND attribute_name = 'AGE'
      AND toFloat64OrNull(attribute_value) IS NOT NULL
);
```

If a large share of patients sits at one boundary, compute age from `DAYS_TO_BIRTH` instead (negative days from birth to diagnosis): age in years = `-toFloat64OrNull(attribute_value) / 365.25`. Tell the user which attribute you used and why. Check the study guide too — it may already name the right attribute. All TARGET GDC studies (`*_target_gdc`) floor `AGE` at 18 — use `DAYS_TO_BIRTH` for them.

## Raw Table Queries (Advanced)

If you need to use raw clinical tables instead of the derived view:

### Patient-Level Query:
```sql
-- Query patient-level data directly
SELECT
    cp.patient_id,
    cp.attr_id,
    cp.attr_value
FROM cancer_study cs
JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
JOIN clinical_patient cp ON p.internal_id = cp.patient_id
WHERE
    cs.cancer_study_identifier = 'your_study_id'
    AND cp.attr_id = 'CANCER_TYPE';
```

### Sample-Level Query:
```sql
-- Query sample-level data directly
SELECT
    cs_sample.internal_id as sample_id,
    cs_sample.attr_id,
    cs_sample.attr_value
FROM cancer_study cs
JOIN patient p ON cs.cancer_study_id = p.cancer_study_id
JOIN sample s ON p.internal_id = s.patient_id
JOIN clinical_sample cs_sample ON s.internal_id = cs_sample.internal_id
WHERE
    cs.cancer_study_identifier = 'your_study_id'
    AND cs_sample.attr_id = 'SAMPLE_TYPE';
```

## Treatment and Clinical Events Data

Treatment data is stored separately from clinical attributes, in the clinical events tables. For per-agent or per-regimen patient counts in one study, use `treatment_counts_in_study` / `treatment_regimens_in_study` (see Study-View Chart Counts above). Details: `cbioportal://treatment-guide`.

### Key Tables for Treatment Data
- `clinical_event`: Contains event records (Treatment, Diagnosis, Surgery, etc.)
- `clinical_event_data`: Contains key-value data for each event

### Query Treatment Information

```sql
-- Find most common treatments in a study
SELECT 
    ced.value as treatment_agent,
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
LIMIT 10;
```

### Available Event Types
Common event types include:
- `Treatment` / `TREATMENT`: Drug/therapy administration
- `Diagnosis`: Diagnosis events
- `SURGERY`: Surgical procedures
- `LAB_TEST`: Laboratory test results
- `Sequencing`: Sequencing events
- `Sample acquisition`: Sample collection events
- `PATHOLOGY` / `Pathology`: Pathology reports

### Discover Event Types in a Study

```sql
-- See what event types are available
SELECT DISTINCT ce.event_type, COUNT(*) as event_count
FROM clinical_event ce
JOIN patient p ON ce.patient_id = p.internal_id
JOIN cancer_study cs ON p.cancer_study_id = cs.cancer_study_id
WHERE cs.cancer_study_identifier = 'your_study_id'
GROUP BY ce.event_type
ORDER BY event_count DESC;
```

## Best Practices

1. **Use clinical_data_derived when possible** - it's pre-optimized and easier to work with
2. **Check attribute availability first** - use clinical_attribute_meta to see what's available
3. **Handle missing values** - clinical data can have NULL or empty values
4. **Distinguish patient vs sample level** - know whether you need patient or sample-level aggregation
5. **Filter by study** - always specify cancer_study_identifier for consistent results
6. **Use clinical_event for treatment data** - treatment info is in clinical events, not clinical attributes
