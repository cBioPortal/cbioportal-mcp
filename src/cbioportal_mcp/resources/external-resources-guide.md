# External Resources Guide

Use this guide when the user asks about data that may be linked from cBioPortal rather than stored directly in molecular or clinical tables.

## Routing Triggers

Read this guide before answering or refusing questions that mention:

- imaging, radiology, CT, MRI, pathology slides, histology, Minerva, viewer
- external portal, external resource, image data, spatial data
- HTAN studies or study-specific linked viewers

## Core Rule

Do not say cBioPortal has no imaging or external-resource data until you have checked:

- `resource_definition`
- `resource_study`
- `resource_sample`
- `resource_patient`

cBioPortal may store links to external viewers or portals even when it does not store raw images.

## Which Studies Have Imaging Data

For "which studies have imaging / pathology slides / CT" or "how many samples have images", read `cancer_study.resource_sample_counts` — per-study sample counts keyed by resource display name, the same numbers as the portal's "Data type" filter. One query, no joins:

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

The map counts sample- and patient-level resources only. For study-level links (`resource_study`) and for the URLs themselves, use the queries below.

## Discovery Query

Start with table and column validation, then use this pattern:

```sql
SELECT
    rd.resource_id,
    rd.display_name,
    rd.description,
    rd.resource_type,
    rs.cancer_study_identifier,
    rs.url
FROM resource_study rs
JOIN resource_definition rd
    ON rs.resource_id = rd.resource_id
WHERE lower(rd.display_name) LIKE '%minerva%'
   OR lower(rd.description) LIKE '%minerva%'
   OR lower(rd.display_name) LIKE '%image%'
   OR lower(rd.description) LIKE '%image%'
   OR lower(rd.display_name) LIKE '%pathology%'
   OR lower(rd.description) LIKE '%pathology%'
   OR lower(rd.display_name) LIKE '%histology%'
   OR lower(rd.description) LIKE '%histology%'
ORDER BY rs.cancer_study_identifier, rd.display_name
LIMIT 100;
```

If no study-level rows appear, check sample- and patient-level resource links:

```sql
SELECT
    rd.resource_id,
    rd.display_name,
    rd.description,
    rs.cancer_study_identifier,
    rs.sample_unique_id,
    rs.url
FROM resource_sample rs
JOIN resource_definition rd
    ON rs.resource_id = rd.resource_id
WHERE lower(rd.display_name) LIKE '%minerva%'
   OR lower(rd.description) LIKE '%minerva%'
   OR lower(rd.display_name) LIKE '%image%'
   OR lower(rd.description) LIKE '%image%'
   OR lower(rd.display_name) LIKE '%pathology%'
   OR lower(rd.description) LIKE '%pathology%'
   OR lower(rd.display_name) LIKE '%histology%'
   OR lower(rd.description) LIKE '%histology%'
ORDER BY rs.cancer_study_identifier, rd.display_name
LIMIT 100;
```

## Answer Pattern

If links exist:

> cBioPortal does not store the raw image files in the molecular/clinical tables, but this deployment has external resource links for some studies. I found links to [resource names] in [studies]. These point to external viewers or portals rather than image pixels stored directly in cBioPortal.

If links do not exist:

> I checked the cBioPortal external-resource tables (`resource_definition`, `resource_study`, `resource_sample`, and `resource_patient`) and did not find matching imaging or viewer links in this deployment.

## Do Not

- Do not answer "no imaging data" from general knowledge alone.
- Do not infer that lack of image columns means no linked image resources.
- Do not return huge resource tables; summarize studies/resources and include a small sample of URLs.
