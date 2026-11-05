# External Resources Guide

Use this guide for data linked from cBioPortal rather than stored in molecular or clinical tables.

## Routing Triggers

Read this guide before answering or refusing questions that mention:

- imaging, radiology, CT, MRI, H&E or pathology slides, histology, Minerva, viewer
- external portal, external resource, image data, spatial data
- HTAN studies or study-specific linked viewers

## Core Rule

Do not say cBioPortal has no imaging or external-resource data until you have checked:

- `resource_definition`
- `resource` (filter `resource_scope` for `STUDY`, `SAMPLE`, or `PATIENT`)

cBioPortal may store links to external viewers or portals even when it does not store raw images.

## Which Studies Have Imaging Data

For "which studies have imaging / H&E or pathology slides / CT" or "how many samples have images", read `cancer_study.resource_sample_counts` — per-study sample counts keyed by resource display name, the same numbers as the portal's "Data type" filter. One query, no joins:

```sql
SELECT
    cancer_study_identifier,
    name,
    resource_sample_counts['Slide Microscopy'] AS slide_microscopy_samples
FROM cancer_study
WHERE resource_sample_counts['Slide Microscopy'] > 0
ORDER BY slide_microscopy_samples DESC;
```

List the resource names that exist with `SELECT DISTINCT arrayJoin(mapKeys(resource_sample_counts)) FROM cancer_study` (e.g. `'Slide Microscopy'`, `'Computed Tomography'`, `'Magnetic Resonance'`, `'H&E Slide'`, `'MxIF Image'`).

The map counts sample- and patient-level resources only. For study-level links (`resource` with `resource_scope = 'STUDY'`) and for the URLs themselves, use the queries below.

`resource.sample_id` and `resource.patient_id` are nullable stable string IDs, scoped by `cancer_study_id`; they are not internal numeric IDs. Join patients on `patient.stable_id` plus study ID. To resolve a sample, join `sample.stable_id` and use its patient to constrain the study. Resource definitions also belong to a study.

## Discovery Query

Validate table and column names, then search across all scopes:

```sql
SELECT
    rd.resource_id,
    rd.display_name,
    rd.description,
    rd.resource_type,
    cs.cancer_study_identifier,
    r.resource_scope,
    r.sample_id,
    r.patient_id,
    r.url
FROM resource r
JOIN resource_definition rd
    ON r.resource_id = rd.resource_id AND r.cancer_study_id = rd.cancer_study_id
JOIN cancer_study cs ON cs.cancer_study_id = r.cancer_study_id
WHERE match(lower(concat(rd.display_name, ' ', ifNull(rd.description, ''))),
            'minerva|image|pathology|histology|slide|h&e')
ORDER BY cs.cancer_study_identifier, rd.display_name
LIMIT 100;
```

To restrict scope, add `AND r.resource_scope = 'STUDY'` or `AND r.resource_scope IN ('SAMPLE', 'PATIENT')`. To search for other resources, extend the pattern (e.g. `|mxif`).

## Answer Pattern

If links exist:

> cBioPortal does not store the raw image files in the molecular/clinical tables, but this deployment has external resource links for some studies. I found links to [resource names] in [studies]. These point to external viewers or portals rather than image pixels stored directly in cBioPortal.

If links do not exist:

> I checked the cBioPortal external-resource tables (`resource_definition` and `resource`) and did not find matching imaging or viewer links in this deployment.

## Do Not

- Do not answer "no imaging data" from general knowledge alone.
- Do not infer that lack of image columns means no linked image resources.
- Do not return huge resource tables; summarize studies/resources and include a small sample of URLs.
