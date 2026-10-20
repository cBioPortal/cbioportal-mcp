"""Classification of every cBioPortal ClickHouse table for study-level row policies.

This is the single source of truth that ``sql/study-authz/0-row-policies.sql``,
the app-level SQL guard (``study_access.PROTECTED_QUERY_MARKERS``),
``scripts/verify_row_policy_coverage.py`` and the tests are checked against.

Every table falls in exactly one bucket:

- ``STUDY_SCOPED_TABLES``: rows belong to one study. Each gets a row policy
  that resolves its real foreign-key path back to
  ``cancer_study.cancer_study_identifier`` (the value is that path, for docs
  and error messages).
- ``STUDY_AGNOSTIC_REFERENCE_TABLES``: reference data identical for every
  study (genes, panels, OncoTree...). Each gets an explicit ``USING 1``
  policy, since the database-wide default-deny would otherwise hide it.
- ``DENIED_TABLES``: cBioPortal's own auth tables. They get no policy, so the
  default-deny keeps them unreadable for the restricted role.
- ``STUDY_SCOPED_VIEWS``: views from ``sql/4..7``. A view is itself caught by
  the database-wide ``USING 0`` default-deny (verified on ClickHouse 24.12),
  so each needs a ``USING 1`` pass-through policy; the underlying tables'
  policies then filter the rows, because views default to
  ``SQL SECURITY INVOKER``.

Table names cover cBioPortal's ClickHouse schema 3.1.0
(``db-scripts/clickhouse/init/schema.sql``) and the legacy schema still used
by cloned deployments (``resource_sample`` / ``resource_patient`` /
``resource_study`` instead of ``resource``). A policy on a table that does not
exist is a no-op, so covering both is harmless.
"""

from __future__ import annotations

STUDY_SCOPED_TABLES: dict[str, str] = {
    # Direct study identifier column.
    "cancer_study": "cancer_study_identifier",
    "cancer_study_query_preferences": "cancer_study_identifier",
    "clinical_data_derived": "cancer_study_identifier",
    "clinical_event_data_derived": "cancer_study_identifier",
    "clinical_event_derived": "cancer_study_identifier",
    "genetic_alteration_derived": "cancer_study_identifier",
    "genomic_event_derived": "cancer_study_identifier",
    "sample_derived": "cancer_study_identifier",
    "sample_to_gene_panel_derived": "cancer_study_identifier",
    "mutation_derived": "studyId",
    # cancer_study_id -> cancer_study.
    "cancer_study_tags": "cancer_study_id -> cancer_study",
    "clinical_attribute_meta": "cancer_study_id -> cancer_study",
    "copy_number_seg": "cancer_study_id -> cancer_study",
    "copy_number_seg_file": "cancer_study_id -> cancer_study",
    "genetic_profile": "cancer_study_id -> cancer_study",
    "gistic": "cancer_study_id -> cancer_study",
    "mut_sig": "cancer_study_id -> cancer_study",
    "patient": "cancer_study_id -> cancer_study",
    "resource": "cancer_study_id -> cancer_study",
    "resource_definition": "cancer_study_id -> cancer_study",
    "sample_list": "cancer_study_id -> cancer_study",
    "resource_study": "internal_id -> cancer_study.cancer_study_id",
    # Through patient.
    "sample": "patient_id -> patient -> cancer_study",
    "clinical_event": "patient_id -> patient -> cancer_study",
    "clinical_patient": "internal_id -> patient -> cancer_study",
    "resource_patient": "internal_id -> patient -> cancer_study",
    # Through sample.
    "clinical_sample": "internal_id -> sample -> patient -> cancer_study",
    "resource_sample": "internal_id -> sample -> patient -> cancer_study",
    # Through genetic_profile.
    "allele_specific_copy_number": "genetic_profile_id -> genetic_profile -> cancer_study",
    "alteration_driver_annotation": "genetic_profile_id -> genetic_profile -> cancer_study",
    "genetic_alteration": "genetic_profile_id -> genetic_profile -> cancer_study",
    "genetic_profile_link": "referring/referred_genetic_profile_id -> genetic_profile",
    "genetic_profile_samples": "genetic_profile_id -> genetic_profile -> cancer_study",
    "mutation": "genetic_profile_id -> genetic_profile -> cancer_study",
    "mutation_count_by_keyword": "genetic_profile_id -> genetic_profile -> cancer_study",
    "sample_cna_event": "genetic_profile_id -> genetic_profile -> cancer_study",
    "sample_profile": "genetic_profile_id -> genetic_profile -> cancer_study",
    "structural_variant": "genetic_profile_id -> genetic_profile -> cancer_study",
    "generic_assay_data_derived": "profile_stable_id -> genetic_profile.stable_id",
    "generic_assay_profile_entity_derived": "profile_stable_id -> genetic_profile.stable_id",
    # Through another study-scoped child table.
    "clinical_event_data": "clinical_event_id -> clinical_event -> patient -> cancer_study",
    "gistic_to_gene": "gistic_roi_id -> gistic -> cancer_study",
    "sample_list_list": "list_id -> sample_list -> cancer_study",
    # Shared event dimensions: one row per distinct event, but an event only
    # exists because some study observed it, so expose only events observed
    # in an allowed study.
    "mutation_event": "mutation_event_id <- mutation -> genetic_profile",
    "cna_event": "cna_event_id <- sample_cna_event -> genetic_profile",
}

