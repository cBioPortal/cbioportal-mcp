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

## Preferred Tool

Call `find_external_resources(search, study_search, limit)` first. Use `search` for the resource concept (for example, `Minerva imaging pathology`) and `study_search` for a study/cohort filter (for example, `HTAN`).

## Discovery Query

If the tool is unavailable or you need to inspect the exact SQL shape, start with table and column validation, then use this pattern:

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
