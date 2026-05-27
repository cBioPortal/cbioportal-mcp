# Study Auth Row Policies

This document covers the ClickHouse deployment side of study-level
authorization for the cBioPortal MCP.

The MCP does not migrate per-user study permissions into ClickHouse. Keycloak
remains the permission source of truth, the MCP converts JWT roles into a
request-local `StudyScope`, and ClickHouse row policies enforce that scope from
per-query custom settings.

## Required ClickHouse Config

Allow the MCP's custom settings prefix in ClickHouse server/user config:

```xml
<custom_settings_prefixes>cbioportal_</custom_settings_prefixes>
```

The MCP sends these settings on every authorized query:

```text
cbioportal_allowed_studies=brca_tcga,luad_tcga
cbioportal_allow_all=0
```

For `cbioportal:ALL`, the MCP sends:

```text
cbioportal_allowed_studies=
cbioportal_allow_all=1
```

The runtime ClickHouse user should remain read-only. If a deployment enforces
read-only at the user/profile level, validate that query-level custom settings
are still accepted. Some deployments use `readonly=2` for read-only users that
may change query settings.

## Apply

Apply these policies only to a cloned/prepped MCP database, not directly to the
production cBioPortal application database.

```bash
export CLICKHOUSE_HOST=...
export CLICKHOUSE_DATABASE=your_prepped_mcp_db
export CLICKHOUSE_ADMIN_USER=...
export CLICKHOUSE_ADMIN_PASSWORD=...
./scripts/apply_sql.sh
```

`sql/6-study-auth-row-policies.sql` targets the MCP runtime user `app_user`,
matching the default `CLICKHOUSE_USER`. If your runtime user differs, update the
`TO app_user` clauses before applying or add a deployment-specific variant.

## Protected Tables

Direct `cancer_study_identifier` policies:

- `cancer_study`
- `cancer_study_query_preferences`
- `clinical_data_derived`
- `genomic_event_derived`
- `sample_to_gene_panel_derived`
- `genetic_alteration_derived`
- `clinical_event_derived`

Indirect `cancer_study_id`/patient policies:

- `genetic_profile`
- `patient`
- `sample`

Mutation-frequency and gene-expression views are backed by the direct tables
above. The row policies apply through those underlying reads.

Tables without direct study columns, or tables not currently exposed through
curated MCP helper paths, should be reviewed again before public schema
metadata is exposed. Later public-mode hardening should restrict metadata tools
and table discovery independently of row policies.

## Manual Validation

Run the helper script against a tiny fixture DB or staging clone:

```bash
export CLICKHOUSE_HOST=...
export CLICKHOUSE_DATABASE=your_prepped_mcp_db
export CLICKHOUSE_USER=app_user
export CLICKHOUSE_PASSWORD=...
./scripts/validate_study_auth_policies.sh brca_tcga luad_tcga
```

Expected behavior:

- `cbioportal_allowed_studies=brca_tcga`, `cbioportal_allow_all=0` returns only
  `brca_tcga`.
- `cbioportal_allowed_studies=luad_tcga`, `cbioportal_allow_all=0` returns only
  `luad_tcga`.
- Empty allowed studies with `cbioportal_allow_all=0` returns zero rows.
- Empty allowed studies with `cbioportal_allow_all=1` returns all rows visible
  to the runtime user.
- Direct SQL such as `WHERE cancer_study_identifier != 'brca_tcga'` still cannot
  return unauthorized studies.

## Rollback

Drop the policies by name:

```sql
DROP ROW POLICY IF EXISTS cbioportal_study_policy_cancer_study ON cancer_study;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_cancer_study_query_preferences ON cancer_study_query_preferences;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_clinical_data_derived ON clinical_data_derived;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_genomic_event_derived ON genomic_event_derived;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_sample_to_gene_panel_derived ON sample_to_gene_panel_derived;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_genetic_alteration_derived ON genetic_alteration_derived;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_clinical_event_derived ON clinical_event_derived;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_genetic_profile ON genetic_profile;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_patient ON patient;
DROP ROW POLICY IF EXISTS cbioportal_study_policy_sample ON sample;
```

Rollback should be treated as a security-impacting operation. If policies are
dropped in an HTTP/SSE deployment, disable public access or set the MCP back to
an internal-only profile.

## Known Limitations

Row filtering prevents unauthorized rows from returning through protected
tables. It does not solve all public-readiness concerns:

- Metadata tools may still reveal table/column names until public-mode
  hardening restricts them.
- Aggregates over authorized studies can still create inference risk for small
  cohorts.
- Raw SQL remains powerful and should be restricted or replaced with curated
  tools for fully public deployments.
