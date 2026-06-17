# Study Resolution Guide

Use this guide when the user names a study, cohort, portal, or data source that may not exist in the connected cBioPortal deployment.

## Routing Triggers

Read this guide when the user mentions:

- PBTA, Pediatric Brain Tumor Atlas, pediatric cBioPortal, Kids First
- GENIE, AACR GENIE, MSK private cohorts, institutional cohorts
- "download study", "which study", "find cohort", "data from [portal]"
- a named cohort that `list_studies(search=...)` does not find

## Core Rules

1. Resolve the requested study before substituting another study.
2. If the requested study is not in this deployment, say so explicitly.
3. Do not silently analyze a substitute cohort.
4. If the user agrees to a substitute, keep a one-line scope caveat when reporting numbers.

## Known External cBioPortal Instances

These are not necessarily queryable from this MCP server, but they are useful redirects:

| User wording | Likely external instance | Scope |
|---|---|---|
| PBTA, Pediatric Brain Tumor Atlas, pediatric brain tumors | https://pedcbioportal.kidsfirstdrc.org/ | Pediatric cancer studies, including pediatric brain tumor cohorts |
| GENIE | https://genie.cbioportal.org/ | AACR GENIE data access, depending on release and permissions |
| MSK private / institutional cohorts | private institutional cBioPortal deployments | Not queryable from public cBioPortal unless exported to the public database |

## Study Resolution Workflow

1. Call `list_studies(search=...)` with the user's exact study/cohort phrase and close variants.
2. If a cancer type is mentioned, call `search_oncotree(search_term)` before disease-level study discovery.
3. If no matching study is found, check known external instances above before declaring the study absent.
4. If proposing a substitute, describe why it is a substitute and how its scope differs.

## Substitute-Cohort Answer Pattern

> I cannot query PBTA from this cBioPortal deployment. PBTA is typically accessed through pediatric cBioPortal at https://pedcbioportal.kidsfirstdrc.org/. I can analyze `[substitute_study_id]` here, but its results should not be interpreted as PBTA results.

When reporting numbers from a substitute:

> Scope note: these counts are from `[substitute_study_id]` in this deployment, not from the requested PBTA cohort.

## Do Not

- Do not answer a PBTA question with `brain_cptac_2020` numbers without a scope warning.
- Do not let later turns drop the substitute-cohort warning.
- Do not claim a study does not exist globally; say it is not available in the connected deployment.