STUDY_AGNOSTIC_REFERENCE_TABLES: frozenset[str] = frozenset(
    {
        "cosmic_mutation",
        "gene",
        "gene_alias",
        "gene_panel",
        "gene_panel_list",
        "gene_panel_to_gene_derived",
        "generic_assay_meta_derived",
        "generic_entity_properties",
        "genetic_entity",
        "geneset",
        "geneset_gene",
        "geneset_hierarchy_leaf",
        "geneset_hierarchy_node",
        "info",
        "oncotree",
        "reference_genome",
        "reference_genome_gene",
        "type_of_cancer",
    }
)

DENIED_TABLES: frozenset[str] = frozenset({"authorities", "data_access_tokens", "users"})

STUDY_SCOPED_VIEWS: frozenset[str] = frozenset(
    {
        # sql/4-mutation-frequency-views.sql
        "cna_panel_gene_coverage",
        "cna_wes_coverage",
        "co_altered_genes_in_study",
        "gene_alteration_frequency_by_cancer_type",
        "gene_cna_distribution_in_study",
        "gene_mutation_frequency_by_cancer_type",
        "gene_mutation_frequency_in_studies",
        "gene_mutation_frequency_in_study",
        "gene_mutation_variants_in_study",
        "mutation_panel_gene_coverage",
        "mutation_wes_coverage",
        "sv_panel_gene_coverage",
        "sv_wes_coverage",
        "top_cna_genes_in_study",
        "top_mutated_genes_in_cohort",
        "top_mutated_genes_in_study",
        "top_sv_genes_in_study",
        # sql/5-gene-expression-views.sql
        "gene_pair_coexpression",
        # sql/7-clinical-views.sql
        "clinical_attribute_counts",
        "treatment_counts_in_study",
        "treatment_regimens_in_study",
    }
)

ROW_POLICY_ROLE = "cbioportal_mcp_study_restricted"
ROW_POLICY_PREFIX = "cbioportal_mcp_"
DEFAULT_DENY_POLICY = "cbioportal_mcp_default_deny"
UNRESTRICTED_OTHERS_POLICY = "cbioportal_mcp_unrestricted_others"


def classified_names() -> set[str]:
    """Every table/view name the catalog knows about."""
    return (
        set(STUDY_SCOPED_TABLES)
        | set(STUDY_AGNOSTIC_REFERENCE_TABLES)
        | set(DENIED_TABLES)
        | set(STUDY_SCOPED_VIEWS)
    )
