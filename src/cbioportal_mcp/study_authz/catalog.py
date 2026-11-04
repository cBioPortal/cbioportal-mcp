"""Classification of every cBioPortal ClickHouse table and view for study row policies.

This is the single source of truth for the generated policy SQL
(``sqlgen.py``), the coverage checker (``coverage.py``) and the test-database
copier (``testdb.py``). Every object falls in exactly one bucket:

- ``STUDY_SCOPED_TABLE_PATHS``: rows belong to one study. The value is the
  foreign-key path from the row back to ``cancer_study``.
- ``STUDY_AGNOSTIC_REFERENCE_TABLES``: identical for every study (genes,
  panels, OncoTree...). Explicit ``USING 1`` policy, since the database-wide
  default-deny would otherwise hide them.
- ``IDENTITY_SCOPED_TABLES``: ``user_study``. Its policy limits rows to the
  caller's own email plus the public ('') rows.
- ``DENIED_TABLES``: cBioPortal's auth tables and the study-authz inputs /
  refresh view. No policy, so the default-deny returns zero rows for them.
- ``STUDY_SCOPED_VIEWS``: views from ``sql/4``, ``sql/5`` and ``sql/7``. The
  default-deny matches views too, so each gets a ``USING 1`` pass-through; the
  underlying tables' policies filter the rows because every view is
  ``SQL SECURITY INVOKER``.

Names cover cBioPortal's ClickHouse schema 3.1.0 and the legacy
``resource_sample`` / ``resource_patient`` / ``resource_study`` tables. A
policy on a table that does not exist is a no-op (verified on 25.8).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StudyIdentifier:
    """``column`` holds cancer_study.cancer_study_identifier."""

    column: str


@dataclass(frozen=True)
class StudyId:
    """``column`` holds cancer_study.cancer_study_id."""

    column: str


@dataclass(frozen=True)
class Via:
    """``column IN (SELECT key FROM parent WHERE <then, evaluated on parent>)``."""

    column: str
    parent: str
    key: str
    then: "StudyPath"


@dataclass(frozen=True)
class AllOf:
    """Every path must hold (a row that links two studies' objects)."""

    paths: tuple["StudyPath", ...]


StudyPath = StudyIdentifier | StudyId | Via | AllOf


def _via_patient(column: str) -> Via:
    return Via(column, "patient", "internal_id", StudyId("cancer_study_id"))


def _via_sample(column: str) -> Via:
    return Via(column, "sample", "internal_id", _via_patient("patient_id"))


def _via_profile(column: str = "genetic_profile_id", key: str = "genetic_profile_id") -> Via:
    return Via(column, "genetic_profile", key, StudyId("cancer_study_id"))


STUDY_SCOPED_TABLE_PATHS: dict[str, StudyPath] = {
    # Direct study identifier column.
    "cancer_study": StudyIdentifier("cancer_study_identifier"),
    "cancer_study_query_preferences": StudyIdentifier("cancer_study_identifier"),
    "clinical_data_derived": StudyIdentifier("cancer_study_identifier"),
    "clinical_event_data_derived": StudyIdentifier("cancer_study_identifier"),
    "clinical_event_derived": StudyIdentifier("cancer_study_identifier"),
    "genetic_alteration_derived": StudyIdentifier("cancer_study_identifier"),
    "genomic_event_derived": StudyIdentifier("cancer_study_identifier"),
    "sample_derived": StudyIdentifier("cancer_study_identifier"),
    "sample_to_gene_panel_derived": StudyIdentifier("cancer_study_identifier"),
    "mutation_derived": StudyIdentifier("studyId"),
    # cancer_study_id -> cancer_study.
    "cancer_study_tags": StudyId("cancer_study_id"),
    "clinical_attribute_meta": StudyId("cancer_study_id"),
    "copy_number_seg": StudyId("cancer_study_id"),
    "copy_number_seg_file": StudyId("cancer_study_id"),
    "genetic_profile": StudyId("cancer_study_id"),
    "gistic": StudyId("cancer_study_id"),
    "mut_sig": StudyId("cancer_study_id"),
    "patient": StudyId("cancer_study_id"),
    "resource": StudyId("cancer_study_id"),
    "resource_definition": StudyId("cancer_study_id"),
    "sample_list": StudyId("cancer_study_id"),
    "resource_study": StudyId("internal_id"),
    # Through patient.
    "sample": _via_patient("patient_id"),
    "clinical_event": _via_patient("patient_id"),
    "clinical_patient": _via_patient("internal_id"),
    "resource_patient": _via_patient("internal_id"),
    # Through sample.
    "clinical_sample": _via_sample("internal_id"),
    "resource_sample": _via_sample("internal_id"),
    # Through genetic_profile.
    "allele_specific_copy_number": _via_profile(),
    "alteration_driver_annotation": _via_profile(),
    "genetic_alteration": _via_profile(),
    "genetic_profile_link": AllOf(
        (
            _via_profile("referring_genetic_profile_id"),
            _via_profile("referred_genetic_profile_id"),
        )
    ),
    "genetic_profile_samples": _via_profile(),
    "mutation": _via_profile(),
    "mutation_count_by_keyword": _via_profile(),
    "sample_cna_event": _via_profile(),
    "sample_profile": _via_profile(),
    "structural_variant": _via_profile(),
    "generic_assay_data_derived": _via_profile("profile_stable_id", "stable_id"),
    "generic_assay_profile_entity_derived": _via_profile("profile_stable_id", "stable_id"),
    # Through another study-scoped child table.
    "clinical_event_data": Via(
        "clinical_event_id", "clinical_event", "clinical_event_id", _via_patient("patient_id")
    ),
    "gistic_to_gene": Via("gistic_roi_id", "gistic", "gistic_roi_id", StudyId("cancer_study_id")),
    "sample_list_list": Via("list_id", "sample_list", "list_id", StudyId("cancer_study_id")),
    # Shared event dimensions: an event row only exists because some study
    # observed it, so expose only events observed in an allowed study.
    "mutation_event": Via("mutation_event_id", "mutation", "mutation_event_id", _via_profile()),
    "cna_event": Via("cna_event_id", "sample_cna_event", "cna_event_id", _via_profile()),
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
        "reference_genome",
        "reference_genome_gene",
        "type_of_cancer",
    }
)

USER_STUDY_TABLE = "user_study"
USER_STUDY_MANUAL_TABLE = "user_study_manual"
USER_STUDY_REFRESH_VIEW = "user_study_refresh"
# Reserved for the future per-email JWT source (docs/study-authz.md); listed
# now so adding it needs no catalog or policy change.
USER_STUDY_JWT_TABLE = "user_study_jwt"

IDENTITY_SCOPED_TABLES: frozenset[str] = frozenset({USER_STUDY_TABLE})

DENIED_TABLES: frozenset[str] = frozenset(
    {
        "authorities",
        "data_access_tokens",
        "users",
        USER_STUDY_MANUAL_TABLE,
        USER_STUDY_JWT_TABLE,
        USER_STUDY_REFRESH_VIEW,
    }
)

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
# Every policy this tool creates is named with this prefix. The tool owns ALL
# row policies on a target database: any other policy there is refused by
# apply and reported by check (docs/study-authz.md, "Policy ownership").
OWNED_POLICY_PREFIX = "cbioportal_mcp_"
DEFAULT_DENY_POLICY = "cbioportal_mcp_default_deny"
UNRESTRICTED_OTHERS_POLICY = "cbioportal_mcp_unrestricted_others"


def describe(path: StudyPath) -> str:
    """Human-readable form of a path, e.g. ``patient_id -> patient -> cancer_study``."""
    if isinstance(path, StudyIdentifier):
        return path.column
    if isinstance(path, StudyId):
        return f"{path.column} -> cancer_study"
    if isinstance(path, Via):
        return f"{path.column} -> {path.parent}.{path.key} | {describe(path.then)}"
    return " AND ".join(describe(p) for p in path.paths)


def classified_names() -> set[str]:
    """Every table/view name the catalog knows about."""
    return (
        set(STUDY_SCOPED_TABLE_PATHS)
        | set(STUDY_AGNOSTIC_REFERENCE_TABLES)
        | set(DENIED_TABLES)
        | set(STUDY_SCOPED_VIEWS)
        | set(IDENTITY_SCOPED_TABLES)
    )
