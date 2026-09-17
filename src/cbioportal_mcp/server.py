#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import os

from ddtrace.llmobs import LLMObs

_dd_api_key = os.getenv("DD_API_KEY")
if _dd_api_key:
    LLMObs.enable(
        ml_app=os.getenv("DD_LLMOBS_ML_APP", "cbioportal-mcp"),
        api_key=_dd_api_key,
        site=os.getenv("DD_SITE", "datadoghq.com"),
        agentless_enabled=True,
    )

import json
import logging
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources as importlib_resources
from importlib.resources.abc import Traversable
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.apps import UI_MIME_TYPE


from cbioportal_mcp import __version__
from cbioportal_mcp.env import get_mcp_config, TransportType
from cbioportal_mcp.authentication.permissions import ensure_db_permissions
from cbioportal_mcp import ui
from cbioportal_mcp import alteration_query as aq
from cbioportal_mcp import distribution_stats as dstats
from cbioportal_mcp.survival_stats import (
    downsample_curve,
    kaplan_meier,
    logrank_test,
    stratified_logrank_test,
)
from cbioportal_mcp.cooccurrence_stats import (
    benjamini_hochberg,
    fisher_exact_two_sided,
    log2_odds_ratio,
    stratified_association_test,
)
from cbioportal_mcp.meta_stats import homogeneity_test, pooled_proportion, wilson_interval
from cbioportal_mcp.telemetry import configure_telemetry, TelemetryMiddleware

logger = logging.getLogger(__name__)

# Regex pattern for valid cBioPortal study identifiers
# Allows alphanumeric characters, underscores, and hyphens
VALID_STUDY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
VALID_TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")

VALID_GENE_SYMBOL_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
VALID_ATTRIBUTE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")

ALTERATION_CONFIGS = {
    "mutation": {
        "event_filter": "variant_type = 'mutation' AND mutation_status != 'UNCALLED'",
        "profiling_type": "MUTATION_EXTENDED",
    },
    "amplification": {
        "event_filter": "variant_type = 'cna' AND cna_alteration = 2",
        "profiling_type": "COPY_NUMBER_ALTERATION",
    },
    "deep_deletion": {
        "event_filter": "variant_type = 'cna' AND cna_alteration = -2",
        "profiling_type": "COPY_NUMBER_ALTERATION",
    },
    "structural_variant": {
        "event_filter": "variant_type = 'structural_variant'",
        "profiling_type": "STRUCTURAL_VARIANT",
    },
}

MAX_ANALYSIS_GENES = 25


def _validate_study_id(study_id: str) -> str:
    """Validate and sanitize a study ID to prevent SQL injection.

    Args:
        study_id: The study identifier to validate

    Returns:
        The validated study_id if valid

    Raises:
        ValueError: If study_id contains invalid characters
    """
    if not study_id:
        raise ValueError("study_id cannot be empty")
    if not VALID_STUDY_ID_PATTERN.match(study_id):
        raise ValueError(
            f"Invalid study_id '{study_id}'. "
            "Study IDs may only contain alphanumeric characters, underscores, and hyphens."
        )
    return study_id


def _validate_table_name(table: str) -> str:
    """Validate a table name to prevent SQL injection.

    Args:
        table: The table name to validate

    Returns:
        The validated table name if valid

    Raises:
        ValueError: If table name contains invalid characters
    """
    if not table:
        raise ValueError("Table name cannot be empty")
    if not VALID_TABLE_NAME_PATTERN.match(table):
        raise ValueError(
            f"Invalid table name '{table}'. "
            "Table names may only contain alphanumeric characters and underscores."
        )
    return table


def _sanitize_search_term(search: str) -> str:
    """Sanitize a search term by escaping SQL special characters.

    Args:
        search: The search term to sanitize

    Returns:
        The sanitized search term safe for use in LIKE clauses
    """
    if not search:
        return ""
    # Escape single quotes by doubling them (SQL standard)
    # Also escape % and _ which are LIKE wildcards
    sanitized = search.replace("'", "''")
    sanitized = sanitized.replace("%", "\\%")
    sanitized = sanitized.replace("_", "\\_")
    return sanitized


def _validate_gene_symbol(gene: str) -> str:
    """Sanitize a gene symbol by escaping SQL special characters.

    Args:
        search: The gene to sanitize

    Returns:
        The sanitized gene to use in queries
    """
    if not gene:
        raise ValueError("Gene symbol cannot be empty")
    if not VALID_GENE_SYMBOL_PATTERN.match(gene):
        raise ValueError(
            f"Invalid gene symbol '{gene}'. "
            "Gene symbols may only contain alphanumeric characters, dots, underscores, and hyphens."
        )
    return gene


def _validate_alteration_type(alteration_type: str) -> dict:
    """Sanitize alteration type by escaping SQL special characters.

    Args:
        search: The alteration type to sanitize

    Returns:
        The sanitized alteration type object to use in queries
    """

    if alteration_type not in ALTERATION_CONFIGS:
        valid = ", ".join(ALTERATION_CONFIGS.keys())
        raise ValueError(f"Invalid alteration_type '{alteration_type}'. Valid options: {valid}")
    return ALTERATION_CONFIGS[alteration_type]


def _validate_attribute_name(attr: str) -> str:
    """Sanitize a attribute name by escaping SQL special characters.

    Args:
        search: The attribute to sanitize

    Returns:
        The sanitized attribute to use in queries
    """

    if not attr:
        raise ValueError("Attribute name cannot be empty")
    if not VALID_ATTRIBUTE_NAME_PATTERN.match(attr):
        raise ValueError(
            f"Invalid attribute name '{attr}'. "
            "Attribute names may only contain alphanumeric characters and underscores."
        )
    return attr


# Resource loading using importlib.resources for proper package support
def _get_resources_path() -> Traversable:
    """Get the resources directory path, supporting both installed packages and dev mode."""
    try:
        # Python 3.9+ approach using importlib.resources.files
        return importlib_resources.files("cbioportal_mcp") / "resources"
    except (TypeError, AttributeError):
        # Fallback for older Python or if package isn't installed
        return Path(__file__).parent / "resources"


def _load_resource(filename: str) -> str:
    """Load a resource guide from the resources directory."""
    try:
        resources_path = _get_resources_path()
        resource_file = resources_path / filename
        # Use read_text() which works for both Traversable and Path
        return resource_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error(f"Resource file not found: {filename}")
        return f"Error: Resource file not found: {filename}"
    except Exception as e:
        logger.error(f"Error loading resource {filename}: {e}")
        return f"Error: Could not load resource: {filename}"


def _load_study_guide(study_id: str) -> str | None:
    """Load a study guide from the study-guides directory if it exists."""
    try:
        resources_path = _get_resources_path()
        study_file = resources_path / "study-guides" / f"{study_id}.md"
        return study_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Error loading study guide for {study_id}: {e}")
        return None


def _list_available_study_guides() -> list[str]:
    """List all available pre-generated study guides."""
    try:
        resources_path = _get_resources_path()
        study_guides_path = resources_path / "study-guides"
        # For Traversable (importlib.resources), iterate contents
        # For Path, use glob
        if hasattr(study_guides_path, "iterdir"):
            # It's a Path-like object
            return [
                f.stem
                for f in study_guides_path.iterdir()
                if f.name.endswith(".md") and not f.name.startswith("_")
            ]
        else:
            # It's a Traversable from importlib.resources
            return [
                f.name.removesuffix(".md")
                for f in study_guides_path.iterdir()
                if f.name.endswith(".md") and not f.name.startswith("_")
            ]
    except Exception as e:
        logger.error(f"Error listing study guides: {e}")
        return []


def _load_general_guide(name: str) -> str | None:
    """Load a general guide from the guides/ directory if it exists."""
    try:
        resources_path = _get_resources_path()
        guide_file = resources_path / "guides" / f"{name}.md"
        return guide_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Error loading general guide {name}: {e}")
        return None


def _list_available_general_guides() -> list[str]:
    """List general guide names available in resources/guides/."""
    try:
        resources_path = _get_resources_path()
        guides_path = resources_path / "guides"
        if hasattr(guides_path, "iterdir"):
            return [
                f.stem
                for f in guides_path.iterdir()
                if f.name.endswith(".md") and not f.name.startswith("_")
            ]
        else:
            return [
                f.name.removesuffix(".md")
                for f in guides_path.iterdir()
                if f.name.endswith(".md") and not f.name.startswith("_")
            ]
    except Exception as e:
        logger.error(f"Error listing general guides: {e}")
        return []


@lru_cache(maxsize=1)
def _load_oncotree_data() -> list[dict]:
    """Load and cache oncotree.json from resources."""
    try:
        resources_path = _get_resources_path()
        oncotree_file = resources_path / "oncotree.json"
        raw = oncotree_file.read_text(encoding="utf-8")
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Failed to load oncotree.json: {e}")
        return []


def _build_hierarchy_path(code: str, entries_by_code: dict[str, dict]) -> str:
    """Walk parent chain to build a hierarchy path like 'TISSUE > PARENT > CODE'."""
    parts = []
    current = code
    seen = set()
    while current and current not in seen:
        seen.add(current)
        entry = entries_by_code.get(current)
        if not entry:
            break
        parts.append(entry.get("code", current))
        current = entry.get("parent")
    parts.reverse()
    return " > ".join(parts)


# --- Query provenance (shared by all four data apps) -------------------------
#
# A chart in a chat is not reproducible: the researcher cannot see which query
# produced it, so they cannot re-run it, adapt it, or check it. Every data-app
# payload is assembled from run_select_query calls, so the executed SQL is
# already in hand -- these helpers record it and hand it back in the payload.
#
# The recording deliberately sits ABOVE run_select_query rather than inside it:
# the data apps route through _query, so only their SQL is captured, and only
# while a payload is being built.


# The SQL executed by the payload build currently running on this thread/task,
# or None when nothing is capturing. A ContextVar (not a module-level list) so
# concurrent tool calls -- these sync tools run in worker threads -- cannot
# record into each other's payloads.
_recorded_queries: ContextVar[list[str] | None] = ContextVar("_recorded_queries", default=None)


@contextmanager
def _record_queries() -> Iterator[list[str]]:
    """Capture the SQL run by _query inside this block."""
    queries: list[str] = []
    token = _recorded_queries.set(queries)
    try:
        yield queries
    finally:
        _recorded_queries.reset(token)


def _query(sql: str) -> list[dict]:
    """run_select_query, recording the SQL when a payload build is capturing it.

    Records the string as passed, not a reconstruction from the parameters, so
    what the payload reports is exactly what ran.
    """
    recorded = _recorded_queries.get()
    if recorded is not None:
        recorded.append(sql)
    return run_select_query(sql)


def _with_provenance(build: Callable[[], dict]) -> dict:
    """Run a payload builder, attaching the SQL it executed to its payload.

    Applied at the tool boundary rather than inside the builders so that every
    builder return path -- including the ones that bail out with an ``error``
    after querying, such as a cohort filter that matched nothing -- carries the
    queries that got it there.
    """
    with _record_queries() as queries:
        payload = build()
    payload["provenance"] = {"queries": queries, "server_version": __version__}
    return payload


# --- Study scope (shared by the data apps) ------------------------------------
#
# Every data app used to take exactly one study_id, so "BAP1 across the TCGA
# PanCancer cohort" was not expressible -- and passing a study-set name such as
# 'pan_cancer_tcga' as the study id did not error: every query matched no rows
# and the lollipop reported "No mutations found for BAP1", which reads as a real
# zero. A scope is one study, an explicit list, or a named set from
# cancer_study_query_preferences. Every data query filters with _scope_sql(scope),
# and an empty result for an unverified single id is diagnosed
# (_unknown_study_error) rather than reported as "nothing found".
#
# The single-study SQL is byte-for-byte what the apps emitted before
# (cancer_study_identifier = '<id>'), so provenance for single-study calls is
# unchanged.

MAX_SCOPE_STUDIES = 400
# Study ids echoed into a payload's scope block before it is truncated.
SCOPE_LISTED_STUDIES = 40


@dataclass(frozen=True)
class StudyScope:
    """The studies one analysis runs over."""

    study_ids: tuple[str, ...]
    preference: str | None = None
    explicit: tuple[str, ...] = ()
    preference_notes: str | None = None
    # True when the ids were checked against cancer_study (multi-study scopes are
    # resolved through the database; a bare study_id is checked lazily).
    verified: bool = False

    @property
    def single(self) -> bool:
        return len(self.study_ids) == 1

    @property
    def label(self) -> str:
        n = len(self.study_ids)
        if self.single and not self.preference:
            return self.study_ids[0]
        if self.preference and not self.explicit:
            return f"{self.preference} ({n} {'study' if n == 1 else 'studies'})"
        if self.preference:
            return f"{self.preference} + {len(self.explicit)} named studies ({n} studies)"
        return f"{n} studies"

    def sql(self, column: str = "cancer_study_identifier") -> str:
        if self.single:
            return f"{column} = '{self.study_ids[0]}'"
        if self.preference and not self.explicit:
            return (
                f"{column} IN (SELECT cancer_study_identifier FROM "
                f"cancer_study_query_preferences WHERE preference_name = '{self.preference}')"
            )
        return f"{column} IN ({_sql_string_list(self.study_ids)})"

    def describe(self) -> dict:
        listed = list(self.study_ids[:SCOPE_LISTED_STUDIES])
        block: dict = {
            "label": self.label,
            "n_studies": len(self.study_ids),
            "study_ids": listed,
        }
        if len(self.study_ids) > len(listed):
            block["study_ids_truncated"] = True
        if self.preference:
            block["preference"] = self.preference
            if self.preference_notes:
                block["preference_notes"] = self.preference_notes
        return block


def _scope_of(scope: "StudyScope | str") -> StudyScope:
    """Accept a resolved scope or a bare (already validated) study id."""
    if isinstance(scope, StudyScope):
        return scope
    return StudyScope(study_ids=(str(scope),))


def _scope_sql(scope: "StudyScope | str", column: str = "cancer_study_identifier") -> str:
    return _scope_of(scope).sql(column)


def _scope_label(scope: "StudyScope | str") -> str:
    return _scope_of(scope).label


def _scope_study_id(scope: "StudyScope | str") -> str | None:
    """The study id for payloads: the id for one study, None for several."""
    s = _scope_of(scope)
    return s.study_ids[0] if s.single else None


def _resolve_scope(study_id, studies=None, preference=None) -> StudyScope:
    """Resolve the tool arguments naming the studies to analyse.

    ``study_id`` alone is one study (validated, existence checked lazily). ``studies``
    and/or ``preference`` name several and are resolved against the database up front:
    an unknown id is an error, never a silently smaller cohort.
    """
    study_id = (study_id or "").strip() if isinstance(study_id, str) else study_id
    has_many = bool(studies) or bool(preference)
    if study_id and has_many:
        raise ValueError(
            "Pass either study_id (one study) or studies=[...] / preference='...' "
            "(several studies), not both."
        )
    if study_id:
        return StudyScope(study_ids=(_validate_study_id(study_id),))
    if not has_many:
        raise ValueError(
            "Name the cohort: study_id='<one study>', studies=[...] (use list_studies to "
            "find ids), or preference='<named study set>' such as 'pan_cancer_tcga' or "
            "'all_studies_non_redundant'."
        )
    ids = _normalize_study_ids(studies)
    pref = _validate_preference_name(preference) if preference else None
    clauses = []
    if ids:
        clauses.append(f"cancer_study_identifier IN ({_sql_string_list(ids)})")
    if pref:
        clauses.append(
            "cancer_study_identifier IN (SELECT cancer_study_identifier FROM "
            f"cancer_study_query_preferences WHERE preference_name = '{pref}')"
        )
    rows = _query(f"""
        SELECT cancer_study_identifier
        FROM cancer_study
        WHERE {" OR ".join(clauses)}
        ORDER BY cancer_study_identifier
    """)
    found = [r["cancer_study_identifier"] for r in rows if r.get("cancer_study_identifier")]
    missing = [sid for sid in ids if sid not in found]
    if missing:
        raise ValueError(_unknown_ids_message(missing))
    if pref and len(found) == len(ids):
        prefs = _query(f"""
            SELECT preference_name, count() AS n
            FROM cancer_study_query_preferences
            WHERE preference_name = '{pref}'
            GROUP BY preference_name
        """)
        if not prefs:
            available = _query(
                "SELECT DISTINCT preference_name FROM cancer_study_query_preferences "
                "ORDER BY preference_name"
            )
            names = ", ".join(r["preference_name"] for r in available if r.get("preference_name"))
            raise ValueError(
                f"Unknown preference '{pref}'. Named study sets in this deployment: "
                f"{names or 'none'}."
            )
    if not found:
        raise ValueError(f"Preference '{pref}' resolved to no studies in this deployment.")
    if len(found) > MAX_SCOPE_STUDIES:
        raise ValueError(
            f"The requested scope has {len(found)} studies; the limit is {MAX_SCOPE_STUDIES}."
        )
    notes = None
    if pref:
        note_rows = _query(f"""
            SELECT any(notes) AS notes
            FROM cancer_study_query_preferences
            WHERE preference_name = '{pref}'
        """)
        notes = (note_rows[0].get("notes") if note_rows else None) or None
    ordered = [sid for sid in ids if sid in found] + [sid for sid in found if sid not in ids]
    return StudyScope(
        study_ids=tuple(ordered),
        preference=pref,
        explicit=tuple(ids),
        preference_notes=notes,
        verified=True,
    )


def _unknown_ids_message(missing: list[str]) -> str:
    """Error text for ids that are not studies, naming any that are study-set names."""
    pref_rows = _query(f"""
        SELECT preference_name, count() AS n, any(notes) AS notes
        FROM cancer_study_query_preferences
        WHERE preference_name IN ({_sql_string_list(missing)})
        GROUP BY preference_name
    """)
    sets = {r["preference_name"]: r for r in pref_rows if r.get("preference_name")}
    parts = []
    for sid in missing:
        if sid in sets:
            n = _as_int(sets[sid].get("n"))
            note = sets[sid].get("notes")
            parts.append(
                f"'{sid}' is not a study id: it is a named study set ({n} studies"
                + (f", {note}" if note else "")
                + f"). Pass preference='{sid}' to analyse all of them"
            )
        else:
            parts.append(
                f"'{sid}' is not a study in this deployment; find the exact id with "
                "list_studies(search=...)"
            )
    return "; ".join(parts) + "."


def _unknown_study_error(scope: "StudyScope | str") -> str | None:
    """Explain an empty result caused by a study id that does not exist.

    Only an unverified single id can be unknown (multi-study scopes are resolved up
    front). Returns None when the study exists -- the empty result is then real.
    """
    s = _scope_of(scope)
    if s.verified or not s.single:
        return None
    sid = s.study_ids[0]
    rows = _query(f"""
        SELECT cancer_study_identifier
        FROM cancer_study
        WHERE cancer_study_identifier = '{sid}'
    """)
    if rows:
        return None
    return _unknown_ids_message([sid])


def _scope_overlap_warning(scope: "StudyScope | str") -> str | None:
    """Warn when studies in a multi-study scope share patient ids.

    Uses the un-prefixed patient_stable_id (the same heuristic as the cross-study
    tool): shared ids are either one patient counted once per study, or a
    coincidental collision of generic ids between unrelated studies.
    """
    s = _scope_of(scope)
    if s.single:
        return None
    rows = _query(f"""
        SELECT count() AS shared_ids, any(studies) AS example
        FROM (
            SELECT patient_stable_id, arraySort(groupUniqArray(cancer_study_identifier)) AS studies
            FROM sample_derived
            WHERE {s.sql()}
                AND patient_stable_id != ''
            GROUP BY patient_stable_id
            HAVING length(studies) > 1
        )
    """)
    if not rows:
        return None
    shared = _as_int(rows[0].get("shared_ids"))
    if shared <= 0:
        return None
    example = rows[0].get("example")
    if isinstance(example, str):
        example = [x.strip(" '\"") for x in example.strip("[]").split(",") if x.strip()]
    example_text = f" (e.g. {' / '.join(example[:3])})" if example else ""
    return (
        f"{shared} patient id(s) occur in more than one study of this scope{example_text}. "
        "Each study's samples are counted separately, so a patient in two studies counts "
        "twice; some shared ids may instead be coincidental collisions of generic ids."
    )


# --- Cohort filtering (shared by all four data apps) -------------------------
#
# Every data app answers for some cohort. With no filter that cohort is the whole
# study, which for a pan-cancer registry like msk_chord_2024 means ~25k samples
# spanning 40+ cancer types -- so "survival for breast cancer in MSK-CHORD" does
# not error, it answers for everything and attaches a real log-rank p-value to a
# cohort nobody asked about. The failure is invisible because the output looks
# right. Hence two things here: the cohort is ALWAYS disclosed in the payload
# (_cohort_block), and it can optionally be narrowed (_cohort_sample_ids /
# _cohort_patient_ids).
#
# v1 predicate scope is deliberately narrow: ONE attribute, equality against one
# or more values, e.g. {"CANCER_TYPE": ["Breast Cancer"]}. Ranges, negation and
# compound predicates are follow-up work.

# Attribute used to describe how heterogeneous an unfiltered study is.
COHORT_SPREAD_ATTRIBUTE = "CANCER_TYPE"


def _escape_sql_string(value: str) -> str:
    """Escape a value for use inside a single-quoted SQL string literal.

    Deliberately NOT ``_sanitize_search_term``: that one also escapes ``%`` and
    ``_`` because it targets LIKE patterns, where those are wildcards. Cohort
    values are compared with ``=``/``IN``, where both are ordinary characters --
    escaping them there would stop ``MSI_HIGH`` or ``Stage_IV`` ever matching.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def _parse_cohort(cohort: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Validate a cohort predicate and return ``(attribute, values)``.

    Raises ValueError with an actionable message for anything outside v1 scope,
    so the tool layer turns it into a readable error rather than a wrong answer.
    """
    if not isinstance(cohort, dict) or not cohort:
        raise ValueError(
            "cohort must be a non-empty mapping of one attribute to the values to "
            'match, e.g. {"CANCER_TYPE": ["Breast Cancer"]}.'
        )
    if len(cohort) > 1:
        raise ValueError(
            f"cohort supports exactly one attribute; got {len(cohort)} "
            f"({', '.join(sorted(str(k) for k in cohort))}). Compound predicates "
            "are not supported yet."
        )
    attribute, raw_values = next(iter(cohort.items()))
    attribute = _validate_attribute_name(str(attribute))
    if isinstance(raw_values, str):  # models routinely pass a bare string
        raw_values = [raw_values]
    if not isinstance(raw_values, (list, tuple)):
        raise ValueError(
            f"cohort['{attribute}'] must be a list of values to match, "
            f"got {type(raw_values).__name__}."
        )
    values = [str(v).strip() for v in raw_values if str(v).strip()]
    if not values:
        raise ValueError(f"cohort['{attribute}'] contained no non-empty values to match.")
    return attribute, values


def _cohort_value_sql(values: list[str]) -> str:
    """Render cohort values as an upper-cased, escaped SQL ``IN`` list.

    Matching is case-insensitive per ``cbioportal://clinical-data-guide``:
    clinical values are free text and differ by case across studies.
    """
    return ", ".join(f"'{_escape_sql_string(v.upper())}'" for v in values)


def _cohort_empty_error(study_id, cohort: dict[str, list[str]], grain: str) -> str:
    """Message for a filter that matched nothing -- never render an empty chart."""
    attribute, values = _parse_cohort(cohort)
    scope = _scope_of(study_id)
    where = f"study '{scope.label}'" if scope.single else scope.label
    return (
        f"Cohort filter matched no {grain} in {where}: {attribute} in "
        f"{values}. Check the attribute name and its exact values in "
        f"clinical_data_derived, or omit the filter to analyse the whole study."
    )


def _cohort_sample_ids(study_id, cohort: dict[str, list[str]] | None) -> set[str] | None:
    """Sample IDs matching the cohort predicate, or None when no filter is supplied.

    ``None`` (no filter) and ``set()`` (filter matched nothing) mean different
    things and callers must treat them differently -- hence not an empty set.
    """
    if cohort is None:
        return None
    attribute, values = _parse_cohort(cohort)
    rows = _query(f"""
        SELECT DISTINCT sample_unique_id
        FROM clinical_data_derived
        WHERE {_scope_sql(study_id)}
            AND attribute_name = '{attribute}'
            AND upper(attribute_value) IN ({_cohort_value_sql(values)})
    """)
    matched: set[str] = set()
    for row in rows:
        sid = row.get("sample_unique_id")
        if sid:
            matched.add(sid)
    return matched


def _cohort_patient_ids(
    study_id, cohort: dict[str, list[str]] | None
) -> tuple[set[str] | None, int]:
    """Patient IDs matching the cohort predicate, plus the count excluded as ambiguous.

    Survival is patient-grain, but CANCER_TYPE and most clinical attributes are
    sample-grain, so a multi-primary patient (say one breast and one lung sample)
    has no single cohort membership. Rather than guessing -- which would silently
    drop or double-count them -- this reuses ``_clinical_patient_values``' rule:
    patients whose samples disagree are excluded, and the count is returned so
    the caller can surface it instead of discarding it.
    """
    if cohort is None:
        return None, 0
    attribute, values = _parse_cohort(cohort)
    pat_values, n_ambiguous = _clinical_patient_values(study_id, attribute)
    wanted = {v.upper() for v in values}
    matched = {pid for pid, val in pat_values.items() if str(val).upper() in wanted}
    return matched, n_ambiguous


def _study_cancer_type_spread(study_id) -> tuple[int, int]:
    """Return ``(n_samples, n_distinct_cancer_types)`` for a study or scope."""
    rows = _query(f"""
        SELECT COUNT(DISTINCT sample_unique_id) AS n_samples,
               COUNT(DISTINCT attribute_value) AS n_cancer_types
        FROM clinical_data_derived
        WHERE {_scope_sql(study_id)}
            AND attribute_name = '{COHORT_SPREAD_ATTRIBUTE}'
    """)
    if not rows:
        return 0, 0
    row = rows[0]
    try:
        return int(row.get("n_samples") or 0), int(row.get("n_cancer_types") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _cohort_block(
    study_id,
    cohort: dict[str, list[str]] | None,
    n_samples: int | None,
    n_patients: int | None,
    warnings: list[str],
) -> dict:
    """Build the payload's ``cohort`` block, warning when the cohort is implicit.

    Present on every data-app payload whether or not a filter was supplied: an
    answer for the wrong cohort is only invisible while the payload declines to
    say which cohort it used.

    ``n_samples`` is the sample universe the analysis actually ran over after
    filtering (None for patient-grain apps); ``n_patients`` likewise for patients.
    """
    scope = _scope_of(study_id)
    block: dict = {
        "study_id": _scope_study_id(scope),
        "filter": cohort,
        "n_samples": n_samples,
        "n_patients": n_patients,
        "unfiltered": cohort is None,
    }
    if not scope.single:
        block["studies"] = scope.describe()
    if cohort is None:
        n_total, n_types = _study_cancer_type_spread(scope)
        block["n_cancer_types"] = n_types
        if n_types > 1:
            where = "" if scope.single else f" in {len(scope.study_ids)} studies"
            message = (
                f"No cohort filter applied: spans {n_total} samples "
                f"across {n_types} cancer types{where}."
            )
            block["warning"] = message
            # The widgets already render payload["warnings"], so appending here
            # surfaces the disclosure in the UI without a widget rebuild.
            warnings.append(message)
    return block


# --- Kaplan-Meier survival app helpers ---------------------------------------

# Supported survival endpoints -> human label. Each maps to clinical attributes
# "{endpoint}_MONTHS" (follow-up time) and "{endpoint}_STATUS" (event indicator).
SURVIVAL_ENDPOINTS = {
    "OS": "Overall Survival",
    "PFS": "Progression-Free Survival",
    "DFS": "Disease-Free Survival",
    "DSS": "Disease-Specific Survival",
}

MAX_SURVIVAL_GROUPS = 4

# Coverage of the pointwise confidence band drawn around each KM curve. Fixed at
# the convention the reader will assume from an unlabelled survival figure;
# echoed into the payload so the widget can label the band rather than hard-code
# "95%" on its own side of the contract.
SURVIVAL_CONF_LEVEL = 0.95

# Max step points reported per curve. A 25k-patient study has ~2,000 distinct
# follow-up times per group; streaming every one of them is both far more
# resolution than the plot can show (the chart is a few hundred pixels wide) and
# enough JSON to blow an agent's context when the payload is read back. Longer
# curves are binned by survival_stats.downsample_curve, which keeps exact
# survival/CI values at the times it does report.
MAX_CURVE_POINTS = 200

# *_STATUS strings encode the event indicator. cBioPortal normally prefixes a
# numeric code ("1:DECEASED"); these keyword sets are a fallback for un-coded
# values. Censored keywords are checked first so "Progression Free" is not
# misread as an event by the "PROGRESSED" rule.
_SURVIVAL_CENSORED_KEYWORDS = (
    "LIVING",
    "ALIVE",
    "CENSORED",
    "DISEASEFREE",
    "DISEASE FREE",
    "DISEASE-FREE",
    "NO EVENT",
    "REMISSION",
    "FREE",
)
_SURVIVAL_EVENT_KEYWORDS = (
    "DECEASED",
    "DEAD",
    "PROGRESSED",
    "PROGRESSION",
    "RECURRED",
    "RELAPSED",
    "METASTA",
    "EVENT",
)


def _validate_endpoint(endpoint: str) -> str:
    """Validate a survival endpoint against the supported set.

    Args:
        endpoint: Endpoint code (e.g. "OS", "PFS"); case-insensitive.

    Returns:
        The normalized upper-case endpoint code.

    Raises:
        ValueError: If the endpoint is not supported.
    """
    ep = (endpoint or "").strip().upper()
    if ep not in SURVIVAL_ENDPOINTS:
        valid = ", ".join(SURVIVAL_ENDPOINTS)
        raise ValueError(f"Invalid endpoint '{endpoint}'. Valid options: {valid}")
    return ep


def _parse_survival_status(value: str | None) -> int | None:
    """Map a *_STATUS value to 1 (event), 0 (censored), or None (unknown).

    Prefers the leading numeric code in cBioPortal's "code:label" form, then
    falls back to keyword matching.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # "code:label" form — the leading code is authoritative.
    if ":" in s:
        code = s.split(":", 1)[0].strip()
        if code == "1":
            return 1
        if code == "0":
            return 0
    if s == "1":
        return 1
    if s == "0":
        return 0
    upper = s.upper()
    if any(k in upper for k in _SURVIVAL_CENSORED_KEYWORDS):
        return 0
    if any(k in upper for k in _SURVIVAL_EVENT_KEYWORDS):
        return 1
    return None


def _survival_time_ticks(max_time: float, n: int = 5) -> list[float]:
    """Return ``n`` evenly spaced, rounded tick times spanning [0, max_time]."""
    if max_time <= 0:
        return [0.0]
    step = max_time / (n - 1)
    # Round the step to a "nice" value for readable axis labels.
    if step >= 12:
        nice = round(step / 6) * 6
    elif step >= 1:
        nice = round(step)
    else:
        nice = round(step, 1)
    nice = nice or 1
    ticks = [round(nice * i, 1) for i in range(n)]
    # Ensure the axis covers the full range.
    if ticks[-1] < max_time:
        ticks.append(round(ticks[-1] + nice, 1))
    return ticks


def _fetch_survival_observations(
    study_id, endpoint: str, cohort_patients: set[str] | None = None
) -> tuple[dict[str, tuple[float, int]], int]:
    """Fetch per-patient (time, event) observations for an endpoint.

    Survival is per-patient, so this aggregates to ``patient_unique_id``.

    ``cohort_patients`` restricts the result before anything is counted, so the
    dropped-patient count describes the cohort rather than the whole study
    (reporting "500 patients dropped" for a 200-patient cohort is its own bug).

    Returns:
        (observations keyed by patient, count of patients dropped for
        missing/unparseable time or status).
    """
    rows = _query(f"""
        SELECT
            patient_unique_id,
            MAX(CASE WHEN attribute_name = '{endpoint}_MONTHS'
                THEN toFloat64OrNull(attribute_value) END) AS time,
            MAX(CASE WHEN attribute_name = '{endpoint}_STATUS'
                THEN attribute_value END) AS status
        FROM clinical_data_derived
        WHERE {_scope_sql(study_id)}
            AND attribute_name IN ('{endpoint}_MONTHS', '{endpoint}_STATUS')
        GROUP BY patient_unique_id
    """)
    observations: dict[str, tuple[float, int]] = {}
    n_dropped = 0
    for row in rows:
        pid = row.get("patient_unique_id")
        if not pid:
            continue
        if cohort_patients is not None and pid not in cohort_patients:
            continue
        event = _parse_survival_status(row.get("status"))
        raw_time = row.get("time")
        if event is None or raw_time is None:
            n_dropped += 1
            continue
        try:
            t = float(raw_time)
        except (TypeError, ValueError):
            n_dropped += 1
            continue
        if t < 0:
            n_dropped += 1
            continue
        observations[pid] = (t, event)
    return observations, n_dropped


def _altered_patients(study_id, gene: str, alteration_types: list[str]) -> set[str]:
    """Return the set of patients with a qualifying alteration in ``gene``.

    A patient is "altered" if any of their samples carries one of the requested
    alteration types (alterations are per-sample; aggregated to the patient).
    """
    filters = []
    for alt in alteration_types:
        cfg = _validate_alteration_type(alt)
        filters.append(f"({cfg['event_filter']})")
    combined = " OR ".join(filters)
    rows = _query(f"""
        SELECT DISTINCT patient_unique_id
        FROM genomic_event_derived
        WHERE {_scope_sql(study_id)}
            AND hugo_gene_symbol = '{gene}'
            AND ({combined})
    """)
    altered: set[str] = set()
    for row in rows:
        pid = row.get("patient_unique_id")
        if pid:
            altered.add(pid)
    return altered


def _clinical_patient_values(study_id, attribute: str) -> tuple[dict[str, str], int]:
    """Map each patient to a single value of ``attribute``.

    Patients with conflicting values across samples are excluded (and counted),
    since a single grouping value cannot be assigned.
    """
    rows = _query(f"""
        SELECT DISTINCT patient_unique_id, attribute_value
        FROM clinical_data_derived
        WHERE {_scope_sql(study_id)}
            AND attribute_name = '{attribute}'
    """)
    values: dict[str, str] = {}
    ambiguous: set[str] = set()
    for row in rows:
        pid = row.get("patient_unique_id")
        val = row.get("attribute_value")
        if not pid or val in (None, ""):
            continue
        if pid in values and values[pid] != val:
            ambiguous.add(pid)
        else:
            values[pid] = val
    for pid in ambiguous:
        values.pop(pid, None)
    return values, len(ambiguous)


# --- Shared alteration-query helpers (OQL tracks over the data apps) ----------

# Columns of genomic_event_derived that alteration_query evaluates.
_EVENT_COLUMNS = (
    "sample_unique_id, patient_unique_id, hugo_gene_symbol, variant_type, "
    "mutation_variant, mutation_type, mutation_status, driver_filter, cna_alteration"
)

# Studies with study-supplied driver annotations are rare; list them in errors.
MAX_DRIVER_STUDIES_LISTED = 12


def _fetch_track_events(scope, tracks: list) -> list[dict]:
    """Every event row of the tracks' genes and alteration kinds, for Python evaluation.

    Rows a track vetoes (``MUT != X``) are fetched too, so the exclusions can be
    counted and reported instead of silently applied.
    """
    genes = sorted({g for t in tracks for g in t.genes})
    types = sorted({vt for t in tracks for vt in aq.track_variant_types(t)})
    if not genes or not types:
        return []
    return _query(f"""
        SELECT {_EVENT_COLUMNS}
        FROM genomic_event_derived
        WHERE {_scope_sql(scope)}
            AND hugo_gene_symbol IN ({_sql_string_list(genes)})
            AND variant_type IN ({_sql_string_list(types)})
    """)


def _check_driver_annotations(scope, tracks: list) -> None:
    """Refuse DRIVER filters where the studies carry no driver annotation.

    cbioportal.org decides "driver" at query time from OncoKB and hotspots; this
    database only has study-supplied custom annotations (driver_filter), which
    almost no study ships. Filtering on an absent annotation would return an empty
    track that looks like "no drivers", so it is an error instead.
    """
    types = sorted(
        {
            aq.VARIANT_TYPE[c.alteration]
            for t in tracks
            for ln in t.lines
            for c in ln.effective_commands
            if c.driver
        }
    )
    if not types:
        return
    rows = _query(f"""
        SELECT cancer_study_identifier, countIf(driver_filter != '') AS annotated
        FROM genomic_event_derived
        WHERE {_scope_sql(scope)}
            AND variant_type IN ({_sql_string_list(types)})
        GROUP BY cancer_study_identifier
    """)
    annotated = {r["cancer_study_identifier"] for r in rows if _as_int(r.get("annotated")) > 0}
    lacking = [sid for sid in _scope_of(scope).study_ids if sid not in annotated]
    if not lacking:
        return
    elsewhere = _query(f"""
        SELECT DISTINCT cancer_study_identifier
        FROM genomic_event_derived
        WHERE driver_filter != ''
        ORDER BY cancer_study_identifier
        LIMIT {MAX_DRIVER_STUDIES_LISTED}
    """)
    having = ", ".join(r["cancer_study_identifier"] for r in elsewhere)
    shown = ", ".join(lacking[:5]) + (f" and {len(lacking) - 5} more" if len(lacking) > 5 else "")
    raise ValueError(
        f"DRIVER filters need driver annotations, and {shown} "
        f"{'has' if len(lacking) == 1 else 'have'} none (driver_filter is empty for every "
        f"event). In this deployment only {having or 'no studies'} carry study-supplied "
        "driver annotations; OncoKB/hotspot driver calls, which cbioportal.org computes at "
        "query time, are not stored. Remove DRIVER / _DRIVER to include all alterations "
        "(say so when reporting), or use cbioportal.org for OncoKB driver filtering."
    )


def _profiled_patients(scope, genes: list[str], profiling_type: str) -> dict[str, set[str]]:
    """Per gene, the patients with at least one sample profiled for it.

    Panel membership comes from sample_to_gene_panel_derived + gene_panel_list
    (via the coverage view for mutations); WES samples count as profiled for every
    gene, which is why they are fetched as a separate '*' branch.
    """
    genes_sql = _sql_string_list(genes)
    if profiling_type == "MUTATION_EXTENDED":
        panel = f"""SELECT DISTINCT s.patient_unique_id AS patient_unique_id,
                   c.hugo_gene_symbol AS gene
            FROM mutation_panel_gene_coverage c
            JOIN sample_derived s ON c.sample_unique_id = s.sample_unique_id
            WHERE {_scope_sql(scope, "c.cancer_study_identifier")}
                AND c.hugo_gene_symbol IN ({genes_sql})"""
        wes = f"""SELECT DISTINCT s.patient_unique_id AS patient_unique_id, '*' AS gene
            FROM mutation_wes_coverage w
            JOIN sample_derived s ON w.sample_unique_id = s.sample_unique_id
            WHERE {_scope_sql(scope, "w.cancer_study_identifier")}"""
    else:
        panel = f"""SELECT DISTINCT s.patient_unique_id AS patient_unique_id,
                   g.hugo_gene_symbol AS gene
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene g ON gpl.gene_id = g.entrez_gene_id
            JOIN sample_derived s ON stgp.sample_unique_id = s.sample_unique_id
            WHERE {_scope_sql(scope, "stgp.cancer_study_identifier")}
                AND stgp.alteration_type = '{profiling_type}'
                AND g.hugo_gene_symbol IN ({genes_sql})"""
        wes = f"""SELECT DISTINCT s.patient_unique_id AS patient_unique_id, '*' AS gene
            FROM sample_to_gene_panel_derived stgp
            JOIN sample_derived s ON stgp.sample_unique_id = s.sample_unique_id
            WHERE {_scope_sql(scope, "stgp.cancer_study_identifier")}
                AND stgp.alteration_type = '{profiling_type}'
                AND stgp.gene_panel_id = 'WES'"""
    rows = _query(f"""
        SELECT patient_unique_id, gene FROM (
            {panel}
            UNION ALL
            {wes}
        )
    """)
    out: dict[str, set[str]] = {g: set() for g in genes}
    everyone: set[str] = set()
    for row in rows:
        pid, gene = row.get("patient_unique_id"), row.get("gene")
        if not pid:
            continue
        if gene == "*":
            everyone.add(pid)
        elif gene in out:
            out[gene].add(pid)
    for gene in genes:
        out[gene] |= everyone
    return out


def _patient_strata(scope, attribute: str) -> tuple[dict[str, str], int]:
    """Patient -> stratum value for stratify_by, plus patients excluded as ambiguous.

    ``STUDY`` stratifies by study; anything else is a clinical attribute resolved
    with the same conflicting-values rule as the cohort filter.
    """
    if attribute == STRATIFY_BY_STUDY:
        rows = _query(f"""
            SELECT DISTINCT patient_unique_id, cancer_study_identifier
            FROM sample_derived
            WHERE {_scope_sql(scope)}
        """)
        return {
            r["patient_unique_id"]: r["cancer_study_identifier"]
            for r in rows
            if r.get("patient_unique_id") and r.get("cancer_study_identifier")
        }, 0
    return _clinical_patient_values(scope, _validate_attribute_name(attribute))


# --- Kaplan-Meier survival: custom and expression groupings -------------------

# stratify_by value that stratifies by study rather than a clinical attribute.
STRATIFY_BY_STUDY = "STUDY"

_EXPRESSION_PROFILE_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _as_track_list(value, field_name: str) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        raise ValueError(f"'{field_name}' must be an OQL string or a list of OQL strings.")
    tracks = []
    for item in items:
        tracks.append(aq.parse_single_track(item))
    return tracks


def _parse_survival_groups(groups) -> list[dict]:
    """Validate ``groups=[{name, altered, unaltered}, ...]`` into parsed tracks."""
    if not isinstance(groups, (list, tuple)) or len(groups) < 2:
        raise ValueError(
            "groups must be a list of 2-4 group definitions, e.g. "
            '[{"name": "TP53+KRAS", "altered": ["TP53: MUT", "KRAS: MUT"]}, '
            '{"name": "KRAS only", "altered": "KRAS: MUT", "unaltered": "TP53: MUT"}].'
        )
    if len(groups) > MAX_SURVIVAL_GROUPS:
        raise ValueError(f"groups supports at most {MAX_SURVIVAL_GROUPS} groups.")
    parsed: list[dict] = []
    names: set[str] = set()
    for i, spec in enumerate(groups):
        if not isinstance(spec, dict):
            raise ValueError(f"groups[{i}] must be an object with 'altered' and/or 'unaltered'.")
        unknown = set(spec) - {"name", "altered", "unaltered"}
        if unknown:
            raise ValueError(
                f"groups[{i}] has unsupported keys {sorted(unknown)}; use name, altered, "
                "unaltered (clinical subsets go in cohort, which applies to every group)."
            )
        altered = _as_track_list(spec.get("altered"), f"groups[{i}].altered")
        unaltered = _as_track_list(spec.get("unaltered"), f"groups[{i}].unaltered")
        if not altered and not unaltered:
            raise ValueError(f"groups[{i}] needs 'altered' and/or 'unaltered' conditions.")
        name = str(spec.get("name") or "").strip()
        if not name:
            name = " & ".join(
                [t.label for t in altered] + [f"not {t.label}" for t in unaltered]
            )
        if name in names:
            raise ValueError(f"Group names must be unique; '{name}' is used twice.")
        names.add(name)
        parsed.append({"name": name, "altered": altered, "unaltered": unaltered})
    return parsed


def _assign_custom_groups(
    scope, parsed: list[dict], patients: set[str], warnings: list[str]
) -> tuple[dict[str, set[str]], dict]:
    """Assign patients to disjoint custom groups; returns (members, grouping block)."""
    all_tracks = [t for g in parsed for t in g["altered"] + g["unaltered"]]
    _check_driver_annotations(scope, all_tracks)
    rows = _fetch_track_events(scope, all_tracks)
    altered_in: dict[int, set[str]] = {}
    for idx, track in enumerate(all_tracks):
        altered_in[idx] = {
            r["patient_unique_id"]
            for r in rows
            if r.get("patient_unique_id") and aq.track_wants_event(track, r)
        }
    # Profiling needed to call a patient "unaltered": every gene of the track, for
    # every alteration kind its commands test.
    needs: dict[str, set[str]] = {}
    for g in parsed:
        for track in g["unaltered"]:
            for ptype in aq.track_profiling_types(track):
                needs.setdefault(ptype, set()).update(track.genes)
    profiled: dict[tuple[str, str], set[str]] = {}
    for ptype, genes in needs.items():
        by_gene = _profiled_patients(scope, sorted(genes), ptype)
        for gene, pids in by_gene.items():
            profiled[(ptype, gene)] = pids

    index = {id(t): i for i, t in enumerate(all_tracks)}
    candidates: dict[str, set[str]] = {}
    for g in parsed:
        members = set(patients)
        for track in g["altered"]:
            members &= altered_in[index[id(track)]]
        for track in g["unaltered"]:
            members -= altered_in[index[id(track)]]
            for ptype in aq.track_profiling_types(track):
                for gene in track.genes:
                    members &= profiled.get((ptype, gene), set())
        candidates[g["name"]] = members

    counts: dict[str, int] = {}
    for members in candidates.values():
        for pid in members:
            counts[pid] = counts.get(pid, 0) + 1
    overlapping = {pid for pid, n in counts.items() if n > 1}
    final = {name: members - overlapping for name, members in candidates.items()}
    if overlapping:
        warnings.append(
            f"{len(overlapping)} patient(s) matched more than one group definition and were "
            "excluded from every group, so the groups are disjoint."
        )
    unassigned = len(patients - set().union(*final.values()) - overlapping)
    block = {
        "type": "custom",
        "groups": [
            {
                "name": g["name"],
                "altered": [t.text for t in g["altered"]],
                "unaltered": [t.text for t in g["unaltered"]],
                "n_patients": len(final[g["name"]]),
            }
            for g in parsed
        ],
        "n_overlapping_excluded": len(overlapping),
        "n_unassigned": unassigned,
        "semantics": (
            "A patient is in a group when every 'altered' track has a qualifying event in "
            "one of the patient's samples and no 'unaltered' track does; 'unaltered' also "
            "requires a sample profiled for every gene of that track (gene-panel aware). "
            "Patients matching several groups are excluded from all of them, and patients "
            "matching none (n_unassigned, e.g. a mutation with no codon position under a "
            "codon-range condition) are not plotted."
        ),
    }
    return final, block


def _choose_expression_profiles(scope, requested: str | None) -> dict[str, str]:
    """Study -> expression profile_type used for grouping.

    With ``requested`` that profile is used where it exists. Otherwise continuous mRNA
    (RNA-seq first) is preferred over z-scores; quantile groups are rank-based, so a
    monotone normalization does not change them.
    """
    rows = _query(f"""
        SELECT cs.cancer_study_identifier AS study, gp.stable_id AS stable_id,
               gp.datatype AS datatype
        FROM genetic_profile gp
        JOIN cancer_study cs ON gp.cancer_study_id = cs.cancer_study_id
        WHERE {_scope_sql(scope, "cs.cancer_study_identifier")}
            AND gp.genetic_alteration_type = 'MRNA_EXPRESSION'
    """)
    by_study: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        study, stable = r.get("study"), r.get("stable_id")
        if not study or not stable or not str(stable).startswith(study + "_"):
            continue
        by_study.setdefault(study, []).append((str(stable)[len(study) + 1 :], r.get("datatype")))

    def rank(item: tuple[str, str]) -> tuple:
        ptype, datatype = item
        lowered = ptype.lower()
        return (
            0 if datatype == "CONTINUOUS" else 1,
            0 if "rna_seq" in lowered else 1,
            1 if "outlier" in lowered else 0,
            0 if "all_sample" in lowered else 1,
            lowered,
        )

    chosen: dict[str, str] = {}
    for study, profiles in by_study.items():
        if requested:
            if any(p == requested for p, _ in profiles):
                chosen[study] = requested
        else:
            usable = [p for p in profiles if p[1] in ("CONTINUOUS", "Z-SCORE")]
            if usable:
                chosen[study] = sorted(usable, key=rank)[0][0]
    return chosen


def _parse_expression_grouping(spec) -> dict:
    if not isinstance(spec, dict) or not spec.get("gene"):
        raise ValueError(
            'group_by_expression must be an object like {"gene": "EGFR", '
            '"split": "top_vs_bottom_quartile"} (optional "profile").'
        )
    unknown = set(spec) - {"gene", "split", "profile"}
    if unknown:
        raise ValueError(f"group_by_expression has unsupported keys {sorted(unknown)}.")
    split = str(spec.get("split") or "median")
    if split not in dstats.SPLITS:
        raise ValueError(
            f"group_by_expression.split must be one of {', '.join(dstats.SPLITS)}."
        )
    profile = spec.get("profile")
    if profile is not None and not _EXPRESSION_PROFILE_RE.match(str(profile)):
        raise ValueError(f"Invalid expression profile '{profile}'.")
    return {
        "gene": _validate_gene_symbol(str(spec["gene"])),
        "split": split,
        "profile": str(profile) if profile else None,
    }


def _assign_expression_groups(
    scope, spec: dict, patients: set[str], warnings: list[str]
) -> tuple[dict[str, set[str]], dict]:
    """Split patients by a gene's mRNA level into quantile groups, within each study."""
    s = _scope_of(scope)
    profiles = _choose_expression_profiles(s, spec["profile"])
    missing = [sid for sid in s.study_ids if sid not in profiles]
    if not profiles:
        wanted = f"profile '{spec['profile']}'" if spec["profile"] else "an mRNA expression profile"
        raise ValueError(
            f"No study in {s.label} has {wanted}; expression grouping is not possible here. "
            "List a study's profiles with get_study_guide(study_id)."
        )
    if missing:
        warnings.append(
            f"{len(missing)} study/studies without "
            + (f"profile '{spec['profile']}'" if spec["profile"] else "mRNA expression")
            + f" were left out of the expression grouping: {', '.join(missing[:5])}"
            + ("…" if len(missing) > 5 else "")
        )
    pairs = ", ".join(f"('{sid}', '{ptype}')" for sid, ptype in sorted(profiles.items()))
    rows = _query(f"""
        SELECT s.patient_unique_id AS patient_unique_id,
               s.cancer_study_identifier AS study,
               avg(toFloat64OrNull(e.alteration_value)) AS value,
               count() AS n_samples
        FROM genetic_alteration_derived e
        JOIN sample_derived s ON e.sample_unique_id = s.sample_unique_id
        WHERE {_scope_sql(s, "e.cancer_study_identifier")}
            AND (e.cancer_study_identifier, e.profile_type) IN ({pairs})
            AND e.hugo_gene_symbol = '{spec["gene"]}'
            AND e.alteration_value NOT IN ('', 'NA')
            AND toFloat64OrNull(e.alteration_value) IS NOT NULL
        GROUP BY patient_unique_id, study
    """)
    by_study: dict[str, dict[str, float]] = {}
    multi = 0
    for r in rows:
        pid, study, value = r.get("patient_unique_id"), r.get("study"), r.get("value")
        if not pid or pid not in patients or value is None:
            continue
        by_study.setdefault(study, {})[pid] = float(value)
        if _as_int(r.get("n_samples")) > 1:
            multi += 1
    if not by_study:
        raise ValueError(
            f"No patients with survival data have {spec['gene']} expression values in "
            f"{s.label}."
        )
    members: dict[str, set[str]] = {}
    cutoffs: dict[str, dict] = {}
    excluded = 0
    skipped: list[str] = []
    order: list[str] = []
    for study, values in sorted(by_study.items()):
        if len(values) < 4:
            skipped.append(study)
            continue
        split = dstats.quantile_split(values, spec["split"])
        cutoffs[study] = {k: round(v, 4) for k, v in split["cutoffs"].items()}
        excluded += split["n_excluded"]
        for group in split["groups"]:
            if group["name"] not in order:
                order.append(group["name"])
            members.setdefault(group["name"], set()).update(group["ids"])
    if skipped:
        warnings.append(
            f"{len(skipped)} study/studies had fewer than 4 patients with expression and "
            f"survival data and were left out: {', '.join(skipped[:5])}"
        )
    if multi:
        warnings.append(
            f"{multi} patient(s) had several samples with expression values; their mean was "
            "used."
        )
    n_with = sum(len(v) for v in by_study.values())
    block = {
        "type": "expression",
        "gene": spec["gene"],
        "split": spec["split"],
        "split_description": dstats.SPLITS[spec["split"]],
        "profiles": profiles if not s.single else next(iter(profiles.values())),
        "cutoffs": cutoffs if not s.single else next(iter(cutoffs.values()), {}),
        "cutoffs_within": "study",
        "n_patients_with_expression": n_with,
        "n_excluded_by_split": excluded,
        "group_order": order,
    }
    return {name: members[name] for name in order}, block


def _build_survival_payload(
    study_id,
    endpoint: str,
    group_by_gene: str | None,
    alteration_types: list[str] | None,
    group_by_clinical: str | None,
    cohort: dict[str, list[str]] | None = None,
    groups: list[dict] | None = None,
    group_by_expression: dict | None = None,
    stratify_by: str | None = None,
) -> dict:
    """Assemble the Kaplan-Meier data contract consumed by the survival widget.

    Returns a dict with study/endpoint metadata, the cohort actually analysed,
    one entry per group (curve, counts, median), the log-rank result when 2+
    groups exist (stratified when ``stratify_by`` is given), and warnings.
    """
    endpoint = _validate_endpoint(endpoint)
    scope = _scope_of(study_id)
    warnings: list[str] = []

    groupings = [
        name
        for name, value in (
            ("groups", groups),
            ("group_by_expression", group_by_expression),
        )
        if value
    ]
    if groupings and (group_by_gene or group_by_clinical or len(groupings) > 1):
        raise ValueError(
            "Use one grouping at a time: group_by_gene, group_by_clinical, groups or "
            "group_by_expression."
        )
    parsed_groups = _parse_survival_groups(groups) if groups else None
    expression_spec = (
        _parse_expression_grouping(group_by_expression) if group_by_expression else None
    )
    if stratify_by is not None:
        stratify_by = str(stratify_by).strip()
        if stratify_by.upper() == STRATIFY_BY_STUDY:
            stratify_by = STRATIFY_BY_STUDY
        else:
            stratify_by = _validate_attribute_name(stratify_by)

    # Restrict the patient set up front: every count, curve and p-value below is
    # then over the cohort, never over the whole study.
    cohort_attribute = _parse_cohort(cohort)[0] if cohort is not None else None
    cohort_patients, n_cohort_ambiguous = _cohort_patient_ids(scope, cohort)
    observations, n_dropped = _fetch_survival_observations(scope, endpoint, cohort_patients)

    payload: dict = {
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "endpoint": endpoint,
        "endpoint_label": SURVIVAL_ENDPOINTS[endpoint],
        "time_unit": "months",
        "grouping": {"type": "none"},
        "groups": [],
        "time_ticks": [],
        "conf_level": SURVIVAL_CONF_LEVEL,
        # Describes ci_lower/ci_upper on every curve point. Kept out of 'notes'
        # on purpose: the widget renders 'notes' verbatim under the figure and
        # already captions the band itself, so this block is for whoever reads
        # the payload rather than the picture.
        "band": {
            "conf_level": SURVIVAL_CONF_LEVEL,
            "scope": "pointwise",
            "method": "Greenwood variance, log-log (log-minus-log) transform",
            "note": (
                "ci_lower/ci_upper are a 95% POINTWISE band: the interval covers "
                "S(t) at each t separately, not the whole curve simultaneously. "
                "Overlapping bands are NOT a test of whether two curves differ - "
                "quote the log-rank p-value in 'stats' for that. Bands widen "
                "sharply once few patients remain at risk, so curves that "
                "separate only in the tail are usually noise; check "
                "at_risk_at_ticks before reading late separation as a finding."
            ),
        },
        "stats": None,
        "stratification": None,
        "warnings": warnings,
        "notes": (
            "Survival is per-patient; alteration status is aggregated to the "
            "patient (any altered sample => altered). 'Wild-type' means no "
            "qualifying alteration among this study's patients with survival "
            "data and is not adjusted for gene-panel coverage. The 'cohort' "
            "block states which patients this curve describes."
        ),
    }
    payload["cohort"] = _cohort_block(
        scope, cohort, n_samples=None, n_patients=len(observations), warnings=warnings
    )
    if n_cohort_ambiguous:
        warnings.append(
            f"{n_cohort_ambiguous} patient(s) have conflicting {cohort_attribute} "
            "values across their samples (e.g. multi-primary) and were excluded "
            "from cohort matching."
        )
    if not observations:
        # Different failures: an over-narrow filter, a study id that does not
        # exist, or a study with no survival data at all. Saying which one saves
        # a round trip.
        if cohort is not None:
            payload["error"] = _cohort_empty_error(scope, cohort, "patients with survival data")
        else:
            payload["error"] = _unknown_study_error(scope) or (
                f"No {endpoint} survival data found for study '{scope.label}'. "
                f"Expected clinical attributes '{endpoint}_MONTHS' and '{endpoint}_STATUS'."
            )
        return payload
    if n_dropped:
        warnings.append(
            f"{n_dropped} patient(s) excluded for missing or unparseable "
            f"{endpoint}_MONTHS / {endpoint}_STATUS."
        )

    strata: dict[str, str] | None = None
    if stratify_by:
        strata, n_ambiguous = _patient_strata(scope, stratify_by)
        missing = [pid for pid in observations if pid not in strata]
        if len(missing) == len(observations):
            payload["error"] = (
                f"stratify_by='{stratify_by}' has no value for any analysed patient in "
                f"{scope.label}; check the attribute name (patient- or sample-level clinical "
                f"attribute, or '{STRATIFY_BY_STUDY}')."
            )
            return payload
        if missing:
            for pid in missing:
                observations.pop(pid, None)
            warnings.append(
                f"{len(missing)} patient(s) without a {stratify_by} value"
                + (f" ({n_ambiguous} with conflicting values)" if n_ambiguous else "")
                + " were excluded so the curves and the stratified test describe the same "
                "patients."
            )
        payload["cohort"]["n_patients"] = len(observations)

    # Decide how to split the cohort into groups (gene alteration takes
    # precedence over a clinical attribute; both unset => whole cohort).
    grouped: list[tuple[str, list[tuple[float, int]]]] = []
    if group_by_gene and group_by_clinical:
        warnings.append("Both group_by_gene and group_by_clinical were given; grouping by gene.")
    if group_by_gene:
        try:
            gene = _validate_gene_symbol(group_by_gene)
        except ValueError as e:
            raise ValueError(
                f"{e} group_by_gene takes a single gene symbol; for co-mutation, codon-range, "
                "exclusion or wild-type groups use groups=[...] with OQL conditions."
            ) from e
        alt_types = alteration_types or ["mutation"]
        altered = _altered_patients(scope, gene, alt_types)
        altered_obs = [obs for pid, obs in observations.items() if pid in altered]
        wt_obs = [obs for pid, obs in observations.items() if pid not in altered]
        grouped = [
            (f"{gene} altered", altered_obs),
            (f"{gene} wild-type", wt_obs),
        ]
        payload["grouping"] = {
            "type": "alteration",
            "gene": gene,
            "alteration_types": alt_types,
        }
        member_of = {
            pid: grouped[0][0] if pid in altered else grouped[1][0] for pid in observations
        }
    elif group_by_clinical:
        attribute = _validate_attribute_name(group_by_clinical)
        pat_values, n_ambiguous = _clinical_patient_values(scope, attribute)
        if n_ambiguous:
            warnings.append(
                f"{n_ambiguous} patient(s) excluded from grouping due to "
                f"conflicting {attribute} values across samples."
            )
        buckets: dict[str, list[tuple[float, int]]] = {}
        for pid, obs in observations.items():
            val = pat_values.get(pid)
            if val is None:
                continue
            buckets.setdefault(val, []).append(obs)
        ordered = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
        if len(ordered) > MAX_SURVIVAL_GROUPS:
            warnings.append(
                f"{attribute} has {len(ordered)} values; showing the "
                f"{MAX_SURVIVAL_GROUPS} largest groups."
            )
            ordered = ordered[:MAX_SURVIVAL_GROUPS]
        kept = {val for val, _ in ordered}
        grouped = [(f"{attribute}: {val}", obs) for val, obs in ordered]
        payload["grouping"] = {"type": "clinical", "attribute": attribute}
        member_of = {
            pid: f"{attribute}: {pat_values[pid]}"
            for pid in observations
            if pat_values.get(pid) in kept
        }
    elif parsed_groups:
        members, block = _assign_custom_groups(scope, parsed_groups, set(observations), warnings)
        grouped = [
            (name, [observations[pid] for pid in sorted(pids)]) for name, pids in members.items()
        ]
        payload["grouping"] = block
        payload["notes"] = (
            "Survival is per-patient. Groups are defined by the OQL conditions in "
            "'grouping.groups' (see grouping.semantics); gene-panel coverage is respected for "
            "'unaltered' conditions. The 'cohort' block states which patients the curves "
            "describe."
        )
        member_of = {pid: name for name, pids in members.items() for pid in pids}
    elif expression_spec:
        members, block = _assign_expression_groups(
            scope, expression_spec, set(observations), warnings
        )
        grouped = [
            (name, [observations[pid] for pid in sorted(pids)]) for name, pids in members.items()
        ]
        payload["grouping"] = block
        payload["notes"] = (
            f"Survival is per-patient. Patients are split by {expression_spec['gene']} mRNA "
            "level into quantile groups computed within each study over the patients that "
            "have both expression and survival data (cut-offs in grouping.cutoffs). The "
            "'cohort' block states which patients the curves describe."
        )
        member_of = {pid: name for name, pids in members.items() for pid in pids}
    else:
        grouped = [("All patients", list(observations.values()))]
        member_of = {pid: "All patients" for pid in observations}

    # Drop empty groups; bail out if nothing is left to plot.
    empty = [name for name, obs in grouped if not obs]
    grouped = [(name, obs) for name, obs in grouped if obs]
    if not grouped:
        payload["error"] = "No patients remained after grouping; nothing to plot."
        return payload
    if empty and (parsed_groups or expression_spec):
        warnings.append(f"Group(s) with no patients were dropped: {', '.join(empty)}.")

    max_time = max(t for _, obs in grouped for t, _ in obs)
    time_ticks = _survival_time_ticks(max_time)
    payload["time_ticks"] = time_ticks

    groups_out = []
    binned_any = False
    for name, obs in grouped:
        km = kaplan_meier(obs, time_ticks=time_ticks, conf_level=SURVIVAL_CONF_LEVEL)
        full_points = len(km["curve"])
        points = downsample_curve(km["curve"], MAX_CURVE_POINTS)
        binned = len(points) < full_points
        binned_any = binned_any or binned
        groups_out.append(
            {
                "name": name,
                "n_patients": km["n_patients"],
                "n_events": km["n_events"],
                "n_censored": km["n_censored"],
                "median_survival": (
                    round(km["median_survival"], 2) if km["median_survival"] is not None else None
                ),
                # Present only when the curve was binned, so the reader can tell
                # a 200-point curve that is complete from one that is a summary.
                **(
                    {"curve_binned": True, "curve_steps_total": full_points - 1}
                    if binned
                    else {}
                ),
                "curve": [
                    {
                        "time": round(p["time"], 3),
                        "survival": round(p["survival"], 5),
                        "ci_lower": round(p["ci_lower"], 5),
                        "ci_upper": round(p["ci_upper"], 5),
                        "at_risk": p["at_risk"],
                        "events": p["events"],
                        "censored": p["censored"],
                    }
                    for p in points
                ],
                "at_risk_at_ticks": km["at_risk_at_ticks"],
            }
        )
    payload["groups"] = groups_out
    if binned_any:
        warnings.append(
            f"Curves longer than {MAX_CURVE_POINTS} points were binned down to "
            f"{MAX_CURVE_POINTS} (see curve_binned / curve_steps_total). Survival, "
            "CI and at-risk values are exact at each time reported; 'events' and "
            "'censored' are summed over the interval ending at that time. Medians, "
            "at-risk tables and the log-rank test are computed from the full data."
        )

    if len(grouped) >= 2:
        lr = logrank_test({name: obs for name, obs in grouped})
        if lr.get("p_value") is not None:
            lr["p_value"] = round(lr["p_value"], 6)
            lr["chi_square"] = round(lr["chi_square"], 4)
        payload["stats"] = lr
        if strata is not None:
            names = [name for name, _ in grouped]
            by_stratum: dict[str, dict[str, list[tuple[float, int]]]] = {}
            for pid, obs in observations.items():
                group = member_of.get(pid)
                if group not in names:
                    continue
                by_stratum.setdefault(strata[pid], {}).setdefault(group, []).append(obs)
            slr = stratified_logrank_test(by_stratum, order=names)
            if slr.get("p_value") is not None:
                slr["p_value"] = round(slr["p_value"], 6)
                slr["chi_square"] = round(slr["chi_square"], 4)
            payload["stats_unstratified"] = lr
            payload["stats"] = slr
            payload["stratification"] = {
                "by": stratify_by,
                "method": "stratified log-rank (observed - expected summed within strata)",
                "n_strata": slr.get("n_strata"),
                "n_informative_strata": slr.get("n_informative_strata"),
            }
        else:
            n_types = payload["cohort"].get("n_cancer_types")
            if n_types is None:
                values, _ = _clinical_patient_values(scope, COHORT_SPREAD_ATTRIBUTE)
                n_types = len({values[pid] for pid in observations if pid in values})
            if n_types > 1 or not scope.single:
                spread = f"{n_types} cancer types" if n_types > 1 else ""
                studies = "" if scope.single else f"{len(scope.study_ids)} studies"
                what = " and ".join(x for x in (spread, studies) if x)
                warnings.append(
                    f"The groups are compared across {what} without stratification, so the "
                    "log-rank p-value is NOT adjusted for cancer type: a difference can come "
                    "from the groups' cancer-type mix rather than the grouping. Re-run with "
                    "stratify_by='CANCER_TYPE' (or 'STUDY') for a stratified log-rank test."
                )
    return payload


# --- OncoPrint app helpers ---------------------------------------------------

# Max sample columns rendered in the OncoPrint matrix. Studies can have tens of
# thousands of samples; the widget shows altered samples first, then fills with
# unaltered profiled samples up to this cap. Per-gene frequencies in gene_stats
# are computed over the full profiled set, not just the shown columns.
MAX_ONCOPRINT_SAMPLES = 500
DEFAULT_ONCOPRINT_GENES = 20

# Alteration types eligible for the matrix (subset of ALTERATION_CONFIGS keys).
ONCOPRINT_ALTERATION_TYPES = (
    "mutation",
    "amplification",
    "deep_deletion",
    "structural_variant",
)

# MAF mutation_type values grouped into the classes used for cell coloring.
# Anything unrecognized falls through to "other".
_TRUNCATING_MUTATION_TYPES = {
    "Nonsense_Mutation",
    "Frame_Shift_Del",
    "Frame_Shift_Ins",
    "Splice_Site",
    "Splice_Region",
    "Nonstop_Mutation",
    "Translation_Start_Site",
}
_INFRAME_MUTATION_TYPES = {"In_Frame_Del", "In_Frame_Ins"}

# Severity order for picking a cell's representative class when a (gene, sample)
# carries multiple mutations. "mutation" is the single class of the collapsed view.
_MUT_CLASS_PRIORITY = {"truncating": 3, "missense": 2, "inframe": 1, "other": 0, "mutation": 1}

# Mutation colour granularity: "detailed" = missense / truncating / inframe / other;
# "collapsed" = one "mutation" class, for a legend that should not split by type.
ONCOPRINT_MUTATION_CLASSES = ("detailed", "collapsed")
# OQL alteration keyword -> the alteration_types name used everywhere else.
OQL_ALTERATION_NAMES = {
    "MUT": "mutation",
    "AMP": "amplification",
    "HOMDEL": "deep_deletion",
    "FUSION": "structural_variant",
}
COLLAPSED_MUTATION_CLASS = "mutation"


def _is_float(value) -> bool:
    """True if ``value`` parses as a float (used to type clinical tracks)."""
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _mutation_class(mutation_type: str | None) -> str:
    """Classify a MAF mutation_type into missense/truncating/inframe/other."""
    if not mutation_type:
        return "other"
    mt = str(mutation_type).strip()
    if mt == "Missense_Mutation":
        return "missense"
    if mt in _TRUNCATING_MUTATION_TYPES:
        return "truncating"
    if mt in _INFRAME_MUTATION_TYPES:
        return "inframe"
    return "other"


def _more_severe_mut(current: str | None, candidate: str) -> str:
    """Keep the higher-priority mutation class for a cell with several mutations."""
    if current is None:
        return candidate
    return candidate if _MUT_CLASS_PRIORITY[candidate] > _MUT_CLASS_PRIORITY[current] else current


def _oncoprint_event_filter(alteration_types: list[str]) -> str:
    """Build the combined SQL predicate for the requested alteration types.

    Reuses the per-type ``event_filter`` strings in ``ALTERATION_CONFIGS`` so the
    OncoPrint and the rest of the server agree on what each alteration means.
    """
    filters = []
    for alt in alteration_types:
        cfg = _validate_alteration_type(alt)
        filters.append(f"({cfg['event_filter']})")
    return " OR ".join(filters)


def _resolve_oncoprint_genes(study_id, genes: list[str] | None) -> list[str]:
    """Return the gene row list for the OncoPrint.

    If ``genes`` is given, validate and de-duplicate (preserving order), clamped
    to ``MAX_ANALYSIS_GENES``. Otherwise default to the study's most-altered
    genes across all alteration types.
    """
    if genes:
        resolved: list[str] = []
        seen: set[str] = set()
        for g in genes:
            gene = _validate_gene_symbol(g)
            if gene not in seen:
                seen.add(gene)
                resolved.append(gene)
        return resolved[:MAX_ANALYSIS_GENES]

    combined = _oncoprint_event_filter(list(ONCOPRINT_ALTERATION_TYPES))
    rows = _query(f"""
        SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS altered_samples
        FROM genomic_event_derived
        WHERE {_scope_sql(study_id)}
            AND ({combined})
        GROUP BY hugo_gene_symbol
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
        LIMIT {DEFAULT_ONCOPRINT_GENES}
    """)
    return [r["hugo_gene_symbol"] for r in rows if r.get("hugo_gene_symbol")]


def _add_event_to_cell(cell: dict, row: dict, collapse: bool = False) -> None:
    """Fold one genomic_event_derived row into an OncoPrint cell."""
    vt = row.get("variant_type")
    if vt == "structural_variant":
        cell["sv"] = True
    elif vt == "cna":
        try:
            cna_val = int(row.get("cna_alteration"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            cna_val = None
        if cna_val == 2:
            cell["cna"] = "amp"
        elif cna_val == -2:
            cell["cna"] = "deepdel"
    elif vt == "mutation":
        cls = COLLAPSED_MUTATION_CLASS if collapse else _mutation_class(row.get("mutation_type"))
        cell["mut"] = _more_severe_mut(cell["mut"], cls)


def _fetch_oncoprint_events(
    study_id, genes: list[str], alteration_types: list[str], collapse: bool = False
) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
    """Fetch per-sample alterations for ``genes`` (study-wide, not just shown).

    Returns ``(cells, sample_to_patient)`` where ``cells[gene][sample]`` is
    ``{"cna": "amp"|"deepdel"|None, "mut": <class>|None, "sv": bool}`` for every
    altered (gene, sample) pair, and ``sample_to_patient`` maps each altered
    sample to its patient (for clinical-track grain resolution).
    """
    gene_list = ", ".join(f"'{g}'" for g in genes)
    combined = _oncoprint_event_filter(alteration_types)
    rows = _query(f"""
        SELECT sample_unique_id, patient_unique_id, hugo_gene_symbol,
               variant_type, mutation_type, cna_alteration
        FROM genomic_event_derived
        WHERE {_scope_sql(study_id)}
            AND hugo_gene_symbol IN ({gene_list})
            AND ({combined})
    """)
    cells: dict[str, dict[str, dict]] = {}
    sample_to_patient: dict[str, str] = {}
    for row in rows:
        sid = row.get("sample_unique_id")
        gene = row.get("hugo_gene_symbol")
        if not sid or not gene:
            continue
        pid = row.get("patient_unique_id")
        if pid:
            sample_to_patient[sid] = pid
        cell = cells.setdefault(gene, {}).setdefault(sid, {"cna": None, "mut": None, "sv": False})
        _add_event_to_cell(cell, row, collapse)
    return cells, sample_to_patient


def _track_cells(
    tracks: list, rows: list[dict], collapse: bool
) -> tuple[dict[str, dict[str, dict]], dict[str, str], list[dict]]:
    """Cells per OQL track, plus what each ``!=`` exclusion removed.

    ``rows`` are every event of the tracks' genes (see _fetch_track_events), so a
    vetoed event is seen and counted rather than silently missing.
    """
    cells: dict[str, dict[str, dict]] = {t.label: {} for t in tracks}
    sample_to_patient: dict[str, str] = {}
    exclusions: list[dict] = []
    for track in tracks:
        vetoes = aq.track_exclusions(track)
        events_removed = [0] * len(vetoes)
        samples_hit: list[set[str]] = [set() for _ in vetoes]
        track_cells = cells[track.label]
        for row in rows:
            sid = row.get("sample_unique_id")
            if not sid:
                continue
            if aq.track_wants_event(track, row):
                pid = row.get("patient_unique_id")
                if pid:
                    sample_to_patient[sid] = pid
                cell = track_cells.setdefault(sid, {"cna": None, "mut": None, "sv": False})
                _add_event_to_cell(cell, row, collapse)
            elif vetoes and aq.track_wants_event_ignoring_exclusions(track, row):
                for i, (line, command) in enumerate(vetoes):
                    if aq.exclusion_vetoes(line, command, row):
                        events_removed[i] += 1
                        samples_hit[i].add(sid)
        for i, (line, command) in enumerate(vetoes):
            exclusions.append(
                {
                    "track": track.label,
                    "gene": line.gene,
                    "exclusion": command.text,
                    "events_removed": events_removed[i],
                    # Samples that carried the excluded change and no other event the
                    # track wants, i.e. samples the exclusion turned unaltered.
                    "samples_removed": len(samples_hit[i] - set(track_cells)),
                }
            )
    return cells, sample_to_patient, exclusions


def _fetch_profiled_samples(study_id, genes: list[str]) -> dict[str, set[str]]:
    """Return, per gene, the set of samples profiled for that gene.

    Uses the canonical coverage views: ``mutation_panel_gene_coverage`` (panel
    gene membership) plus ``mutation_wes_coverage`` (WES samples, profiled for
    every gene). This is the documented way to avoid the >100% frequency trap
    from treating WES as a named panel. Covers MUTATION_EXTENDED profiling.
    """
    gene_list = ", ".join(f"'{g}'" for g in genes)
    profiled: dict[str, set[str]] = {g: set() for g in genes}

    panel_rows = _query(f"""
        SELECT sample_unique_id, hugo_gene_symbol
        FROM mutation_panel_gene_coverage
        WHERE {_scope_sql(study_id)}
            AND hugo_gene_symbol IN ({gene_list})
    """)
    for row in panel_rows:
        sid = row.get("sample_unique_id")
        gene = row.get("hugo_gene_symbol")
        if sid and gene in profiled:
            profiled[gene].add(sid)

    wes_rows = _query(f"""
        SELECT sample_unique_id
        FROM mutation_wes_coverage
        WHERE {_scope_sql(study_id)}
    """)
    wes_samples = {r.get("sample_unique_id") for r in wes_rows if r.get("sample_unique_id")}
    for gene in genes:
        profiled[gene].update(wes_samples)
    return profiled


def _select_oncoprint_samples(
    profiled_union: set[str], altered: set[str], max_samples: int
) -> tuple[list[str], int, bool]:
    """Choose which samples become matrix columns.

    Altered samples first (the alteration landscape), then unaltered profiled
    samples for frequency context, capped at ``max_samples``. The returned order
    is provisional — ``_memo_sort`` reorders the final columns.

    Returns ``(selected_samples, n_total, truncated)``.
    """
    universe = profiled_union | altered
    n_total = len(universe)
    selected = sorted(altered)[:max_samples]
    if len(selected) < max_samples:
        selected += sorted(universe - altered)[: max_samples - len(selected)]
    return selected, n_total, n_total > len(selected)


def _memo_sort(
    samples: list[str],
    genes: list[str],
    cells: dict[str, dict[str, dict]],
    not_profiled: dict[str, set[str]],
) -> list[str]:
    """Order samples MemoSort-style so alterations cluster top-left.

    Per (sample, gene): score = cna(4) + mut(2) + sv(1), or -1 if the sample is
    not profiled for that gene. Samples are compared gene-by-gene in row order
    (descending score); the first gene that differs decides. Ties break on the
    sample id for determinism.
    """

    def cell_score(gene: str, sid: str) -> int:
        if sid in not_profiled.get(gene, ()):
            return -1  # not profiled sinks below "profiled, no alteration"
        cell = cells.get(gene, {}).get(sid)
        if not cell:
            return 0
        return (
            (4 if cell.get("cna") else 0)
            + (2 if cell.get("mut") else 0)
            + (1 if cell.get("sv") else 0)
        )

    return sorted(samples, key=lambda sid: (tuple(-cell_score(g, sid) for g in genes), sid))


def _fetch_clinical_tracks(
    study_id,
    samples: list[str],
    sample_to_patient: dict[str, str],
    attributes: list[str],
) -> list[dict]:
    """Build one clinical annotation track per attribute for the shown samples.

    Resolves each sample's value at the right grain using the ``type`` column:
    sample-level rows map directly by ``sample_unique_id``; patient-level rows
    fan out to that patient's shown samples. A track is "numeric" iff every
    present value parses as a float.
    """
    if not samples or not attributes:
        return []
    sample_set = set(samples)
    patient_to_samples: dict[str, list[str]] = {}
    for sid in samples:
        pid = sample_to_patient.get(sid)
        if pid:
            patient_to_samples.setdefault(pid, []).append(sid)

    tracks: list[dict] = []
    for attr in attributes:
        try:
            attribute = _validate_attribute_name(attr)
        except ValueError:
            continue
        rows = _query(f"""
            SELECT sample_unique_id, patient_unique_id, attribute_value, type
            FROM clinical_data_derived
            WHERE {_scope_sql(study_id)}
                AND attribute_name = '{attribute}'
        """)
        values: dict[str, str] = {}
        patient_values: dict[str, str] = {}
        for row in rows:
            val = row.get("attribute_value")
            if val in (None, ""):
                continue
            if row.get("type") == "patient":
                pid = row.get("patient_unique_id")
                if pid:
                    patient_values.setdefault(pid, val)
            else:  # sample-level
                sid = row.get("sample_unique_id")
                if sid and sid in sample_set:
                    values[sid] = val
        # Fall back to patient-level values for samples without a sample-level row.
        for pid, val in patient_values.items():
            for sid in patient_to_samples.get(pid, ()):
                values.setdefault(sid, val)
        if not values:
            continue
        tracks.append(
            {
                "name": attribute,
                "label": attribute.replace("_", " ").title(),
                "kind": "numeric" if all(_is_float(v) for v in values.values()) else "categorical",
                "values": values,
            }
        )
    return tracks


def _oncoprint_gene_stats(
    genes: list[str],
    cells: dict[str, dict[str, dict]],
    profiled: dict[str, set[str]],
) -> list[dict]:
    """Per-gene altered/profiled counts and frequency over the full study set."""
    stats = []
    for gene in genes:
        gene_cells = cells.get(gene, {})
        altered = len(gene_cells)
        prof = len(profiled.get(gene, set()))
        by_type = {"mutation": 0, "amplification": 0, "deep_deletion": 0, "structural_variant": 0}
        for cell in gene_cells.values():
            if cell.get("mut"):
                by_type["mutation"] += 1
            if cell.get("cna") == "amp":
                by_type["amplification"] += 1
            elif cell.get("cna") == "deepdel":
                by_type["deep_deletion"] += 1
            if cell.get("sv"):
                by_type["structural_variant"] += 1
        stats.append(
            {
                "gene": gene,
                "altered": altered,
                "profiled": prof,
                "freq_pct": round(altered * 100.0 / prof, 1) if prof else None,
                "by_type": by_type,
            }
        )
    return stats


def _build_oncoprint_payload(
    study_id,
    genes: list[str] | None,
    alteration_types: list[str] | None,
    clinical_tracks: list[str] | None,
    max_samples: int | None,
    cohort: dict[str, list[str]] | None = None,
    oql: str | None = None,
    mutation_classes: str = "detailed",
) -> dict:
    """Assemble the OncoPrint data contract consumed by the widget."""
    scope = _scope_of(study_id)
    warnings: list[str] = []
    if mutation_classes not in ONCOPRINT_MUTATION_CLASSES:
        raise ValueError(
            f"mutation_classes must be one of {', '.join(ONCOPRINT_MUTATION_CLASSES)}; "
            f"got '{mutation_classes}'."
        )
    collapse = mutation_classes == "collapsed"
    tracks = None
    if oql is not None:
        if genes or alteration_types:
            raise ValueError(
                "Pass either genes/alteration_types or oql, not both: oql already names the "
                "genes and the alterations of each track."
            )
        tracks = aq.parse_alteration_query(oql)
        if len(tracks) > MAX_ANALYSIS_GENES:
            raise ValueError(
                f"The query has {len(tracks)} tracks; the OncoPrint shows at most "
                f"{MAX_ANALYSIS_GENES}."
            )
    cohort_samples = _cohort_sample_ids(scope, cohort)
    alt_types = alteration_types or list(ONCOPRINT_ALTERATION_TYPES)
    for alt in alt_types:  # validate up front (raises ValueError on bad input)
        _validate_alteration_type(alt)
    cap = (
        MAX_ONCOPRINT_SAMPLES
        if max_samples is None
        else max(1, min(int(max_samples), MAX_ONCOPRINT_SAMPLES))
    )
    track_attrs = clinical_tracks if clinical_tracks is not None else ["CANCER_TYPE", "SAMPLE_TYPE"]

    payload: dict = {
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "genes": [],
        "samples": [],
        "alteration_types": alt_types,
        "mutation_classes": mutation_classes,
        "cells": {},
        "not_profiled": {},
        "gene_stats": [],
        "clinical_tracks": [],
        "n_samples_total": 0,
        "n_samples_shown": 0,
        "warnings": warnings,
        "notes": (
            "OncoPrint is per-sample (columns = samples). Cells show the "
            "alteration type; a gray cell means the sample was not profiled for "
            "that gene (gene-panel coverage). Per-gene % is computed over the "
            "full profiled set, not just the shown columns; for cohorts larger "
            "than the column cap, altered samples are shown first and the matrix "
            "is truncated. The 'cohort' block states which samples this matrix "
            "describes."
        ),
    }
    if collapse:
        payload["notes"] += (
            " mutation_classes='collapsed': every mutation (missense, truncating, inframe, "
            "splice, other) is drawn in one 'mutation' colour."
        )

    if cohort is not None and not cohort_samples:
        payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
        payload["error"] = _cohort_empty_error(scope, cohort, "samples")
        return payload

    if tracks is not None:
        _check_driver_annotations(scope, tracks)
        genes_resolved = [t.label for t in tracks]
        rows = _fetch_track_events(scope, tracks)
        if cohort_samples is not None:
            rows = [r for r in rows if r.get("sample_unique_id") in cohort_samples]
        cells, sample_to_patient, exclusions = _track_cells(tracks, rows, collapse)
        member_genes = sorted({g for t in tracks for g in t.genes})
        by_gene = _fetch_profiled_samples(scope, member_genes)
        # A merged track counts a sample as profiled when any of its genes was
        # (cbioportal.org's convention for merged tracks).
        profiled = {t.label: set().union(*(by_gene[g] for g in t.genes)) for t in tracks}
        payload["alteration_types"] = [
            name
            for keyword, name in OQL_ALTERATION_NAMES.items()
            if any(
                c.alteration == keyword
                for t in tracks
                for ln in t.lines
                for c in ln.effective_commands
            )
        ]
        payload["query"] = aq.format_query(tracks)
        payload["tracks"] = [aq.describe_track(t) for t in tracks]
        payload["exclusions"] = exclusions
        payload["notes"] += (
            " Rows are the tracks of 'query' in the order given; a merged track "
            "([...]) is altered when any of its genes is, and counts a sample as profiled "
            "when any of its genes was."
        )
        if any(t.merged for t in tracks) or any(
            aq.track_variant_types(t) != {"mutation"} for t in tracks
        ):
            warnings.append(
                "Profiling uses mutation (MUTATION_EXTENDED) gene-panel coverage for every "
                "track, including copy-number and structural-variant commands."
            )
        for item in exclusions:
            if item["events_removed"] == 0:
                warnings.append(
                    f"Exclusion '{item['exclusion']}' on {item['gene']} matched no event in "
                    "this cohort, so it removed nothing."
                )
    else:
        genes_resolved = _resolve_oncoprint_genes(scope, genes)
        if not genes_resolved:
            payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
            payload["error"] = _unknown_study_error(scope) or (
                f"No genes to display for study '{scope.label}'. The study may have no "
                "genomic events, or the named genes have no alterations."
            )
            return payload
        if cohort_samples is not None and not genes:
            warnings.append(
                "Genes were auto-selected by study-wide alteration frequency, not by "
                "frequency within the filtered cohort."
            )
        cells, sample_to_patient = _fetch_oncoprint_events(
            scope, genes_resolved, alt_types, collapse
        )
        profiled = _fetch_profiled_samples(scope, genes_resolved)

    # Apply the cohort to BOTH the alterations and the profiled denominator. If
    # only the numerator were filtered, every per-gene frequency below would be
    # computed against a study-wide denominator and come out wrong.
    if cohort_samples is not None:
        cells = {
            g: {sid: c for sid, c in gene_cells.items() if sid in cohort_samples}
            for g, gene_cells in cells.items()
        }
        cells = {g: c for g, c in cells.items() if c}
        profiled = {g: s & cohort_samples for g, s in profiled.items()}
        sample_to_patient = {
            sid: pid for sid, pid in sample_to_patient.items() if sid in cohort_samples
        }

    if tracks is None:
        # Order rows most-altered first (the staircase); ties keep input order.
        # OQL tracks keep the order the user wrote them in, as the portal does.
        orig_index = {g: i for i, g in enumerate(genes_resolved)}
        genes_resolved.sort(key=lambda g: (-len(cells.get(g, {})), orig_index[g]))
    payload["genes"] = genes_resolved

    altered_samples: set[str] = set()
    for gene_cells in cells.values():
        altered_samples.update(gene_cells.keys())
    profiled_union: set[str] = set()
    for s in profiled.values():
        profiled_union |= s

    if not altered_samples and not profiled_union:
        payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
        payload["error"] = _unknown_study_error(scope) or (
            f"No samples found for study '{scope.label}' with the selected genes and "
            "alteration types."
        )
        return payload

    selected, n_total, truncated = _select_oncoprint_samples(profiled_union, altered_samples, cap)
    selected_set = set(selected)
    payload["n_samples_total"] = n_total
    payload["n_samples_shown"] = len(selected)
    payload["cohort"] = _cohort_block(scope, cohort, n_total, None, warnings)
    if not scope.single:
        overlap = _scope_overlap_warning(scope)
        if overlap:
            warnings.append(overlap)
    if truncated:
        warnings.append(
            f"Showing {len(selected)} of {n_total} profiled samples (altered "
            f"samples prioritized). Per-gene frequencies reflect all {n_total} samples."
        )

    # Gray "not profiled" cells: shown samples outside the gene's profiled set.
    # Only meaningful when coverage data exists for the study.
    not_profiled: dict[str, set[str]] = {}
    if profiled_union:
        for gene in genes_resolved:
            gene_profiled = profiled.get(gene, set())
            np = {
                sid
                for sid in selected
                if sid not in gene_profiled and sid not in cells.get(gene, {})
            }
            if np:
                not_profiled[gene] = np
    else:
        warnings.append("Gene-panel coverage data unavailable; 'not profiled' cells are not shown.")

    payload["samples"] = _memo_sort(selected, genes_resolved, cells, not_profiled)

    # Trim alteration cells to the shown samples (matrix render only).
    trimmed: dict[str, dict[str, dict]] = {}
    for gene, gene_cells in cells.items():
        kept = {sid: c for sid, c in gene_cells.items() if sid in selected_set}
        if kept:
            trimmed[gene] = kept
    payload["cells"] = trimmed
    payload["not_profiled"] = {g: sorted(s) for g, s in not_profiled.items()}

    # gene_stats uses the full (untrimmed) cells + profiled set for accurate %.
    payload["gene_stats"] = _oncoprint_gene_stats(genes_resolved, cells, profiled)
    payload["clinical_tracks"] = _fetch_clinical_tracks(
        scope, payload["samples"], sample_to_patient, track_attrs
    )
    return payload


# --- Mutation lollipop app helpers -------------------------------------------

# Max distinct protein-change lollipops kept in the payload (highest-recurrence
# first). A single gene rarely exceeds this many distinct changes, but large
# pan-cancer studies can; truncating keeps the payload and the widget readable.
MAX_LOLLIPOP_MUTATIONS = 400

# Codon-position parser for protein-change notation. HGVS protein strings in
# mutation_variant look like p.V600E / p.R175H / p.E746_A750del / p.R213* /
# p.K27fs; the codon number is the first run of digits in the string.
_PROTEIN_POS_RE = re.compile(r"\d+")


def _parse_protein_position(mutation_variant: str | None) -> int | None:
    """Parse the 1-based codon position from a protein-change string.

    Returns the first integer in the HGVS protein notation (``p.V600E`` -> 600,
    ``p.E746_A750del`` -> 746), or ``None`` when there is no usable position
    ("NA", blank, or notations without a codon number).
    """
    if not mutation_variant:
        return None
    m = _PROTEIN_POS_RE.search(str(mutation_variant))
    if not m:
        return None
    try:
        pos = int(m.group())
    except ValueError:
        return None
    return pos if pos > 0 else None


def _clean_protein_change(mutation_variant: str) -> str:
    """Strip a leading ``p.`` from a protein-change label for display."""
    s = str(mutation_variant).strip()
    if s[:2].lower() == "p.":
        s = s[2:]
    return s


def _fetch_lollipop_mutations(study_id, gene: str) -> list[dict]:
    """Fetch raw per-sample mutation rows for one gene.

    Uses the canonical mutation filter (``variant_type='mutation'`` excluding
    UNCALLED, matching ``ALTERATION_CONFIGS['mutation']``). One row per stored
    event, so the caller can count *distinct samples* per protein change exactly.
    """
    mut_filter = ALTERATION_CONFIGS["mutation"]["event_filter"]
    return _query(f"""
        SELECT sample_unique_id, mutation_variant, mutation_type, cancer_study_identifier
        FROM genomic_event_derived
        WHERE {_scope_sql(study_id)}
            AND hugo_gene_symbol = '{gene}'
            AND ({mut_filter})
    """)


# --- Protein regions (Pfam domains via Genome Nexus, or explicit codon ranges) ---
#
# The widget has always drawn Pfam domains fetched from Genome Nexus, but only for
# display: "mutations in EGFR's tyrosine kinase domain" could not be counted. The
# server now resolves the same canonical transcript (MSKCC isoform override, the
# one cBioPortal's own annotation pipeline uses) so a domain can filter the counts.

GENOME_NEXUS_API = ui.GENOME_NEXUS_ORIGIN
GENOME_NEXUS_TIMEOUT_SECS = 10
# Words that carry no domain identity ("the kinase domain" = "kinase").
_DOMAIN_STOPWORDS = frozenset({"domain", "domains", "region", "the", "of", "a", "an", "motif"})
_GENOME_NEXUS_CACHE: dict[str, object] = {}


def _genome_nexus_get(path: str):
    """GET a Genome Nexus JSON resource; raises ValueError when it cannot be read."""
    import urllib.error
    import urllib.request

    url = GENOME_NEXUS_API + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=GENOME_NEXUS_TIMEOUT_SECS) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise ValueError(
            f"Could not reach Genome Nexus ({url}) to resolve protein domains: {e}. Pass the "
            "codon range explicitly with protein_range=[start, end] instead."
        ) from e


def _genome_nexus_cached(path: str):
    if path not in _GENOME_NEXUS_CACHE:
        _GENOME_NEXUS_CACHE[path] = _genome_nexus_get(path)
    return _GENOME_NEXUS_CACHE[path]


def _pfam_domains(gene: str) -> dict:
    """Canonical transcript, protein length and named Pfam domains of a gene."""
    transcript = _genome_nexus_cached(
        f"/ensembl/canonical-transcript/hgnc/{gene}?isoformOverrideSource=mskcc"
    )
    if not isinstance(transcript, dict) or not transcript.get("transcriptId"):
        raise ValueError(
            f"Genome Nexus has no canonical transcript for {gene}; pass protein_range=[start, "
            "end] to filter by codons instead."
        )
    domains = []
    for d in transcript.get("pfamDomains") or []:
        accession = d.get("pfamDomainId")
        start, end = d.get("pfamDomainStart"), d.get("pfamDomainEnd")
        if not accession or start is None or end is None:
            continue
        info = _genome_nexus_cached(f"/pfam/domain/{accession}")
        info = info if isinstance(info, dict) else {}
        domains.append(
            {
                "pfam_accession": accession,
                "name": info.get("name") or accession,
                "description": info.get("description") or "",
                "start": int(start),
                "end": int(end),
            }
        )
    return {
        "transcript_id": transcript.get("transcriptId"),
        "protein_length": transcript.get("proteinLength"),
        "domains": sorted(domains, key=lambda d: d["start"]),
    }


def _resolve_protein_region(gene: str, domain, protein_range) -> dict | None:
    """Turn ``domain`` (Pfam accession, name or words) or ``protein_range`` into codon ranges."""
    if domain is None and protein_range is None:
        return None
    if domain is not None and protein_range is not None:
        raise ValueError("Pass either domain or protein_range, not both.")
    if protein_range is not None:
        if (
            not isinstance(protein_range, (list, tuple))
            or len(protein_range) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in protein_range)
        ):
            raise ValueError("protein_range must be [start_codon, end_codon], e.g. [712, 979].")
        start, end = protein_range
        if start < 1 or end < start:
            raise ValueError("protein_range needs 1 <= start <= end.")
        return {
            "source": "protein_range",
            "label": f"codons {start}-{end}",
            "ranges": [[start, end]],
        }

    term = str(domain).strip()
    if not term:
        raise ValueError("domain cannot be empty.")
    info = _pfam_domains(gene)
    domains = info["domains"]
    if not domains:
        raise ValueError(f"Genome Nexus lists no Pfam domains for {gene}.")
    lowered = term.lower()
    matches = [d for d in domains if d["pfam_accession"].lower() == lowered]
    if not matches:
        matches = [d for d in domains if d["name"].lower() == lowered]
    if not matches:
        tokens = [t for t in re.split(r"[^a-z0-9]+", lowered) if t and t not in _DOMAIN_STOPWORDS]
        if tokens:
            matches = [
                d
                for d in domains
                if all(t in f"{d['name']} {d['description']}".lower() for t in tokens)
            ]
    available = "; ".join(
        f"{d['pfam_accession']} {d['name']} ({d['description']}) codons {d['start']}-{d['end']}"
        for d in domains
    )
    accessions = sorted({d["pfam_accession"] for d in matches})
    if not accessions:
        raise ValueError(f"No Pfam domain of {gene} matches '{term}'. {gene} domains: {available}.")
    if len(accessions) > 1:
        raise ValueError(
            f"'{term}' matches several {gene} Pfam domains; pass one accession. "
            f"{gene} domains: {available}."
        )
    chosen = [d for d in domains if d["pfam_accession"] == accessions[0]]
    ranges = [[d["start"], d["end"]] for d in chosen]
    return {
        "source": "pfam",
        "pfam_accession": accessions[0],
        "name": chosen[0]["name"],
        "description": chosen[0]["description"],
        "label": f"{chosen[0]['name']} ({accessions[0]}), codons "
        + ", ".join(f"{s}-{e}" for s, e in ranges),
        "ranges": ranges,
        "transcript_id": info["transcript_id"],
        "protein_length": info["protein_length"],
        "requested": term,
    }


def _build_lollipop_payload(
    study_id,
    gene: str,
    cohort: dict[str, list[str]] | None = None,
    domain: str | None = None,
    protein_range: list[int] | None = None,
) -> dict:
    """Assemble the mutation-lollipop data contract consumed by the widget."""
    scope = _scope_of(study_id)
    warnings: list[str] = []
    region = _resolve_protein_region(gene, domain, protein_range)
    cohort_samples = _cohort_sample_ids(scope, cohort)
    payload: dict = {
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "gene": gene,
        "mutations": [],
        "class_counts": {},
        "n_samples_mutated": 0,
        "protein_change_count": 0,
        "unmapped_count": 0,
        "max_position": None,
        "region": None,
        "warnings": warnings,
        "notes": (
            "Mutation lollipop for a single gene. Each lollipop is a distinct "
            "protein change; its height/head is the number of samples carrying it "
            "(recurrence) and its color is the mutation class. Positions are "
            "parsed from the protein-change notation (mutation_variant). The "
            "protein backbone length and Pfam domains are fetched live from Genome "
            "Nexus by the widget; if unavailable, the axis is scaled to the highest "
            "observed position and no domains are drawn. Counts are per sample "
            "(somatic + germline, excluding UNCALLED). The 'cohort' block states "
            "which samples these counts describe."
        ),
    }

    if cohort is not None and not cohort_samples:
        payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
        payload["error"] = _cohort_empty_error(scope, cohort, "samples")
        return payload

    rows = _fetch_lollipop_mutations(scope, gene)
    if cohort_samples is not None:
        rows = [r for r in rows if r.get("sample_unique_id") in cohort_samples]
    if not rows:
        payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
        unknown = None if cohort_samples is not None else _unknown_study_error(scope)
        where = f"study '{scope.label}'" if scope.single else scope.label
        payload["error"] = unknown or (
            f"No mutations found for {gene} in {where}"
            + (" within the requested cohort. " if cohort_samples is not None else ". ")
            + "The gene may have no mutation events, or the study may have no mutation data."
        )
        return payload

    if region is not None:
        mutated_anywhere = {r["sample_unique_id"] for r in rows if r.get("sample_unique_id")}
        unplaceable: set[str] = set()
        kept = []
        for r in rows:
            start, end = aq.protein_range(r.get("mutation_variant"))
            if start is None or end is None:
                unplaceable.add(r.get("sample_unique_id"))
                continue
            if any(start <= hi and end >= lo for lo, hi in region["ranges"]):
                kept.append(r)
        in_region = {r["sample_unique_id"] for r in kept if r.get("sample_unique_id")}
        region.update(
            {
                "match": "a protein change counts when its codon span overlaps the region",
                "n_samples_in_region": len(in_region),
                "n_samples_mutated_anywhere": len(mutated_anywhere),
                "pct_of_mutated_samples": (
                    round(100.0 * len(in_region) / len(mutated_anywhere), 1)
                    if mutated_anywhere
                    else None
                ),
                "n_protein_changes_in_region": len({r.get("mutation_variant") for r in kept}),
                "n_samples_unplaceable": len(unplaceable - in_region),
            }
        )
        payload["region"] = region
        payload["notes"] += (
            f" Filtered to {region['label']}: 'mutations' and the counts describe only protein "
            "changes overlapping the region; region.n_samples_mutated_anywhere is the gene-wide "
            "total for context. Positions are on the canonical transcript cBioPortal annotates "
            "against."
        )
        if unplaceable - in_region:
            warnings.append(
                f"{len(unplaceable - in_region)} mutated sample(s) have changes without a "
                "codon position (e.g. splice or 'NA') and cannot be placed in or out of the region."
            )
        rows = kept
        if not rows:
            payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
            payload["error"] = (
                f"No {gene} mutations fall in {region['label']} "
                f"({len(mutated_anywhere)} sample(s) carry {gene} mutations elsewhere)."
            )
            return payload

    # Aggregate per distinct protein change: the set of samples carrying it (for
    # recurrence) and the most-severe mutation class + the MAF types seen for it.
    by_change: dict[str, dict] = {}
    all_samples: set[str] = set()
    unmapped_samples: set[str] = set()
    by_study: dict[str, set[str]] = {}
    for row in rows:
        sid = row.get("sample_unique_id")
        if not sid:
            continue
        all_samples.add(sid)
        variant = row.get("mutation_variant")
        pos = _parse_protein_position(variant)
        if variant is None or str(variant).strip().upper() == "NA" or pos is None:
            unmapped_samples.add(sid)
            continue
        cls = _mutation_class(row.get("mutation_type"))
        rec = by_change.get(variant)
        if rec is None:
            rec = {
                "protein_change": _clean_protein_change(variant),
                "position": pos,
                "samples": set(),
                "class": cls,
                "types": set(),
            }
            by_change[variant] = rec
        rec["samples"].add(sid)
        rec["class"] = _more_severe_mut(rec["class"], cls)
        mtype = row.get("mutation_type")
        if mtype:
            rec["types"].add(str(mtype))

    mutations = [
        {
            "protein_change": rec["protein_change"],
            "position": rec["position"],
            "count": len(rec["samples"]),
            "class": rec["class"],
            "types": sorted(rec["types"]),
        }
        for rec in by_change.values()
    ]
    # Sort by recurrence (desc) then position so truncation keeps the hotspots;
    # the widget re-sorts by position for drawing.
    mutations.sort(key=lambda m: (-m["count"], m["position"]))
    n_distinct = len(mutations)
    if n_distinct > MAX_LOLLIPOP_MUTATIONS:
        mutations = mutations[:MAX_LOLLIPOP_MUTATIONS]
        warnings.append(
            f"{n_distinct} distinct protein changes found; showing the "
            f"{MAX_LOLLIPOP_MUTATIONS} most recurrent."
        )

    class_counts: dict[str, int] = {}
    max_pos = 0
    for m in mutations:
        class_counts[m["class"]] = class_counts.get(m["class"], 0) + 1
        max_pos = max(max_pos, m["position"])

    payload["mutations"] = mutations
    payload["class_counts"] = class_counts
    payload["protein_change_count"] = n_distinct
    payload["n_samples_mutated"] = len(all_samples)
    payload["max_position"] = max_pos or None
    payload["unmapped_count"] = len(unmapped_samples)
    if not scope.single:
        for row in rows:
            sid = row.get("sample_unique_id")
            if sid:
                by_study.setdefault(row.get("cancer_study_identifier") or "unknown", set()).add(sid)
        payload["by_study"] = [
            {"study_id": k, "n_samples_mutated": len(v)}
            for k, v in sorted(by_study.items(), key=lambda kv: -len(kv[1]))[:15]
        ]
    # The lollipop has no profiled denominator (it plots counts, not rates), so
    # the sample universe it ran over is exactly the mutated samples it counted.
    payload["cohort"] = _cohort_block(scope, cohort, len(all_samples), None, warnings)
    if not scope.single:
        overlap = _scope_overlap_warning(scope)
        if overlap:
            warnings.append(overlap)
    if not mutations:
        payload["error"] = (
            f"{gene} has mutation events in {scope.label}, but none carry a "
            "plottable protein-change position."
        )
        return payload
    if unmapped_samples:
        warnings.append(
            f"{len(unmapped_samples)} sample(s) have mutations without a plottable "
            "protein position (e.g. splice sites or 'NA'); they are counted but not "
            "shown as lollipops."
        )
    return payload


# --- Alteration co-occurrence app helpers ------------------------------------

# Gene-count bounds for the pairwise co-occurrence analysis. The heatmap shows
# every gene pair, so the pair count grows as G*(G-1)/2; cap the genes to keep
# the matrix (and the all-pairs Fisher computation) bounded and readable.
MAX_COOCCURRENCE_GENES = 12
DEFAULT_COOCCURRENCE_GENES = 8

# A pair is flagged significant when its Benjamini-Hochberg q-value is below this.
COOCCURRENCE_Q_SIGNIFICANT = 0.05

# Alteration types considered "altered" for a gene (same set as the OncoPrint).
COOCCURRENCE_ALTERATION_TYPES = ONCOPRINT_ALTERATION_TYPES


def _resolve_cooccurrence_genes(study_id, genes: list[str] | None) -> list[str]:
    """Return the gene list for the co-occurrence analysis.

    If ``genes`` is given, validate and de-duplicate (preserving order), clamped
    to ``MAX_COOCCURRENCE_GENES``. Otherwise default to the study's most-altered
    genes across all alteration types.
    """
    if genes:
        resolved: list[str] = []
        seen: set[str] = set()
        for g in genes:
            gene = _validate_gene_symbol(g)
            if gene not in seen:
                seen.add(gene)
                resolved.append(gene)
        return resolved[:MAX_COOCCURRENCE_GENES]

    combined = _oncoprint_event_filter(list(COOCCURRENCE_ALTERATION_TYPES))
    rows = _query(f"""
        SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS altered_samples
        FROM genomic_event_derived
        WHERE {_scope_sql(study_id)}
            AND ({combined})
        GROUP BY hugo_gene_symbol
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
        LIMIT {DEFAULT_COOCCURRENCE_GENES}
    """)
    return [r["hugo_gene_symbol"] for r in rows if r.get("hugo_gene_symbol")]


def _sample_strata(scope, attribute: str) -> dict[str, str]:
    """Sample -> stratum value for co-occurrence stratification.

    ``STUDY`` stratifies by study. A clinical attribute is read at sample grain;
    patient-level attributes fan out to every sample of the patient.
    """
    if attribute == STRATIFY_BY_STUDY:
        rows = _query(f"""
            SELECT sample_unique_id, cancer_study_identifier
            FROM sample_derived
            WHERE {_scope_sql(scope)}
        """)
        return {
            r["sample_unique_id"]: r["cancer_study_identifier"]
            for r in rows
            if r.get("sample_unique_id") and r.get("cancer_study_identifier")
        }
    rows = _query(f"""
        SELECT sample_unique_id, patient_unique_id, attribute_value, type
        FROM clinical_data_derived
        WHERE {_scope_sql(scope)}
            AND attribute_name = '{_validate_attribute_name(attribute)}'
    """)
    strata: dict[str, str] = {}
    by_patient: dict[str, str] = {}
    for r in rows:
        value = r.get("attribute_value")
        if value in (None, ""):
            continue
        if r.get("type") == "patient":
            if r.get("patient_unique_id"):
                by_patient.setdefault(r["patient_unique_id"], str(value))
        elif r.get("sample_unique_id"):
            strata[r["sample_unique_id"]] = str(value)
    if by_patient:
        samples = _query(f"""
            SELECT sample_unique_id, patient_unique_id
            FROM sample_derived
            WHERE {_scope_sql(scope)}
        """)
        for r in samples:
            sid, pid = r.get("sample_unique_id"), r.get("patient_unique_id")
            if sid and pid in by_patient:
                strata.setdefault(sid, by_patient[pid])
    return strata


def _cooccurrence_pair(
    gene_a: str,
    gene_b: str,
    altered: dict[str, set[str]],
    profiled: dict[str, set[str]],
    strata: dict[str, str] | None = None,
) -> dict | None:
    """Build the 2x2 contingency result for one gene pair, or None.

    The sample universe is the set profiled for **both** genes (the correct
    pairwise denominator). Returns ``None`` when that universe is empty (the two
    genes share no profiled samples, e.g. disjoint panels), so the caller can
    skip and warn.

    With ``strata`` (sample -> stratum) the universe is also limited to samples
    with a stratum value, one 2x2 table is built per stratum, and the headline
    statistics are the stratified ones (exact conditional or CMH test, Mantel-
    Haenszel odds ratio); the unadjusted Fisher result is kept under ``crude``.
    """
    universe = profiled[gene_a] & profiled[gene_b]
    if strata is not None:
        universe = {sid for sid in universe if sid in strata}
    if not universe:
        return None
    alt_a = altered[gene_a] & universe
    alt_b = altered[gene_b] & universe
    both = alt_a & alt_b
    a = len(both)
    b = len(alt_a) - a
    c = len(alt_b) - a
    d = len(universe) - len(alt_a | alt_b)

    p_value = fisher_exact_two_sided(a, b, c, d)
    expected_both = (a + b) * (a + c) / len(universe)
    tendency = "Co-occurrence" if a > expected_both else "Mutual exclusivity"
    pair = {
        "gene_a": gene_a,
        "gene_b": gene_b,
        "n_both": a,
        "n_a_only": b,
        "n_b_only": c,
        "n_neither": d,
        "n_profiled": len(universe),
        "log2_odds_ratio": round(log2_odds_ratio(a, b, c, d), 3),
        "p_value": p_value,
        "tendency": tendency,
    }
    if strata is None:
        return pair
    tables: dict[str, list[int]] = {}
    for sid in universe:
        t = tables.setdefault(strata[sid], [0, 0, 0, 0])
        in_a, in_b = sid in alt_a, sid in alt_b
        t[0 if in_a and in_b else 1 if in_a else 2 if in_b else 3] += 1
    adjusted = stratified_association_test([tuple(t) for t in tables.values()])
    pair["crude"] = {
        "log2_odds_ratio": pair["log2_odds_ratio"],
        "p_value": p_value,
        "tendency": tendency,
    }
    log2_or = adjusted["log2_mh_odds_ratio"]
    pair["log2_odds_ratio"] = round(log2_or, 3)
    pair["p_value"] = adjusted["p_value"]
    pair["tendency"] = "Co-occurrence" if log2_or > 0 else "Mutual exclusivity"
    pair["test"] = adjusted["method"] or "no informative stratum"
    pair["n_strata_informative"] = adjusted["n_informative_strata"]
    return pair


def _build_cooccurrence_payload(
    study_id,
    genes: list[str] | None,
    alteration_types: list[str] | None,
    cohort: dict[str, list[str]] | None = None,
    tracks: list[str] | str | None = None,
    stratify_by: str | None = None,
) -> dict:
    """Assemble the co-occurrence data contract consumed by the heatmap widget.

    For every gene (or OQL track) pair, builds a 2x2 alteration contingency table
    over the samples profiled for both, runs a two-sided Fisher exact test (or,
    with ``stratify_by``, a test stratified by that attribute), adds a log2 odds
    ratio and tendency, then Benjamini-Hochberg-corrects the p-values to q-values
    across all pairs.
    """
    scope = _scope_of(study_id)
    warnings: list[str] = []
    parsed = None
    if tracks is not None:
        if genes or alteration_types:
            raise ValueError(
                "Pass either genes/alteration_types or tracks, not both: each OQL track "
                "already names its genes and alterations."
            )
        parsed = aq.parse_track_list(tracks)
        if len(parsed) > MAX_COOCCURRENCE_GENES:
            raise ValueError(
                f"{len(parsed)} tracks given; co-occurrence tests at most "
                f"{MAX_COOCCURRENCE_GENES} (merge genes into pathway tracks with [...])."
            )
    if stratify_by is not None:
        stratify_by = str(stratify_by).strip()
        stratify_by = (
            STRATIFY_BY_STUDY
            if stratify_by.upper() == STRATIFY_BY_STUDY
            else _validate_attribute_name(stratify_by)
        )
    cohort_samples = _cohort_sample_ids(scope, cohort)
    requested_types = alteration_types or list(COOCCURRENCE_ALTERATION_TYPES)
    resolved_types: list[str] = []
    seen_types: set[str] = set()
    for alt in requested_types:
        _validate_alteration_type(alt)  # raises ValueError on unknown types
        if alt not in seen_types:
            seen_types.add(alt)
            resolved_types.append(alt)

    payload: dict = {
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "alteration_types": resolved_types,
        "genes": [],
        "gene_stats": [],
        "pairs": [],
        "n_significant": 0,
        "q_threshold": COOCCURRENCE_Q_SIGNIFICANT,
        "stratification": None,
        "warnings": warnings,
        "notes": (
            "Pairwise alteration co-occurrence / mutual exclusivity. For each gene "
            "pair a 2x2 table (both / either-only / neither altered) is built over "
            "the samples profiled for both genes, scored with a two-sided Fisher "
            "exact test and a log2 odds ratio (positive = tend to co-occur, "
            "negative = mutually exclusive), and the p-values are Benjamini-Hochberg "
            "corrected to q-values. Alterations are per sample; profiling uses "
            "mutation (MUTATION_EXTENDED) coverage, so copy-number/structural events "
            "on samples without mutation profiling are not counted. The 'cohort' "
            "block states which samples these tests describe."
        ),
    }

    if cohort is not None and not cohort_samples:
        payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
        payload["error"] = _cohort_empty_error(scope, cohort, "samples")
        return payload

    if parsed is not None:
        _check_driver_annotations(scope, parsed)
        resolved_genes = [t.label for t in parsed]
        payload["genes"] = resolved_genes
        if len(parsed) < 2:
            payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
            payload["error"] = "Co-occurrence analysis needs at least two tracks."
            return payload
        rows = _fetch_track_events(scope, parsed)
        altered = {
            t.label: {
                r["sample_unique_id"]
                for r in rows
                if r.get("sample_unique_id") and aq.track_wants_event(t, r)
            }
            for t in parsed
        }
        by_gene = _fetch_profiled_samples(scope, sorted({g for t in parsed for g in t.genes}))
        # A pathway track is tested only on samples profiled for every one of its
        # genes: counting a sample whose panel lacks some member genes as
        # "unaltered in the pathway" would bias the table toward exclusivity.
        profiled = {
            t.label: set.intersection(*(by_gene[g] for g in t.genes)) for t in parsed
        }
        payload["alteration_types"] = [
            name
            for keyword, name in OQL_ALTERATION_NAMES.items()
            if any(
                c.alteration == keyword
                for t in parsed
                for ln in t.lines
                for c in ln.effective_commands
            )
        ]
        payload["query"] = aq.format_query(parsed)
        payload["tracks"] = [aq.describe_track(t) for t in parsed]
        payload["notes"] += (
            " Rows are the OQL tracks in 'query'; a merged track ([...]) is altered in a "
            "sample when any of its genes is, and is tested only on samples profiled for "
            "all of its genes."
        )
    else:
        resolved_genes = _resolve_cooccurrence_genes(scope, genes)
        payload["genes"] = resolved_genes
        if len(resolved_genes) < 2:
            payload["cohort"] = _cohort_block(scope, cohort, 0, None, warnings)
            payload["error"] = _unknown_study_error(scope) or (
                "Co-occurrence analysis needs at least two genes. "
                f"Found {len(resolved_genes)} for study '{scope.label}'. Provide 2+ valid "
                "gene symbols, or pick a study with alteration data."
            )
            return payload
        if cohort_samples is not None and not genes:
            warnings.append(
                "Genes were auto-selected by study-wide alteration frequency, not by "
                "frequency within the filtered cohort."
            )
        if not genes:
            warnings.append(
                f"No genes were given, so the {len(resolved_genes)} most frequently altered "
                "genes were tested. This is not a genome-wide search; for genes enriched "
                "or depleted relative to one alteration across all genes use "
                "alteration_enrichment."
            )
        cells, _ = _fetch_oncoprint_events(scope, resolved_genes, resolved_types)
        altered = {g: set(cells.get(g, {}).keys()) for g in resolved_genes}
        profiled = _fetch_profiled_samples(scope, resolved_genes)

    # Restrict alterations AND profiling to the cohort before any pair is scored.
    # _cooccurrence_pair derives its universe from profiled[a] & profiled[b], so
    # narrowing profiled here is what keeps each 2x2 table's denominator honest;
    # filtering only `altered` would inflate every odds ratio.
    if cohort_samples is not None:
        altered = {g: s & cohort_samples for g, s in altered.items()}
        profiled = {g: s & cohort_samples for g, s in profiled.items()}

    payload["cohort"] = _cohort_block(
        scope,
        cohort,
        n_samples=len(set().union(*profiled.values())) if profiled else 0,
        n_patients=None,
        warnings=warnings,
    )
    if not scope.single:
        overlap = _scope_overlap_warning(scope)
        if overlap:
            warnings.append(overlap)

    strata = None
    if stratify_by:
        strata = _sample_strata(scope, stratify_by)
        analysed = set().union(*profiled.values()) if profiled else set()
        without = len([sid for sid in analysed if sid not in strata])
        if analysed and without == len(analysed):
            payload["error"] = (
                f"stratify_by='{stratify_by}' has no value for any analysed sample in "
                f"{scope.label}; check the attribute name."
            )
            return payload
        sizes: dict[str, int] = {}
        for sid in analysed:
            if sid in strata:
                sizes[strata[sid]] = sizes.get(strata[sid], 0) + 1
        payload["stratification"] = {
            "by": stratify_by,
            "method": (
                "per pair, one 2x2 table per stratum; exact conditional test of independence "
                "(Fisher's test generalized to strata) when tables are sparse by the "
                "Mantel-Fleiss criterion, else the Cochran-Mantel-Haenszel chi-square; odds "
                "ratio = Mantel-Haenszel common odds ratio"
            ),
            "n_strata": len(sizes),
            "n_samples_without_value": without,
            "strata": [
                {"name": k, "n_samples": v}
                for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])[:60]
            ],
        }
        if without:
            warnings.append(
                f"{without} sample(s) without a {stratify_by} value were left out of the "
                "stratified tests."
            )
        payload["notes"] += (
            f" Stratified by {stratify_by}: p_value, q_value, log2_odds_ratio and tendency "
            "are adjusted for it (see 'stratification'); the unadjusted Fisher result of "
            "each pair is under 'crude'."
        )

    payload["gene_stats"] = [
        {
            "gene": g,
            "altered": len(altered[g] & profiled[g]),
            "profiled": len(profiled[g]),
            "freq_pct": (
                round(len(altered[g] & profiled[g]) * 100.0 / len(profiled[g]), 1)
                if profiled[g]
                else None
            ),
        }
        for g in resolved_genes
    ]

    pairs: list[dict] = []
    skipped = 0
    for i in range(len(resolved_genes)):
        for j in range(i + 1, len(resolved_genes)):
            pair = _cooccurrence_pair(
                resolved_genes[i], resolved_genes[j], altered, profiled, strata
            )
            if pair is None:
                skipped += 1
                continue
            pairs.append(pair)

    if not pairs:
        # Rows came back, so the study exists; only an empty result can mean an
        # unknown study id.
        unknown = None if any(altered.values()) else _unknown_study_error(scope)
        payload["error"] = unknown or (
            f"No gene pairs could be evaluated for study '{scope.label}': the selected "
            "genes share no profiled samples."
        )
        return payload

    qvalues = benjamini_hochberg([p["p_value"] for p in pairs])
    if strata is not None:
        crude_q = benjamini_hochberg([p["crude"]["p_value"] for p in pairs])
        for pair, q in zip(pairs, crude_q, strict=True):
            pair["crude"]["q_value"] = q
    n_significant = 0
    for pair, q in zip(pairs, qvalues, strict=True):
        pair["q_value"] = q
        pair["significant"] = q < COOCCURRENCE_Q_SIGNIFICANT
        if pair["significant"]:
            n_significant += 1

    # Most significant first; the widget arranges the matrix by gene order.
    pairs.sort(key=lambda p: (p["p_value"], -abs(p["log2_odds_ratio"])))
    payload["pairs"] = pairs
    payload["n_significant"] = n_significant

    if skipped:
        warnings.append(
            f"{skipped} gene pair(s) skipped because the two genes share no profiled samples."
        )
    if parsed is None and any(t != "mutation" for t in resolved_types):
        warnings.append(
            "Copy-number/structural alterations are included, but profiling counts use "
            "mutation coverage; pairs involving genes without copy-number/SV profiling may "
            "be approximate."
        )
    if strata is None:
        n_types = payload["cohort"].get("n_cancer_types")
        if n_types is None:
            sample_types = _sample_strata(scope, COHORT_SPREAD_ATTRIBUTE)
            analysed = set().union(*profiled.values()) if profiled else set()
            n_types = len({sample_types[sid] for sid in analysed if sid in sample_types})
        if n_types > 1 or not scope.single:
            where = (
                f"{n_types} cancer types" if n_types > 1 else f"{len(scope.study_ids)} studies"
            )
            warnings.append(
                f"These tests pool samples from {where} and are NOT adjusted for tumour type: "
                "genes that are each common in the same cancer type look co-occurring, and "
                "genes common in different cancer types look mutually exclusive, without "
                "any interaction. Re-run with stratify_by='CANCER_TYPE' (or 'STUDY') for "
                "tumour-type-adjusted p-values before interpreting them."
            )
    return payload


# --- Genome-wide alteration enrichment between two sample groups ---------------
#
# "Genes mutated in TP53 wild-type but rarely in TP53-mutant tumours" is a scan over
# every gene, not a gene list, so it could not be answered by the co-occurrence app
# (which tests at most 12 named genes) and a model would otherwise produce a "hit
# list" with no scan behind it. This is cBioPortal's group-comparison enrichment:
# one 2x2 table per gene with panel-aware profiled denominators, Fisher's exact test
# (or the stratified test), Benjamini-Hochberg across every tested gene. All
# counting happens in ClickHouse; only per-gene (per-stratum) counts come back.

ENRICHMENT_KIND = "alteration_enrichment"
ENRICHMENT_DEFAULT_MIN_ALTERED = 5
ENRICHMENT_MAX_RESULTS = 200
ENRICHMENT_Q_SIGNIFICANT = 0.05
# Mean altered genes per sample differing by more than this factor between the
# groups triggers the mutation-burden warning.
ENRICHMENT_BURDEN_RATIO_WARN = 1.2


def _profiled_samples_sql(scope, genes: list[str], profiling_type: str) -> str:
    """Samples profiled for EVERY gene in ``genes`` (panel membership or WES)."""
    genes_sql = _sql_string_list(genes)
    if profiling_type == "MUTATION_EXTENDED":
        panel = f"""SELECT sample_unique_id
                FROM mutation_panel_gene_coverage
                WHERE {_scope_sql(scope)} AND hugo_gene_symbol IN ({genes_sql})
                GROUP BY sample_unique_id
                HAVING uniqExact(hugo_gene_symbol) = {len(genes)}"""
        wes = f"""SELECT sample_unique_id FROM mutation_wes_coverage WHERE {_scope_sql(scope)}"""
    else:
        panel = f"""SELECT stgp.sample_unique_id AS sample_unique_id
                FROM sample_to_gene_panel_derived stgp
                JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
                JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
                JOIN gene g ON gpl.gene_id = g.entrez_gene_id
                WHERE {_scope_sql(scope, "stgp.cancer_study_identifier")}
                    AND stgp.alteration_type = '{profiling_type}'
                    AND g.hugo_gene_symbol IN ({genes_sql})
                GROUP BY sample_unique_id
                HAVING uniqExact(g.hugo_gene_symbol) = {len(genes)}"""
        wes = f"""SELECT sample_unique_id FROM sample_to_gene_panel_derived
                WHERE {_scope_sql(scope)} AND alteration_type = '{profiling_type}'
                    AND gene_panel_id = 'WES'"""
    return f"SELECT sample_unique_id FROM ({panel} UNION ALL {wes})"


def _clinical_samples_sql(scope, attribute: str, values: list[str] | None, negate=False) -> str:
    """Samples whose attribute (sample- or patient-level) is in / not in ``values``."""
    cond = (
        f"upper(attribute_value) {'NOT ' if negate else ''}IN ({_cohort_value_sql(values)})"
        if values is not None
        else "attribute_value != ''"
    )
    patient_cond = cond.replace("attribute_value", "c.attribute_value")
    return f"""SELECT sample_unique_id FROM clinical_data_derived
            WHERE {_scope_sql(scope)} AND attribute_name = '{attribute}'
                AND sample_unique_id != '' AND attribute_value != '' AND {cond}
            UNION ALL
            SELECT s.sample_unique_id AS sample_unique_id
            FROM sample_derived s
            JOIN clinical_data_derived c ON c.patient_unique_id = s.patient_unique_id
            WHERE {_scope_sql(scope, "s.cancer_study_identifier")}
                AND {_scope_sql(scope, "c.cancer_study_identifier")}
                AND c.attribute_name = '{attribute}' AND c.sample_unique_id = ''
                AND c.attribute_value != '' AND {patient_cond}"""


def _parse_enrichment_group(spec, label: str) -> dict:
    """An OQL group (str / list of str, ANDed) or a clinical group ({attribute: values})."""
    if isinstance(spec, dict):
        attribute, values = _parse_cohort(spec)
        return {"kind": "clinical", "attribute": attribute, "values": values}
    if isinstance(spec, str) or (
        isinstance(spec, (list, tuple)) and spec and all(isinstance(x, str) for x in spec)
    ):
        tracks = _as_track_list(spec, label)
        return {"kind": "oql", "tracks": tracks}
    raise ValueError(
        f"{label} must be an OQL alteration (e.g. \"TP53: MUT\"), a list of OQL strings "
        'that must all hold, or a clinical attribute filter such as {"SAMPLE_TYPE": '
        '["Metastasis"]}.'
    )


def _group_condition_sql(scope, group: dict) -> tuple[str, str, list[str]]:
    """(membership SQL condition, human definition, genes it names)."""
    if group["kind"] == "clinical":
        members = _clinical_samples_sql(scope, group["attribute"], group["values"])
        cond = f"sample_unique_id IN ({members})"
        return cond, f"{group['attribute']} in {group['values']}", []
    parts = []
    genes: list[str] = []
    for track in group["tracks"]:
        parts.append(
            "sample_unique_id IN (SELECT sample_unique_id FROM genomic_event_derived "
            f"WHERE {_scope_sql(scope)} AND {aq.track_sql_predicate(track)})"
        )
        genes.extend(g for g in track.genes if g not in genes)
    return " AND ".join(parts), " AND ".join(t.text for t in group["tracks"]), genes


def _group_profiled_sql(scope, group: dict) -> str:
    """Samples profiled for every gene and alteration kind an OQL group tests."""
    parts = []
    for track in group["tracks"]:
        for ptype in sorted(aq.track_profiling_types(track)):
            parts.append(
                f"sample_unique_id IN ({_profiled_samples_sql(scope, list(track.genes), ptype)})"
            )
    return " AND ".join(parts)


def _build_enrichment_payload(
    scope,
    group_a,
    group_b,
    alteration: str,
    stratify_by: str | None,
    cohort: dict | None,
    min_altered: int,
    max_results: int,
    direction: str = "any",
    genes: list[str] | None = None,
) -> dict:
    scope = _scope_of(scope)
    config = _validate_alteration_type(alteration)
    only_genes: list[str] = []
    if genes:
        if isinstance(genes, str):
            genes = [genes]
        only_genes = list(dict.fromkeys(_validate_gene_symbol(str(g)) for g in genes))
    direction = str(direction or "any").strip().upper()
    if direction not in ("ANY", "A", "B"):
        raise ValueError("direction must be 'any', 'A' or 'B'.")
    try:
        min_altered = int(min_altered)
        max_results = int(max_results)
    except (TypeError, ValueError) as e:
        raise ValueError("min_altered and max_results must be integers.") from e
    if min_altered < 1:
        raise ValueError("min_altered must be at least 1.")
    max_results = max(1, min(max_results, ENRICHMENT_MAX_RESULTS))
    a = _parse_enrichment_group(group_a, "group_a")
    b = _parse_enrichment_group(group_b, "group_b") if group_b is not None else None
    if stratify_by is not None:
        stratify_by = str(stratify_by).strip()
        stratify_by = (
            STRATIFY_BY_STUDY
            if stratify_by.upper() == STRATIFY_BY_STUDY
            else _validate_attribute_name(stratify_by)
        )
    for group in (a, b):
        if group and group["kind"] == "oql":
            _check_driver_annotations(scope, group["tracks"])

    a_cond, a_def, a_genes = _group_condition_sql(scope, a)
    if b is not None:
        b_cond, b_def, b_genes = _group_condition_sql(scope, b)
        if b["kind"] == "oql":
            b_cond = f"{b_cond} AND {_group_profiled_sql(scope, b)}"
        a_members = f"({a_cond}) AND NOT ({b_cond})"
        b_members = f"({b_cond}) AND NOT ({a_cond})"
        overlap_note = "Samples meeting both definitions are excluded from both groups."
    elif a["kind"] == "oql":
        profiled = _group_profiled_sql(scope, a)
        a_members = f"({a_cond}) AND {profiled}"
        b_members = f"NOT ({a_cond}) AND {profiled}"
        b_def = f"profiled for {', '.join(a_genes)} and NOT ({a_def})"
        b_genes = []
        overlap_note = ""
    else:
        a_members = a_cond
        b_members = (
            "sample_unique_id IN ("
            + _clinical_samples_sql(scope, a["attribute"], a["values"], negate=True)
            + ")"
        )
        b_def = f"{a['attribute']} not in {a['values']}"
        b_genes = []
        overlap_note = ""
    if cohort is not None:
        attribute, values = _parse_cohort(cohort)
        cohort_cond = f"sample_unique_id IN ({_clinical_samples_sql(scope, attribute, values)})"
        a_members = f"{a_members} AND {cohort_cond}"
        b_members = f"{b_members} AND {cohort_cond}"

    groups_cte = f"""groups AS (
            SELECT sample_unique_id, 'A' AS grp FROM sample_derived
            WHERE {_scope_sql(scope)} AND {a_members}
            UNION ALL
            SELECT sample_unique_id, 'B' AS grp FROM sample_derived
            WHERE {_scope_sql(scope)} AND {b_members}
        )"""
    if stratify_by == STRATIFY_BY_STUDY:
        strata_cte = f""",
        strata AS (
            SELECT sample_unique_id, cancer_study_identifier AS stratum
            FROM sample_derived WHERE {_scope_sql(scope)}
        )"""
    elif stratify_by:
        strata_cte = f""",
        strata AS (
            SELECT sample_unique_id, any(stratum) AS stratum FROM (
                SELECT sample_unique_id, attribute_value AS stratum FROM clinical_data_derived
                WHERE {_scope_sql(scope)} AND attribute_name = '{stratify_by}'
                    AND sample_unique_id != '' AND attribute_value != ''
                UNION ALL
                SELECT s.sample_unique_id AS sample_unique_id, c.attribute_value AS stratum
                FROM clinical_data_derived c
                JOIN sample_derived s ON c.patient_unique_id = s.patient_unique_id
                WHERE {_scope_sql(scope, "c.cancer_study_identifier")}
                    AND {_scope_sql(scope, "s.cancer_study_identifier")}
                    AND c.attribute_name = '{stratify_by}' AND c.sample_unique_id = ''
                    AND c.attribute_value != ''
            )
            GROUP BY sample_unique_id
        )"""
    else:
        strata_cte = ""
    strata_join = (
        "JOIN strata st ON st.sample_unique_id = g.sample_unique_id" if stratify_by else ""
    )
    stratum_col = "st.stratum" if stratify_by else "''"

    sizes = _query(f"""
        WITH {groups_cte}{strata_cte}
        SELECT g.grp AS grp, {stratum_col} AS stratum, uniqExact(g.sample_unique_id) AS n
        FROM groups g
        {strata_join}
        GROUP BY grp, stratum
    """)
    n_group: dict[str, dict[str, int]] = {"A": {}, "B": {}}
    for row in sizes:
        if row.get("grp") in n_group:
            n_group[row["grp"]][str(row.get("stratum", ""))] = _as_int(row.get("n"))
    n_a, n_b = sum(n_group["A"].values()), sum(n_group["B"].values())

    payload: dict = {
        "kind": ENRICHMENT_KIND,
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "alteration": alteration,
        "groups": {
            "A": {"definition": a_def, "n_samples": n_a},
            "B": {"definition": b_def, "n_samples": n_b},
        },
        "min_altered": min_altered,
        "genes": [],
        "n_genes_tested": 0,
        "n_significant": 0,
        "q_threshold": ENRICHMENT_Q_SIGNIFICANT,
        "stratification": None,
        "warnings": [],
        "notes": (
            f"Enrichment of {alteration} per gene between group A and group B. Each gene gets "
            "a 2x2 table over the samples of each group profiled for that gene (gene-panel "
            "aware, WES counts as profiled for every gene), a two-sided Fisher exact test and "
            "a log2 odds ratio (positive = more frequent in A); p-values are Benjamini-"
            f"Hochberg corrected across all {'{n}'} genes altered in at least {min_altered} "
            "samples. An enrichment is an association between alteration frequencies: it does "
            "not by itself show synthetic lethality, dependency or causation."
        ),
    }
    warnings: list[str] = payload["warnings"]
    if overlap_note:
        payload["notes"] += " " + overlap_note
    if not n_a or not n_b:
        payload["error"] = _unknown_study_error(scope) or (
            f"Group A has {n_a} and group B has {n_b} samples in {scope.label}; both need "
            "samples. Check the definitions (and that the genes are profiled in these studies)."
        )
        return payload

    alt_filter = config["event_filter"]
    if only_genes:
        alt_filter += f" AND e.hugo_gene_symbol IN ({_sql_string_list(only_genes)})"
    altered_rows = _query(f"""
        WITH {groups_cte}{strata_cte}
        SELECT e.hugo_gene_symbol AS gene, {stratum_col} AS stratum,
               uniqExactIf(e.sample_unique_id, g.grp = 'A') AS a,
               uniqExactIf(e.sample_unique_id, g.grp = 'B') AS c
        FROM genomic_event_derived e
        JOIN groups g ON e.sample_unique_id = g.sample_unique_id
        {strata_join}
        WHERE {_scope_sql(scope, "e.cancer_study_identifier")}
            AND {alt_filter}
            AND e.off_panel = 0
        GROUP BY gene, stratum
    """)
    ptype = config["profiling_type"]
    if ptype == "MUTATION_EXTENDED":
        wes_from = f"""FROM mutation_wes_coverage w
            JOIN groups g ON w.sample_unique_id = g.sample_unique_id
            {strata_join}
            WHERE {_scope_sql(scope, "w.cancer_study_identifier")}"""
        panel_from = f"""FROM mutation_panel_gene_coverage c
            JOIN groups g ON c.sample_unique_id = g.sample_unique_id
            {strata_join}
            WHERE {_scope_sql(scope, "c.cancer_study_identifier")}"""
        panel_gene = "c.hugo_gene_symbol"
        panel_sample = "c.sample_unique_id"
    else:
        wes_from = f"""FROM sample_to_gene_panel_derived w
            JOIN groups g ON w.sample_unique_id = g.sample_unique_id
            {strata_join}
            WHERE {_scope_sql(scope, "w.cancer_study_identifier")}
                AND w.alteration_type = '{ptype}' AND w.gene_panel_id = 'WES'"""
        panel_from = f"""FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene gn ON gpl.gene_id = gn.entrez_gene_id
            JOIN groups g ON stgp.sample_unique_id = g.sample_unique_id
            {strata_join}
            WHERE {_scope_sql(scope, "stgp.cancer_study_identifier")}
                AND stgp.alteration_type = '{ptype}'"""
        panel_gene = "gn.hugo_gene_symbol"
        panel_sample = "stgp.sample_unique_id"
    profiled_rows = _query(f"""
        WITH {groups_cte}{strata_cte}
        SELECT gene, grp, stratum, n FROM (
            SELECT '*' AS gene, g.grp AS grp, {stratum_col} AS stratum,
                   uniqExact(w.sample_unique_id) AS n
            {wes_from}
            GROUP BY gene, grp, stratum
            UNION ALL
            SELECT {panel_gene} AS gene, g.grp AS grp, {stratum_col} AS stratum,
                   uniqExact({panel_sample}) AS n
            {panel_from}
            GROUP BY gene, grp, stratum
        )
    """)

    wes: dict[str, dict[str, int]] = {"A": {}, "B": {}}
    panel: dict[str, dict[str, dict[str, int]]] = {}
    for row in profiled_rows:
        grp, stratum, n = row.get("grp"), str(row.get("stratum", "")), _as_int(row.get("n"))
        if grp not in ("A", "B"):
            continue
        if row.get("gene") == "*":
            wes[grp][stratum] = wes[grp].get(stratum, 0) + n
        elif row.get("gene"):
            panel.setdefault(row["gene"], {"A": {}, "B": {}})[grp][stratum] = n
    altered: dict[str, dict[str, tuple[int, int]]] = {}
    burden = {"A": 0, "B": 0}
    for row in altered_rows:
        gene = row.get("gene")
        if not gene:
            continue
        ca, cc = _as_int(row.get("a")), _as_int(row.get("c"))
        altered.setdefault(gene, {})[str(row.get("stratum", ""))] = (ca, cc)
        burden["A"] += ca
        burden["B"] += cc

    defining = set(a_genes) | set(b_genes)
    if only_genes:
        # Named genes are always reported, including ones never altered here.
        for gene in only_genes:
            altered.setdefault(gene, {})
    results = []
    for gene, per_stratum in altered.items():
        total = sum(x + y for x, y in per_stratum.values())
        if gene in defining or (not only_genes and total < min_altered):
            continue
        if only_genes and gene not in only_genes:
            continue
        tables = []
        sum_a = sum_pa = sum_c = sum_pb = 0
        gene_panel = panel.get(gene, {"A": {}, "B": {}})
        strata_keys = (
            set(per_stratum)
            | set(wes["A"])
            | set(wes["B"])
            | set(gene_panel["A"])
            | set(gene_panel["B"])
        )
        for stratum in strata_keys:
            ca, cc = per_stratum.get(stratum, (0, 0))
            pa = wes["A"].get(stratum, 0) + gene_panel["A"].get(stratum, 0)
            pb = wes["B"].get(stratum, 0) + gene_panel["B"].get(stratum, 0)
            pa, pb = max(pa, ca), max(pb, cc)
            if pa + pb == 0:
                continue
            tables.append((ca, pa - ca, cc, pb - cc))
            sum_a += ca
            sum_pa += pa
            sum_c += cc
            sum_pb += pb
        if not sum_pa or not sum_pb:
            continue
        crude_p = fisher_exact_two_sided(sum_a, sum_pa - sum_a, sum_c, sum_pb - sum_c)
        crude_or = log2_odds_ratio(sum_a, sum_pa - sum_a, sum_c, sum_pb - sum_c)
        entry = {
            "gene": gene,
            "altered_a": sum_a,
            "profiled_a": sum_pa,
            "pct_a": round(100.0 * sum_a / sum_pa, 2),
            "altered_b": sum_c,
            "profiled_b": sum_pb,
            "pct_b": round(100.0 * sum_c / sum_pb, 2),
        }
        if stratify_by:
            adjusted = stratified_association_test(tables)
            entry.update(
                {
                    "log2_odds_ratio": round(adjusted["log2_mh_odds_ratio"], 3),
                    "p_value": adjusted["p_value"],
                    "test": adjusted["method"] or "no informative stratum",
                    "crude_log2_odds_ratio": round(crude_or, 3),
                    "crude_p_value": crude_p,
                }
            )
        else:
            entry.update({"log2_odds_ratio": round(crude_or, 3), "p_value": crude_p})
        entry["enriched_in"] = "A" if entry["log2_odds_ratio"] > 0 else "B"
        results.append(entry)

    qvalues = benjamini_hochberg([r["p_value"] for r in results])
    for entry, q in zip(results, qvalues, strict=True):
        entry["q_value"] = q
    results.sort(key=lambda r: (r["p_value"], -abs(r["log2_odds_ratio"]), r["gene"]))
    significant = [r for r in results if r["q_value"] < ENRICHMENT_Q_SIGNIFICANT]
    payload["n_genes_tested"] = len(results)
    if only_genes:
        payload["genes_requested"] = only_genes
        payload["notes"] = payload["notes"].replace(
            f"all {{n}} genes altered in at least {min_altered} samples", "the {n} requested genes"
        )
    payload["notes"] = payload["notes"].replace("{n}", str(len(results)))
    payload["n_significant"] = len(significant)
    payload["n_significant_enriched_in_a"] = sum(1 for r in significant if r["enriched_in"] == "A")
    payload["n_significant_enriched_in_b"] = sum(1 for r in significant if r["enriched_in"] == "B")
    shown = [r for r in results if direction == "ANY" or r["enriched_in"] == direction]
    payload["direction"] = direction.lower() if direction == "ANY" else direction
    payload["genes"] = shown[:max_results]
    if len(shown) > max_results:
        which = "" if direction == "ANY" else f" enriched in {direction}"
        warnings.append(
            f"Showing the {max_results} most significant of {len(shown)} tested genes{which}; "
            "n_significant counts every tested gene."
        )
    if defining:
        payload["excluded_genes"] = sorted(defining)
        warnings.append(
            f"{', '.join(sorted(defining))} define the groups and were not tested (they are "
            "enriched by construction)."
        )
    mean_a = burden["A"] / n_a
    mean_b = burden["B"] / n_b
    payload["alteration_burden"] = {
        "mean_altered_genes_per_sample_a": round(mean_a, 2),
        "mean_altered_genes_per_sample_b": round(mean_b, 2),
    }
    ratio = max(mean_a, mean_b) / min(mean_a, mean_b) if min(mean_a, mean_b) > 0 else None
    payload["alteration_burden"]["ratio"] = round(ratio, 2) if ratio else None
    if ratio and ratio > ENRICHMENT_BURDEN_RATIO_WARN:
        heavier = "A" if mean_a > mean_b else "B"
        warnings.append(
            f"Group {heavier} carries {ratio:.2f}x more altered genes per sample, so large or "
            f"passenger-rich genes (e.g. TTN, MUC16, CSMD3) will look enriched in {heavier} "
            "without any biological interaction. Treat such hits with caution, restrict the "
            "cohort (e.g. exclude hypermutated tumours) or compare within cancer types."
        )
    if stratify_by:
        stratum_sizes: dict[str, int] = {}
        for grp in ("A", "B"):
            for stratum, n in n_group[grp].items():
                stratum_sizes[stratum] = stratum_sizes.get(stratum, 0) + n
        payload["stratification"] = {
            "by": stratify_by,
            "method": (
                "per gene, one 2x2 table per stratum; exact conditional test when sparse "
                "(Mantel-Fleiss), else Cochran-Mantel-Haenszel; Mantel-Haenszel odds ratio"
            ),
            "n_strata": len(stratum_sizes),
        }
        payload["notes"] += (
            f" Stratified by {stratify_by}: p_value / q_value / log2_odds_ratio and enriched_in "
            "are adjusted; crude_* fields are unadjusted. pct_a / pct_b are pooled frequencies, "
            "so they can point the other way from the adjusted odds ratio when the groups' "
            "cancer-type mix differs (report both). Samples without a value are not in any group."
        )
    payload["cohort"] = _cohort_block(scope, cohort, n_a + n_b, None, warnings)
    if not stratify_by:
        spread = payload["cohort"].get("n_cancer_types") or 0
        if spread > 1 or not scope.single:
            warnings.append(
                "Groups are compared across cancer types without stratification and the "
                "p-values are NOT adjusted for it: a gene can look enriched simply because one "
                "group holds more samples of a cancer type where that gene is commonly "
                "altered. Re-run with stratify_by='CANCER_TYPE' before interpreting hits."
            )
    return payload


# --- Nucleotide-level detail and allele frequencies (mutation + mutation_event) ---
#
# genomic_event_derived is keyed on the protein change, so "which codon changes
# produce BRAF V600E" and "VAF of TP53 missense mutations in diploid samples" were
# not answerable from the data apps, and a model asked for them had nothing to
# quote. The raw MAF-level tables carry both: mutation_event.codon_change and the
# genomic alleles, mutation.tumor_alt_count / tumor_ref_count.

NUCLEOTIDE_KIND = "nucleotide_variants"
MAX_NUCLEOTIDE_ROWS = 200
MAX_VAF_EVENTS = 200_000
_CODON_RE = re.compile(r"^([ACGTacgt]*)\s*(?:>|/)\s*([ACGTacgt]*)$")
_GENETIC_CODE = {
    a + b + c: aa
    for (a, b, c), aa in zip(
        (
            (x, y, z)
            for x in "TCAG"
            for y in "TCAG"
            for z in "TCAG"
        ),
        "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG",
        strict=True,
    )
}

# GISTIC discrete copy-number calls.
COPY_NUMBER_STATES = {
    "deep_deletion": -2,
    "shallow_deletion": -1,
    "diploid": 0,
    "gain": 1,
    "amplification": 2,
}


def _mutation_rows_sql(scope, genes: list[str], extra_columns: str = "") -> str:
    """Per-event rows from the MAF-level tables, with genomic_event_derived column names.

    Aliasing the columns this way lets alteration_query's compiled predicates run
    unchanged on them. Every joined table is filtered to the genes / studies first:
    mutation holds ~19M rows, and an unfiltered right-hand join would build a hash
    table over all of mutation_event.
    """
    gene_list = _sql_string_list(genes)
    entrez = f"SELECT entrez_gene_id FROM gene WHERE hugo_gene_symbol IN ({gene_list})"
    return f"""SELECT s.sample_unique_id AS sample_unique_id,
                   s.patient_unique_id AS patient_unique_id,
                   s.cancer_study_identifier AS cancer_study_identifier,
                   g.hugo_gene_symbol AS hugo_gene_symbol,
                   'mutation' AS variant_type,
                   coalesce(me.protein_change, '') AS mutation_variant,
                   coalesce(me.mutation_type, '') AS mutation_type,
                   coalesce(m.mutation_status, '') AS mutation_status,
                   coalesce(ada.driver_filter, '') AS driver_filter,
                   CAST(NULL AS Nullable(Int8)) AS cna_alteration{extra_columns}
            FROM mutation m
            JOIN (
                SELECT * FROM mutation_event WHERE entrez_gene_id IN ({entrez})
            ) me ON m.mutation_event_id = me.mutation_event_id
            JOIN (
                SELECT internal_id, sample_unique_id, patient_unique_id, cancer_study_identifier
                FROM sample_derived WHERE {_scope_sql(scope)}
            ) s ON m.sample_id = s.internal_id
            JOIN (
                SELECT entrez_gene_id, hugo_gene_symbol FROM gene
                WHERE hugo_gene_symbol IN ({_sql_string_list(genes)})
            ) g ON me.entrez_gene_id = g.entrez_gene_id
            LEFT JOIN alteration_driver_annotation ada
                ON ada.alteration_event_id = m.mutation_event_id
                AND ada.genetic_profile_id = m.genetic_profile_id
                AND ada.sample_id = m.sample_id
            WHERE m.entrez_gene_id IN ({entrez})"""


def _parse_codon_change(text: str) -> tuple[str, str]:
    m = _CODON_RE.match(str(text or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(
            "codon_change must look like 'GAG>GAA' (reference>alternate codon), or 'GAG>' / "
            "'>GAA' for one side."
        )
    return m.group(1).upper(), m.group(2).upper()


def _build_nucleotide_payload(
    scope, gene: str, protein_change, codon_change, cohort, max_rows: int
) -> dict:
    scope = _scope_of(scope)
    gene = _validate_gene_symbol(gene)
    filters = []
    described: dict = {}
    if protein_change:
        value = aq.parse_mutation_value(protein_change)
        filters.append(aq.mutation_filter_sql(value))
        described["protein_change"] = value.text
    ref = alt = ""
    if codon_change:
        ref, alt = _parse_codon_change(codon_change)
        if ref:
            filters.append(f"upper(splitByChar('/', codon_change)[1]) = '{ref}'")
        if alt:
            filters.append(f"upper(splitByChar('/', codon_change)[2]) = '{alt}'")
        described["codon_change"] = f"{ref or '*'}>{alt or '*'}"
    try:
        max_rows = max(1, min(int(max_rows), MAX_NUCLEOTIDE_ROWS))
    except (TypeError, ValueError) as e:
        raise ValueError("max_rows must be an integer.") from e
    cohort_samples = _cohort_sample_ids(scope, cohort)
    extra = """,
                   coalesce(me.variant_type, '') AS variant_class,
                   coalesce(me.chr, '') AS chromosome,
                   me.start_position AS start_position,
                   me.end_position AS end_position,
                   coalesce(me.reference_allele, '') AS reference_allele,
                   coalesce(me.tumor_seq_allele, '') AS tumor_allele,
                   coalesce(me.codon_change, '') AS codon_change,
                   coalesce(me.ncbi_build, '') AS genome_build,
                   coalesce(me.refseq_mrna_id, '') AS refseq_mrna_id"""
    where = " AND ".join(["lower(mutation_status) != 'uncalled'", *filters])
    rows = _query(f"""
        SELECT sample_unique_id, cancer_study_identifier, mutation_variant, mutation_type,
               variant_class, chromosome, start_position, end_position, reference_allele,
               tumor_allele, codon_change, genome_build, refseq_mrna_id
        FROM ({_mutation_rows_sql(scope, [gene], extra)})
        WHERE {where}
    """)
    if cohort_samples is not None:
        rows = [r for r in rows if r.get("sample_unique_id") in cohort_samples]

    payload: dict = {
        "kind": NUCLEOTIDE_KIND,
        "gene": gene,
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "filters": described,
        "variants": [],
        "codon_changes": [],
        "n_samples": 0,
        "n_events": 0,
        "warnings": [],
        "notes": (
            "One row per distinct nucleotide-level variant (genomic position, alleles, codon "
            "change) with the samples carrying it. reference_allele / tumor_allele are on the "
            "genomic + strand of genome_build; codon_change is on the transcript "
            "(refseq_mrna_id), upper-case letters marking the changed bases -- for a gene on "
            "the minus strand the two read as complements. cDNA (c.) coordinates are not "
            "stored, so do not quote a c. notation that is not in these fields."
        ),
    }
    warnings: list[str] = payload["warnings"]
    payload["cohort"] = _cohort_block(scope, cohort, None, None, warnings)
    synonymous = None
    if len(ref) == 3 and len(alt) == 3:
        aa_ref, aa_alt = _GENETIC_CODE.get(ref), _GENETIC_CODE.get(alt)
        if aa_ref and aa_alt:
            synonymous = aa_ref == aa_alt
            described["codon_amino_acids"] = f"{aa_ref}>{aa_alt}"
    if not rows:
        unknown = _unknown_study_error(scope) if cohort is None else None
        if unknown:
            payload["error"] = unknown
            return payload
        silent = _query(f"""
            SELECT countIf(mutation_type = 'Silent') AS silent, count() AS total
            FROM genomic_event_derived
            WHERE {_scope_sql(scope)} AND variant_type = 'mutation'
        """)
        n_silent = _as_int(silent[0].get("silent")) if silent else 0
        message = f"No {gene} mutation matches {described or 'the request'} in {scope.label}."
        if synonymous or (protein_change and str(protein_change).strip()[-1:] == "="):
            message += (
                " The requested change is synonymous (silent); most studies drop silent "
                f"mutations before loading ({n_silent} silent call(s) exist in this scope), so "
                "zero here means filtered upstream, not absent in patients."
            )
        payload["error"] = message
        payload["silent_mutations_in_scope"] = n_silent
        return payload

    groups: dict[tuple, dict] = {}
    for r in rows:
        key = tuple(
            r.get(k)
            for k in (
                "mutation_variant",
                "codon_change",
                "chromosome",
                "start_position",
                "end_position",
                "reference_allele",
                "tumor_allele",
                "variant_class",
                "mutation_type",
                "genome_build",
                "refseq_mrna_id",
            )
        )
        g = groups.setdefault(key, {"samples": set(), "studies": set(), "events": 0})
        g["samples"].add(r.get("sample_unique_id"))
        g["studies"].add(r.get("cancer_study_identifier"))
        g["events"] += 1
    all_samples = {r.get("sample_unique_id") for r in rows}
    variants = []
    for key, g in groups.items():
        change, codon, chrom, start, end, ref_allele, tumor_allele, vclass, mtype, build, refseq = (
            key
        )
        variants.append(
            {
                "protein_change": _clean_protein_change(change) if change else None,
                "codon_change": codon or None,
                "chromosome": chrom or None,
                "start_position": start,
                "end_position": end,
                "reference_allele": ref_allele or None,
                "tumor_allele": tumor_allele or None,
                "variant_class": vclass or None,
                "mutation_type": mtype or None,
                "genome_build": build or None,
                "refseq_mrna_id": refseq or None,
                "n_samples": len(g["samples"]),
                "n_events": g["events"],
                "n_studies": len(g["studies"]),
                "pct_of_samples": round(100.0 * len(g["samples"]) / len(all_samples), 1),
            }
        )
    variants.sort(key=lambda v: (-v["n_samples"], str(v["protein_change"]), str(v["codon_change"])))
    by_codon: dict[str, set] = {}
    for r in rows:
        by_codon.setdefault((r.get("codon_change") or "unknown").upper(), set()).add(
            r.get("sample_unique_id")
        )
    payload["codon_changes"] = [
        {
            "codon_change": k,
            "n_samples": len(v),
            "pct_of_samples": round(100.0 * len(v) / len(all_samples), 1),
        }
        for k, v in sorted(by_codon.items(), key=lambda kv: -len(kv[1]))
    ]
    payload["variants"] = variants[:max_rows]
    payload["n_variants"] = len(variants)
    payload["n_samples"] = len(all_samples)
    payload["n_events"] = len(rows)
    if len(variants) > max_rows:
        warnings.append(f"Showing {max_rows} of {len(variants)} distinct variants.")
    missing = sum(1 for r in rows if not r.get("codon_change") or r["codon_change"].upper() == "NA")
    if missing:
        warnings.append(
            f"{missing} of {len(rows)} event(s) have no codon_change recorded; they appear under "
            "'unknown' in codon_changes."
        )
    return payload


def _build_allele_frequency_payload(
    scope, alteration: str, copy_number, cohort, bins, group_by
) -> dict:
    scope = _scope_of(scope)
    track = aq.parse_single_track(alteration)
    if aq.track_variant_types(track) != {"mutation"}:
        raise ValueError(
            "alteration must select mutations only (e.g. 'TP53: MISSENSE', 'KRAS: MUT = G12D'); "
            "allele frequencies do not exist for copy-number or structural events."
        )
    _check_driver_annotations(scope, [track])
    state = None
    if copy_number is not None:
        state = str(copy_number).strip().lower()
        if state not in COPY_NUMBER_STATES:
            raise ValueError(
                f"copy_number must be one of {', '.join(COPY_NUMBER_STATES)}; got '{copy_number}'."
            )
    try:
        bins = int(bins)
    except (TypeError, ValueError) as e:
        raise ValueError("bins must be an integer.") from e
    genes = list(track.genes)
    warnings: list[str] = []
    profiles: dict[str, str] = {}
    cn_join = ""
    cn_select = ""
    if state is not None:
        prof_rows = _query(f"""
            SELECT cs.cancer_study_identifier AS study, gp.stable_id AS stable_id
            FROM genetic_profile gp
            JOIN cancer_study cs ON gp.cancer_study_id = cs.cancer_study_id
            WHERE {_scope_sql(scope, "cs.cancer_study_identifier")}
                AND gp.genetic_alteration_type = 'COPY_NUMBER_ALTERATION'
                AND gp.datatype = 'DISCRETE'
        """)
        for r in prof_rows:
            study, stable = r.get("study"), str(r.get("stable_id") or "")
            if study and stable.startswith(study + "_") and study not in profiles:
                profiles[study] = stable[len(study) + 1 :]
        if not profiles:
            raise ValueError(
                f"No study in {scope.label} has a discrete (GISTIC-style) copy-number profile, "
                "so the copy_number filter cannot be applied."
            )
        pairs = ", ".join(f"('{s}', '{p}')" for s, p in sorted(profiles.items()))
        cn_select = ", cn.cn_value AS cn_value"
        cn_join = f"""
        LEFT JOIN (
            SELECT sample_unique_id AS cn_sample, hugo_gene_symbol AS cn_gene,
                   toInt32OrNull(alteration_value) AS cn_value
            FROM genetic_alteration_derived
            WHERE {_scope_sql(scope)}
                AND (cancer_study_identifier, profile_type) IN ({pairs})
                AND hugo_gene_symbol IN ({_sql_string_list(genes)})
        ) cn ON cn.cn_sample = ev.sample_unique_id AND cn.cn_gene = ev.hugo_gene_symbol"""
    extra = """,
                   m.tumor_alt_count AS tumor_alt_count,
                   m.tumor_ref_count AS tumor_ref_count"""
    rows = _query(f"""
        SELECT ev.sample_unique_id AS sample_unique_id, ev.hugo_gene_symbol AS gene,
               ev.cancer_study_identifier AS study, ev.mutation_variant AS protein_change,
               ev.tumor_alt_count AS alt, ev.tumor_ref_count AS ref{cn_select}
        FROM ({_mutation_rows_sql(scope, genes, extra)}) ev{cn_join}
        WHERE {aq.track_sql_predicate(track)}
        LIMIT {MAX_VAF_EVENTS + 1}
    """)
    if len(rows) > MAX_VAF_EVENTS:
        raise ValueError(
            f"More than {MAX_VAF_EVENTS} mutation events match; narrow the alteration or cohort."
        )
    cohort_samples = _cohort_sample_ids(scope, cohort)
    if cohort_samples is not None:
        rows = [r for r in rows if r.get("sample_unique_id") in cohort_samples]
    n_matched = len(rows)
    no_depth = 0
    no_cn = 0
    wrong_cn = 0
    uncovered_studies: set[str] = set()
    kept: list[dict] = []
    for r in rows:
        alt, ref = r.get("alt"), r.get("ref")
        try:
            alt, ref = int(alt), int(ref)
        except (TypeError, ValueError):
            no_depth += 1
            continue
        if alt < 0 or ref < 0 or alt + ref == 0:
            no_depth += 1
            continue
        if state is not None:
            if r.get("study") not in profiles:
                uncovered_studies.add(r.get("study"))
                continue
            value = r.get("cn_value")
            if value is None:
                no_cn += 1
                continue
            if int(value) != COPY_NUMBER_STATES[state]:
                wrong_cn += 1
                continue
        kept.append({**r, "vaf": alt / (alt + ref)})

    title = f"{track.label} variant allele frequency"
    payload: dict = {
        "kind": "histogram",
        "title": title,
        "subtitle": None,
        "x_label": "Variant allele frequency  (tumor alt reads / (alt + ref))",
        "y_label": "Mutations",
        "study_id": _scope_study_id(scope),
        "scope": scope.describe(),
        "alteration": track.text,
        "unit": "mutation event (a sample with two matching mutations contributes two values)",
        "filters": {
            "alteration": track.text,
            "copy_number": (
                {
                    "state": state,
                    "gistic_value": COPY_NUMBER_STATES[state],
                    "definition": (
                        f"the gene's discrete copy-number call in that sample is "
                        f"{COPY_NUMBER_STATES[state]} ({state}); this is gene-level copy number, "
                        "not whole-genome ploidy"
                    ),
                    "profiles": _collapse_profiles(profiles),
                }
                if state is not None
                else None
            ),
        },
        "counts": {
            "n_mutations_matched": n_matched,
            "n_excluded_no_read_counts": no_depth,
            "n_excluded_no_copy_number_call": no_cn,
            "n_excluded_other_copy_number": wrong_cn,
            "n_mutations": len(kept),
            "n_samples": len({r["sample_unique_id"] for r in kept}),
        },
        "bins": [],
        "stats": None,
        "reference_lines": [],
        "warnings": warnings,
        "notes": (
            "Histogram of per-mutation variant allele frequency computed from the MAF read "
            "counts. Mean and median are computed from the plotted values and drawn as lines. "
            "VAF mixes tumour purity, copy number and clonality; the copy_number filter holds "
            "only the gene's copy-number call fixed."
        ),
    }
    payload["cohort"] = _cohort_block(scope, cohort, payload["counts"]["n_samples"], None, warnings)
    if uncovered_studies:
        warnings.append(
            f"{len(uncovered_studies)} study/studies without a discrete copy-number profile were "
            "excluded by the copy_number filter."
        )
    if no_depth:
        warnings.append(f"{no_depth} mutation(s) without tumor read counts were excluded.")
    if not kept:
        payload["error"] = _unknown_study_error(scope) if not rows else None
        payload["error"] = payload["error"] or (
            f"No {track.label} mutations with read counts"
            + (f" in {state} samples" if state else "")
            + f" in {scope.label}."
        )
        return payload
    values = [r["vaf"] for r in kept]
    hist = dstats.histogram(values, bins=bins, value_range=(0.0, 1.0))
    stats = dstats.describe(values)
    payload["bins"] = [
        {"start": round(hist["edges"][i], 6), "end": round(hist["edges"][i + 1], 6), "count": c}
        for i, c in enumerate(hist["counts"])
    ]
    payload["stats"] = _rounded(stats)
    payload["reference_lines"] = [
        {"label": "mean", "value": round(stats["mean"], 4)},
        {"label": "median", "value": round(stats["median"], 4)},
    ]
    cn_text = (
        f" · {'/'.join(genes)} copy number: {state} (GISTIC {COPY_NUMBER_STATES[state]})"
        if state
        else ""
    )
    payload["subtitle"] = (
        f"{scope.label}{cn_text} · {len(kept)} mutations in "
        f"{payload['counts']['n_samples']} samples"
    )
    if group_by:
        attribute = (
            STRATIFY_BY_STUDY
            if str(group_by).strip().upper() == STRATIFY_BY_STUDY
            else _validate_attribute_name(str(group_by).strip())
        )
        strata = _sample_strata(scope, attribute)
        buckets: dict[str, list[float]] = {}
        for r in kept:
            buckets.setdefault(strata.get(r["sample_unique_id"], "(no value)"), []).append(r["vaf"])
        payload["group_by"] = attribute
        payload["groups"] = [
            {"name": name, **_rounded(dstats.describe(vals))}
            for name, vals in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        ][:40]
        payload["notes"] += (
            f" 'groups' summarizes the same values per {attribute}; the chart is the pooled "
            "histogram (per-group violin or jitter plots are not rendered)."
        )
    return payload


def _collapse_profiles(profiles: dict[str, str]):
    """One profile name when every study uses the same one, else the per-study map."""
    names = set(profiles.values())
    return next(iter(names)) if len(names) == 1 else profiles


def _rounded(stats: dict, digits: int = 4) -> dict:
    return {k: (round(v, digits) if isinstance(v, float) else v) for k, v in stats.items()}


# --- Cross-study alteration frequency (meta-analysis) app --------------------
#
# Every other data app answers for ONE study. Researchers routinely ask for a
# gene "across MSK-CHORD and TCGA" or "in all lung adenocarcinoma studies", and
# the shipped SQL views cannot express that either: they bucket on the broad
# CANCER_TYPE only, so "Lung Adenocarcinoma" inside msk_chord_2024 -- a
# CANCER_TYPE_DETAILED / ONCOTREE_CODE value -- is unreachable, and
# gene_mutation_frequency_in_studies merges studies into one bucket with no
# overlap protection.
#
# This app returns one row per study (its own cohort, its own panel-aware
# denominator) and only then a pooled estimate: a DerSimonian-Laird
# random-effects proportion with heterogeneity, never SUM/SUM. Studies that
# share patients (MSK-CHORD is a subset of MSK-IMPACT-50k; the TCGA releases of
# one cohort overlap) are detected from the data and kept out of the pooling.
# Design, verified numbers and the reconciliation checklist:
# docs/cross-study-meta-analysis-plan.md.

CROSS_STUDY_KIND = "cross_study_frequency"
CROSS_STUDY_UNITS = ("sample", "patient")
CROSS_STUDY_DEFAULT_MIN_PROFILED = 10
# Cap on OncoTree codes after subtype expansion: a tissue-level code such as
# LUNG expands to a few dozen; anything larger is a mistake, not a cohort.
MAX_CROSS_STUDY_CODES = 250
# I-squared above which the payload warns that the studies disagree.
CROSS_STUDY_HIGH_I2 = 0.75
# How the cancer-type filter was applied in a study, in the order tried.
COHORT_KEY_ONCOTREE = "ONCOTREE_CODE"
COHORT_KEY_DETAILED = "CANCER_TYPE_DETAILED"
COHORT_KEY_STUDY_TYPE = "STUDY_TYPE"  # whole study; cancer_study.type_of_cancer_id matched
COHORT_KEY_ALL_SAMPLES = "ALL_SAMPLES"  # whole study; no cancer type requested
VALID_PREFERENCE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")


def _validate_preference_name(name: str) -> str:
    """Validate a cancer_study_query_preferences.preference_name for SQL use."""
    if not name or not VALID_PREFERENCE_NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid preference name '{name}'. Preference names may only contain "
            "alphanumeric characters and underscores (e.g. 'pan_cancer_tcga')."
        )
    return name


def _normalize_study_ids(studies) -> list[str]:
    """Accept a list of study ids (or one comma-separated string) and validate each."""
    if studies is None:
        return []
    if isinstance(studies, str):
        items = studies.split(",")
    elif isinstance(studies, (list, tuple, set)):
        items = list(studies)
    else:
        raise ValueError("studies must be a list of cBioPortal study identifiers.")
    out: list[str] = []
    for item in items:
        sid = str(item).strip()
        if not sid:
            continue
        sid = _validate_study_id(sid)
        if sid not in out:
            out.append(sid)
    return out


def _sql_string_list(values) -> str:
    """Render values as an escaped, single-quoted SQL list body: 'a', 'b'."""
    return ", ".join(f"'{_escape_sql_string(str(v))}'" for v in values)


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _round_p(p: float) -> float:
    """Three significant digits: keeps 1e-9 as 1e-9 instead of rounding it to 0."""
    return float(f"{p:.3g}")


def _oncotree_suggestions(term: str, entries: list[dict]) -> str:
    """Up to five OncoTree entries whose name contains the term, for error messages."""
    t = term.strip().lower()
    hits = [
        e
        for e in entries
        if t in (e.get("name") or "").lower() or (e.get("code") or "").lower().startswith(t)
    ]
    return ", ".join(f"{e['code']} ({e.get('name', '')})" for e in hits[:5])


def _resolve_cancer_type_codes(
    cancer_type, include_subtypes: bool
) -> tuple[list[str], list[str], list[str]]:
    """Map the caller's cancer type(s) to OncoTree codes.

    Returns ``(requested, expanded, names)``: the codes as given (upper-cased),
    the codes after subtype expansion, and the OncoTree names of the expanded
    set -- the CANCER_TYPE_DETAILED fallback matches on those names. Accepts a
    code ("LUAD"), an exact OncoTree name ("Lung Adenocarcinoma"), or a list of
    either. Anything else raises ValueError with suggestions, because a typo
    here would silently produce an empty cohort.
    """
    if isinstance(cancer_type, str):
        items = cancer_type.split(",")
    elif isinstance(cancer_type, (list, tuple, set)):
        items = list(cancer_type)
    else:
        raise ValueError("cancer_type must be an OncoTree code or a list of OncoTree codes.")
    entries = _load_oncotree_data()
    if not entries:
        raise ValueError("OncoTree data is not available; filter with cohort={...} instead.")
    by_code = {e["code"].upper(): e for e in entries if e.get("code")}
    by_name = {e["name"].strip().lower(): e for e in entries if e.get("name")}

    requested: list[str] = []
    for item in items:
        term = str(item).strip()
        if not term:
            continue
        entry = by_code.get(term.upper()) or by_name.get(term.lower())
        if entry is None:
            suggestions = _oncotree_suggestions(term, entries)
            hint = f" Did you mean: {suggestions}?" if suggestions else ""
            raise ValueError(
                f"Unknown OncoTree code '{term}'. Resolve the cancer type with "
                f"search_oncotree(...) first and pass its code.{hint}"
            )
        code = entry["code"].upper()
        if code not in requested:
            requested.append(code)
    if not requested:
        raise ValueError("cancer_type contained no OncoTree codes.")

    expanded = list(requested)
    if include_subtypes:
        children: dict[str, list[str]] = {}
        for e in entries:
            parent = (e.get("parent") or "").upper()
            code = (e.get("code") or "").upper()
            if parent and code:
                children.setdefault(parent, []).append(code)
        queue = list(requested)
        while queue:
            current = queue.pop(0)
            for child in children.get(current, []):
                if child in expanded:
                    continue
                expanded.append(child)
                queue.append(child)
                if len(expanded) > MAX_CROSS_STUDY_CODES:
                    raise ValueError(
                        f"cancer_type {requested} expands to more than "
                        f"{MAX_CROSS_STUDY_CODES} OncoTree subtypes; pass a more specific "
                        "code or include_subtypes=False."
                    )
    names = [by_code[c]["name"] for c in expanded if c in by_code and by_code[c].get("name")]
    return requested, expanded, names


def _fetch_cross_study_studies(study_ids: list[str], preference: str | None) -> list[dict]:
    """Resolve explicit ids and/or a named preference against cancer_study."""
    clauses = []
    if study_ids:
        clauses.append(f"cancer_study_identifier IN ({_sql_string_list(study_ids)})")
    if preference:
        clauses.append(
            "cancer_study_identifier IN (SELECT cancer_study_identifier "
            f"FROM cancer_study_query_preferences WHERE preference_name = '{preference}')"
        )
    return _query(f"""
        SELECT cancer_study_identifier, name, type_of_cancer_id
        FROM cancer_study
        WHERE {" OR ".join(clauses)}
        ORDER BY cancer_study_identifier
    """)


def _fetch_cross_study_attributes(study_ids: list[str]) -> dict[str, dict]:
    """Per study: does it carry ONCOTREE_CODE / CANCER_TYPE_DETAILED, and how mixed is it."""
    rows = _query(f"""
        SELECT cancer_study_identifier,
               uniqExactIf(sample_unique_id, attribute_name = 'ONCOTREE_CODE') AS n_oncotree,
               uniqExactIf(sample_unique_id, attribute_name = 'CANCER_TYPE_DETAILED') AS n_detailed,
               uniqExactIf(attribute_value, attribute_name = 'CANCER_TYPE') AS n_cancer_types
        FROM clinical_data_derived
        WHERE cancer_study_identifier IN ({_sql_string_list(study_ids)})
            AND attribute_name IN ('ONCOTREE_CODE', 'CANCER_TYPE_DETAILED', 'CANCER_TYPE')
        GROUP BY cancer_study_identifier
    """)
    out: dict[str, dict] = {}
    for row in rows:
        sid = row.get("cancer_study_identifier")
        if sid:
            out[sid] = {
                key: _as_int(row.get(key)) for key in ("n_oncotree", "n_detailed", "n_cancer_types")
            }
    return out


def _assign_cohort_keys(
    study_ids: list[str],
    studies_meta: dict[str, dict],
    attrs: dict[str, dict],
    codes: list[str],
) -> tuple[dict[str, str], list[dict]]:
    """Decide, per study, how the cancer-type filter is applied.

    ONCOTREE_CODE is the per-sample key that harmonises across studies (518 of
    545 public studies carry it, none partially). Studies without it fall back
    to CANCER_TYPE_DETAILED, matched on the OncoTree names, and a study with
    neither attribute counts as a whole when its own study-level cancer type
    is one of the requested codes. Anything else cannot be filtered and is
    reported rather than silently answered.
    """
    assignments: dict[str, str] = {}
    without: list[dict] = []
    for sid in study_ids:
        if not codes:
            assignments[sid] = COHORT_KEY_ALL_SAMPLES
            continue
        a = attrs.get(sid, {})
        study_type = (studies_meta.get(sid, {}).get("type_of_cancer_id") or "").upper()
        if a.get("n_oncotree", 0) > 0:
            assignments[sid] = COHORT_KEY_ONCOTREE
        elif a.get("n_detailed", 0) > 0:
            assignments[sid] = COHORT_KEY_DETAILED
        elif study_type and study_type in codes:
            assignments[sid] = COHORT_KEY_STUDY_TYPE
        else:
            without.append(
                {
                    "study_id": sid,
                    "reason": (
                        "no per-sample ONCOTREE_CODE or CANCER_TYPE_DETAILED attribute, and "
                        f"the study's own cancer type '{study_type.lower() or 'unknown'}' is "
                        "not among the requested codes"
                    ),
                }
            )
    return assignments, without


def _cross_study_cohort_sql(
    assignments: dict[str, str],
    codes: list[str],
    names: list[str],
    cohort_pred: tuple[str, list[str]] | None,
) -> str:
    """Body of the ``cohort`` CTE: one UNION ALL branch per cohort key.

    The cohort CTE drives every other query (counts, overlap, sample types), so
    the generic ``cohort`` predicate is applied here once and the numerator and
    denominator can never disagree about which samples are in.
    """
    by_key: dict[str, list[str]] = {}
    for sid, key in assignments.items():
        by_key.setdefault(key, []).append(sid)
    branches: list[str] = []
    if by_key.get(COHORT_KEY_ONCOTREE):
        branches.append(f"""SELECT cancer_study_identifier, sample_unique_id, patient_unique_id
            FROM clinical_data_derived
            WHERE cancer_study_identifier IN ({_sql_string_list(by_key[COHORT_KEY_ONCOTREE])})
                AND attribute_name = 'ONCOTREE_CODE'
                AND upper(attribute_value) IN ({_sql_string_list(codes)})""")
    if by_key.get(COHORT_KEY_DETAILED):
        upper_names = [n.upper() for n in names]
        branches.append(f"""SELECT cancer_study_identifier, sample_unique_id, patient_unique_id
            FROM clinical_data_derived
            WHERE cancer_study_identifier IN ({_sql_string_list(by_key[COHORT_KEY_DETAILED])})
                AND attribute_name = 'CANCER_TYPE_DETAILED'
                AND upper(attribute_value) IN ({_sql_string_list(upper_names)})""")
    whole = by_key.get(COHORT_KEY_STUDY_TYPE, []) + by_key.get(COHORT_KEY_ALL_SAMPLES, [])
    if whole:
        branches.append(f"""SELECT cancer_study_identifier, sample_unique_id, patient_unique_id
            FROM sample_derived
            WHERE cancer_study_identifier IN ({_sql_string_list(whole)})""")
    body = "\n            UNION ALL\n            ".join(branches)
    if cohort_pred is not None:
        attribute, values = cohort_pred
        body = f"""SELECT cancer_study_identifier, sample_unique_id, patient_unique_id
            FROM (
            {body}
            )
            WHERE sample_unique_id IN (
                SELECT sample_unique_id
                FROM clinical_data_derived
                WHERE cancer_study_identifier IN ({_sql_string_list(assignments)})
                    AND attribute_name = '{attribute}'
                    AND upper(attribute_value) IN ({_cohort_value_sql(values)})
            )"""
    return body


def _cross_study_profiled_sql(gene: str, config: dict, study_ids: list[str]) -> str:
    """Samples profiled for ``gene`` under the alteration's profiling type: panel ∪ WES.

    Mutations use the shipped coverage views; other profiling types take the
    same panel-membership-or-WES branch that gene_alteration_frequency_by_cancer_type
    uses. WES is not a row in gene_panel, which is the >100% trap the views exist for.
    """
    ids = _sql_string_list(study_ids)
    ptype = config["profiling_type"]
    if ptype == "MUTATION_EXTENDED":
        return f"""SELECT sample_unique_id
            FROM mutation_panel_gene_coverage
            WHERE hugo_gene_symbol = '{gene}' AND cancer_study_identifier IN ({ids})
            UNION ALL
            SELECT sample_unique_id
            FROM mutation_wes_coverage
            WHERE cancer_study_identifier IN ({ids})"""
    return f"""SELECT stgp.sample_unique_id
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene g ON gpl.gene_id = g.entrez_gene_id
            WHERE g.hugo_gene_symbol = '{gene}'
                AND stgp.alteration_type = '{ptype}'
                AND stgp.cancer_study_identifier IN ({ids})
            UNION ALL
            SELECT sample_unique_id
            FROM sample_to_gene_panel_derived
            WHERE gene_panel_id = 'WES' AND alteration_type = '{ptype}'
                AND cancer_study_identifier IN ({ids})"""


def _fetch_cross_study_counts(
    gene: str, config: dict, study_ids: list[str], cohort_sql: str
) -> dict[str, dict]:
    """Per study: cohort / profiled / altered on both grains, in one grouped query.

    ``altered`` is intersected with ``profiled`` so an off-panel-but-called event
    can never push a study over 100%. ClickHouse LEFT JOIN fills non-matches
    with '' (not NULL), so the guards are ``uniqExactIf(col, joined != '')`` --
    a plain COUNT(DISTINCT CASE ...) counts the empty string as one extra value.
    """
    ids = _sql_string_list(study_ids)
    rows = _query(f"""
        WITH cohort AS (
            {cohort_sql}
        ),
        profiled AS (
            {_cross_study_profiled_sql(gene, config, study_ids)}
        ),
        altered AS (
            SELECT DISTINCT sample_unique_id
            FROM genomic_event_derived
            WHERE cancer_study_identifier IN ({ids})
                AND hugo_gene_symbol = '{gene}'
                AND {config["event_filter"]}
                AND off_panel = 0
        )
        SELECT c.cancer_study_identifier AS study,
               uniqExact(c.sample_unique_id) AS cohort_samples,
               uniqExactIf(c.sample_unique_id, p.sample_unique_id != '') AS profiled_samples,
               uniqExactIf(c.sample_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '') AS altered_samples,
               uniqExact(c.patient_unique_id) AS cohort_patients,
               uniqExactIf(c.patient_unique_id, p.sample_unique_id != '') AS profiled_patients,
               uniqExactIf(c.patient_unique_id, p.sample_unique_id != '' AND a.sample_unique_id != '') AS altered_patients
        FROM cohort c
        LEFT JOIN profiled p USING (sample_unique_id)
        LEFT JOIN altered a USING (sample_unique_id)
        GROUP BY study
        ORDER BY study
    """)  # noqa: E501
    keys = (
        "cohort_samples",
        "profiled_samples",
        "altered_samples",
        "cohort_patients",
        "profiled_patients",
        "altered_patients",
    )
    out: dict[str, dict] = {}
    for row in rows:
        sid = row.get("study")
        if sid:
            out[sid] = {key: _as_int(row.get(key)) for key in keys}
    return out


def _fetch_cross_study_overlap(study_ids: list[str], cohort_sql: str) -> list[dict]:
    """Institutional ids shared between studies, within the cohort.

    ``sample_derived`` carries the un-prefixed ``patient_stable_id`` /
    ``sample_stable_id`` (P-0001234, TCGA-05-4244, ...), which stay the same
    across MSK releases and TCGA versions. Returns one row per (grain, set of
    studies) with the number of ids they share. Heuristic: generic ids can
    collide between unrelated studies, re-identified samples escape it.
    """
    rows = _query(f"""
        WITH cohort AS (
            {cohort_sql}
        ),
        ids AS (
            SELECT cancer_study_identifier, patient_stable_id, sample_stable_id
            FROM sample_derived
            WHERE cancer_study_identifier IN ({_sql_string_list(study_ids)})
                AND sample_unique_id IN (SELECT sample_unique_id FROM cohort)
        )
        SELECT grain, studies, count() AS shared_ids
        FROM (
            SELECT grain, id, arraySort(groupUniqArray(cancer_study_identifier)) AS studies
            FROM (
                SELECT 'patient' AS grain, patient_stable_id AS id, cancer_study_identifier FROM ids
                UNION ALL
                SELECT 'sample' AS grain, sample_stable_id AS id, cancer_study_identifier FROM ids
            )
            WHERE id != ''
            GROUP BY grain, id
            HAVING length(studies) > 1
        )
        GROUP BY grain, studies
        ORDER BY grain, shared_ids DESC
    """)
    return rows


def _fetch_cross_study_panels(study_ids: list[str], profiling_type: str) -> dict[str, list[str]]:
    rows = _query(f"""
        SELECT cancer_study_identifier, arraySort(groupUniqArray(gene_panel_id)) AS panels
        FROM sample_to_gene_panel_derived
        WHERE cancer_study_identifier IN ({_sql_string_list(study_ids)})
            AND alteration_type = '{profiling_type}'
        GROUP BY cancer_study_identifier
    """)
    out: dict[str, list[str]] = {}
    for row in rows:
        sid = row.get("cancer_study_identifier")
        panels = row.get("panels")
        if isinstance(panels, str):  # a transport that serialises arrays as text
            panels = [p.strip(" '\"") for p in panels.strip("[]").split(",") if p.strip()]
        if sid:
            out[sid] = [str(p) for p in (panels or [])]
    return out


def _fetch_cross_study_sample_types(
    study_ids: list[str], cohort_sql: str
) -> dict[str, dict[str, int]]:
    rows = _query(f"""
        WITH cohort AS (
            {cohort_sql}
        )
        SELECT cd.cancer_study_identifier, cd.attribute_value AS sample_type,
               uniqExact(cd.sample_unique_id) AS n
        FROM clinical_data_derived cd
        WHERE cd.cancer_study_identifier IN ({_sql_string_list(study_ids)})
            AND cd.attribute_name = 'SAMPLE_TYPE'
            AND cd.sample_unique_id IN (SELECT sample_unique_id FROM cohort)
        GROUP BY cd.cancer_study_identifier, sample_type
        ORDER BY cd.cancer_study_identifier, n DESC
    """)
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        sid = row.get("cancer_study_identifier")
        stype = row.get("sample_type")
        if sid and stype:
            out.setdefault(sid, {})[str(stype)] = _as_int(row.get("n"))
    return out


def _overlap_pairs(rows: list[dict], present: list[str]) -> list[dict]:
    """Collapse the overlap probe into per-pair counts among ``present`` studies."""
    pairs: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        grain = row.get("grain")
        studies = row.get("studies")
        if isinstance(studies, str):
            studies = [s.strip(" '\"") for s in studies.strip("[]").split(",") if s.strip()]
        studies = sorted(s for s in (studies or []) if s in present)
        n = _as_int(row.get("shared_ids"))
        if grain not in ("patient", "sample") or len(studies) < 2 or n <= 0:
            continue
        for i, a in enumerate(studies):
            for b in studies[i + 1 :]:
                entry = pairs.setdefault((a, b), {"shared_patients": 0, "shared_samples": 0})
                entry["shared_patients" if grain == "patient" else "shared_samples"] += n
    return [
        {"studies": list(key), **counts}
        for key, counts in sorted(
            pairs.items(), key=lambda kv: (-kv[1]["shared_patients"], -kv[1]["shared_samples"])
        )
    ]


def _cross_study_design_summary(rows: list[dict], limit: int = 6) -> str:
    """One line naming how the pooled studies differ: panels and sample-type mix.

    Ordered by profiled size (the rows already are), capped so the warning stays
    readable when fifteen studies are pooled.
    """
    designs = []
    for r in rows[:limit]:
        bits = []
        if r.get("panels"):
            bits.append("/".join(r["panels"][:4]) + ("…" if len(r["panels"]) > 4 else ""))
        if r.get("sample_types"):
            top = sorted(r["sample_types"].items(), key=lambda kv: -kv[1])[:2]
            bits.append(", ".join(f"{k} {v}" for k, v in top))
        designs.append(f"{r['study_id']} ({'; '.join(bits) or 'design unknown'})")
    if len(rows) > limit:
        designs.append(f"and {len(rows) - limit} more")
    return "; ".join(designs)


def _build_cross_study_payload(
    gene: str,
    studies,
    preference: str | None,
    cancer_type,
    include_subtypes: bool,
    cohort: dict[str, list[str]] | None,
    alteration: str,
    unit: str,
    min_profiled: int,
    pool: bool,
) -> dict:
    """Assemble the cross-study payload; raises ValueError for bad arguments."""
    gene = _validate_gene_symbol(gene)
    config = _validate_alteration_type(alteration)
    if unit not in CROSS_STUDY_UNITS:
        raise ValueError(f"unit must be one of {', '.join(CROSS_STUDY_UNITS)}; got '{unit}'.")
    try:
        min_profiled = int(min_profiled)
    except (TypeError, ValueError) as e:
        raise ValueError("min_profiled must be an integer.") from e
    if min_profiled < 1:
        raise ValueError("min_profiled must be at least 1.")
    study_ids = _normalize_study_ids(studies)
    preference = _validate_preference_name(preference) if preference else None
    if not study_ids and not preference:
        raise ValueError(
            "Name the studies to compare: pass study ids in `studies` (use list_studies to "
            "find them) and/or a `preference` from cancer_study_query_preferences "
            "(e.g. 'pan_cancer_tcga')."
        )
    if cancer_type is not None and cancer_type != "" and cancer_type != []:
        requested_codes, codes, names = _resolve_cancer_type_codes(cancer_type, include_subtypes)
    else:
        requested_codes, codes, names = [], [], []
    cohort_pred = _parse_cohort(cohort) if cohort is not None else None
    profiling_type = config["profiling_type"]

    def _error(message: str) -> dict:
        return {"error": message, "kind": CROSS_STUDY_KIND, "gene": gene, "studies": []}

    # 1. Which studies exist. Unknown ids are an error, not a silent drop.
    resolved = _fetch_cross_study_studies(study_ids, preference)
    studies_meta = {
        r["cancer_study_identifier"]: r for r in resolved if r.get("cancer_study_identifier")
    }
    missing = [sid for sid in study_ids if sid not in studies_meta]
    if missing:
        return _error(
            f"Unknown study id(s): {', '.join(missing)}. Use list_studies(search=...) to "
            "find the exact identifier."
        )
    if not studies_meta:
        return _error(
            f"Preference '{preference}' resolved to no studies in this deployment. See "
            "SELECT DISTINCT preference_name FROM cancer_study_query_preferences."
        )
    all_ids = list(study_ids) + sorted(sid for sid in studies_meta if sid not in study_ids)

    # 2. How each study can be filtered to the cancer type.
    attrs = _fetch_cross_study_attributes(all_ids)
    assignments, without_cohort = _assign_cohort_keys(all_ids, studies_meta, attrs, codes)
    if not assignments:
        return _error(
            f"None of the requested studies can be filtered to {requested_codes}: "
            + "; ".join(f"{w['study_id']}: {w['reason']}" for w in without_cohort)
        )
    cohort_sql = _cross_study_cohort_sql(assignments, codes, names, cohort_pred)
    query_ids = list(assignments)

    # 3. The counts.
    counts = _fetch_cross_study_counts(gene, config, query_ids, cohort_sql)
    present: list[str] = []
    for sid in query_ids:
        if counts.get(sid, {}).get("cohort_samples", 0) > 0:
            present.append(sid)
        else:
            reason = "no samples matched"
            if codes:
                reason += f" {assignments[sid]} in {codes}"
            if cohort_pred is not None:
                reason += f" with cohort filter {cohort_pred[0]} in {cohort_pred[1]}"
            without_cohort.append({"study_id": sid, "reason": reason})
    if not present:
        return _error(
            "No samples matched the cohort in any requested study: "
            + "; ".join(f"{w['study_id']}: {w['reason']}" for w in without_cohort)
            + ". Check the OncoTree code / cohort values (clinical_data_derived)."
        )

    # 4-6. Overlap (only meaningful for 2+ studies), panels, sample-type mix.
    overlap_rows = _fetch_cross_study_overlap(present, cohort_sql) if len(present) >= 2 else []
    panels = _fetch_cross_study_panels(present, profiling_type)
    sample_types = _fetch_cross_study_sample_types(present, cohort_sql)

    warnings: list[str] = []
    notes: list[str] = []
    grain = "samples" if unit == "sample" else "patients"
    rows: list[dict] = []
    by_id: dict[str, dict] = {}
    for sid in present:
        c = counts[sid]
        n_profiled = c[f"profiled_{grain}"]
        n_altered = c[f"altered_{grain}"]
        row = {
            "study_id": sid,
            "name": studies_meta[sid].get("name"),
            "cohort_key": assignments[sid],
            "samples": {
                "cohort": c["cohort_samples"],
                "profiled": c["profiled_samples"],
                "altered": c["altered_samples"],
            },
            "patients": {
                "cohort": c["cohort_patients"],
                "profiled": c["profiled_patients"],
                "altered": c["altered_patients"],
            },
            "frequency_pct": None,
            "ci95": None,
            "weight_pct": None,
            "panels": panels.get(sid, []),
            "sample_types": sample_types.get(sid) or None,
            "status": "included",
        }
        if n_profiled == 0:
            row["status"] = "not_covered"
            warnings.append(
                f"{gene} is not on any {profiling_type} panel in {sid}: "
                f"{c['cohort_samples']} cohort samples, 0 profiled. Shown as not covered, "
                "not as 0%."
            )
        else:
            lo, hi = wilson_interval(n_altered, n_profiled)
            row["frequency_pct"] = round(100.0 * n_altered / n_profiled, 1)
            row["ci95"] = [round(100.0 * lo, 1), round(100.0 * hi, 1)]
            if n_profiled < min_profiled:
                row["status"] = "below_min_profiled"
        rows.append(row)
        by_id[sid] = row

    # Overlap guard: of two studies that share ids, keep the larger profiled
    # cohort in the pooling and exclude the smaller. The remaining included set
    # is pairwise disjoint; per-study rows are never touched.
    pairs = _overlap_pairs(overlap_rows, present)
    excluded_for_overlap: list[str] = []
    for pair in pairs:
        a, b = pair["studies"]
        if by_id[a]["status"] != "included" or by_id[b]["status"] != "included":
            continue
        drop = min((a, b), key=lambda s: (by_id[s][grain]["profiled"], -present.index(s)))
        keep = b if drop == a else a
        by_id[drop]["status"] = "overlap"
        by_id[drop]["overlaps_with"] = keep
        excluded_for_overlap.append(drop)
        shared = pair["shared_patients"] or pair["shared_samples"]
        which = "patients" if pair["shared_patients"] else "samples"
        warnings.append(
            f"{a} and {b} share {shared} {which} (same institutional ids): {drop} was "
            f"excluded from pooling, {keep} kept (larger profiled cohort). Per-study rows "
            "are unaffected."
        )

    included = [r for r in rows if r["status"] == "included"]
    below = [r["study_id"] for r in rows if r["status"] == "below_min_profiled"]
    tuples = [(r[grain]["altered"], r[grain]["profiled"]) for r in included]

    pooled = heterogeneity_block = difference = None
    if len(included) >= 2:
        meta = pooled_proportion(tuples)
        heterogeneity_block = {
            "q": round(meta["q"], 3),
            "df": meta["df"],
            "p_value": _round_p(meta["p_heterogeneity"]),
            "i2_pct": round(100.0 * meta["i2"], 1),
            "tau2": round(meta["tau2"], 4),
        }
        if pool:
            re_ = meta["random"]
            fe = meta["fixed"]
            pooled = {
                "method": "random_effects_dersimonian_laird",
                "scale": "logit",
                "k": meta["k"],
                "studies": [r["study_id"] for r in included],
                "frequency_pct": round(100.0 * re_["proportion"], 1),
                "ci95": [round(100.0 * re_["ci"][0], 1), round(100.0 * re_["ci"][1], 1)],
                "fixed_effect_pct": round(100.0 * fe["proportion"], 1),
                "fixed_effect_ci95": [round(100.0 * fe["ci"][0], 1), round(100.0 * fe["ci"][1], 1)],
                "n_altered": sum(a for a, _ in tuples),
                "n_profiled": sum(n for _, n in tuples),
            }
            for r, w in zip(included, re_["weights"], strict=True):
                r["weight_pct"] = round(100.0 * w, 1)
        test = homogeneity_test(tuples)
        difference = {
            "test": test["test"],
            "statistic": None if test["statistic"] is None else round(test["statistic"], 3),
            "df": test["df"],
            "p_value": _round_p(test["p_value"]),
            "k": test["k"],
        }
        if meta["i2"] >= CROSS_STUDY_HIGH_I2:
            warnings.append(
                f"High between-study heterogeneity (I² = {heterogeneity_block['i2_pct']}%, "
                f"p = {heterogeneity_block['p_value']}). The studies differ in design: "
                + _cross_study_design_summary(included)
                + ". Report the per-study rates as the headline; the pooled value is a "
                "summary of these studies, not a population estimate."
            )
    else:
        notes.append(
            f"Pooled estimate and difference test not computed: {len(included)} study "
            f"eligible (need 2 with >= {min_profiled} profiled {grain}, not overlapping)."
        )
    if len(included) >= 2 and not pool:
        notes.append("pool=False: pooled estimate withheld; heterogeneity and test still shown.")

    # Disclosures the model must carry into its answer.
    if not codes:
        for sid in present:
            n_types = attrs.get(sid, {}).get("n_cancer_types", 0)
            if n_types > 1 and cohort_pred is None:
                warnings.append(
                    f"No cancer-type filter: {sid} spans {n_types} cancer types "
                    f"({counts[sid]['cohort_samples']} samples). Pass cancer_type=<OncoTree "
                    "code> to compare like with like."
                )
    notes.append(
        f"Counting unit: {grain}. frequency_pct = altered {grain} / {grain} profiled for "
        f"{gene} ({profiling_type}) within each study's cohort; both grains are reported "
        "per study. Pass unit='patient' for prevalence / fraction-of-patients questions."
    )
    notes.append(
        "Per-study rows are the primary result. The pooled value is a DerSimonian-Laird "
        "random-effects meta-analytic proportion (logit scale) over the non-overlapping "
        "included studies, not a sum of counts; report it with its CI and I²."
    )
    if below:
        notes.append(
            f"Shown but excluded from pooling and the test (fewer than {min_profiled} "
            f"profiled {grain}): {', '.join(below)}."
        )
    if codes:
        notes.append(
            f"Cancer type: OncoTree {requested_codes}"
            + (f" expanded to {codes}" if codes != requested_codes else "")
            + " matched per sample on ONCOTREE_CODE (fallbacks per row in cohort_key)."
        )
    if profiling_type != "MUTATION_EXTENDED":
        notes.append(
            f"Denominators count samples profiled for {profiling_type}; alteration "
            f"'{alteration}' counts {config['event_filter']}."
        )

    return {
        "kind": CROSS_STUDY_KIND,
        "gene": gene,
        "alteration": alteration,
        "unit": unit,
        "min_profiled": min_profiled,
        "preference": preference,
        "cancer_type": (
            {
                "requested": requested_codes,
                "codes": codes,
                "names": names,
                "include_subtypes": bool(include_subtypes),
            }
            if codes
            else None
        ),
        "filter": cohort,
        "studies": rows,
        "studies_without_cohort": without_cohort,
        "pooled": pooled,
        "heterogeneity": heterogeneity_block,
        "difference_test": difference,
        "overlap": {
            "checked": len(present) >= 2,
            "pairs": pairs,
            "excluded": excluded_for_overlap,
            "pooling_blocked": bool(excluded_for_overlap) and len(included) < 2,
        },
        "warnings": warnings,
        "notes": notes,
    }


# Create FastMCP instance
mcp = FastMCP(
    name="cBioPortal MCP Server",
    instructions=_load_resource("system-prompt.md"),
)


def main():
    """Main entry point for the server."""
    logging.basicConfig(level=logging.INFO)
    logger.info("🚀 Starting cBioPortal MCP Server with FastMCP...")

    # Get config
    config = get_mcp_config()

    try:
        ensure_db_permissions(config=config)
    except PermissionError as e:
        logger.critical("❌ ClickHouse permission check failed: %s", e)
        sys.exit(2)

    # Set up OpenTelemetry → Datadog agent (no-op if env vars not set or agent unreachable)
    provider = configure_telemetry()
    if provider is not None:
        mcp.add_middleware(TelemetryMiddleware())

    transport = config.mcp_server_transport

    try:
        # For HTTP and SSE transports, we need to specify host and port
        http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
        if transport in http_transports:
            # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
            # and bind port (defaults to 8000)
            run_kwargs = {
                "transport": transport,
                "host": config.mcp_bind_host,
                "port": config.mcp_bind_port,
            }
            if config.mcp_http_path:
                run_kwargs["path"] = config.mcp_http_path
            # Behind a TLS-terminating reverse proxy, uvicorn must trust
            # X-Forwarded-Proto or the trailing-slash 307 on /mcp will
            # downgrade https → http and strict clients (e.g. Claude
            # Desktop) refuse to follow. Opt-in via env so direct
            # deployments aren't asked to trust spoofed headers.
            if config.mcp_forwarded_allow_ips:
                run_kwargs["uvicorn_config"] = {
                    "proxy_headers": True,
                    "forwarded_allow_ips": config.mcp_forwarded_allow_ips,
                }
            mcp.run(**run_kwargs)
        else:
            # For stdio transport, no host or port is needed
            mcp.run(transport=transport)
    except ValueError as e:
        if "I/O operation on closed file" in str(e):
            # Handle the stdio buffer closed error gracefully
            logger.warning(f"Stdio transport initialization failed: {e}")
            logger.info("This may happen during subprocess cleanup. Server completed successfully.")
        else:
            # Re-raise other ValueError exceptions
            raise
    except Exception as e:
        logger.error(f"Failed to start MCP server: {e}")
        raise


# Core query guides shipped in the image. Each is served both as a
# cbioportal://<name> MCP resource and via the read_guide/list_guides tools. The
# URI suffix is the markdown filename (cbioportal://<name> -> <name>.md), so this
# one table drives the resource registration, read_guide, and list_guides below.
GUIDES: list[tuple[str, str]] = [
    (
        "cbioportal://mutation-frequency-guide",
        "Comprehensive guide for calculating gene mutation frequencies with gene-specific profiling denominators",
    ),
    (
        "cbioportal://clinical-data-guide",
        "Guide for querying clinical data including patient vs sample level considerations",
    ),
    (
        "cbioportal://sample-filtering-guide",
        "Guide for filtering samples and studies in cBioPortal queries",
    ),
    (
        "cbioportal://common-pitfalls",
        "Guide to avoid common mistakes when querying cBioPortal data",
    ),
    (
        "cbioportal://treatment-guide",
        "Guide for querying treatment/clinical event data including drug agents, timelines, and linking to genomic data",
    ),
    (
        "cbioportal://faq-guide",
        "General cBioPortal FAQ: history, how to cite, data types, reference genome, abbreviations, GISTIC thresholds, API access",
    ),
    (
        "cbioportal://statistical-tests-guide",
        "Statistical test selection guide — decision matrix for choosing Fisher's exact, Wilcoxon, chi-squared, t-test, ANOVA, etc. based on data type and group count",
    ),
    (
        "cbioportal://gene-expression-guide",
        "Gene expression / copy-number / methylation analysis. Covers genetic_alteration_derived, profile_type discovery, and the gene_pair_coexpression view for Spearman correlation between two genes",
    ),
    (
        "cbioportal://external-resources-guide",
        "Guide for finding external linked resources such as imaging, pathology, Minerva, HTAN, or other resource_* table links before declaring data unavailable",
    ),
    (
        "cbioportal://gene-resolution-guide",
        "Guide for resolving ambiguous gene symbols, aliases, gene families, and shorthand such as CD3 before querying expression or alteration data",
    ),
    (
        "cbioportal://oql-guide",
        "Alteration query (OQL) syntax accepted by oncoprint, alteration_cooccurrence, "
        "survival_curve groups, alteration_enrichment and mutation_allele_frequency: merged "
        "tracks, mutation classes, codon ranges, exclusions, what is refused",
    ),
    (
        "cbioportal://study-resolution-guide",
        "Guide for resolving requested studies, avoiding silent substitute cohorts, and redirecting to known external cBioPortal instances when data is not in this deployment",
    ),
]


def _guide_filename(uri: str) -> str:
    """Map a cbioportal://<name> guide URI to its shipped <name>.md filename."""
    return uri.removeprefix("cbioportal://") + ".md"


# --- MCP resources: register each guide URI from the GUIDES table -------------
def _register_guide_resources() -> None:
    def _reader(uri: str):
        # The reader must take no args: a parameter makes FastMCP treat the
        # static URI as a resource *template* and reject it for lacking a
        # {placeholder}. So bind `uri` via this factory's closure instead.
        def _read() -> str:
            return _load_resource(_guide_filename(uri))

        _read.__name__ = uri.removeprefix("cbioportal://").replace("-", "_")
        return _read

    for uri, _desc in GUIDES:
        mcp.resource(uri)(_reader(uri))


_register_guide_resources()


@mcp.tool(
    description="""
    Execute a ClickHouse SQL SELECT query.

    For complex analysis patterns, consult these query guides:
    - cbioportal://mutation-frequency-guide - Gene mutation frequency calculations with proper denominators
    - cbioportal://clinical-data-guide - Patient vs sample-level clinical data queries
    - cbioportal://sample-filtering-guide - Study and sample type filtering strategies
    - cbioportal://external-resources-guide - External linked resources such as imaging viewers
    - cbioportal://gene-resolution-guide - Ambiguous gene symbols and aliases
    - cbioportal://study-resolution-guide - Missing studies, external portals, and substitute cohorts
    - cbioportal://common-pitfalls - Common query mistakes and how to avoid them

    Returns:
        - On success: an object with a single field "rows" containing an array of result rows.
        - On failure: an object with a single field "error_message" containing a string describing the error.
"""
)
def clickhouse_run_select_query(query: str) -> dict[str, list[dict] | str]:
    try:
        result = run_select_query(query)
        logger.debug(f"clickhouse_run_select_query returns {result}")
        return {"rows": result}
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_run_select_query: {error_message}")
        return {"error_message": error_message}


@mcp.tool(
    description="""
    Retrieve a list of all tables in the current database.

    Returns:
        - On success: an object with a single field "tables" containing an array of objects with the following fields:
            - name: Table name.
        - On failure: an object with a single field "error_message" containing a string describing the error.
"""
)
def clickhouse_list_tables() -> dict[str, list[dict] | str]:
    logger.info(f"clickhouse_list_tables: called")

    try:
        from mcp_clickhouse.mcp_server import execute_query

        raw = execute_query("SHOW TABLES")
        rows = raw.get("rows", [])
        result = [{"name": row[0]} for row in rows if row]
        logger.debug(f"clickhouse_list_tables result: {result}")
        return {"tables": result}
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_list_tables: {error_message}")
        return {"error_message": error_message}


@mcp.tool(
    description="""
    Retrieve a list of all columns for the table in the current database.

    Returns:
        - On success: an object with a single field "columns" containing an array of objects with the following fields:
            - name: Column name.
            - type: ClickHouse data type of the column.
            - comment: Column description, if available.
        - On failure: an object with a single field "error_message" containing a string describing the error.
"""
)
def clickhouse_list_table_columns(table: str) -> dict[str, list[dict] | str]:
    logger.info(f"clickhouse_list_table_columns: called")

    try:
        table = _validate_table_name(table)
        from mcp_clickhouse.mcp_server import execute_query

        raw = execute_query(f"DESCRIBE TABLE {table}")
        columns_list = raw.get("columns", [])
        rows = raw.get("rows", [])
        # DESCRIBE TABLE returns: name, type, default_type, default_expression, comment, ...
        col_idx = {name: i for i, name in enumerate(columns_list)}
        name_idx = col_idx.get("name", 0)
        type_idx = col_idx.get("type", 1)
        comment_idx = col_idx.get("comment", 4)
        result = []
        for row in rows:
            entry = {
                "name": row[name_idx] if len(row) > name_idx else "",
                "type": row[type_idx] if len(row) > type_idx else "",
            }
            if len(row) > comment_idx and row[comment_idx]:
                entry["comment"] = row[comment_idx]
            result.append(entry)
        logger.debug(f"clickhouse_list_table_columns result: {result}")
        return {"columns": result}
    except Exception as e:
        error_message = str(e)
        logger.error(f"clickhouse_list_table_columns: {error_message}")
        return {"error_message": error_message}


def run_select_query(query: str) -> list[dict]:
    """
    Execute arbitrary ClickHouse SQL SELECT query.

    Note: CTEs (WITH ... AS) are supported. Query validation is handled at the
    database level via read-only user permissions (see authentication/permissions.py).

    Returns:
        list: A list of rows, where each row is a dictionary with
              column names as keys and corresponding values.
    """
    from mcp_clickhouse.mcp_server import run_select_query

    # DB-level read-only permissions (enforced on startup) prevent non-SELECT queries,
    # so we don't need application-level query filtering. This allows CTEs (WITH ... AS).
    logger.debug("run_select_query: delegate the query to run_select_query tool of ClickHouse MCP")
    ch_query_result = run_select_query(query)
    result = zip_select_query_result(ch_query_result)
    return result


def zip_select_query_result(ch_query_result) -> list[dict]:
    """
    Join columns and corresponding row values into dictionaries skipping dictionary entries if value is emtpy or None
    """
    columns = ch_query_result["columns"]
    rows = ch_query_result["rows"]
    result = []
    for row in rows:
        result.append({k: v for k, v in zip(columns, row) if v not in ("", None)})
    return result


# Resource Access Tools for AI Agents
@mcp.tool()
def list_guides() -> list[dict]:
    """List all available query guides with their URIs and descriptions.

    Call this tool first to see what guides are available before answering complex queries.

    Includes:
      - Core guides baked into the MCP image (mutation-frequency, clinical-data, etc.)
      - Deployment-specific general guides under resources/guides/, accessed via
        get_general_guide(name). Use these for anything that's specific to the
        local deployment (e.g. data-source provenance, institutional policies).
      - Study-specific guides under resources/study-guides/, accessed via
        get_study_guide(study_id).
    """
    deployment_guides = [
        {
            "uri": f"cbioportal://general-guide/{name}",
            "description": f"Deployment-specific guide — call get_general_guide('{name}')",
        }
        for name in _list_available_general_guides()
    ]
    core_guides = [{"uri": uri, "description": desc} for uri, desc in GUIDES]
    return (
        deployment_guides
        + core_guides
        + [
            {
                "uri": "cbioportal://study-guide/{study_id}",
                "description": "Dynamic study-specific guide - use get_study_guide(study_id) tool to generate",
            },
        ]
    )


@mcp.tool()
def read_guide(uri: str) -> str:
    """Read the content of a specific guide by URI.

    Use this after calling list_guides() to read the detailed content of guides.

    Args:
        uri: The guide URI (e.g., "cbioportal://mutation-frequency-guide")
    """
    valid_uris = [u for u, _ in GUIDES]
    if uri not in valid_uris:
        available_list = "\n".join(f"  - {u}" for u in valid_uris)
        return (
            f"Resource not found: {uri}.\n"
            f"Available resources:\n{available_list}\n\n"
            "Use list_guides() for descriptions, or get_study_guide(study_id) for study-specific guides."
        )

    return _load_resource(_guide_filename(uri))


@mcp.tool()
def get_general_guide(name: str) -> str:
    """Get a deployment-specific general guide by name.

    Reads `resources/guides/{name}.md`. Deployments can drop additional
    `.md` files into that directory (or replace its contents) to publish
    guides that aren't appropriate for the upstream image — e.g. local
    data governance, custom tool integrations, or deployment-specific
    data sources.

    Call list_guides() first to see what's available in this deployment.

    Args:
        name: The guide name without the .md extension (e.g. "cdsi-info").
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return f"Error: invalid guide name '{name}'"
    content = _load_general_guide(name)
    if content is None:
        available = _list_available_general_guides()
        if available:
            available_list = "\n".join(f"  - {n}" for n in available)
            return f"General guide '{name}' not found.\nAvailable:\n{available_list}"
        return f"General guide '{name}' not found. No deployment-specific guides are configured."
    return content


@mcp.tool()
def get_study_guide(study_id: str) -> str:
    """Get a guide for a specific cBioPortal study.

    First checks for a pre-generated guide in resources/study-guides/{study_id}.md.
    If not found, dynamically generates one by querying the database.

    Pre-generated guides may include curated notes and tips specific to each study.

    Args:
        study_id: The cancer study identifier (e.g., "msk_chord_2024", "brca_tcga_pan_can_atlas_2018")

    Returns:
        A markdown-formatted guide specific to the requested study
    """
    # Validate study_id to prevent SQL injection
    try:
        study_id = _validate_study_id(study_id)
    except ValueError as e:
        return f"Error: {str(e)}"

    # First, check for a pre-generated guide file
    static_guide = _load_study_guide(study_id)
    if static_guide:
        logger.info(f"Loaded static study guide for {study_id}")
        return static_guide

    # Fall back to dynamic generation
    logger.info(f"Generating dynamic study guide for {study_id}")
    try:
        guide_sections = []

        # 1. Basic study info
        study_info = run_select_query(f"""
            SELECT
                cancer_study_identifier,
                name,
                description,
                type_of_cancer_id
            FROM cancer_study
            WHERE cancer_study_identifier = '{study_id}'
        """)

        if not study_info:
            return f"Study '{study_id}' not found. Use clickhouse_list_tables or query cancer_study table to find valid study identifiers."

        info = study_info[0]
        guide_sections.append(f"""# Study Guide: {info.get("name", study_id)}

**Study ID:** `{study_id}`
**Cancer Type:** {info.get("type_of_cancer_id", "N/A")}
**Description:** {info.get("description", "N/A")}
""")

        # 2. Patient and sample counts
        counts = run_select_query(f"""
            SELECT
                COUNT(DISTINCT patient_unique_id) as patient_count,
                COUNT(DISTINCT sample_unique_id) as sample_count
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
        """)
        if counts:
            c = counts[0]
            guide_sections.append(f"""## Cohort Statistics
- **Patients:** {c.get("patient_count", "N/A"):,}
- **Samples:** {c.get("sample_count", "N/A"):,}
""")

        # 3. Available data types
        profiles = run_select_query(f"""
            SELECT DISTINCT
                gp.genetic_alteration_type,
                gp.datatype,
                gp.name
            FROM genetic_profile gp
            JOIN cancer_study cs ON gp.cancer_study_id = cs.cancer_study_id
            WHERE cs.cancer_study_identifier = '{study_id}'
        """)
        if profiles:
            guide_sections.append("## Available Data Types\n")
            for p in profiles:
                guide_sections.append(
                    f"- **{p.get('genetic_alteration_type', 'Unknown')}**: {p.get('name', 'N/A')}"
                )
            guide_sections.append("")

        # 4. Gene panels used
        panels = run_select_query(f"""
            SELECT DISTINCT gene_panel_id, COUNT(DISTINCT sample_unique_id) as sample_count
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier = '{study_id}'
            GROUP BY gene_panel_id
            ORDER BY sample_count DESC
            LIMIT 10
        """)
        if panels:
            guide_sections.append("## Gene Panels\n")
            for p in panels:
                panel_id = p.get("gene_panel_id", "Unknown")
                count = p.get("sample_count", 0)
                if panel_id == "WES":
                    guide_sections.append(
                        f"- **{panel_id}** (Whole Exome): {count:,} samples — all genes profiled"
                    )
                else:
                    guide_sections.append(f"- **{panel_id}**: {count:,} samples")
            guide_sections.append("")

        # 5. Clinical attributes available
        attrs = run_select_query(f"""
            SELECT DISTINCT attribute_name, COUNT(DISTINCT sample_unique_id) as coverage
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
            GROUP BY attribute_name
            ORDER BY coverage DESC
            LIMIT 20
        """)
        if attrs:
            guide_sections.append("## Available Clinical Attributes\n")
            guide_sections.append("| Attribute | Samples with Data |")
            guide_sections.append("|-----------|------------------|")
            for a in attrs:
                guide_sections.append(
                    f"| {a.get('attribute_name', 'Unknown')} | {a.get('coverage', 0):,} |"
                )
            guide_sections.append("")

        # 6. Top mutated genes (if mutation data exists)
        top_genes = run_select_query(f"""
            SELECT
                hugo_gene_symbol,
                COUNT(DISTINCT sample_unique_id) as altered_samples
            FROM genomic_event_derived
            WHERE cancer_study_identifier = '{study_id}'
                AND variant_type = 'mutation'
                AND mutation_status != 'UNCALLED'
            GROUP BY hugo_gene_symbol
            ORDER BY altered_samples DESC
            LIMIT 10
        """)
        if top_genes:
            guide_sections.append("## Top Mutated Genes\n")
            guide_sections.append("| Gene | Altered Samples |")
            guide_sections.append("|------|----------------|")
            for g in top_genes:
                guide_sections.append(
                    f"| {g.get('hugo_gene_symbol', 'Unknown')} | {g.get('altered_samples', 0):,} |"
                )
            guide_sections.append("")

        # 7. Sample type distribution
        sample_types = run_select_query(f"""
            SELECT attribute_value as sample_type, COUNT(DISTINCT sample_unique_id) as count
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
                AND attribute_name = 'SAMPLE_TYPE'
            GROUP BY attribute_value
            ORDER BY count DESC
        """)
        if sample_types:
            guide_sections.append("## Sample Types\n")
            for st in sample_types:
                guide_sections.append(
                    f"- **{st.get('sample_type', 'Unknown')}**: {st.get('count', 0):,} samples"
                )
            guide_sections.append("")

        # 8. Query tips for this study
        guide_sections.append(f"""## Query Tips for {study_id}

```sql
-- Get all samples in this study
SELECT DISTINCT sample_unique_id, patient_unique_id
FROM clinical_data_derived
WHERE cancer_study_identifier = '{study_id}';

-- Get mutations for a specific gene
SELECT sample_unique_id, hugo_gene_symbol, mutation_variant, mutation_type
FROM genomic_event_derived
WHERE cancer_study_identifier = '{study_id}'
    AND hugo_gene_symbol = 'TP53'
    AND variant_type = 'mutation';

-- Get clinical data for specific attributes
SELECT sample_unique_id, attribute_name, attribute_value
FROM clinical_data_derived
WHERE cancer_study_identifier = '{study_id}'
    AND attribute_name IN ('CANCER_TYPE', 'SAMPLE_TYPE', 'OS_MONTHS');
```
""")

        return "\n".join(guide_sections)

    except Exception as e:
        logger.error(f"get_study_guide error: {e}")
        return f"Error generating study guide for '{study_id}': {str(e)}"


# Maximum allowed limit for list queries to prevent expensive unbounded queries
MAX_LIST_LIMIT = 100


@mcp.tool()
def list_studies(search: str | None = None, limit: int = 20) -> list[dict]:
    """List available cBioPortal studies.

    Studies with pre-generated guides (in resources/study-guides/) will have has_guide=True.

    Args:
        search: Optional search term to filter studies by name, identifier,
                cancer type, or description
        limit: Maximum number of studies to return (default 20, max 100)

    Returns:
        List of studies with their identifiers, names, descriptions,
        sample counts, and guide availability
    """
    available_guides = set(_list_available_study_guides())

    # Clamp limit to safe bounds
    safe_limit = max(1, min(int(limit), MAX_LIST_LIMIT))

    try:
        if search:
            # Sanitize search term to prevent SQL injection
            safe_search = _sanitize_search_term(search)
            query = f"""
                SELECT
                    cs.cancer_study_identifier,
                    cs.name,
                    cs.description,
                    cs.type_of_cancer_id,
                    COUNT(DISTINCT cd.sample_unique_id) as sample_count
                FROM cancer_study cs
                LEFT JOIN clinical_data_derived cd ON cs.cancer_study_identifier = cd.cancer_study_identifier
                WHERE cs.cancer_study_identifier ILIKE '%{safe_search}%'
                    OR cs.name ILIKE '%{safe_search}%'
                    OR cs.type_of_cancer_id ILIKE '%{safe_search}%'
                    OR cs.description ILIKE '%{safe_search}%'
                GROUP BY cs.cancer_study_identifier, cs.name, cs.description, cs.type_of_cancer_id
                ORDER BY sample_count DESC
                LIMIT {safe_limit}
            """
        else:
            query = f"""
                SELECT
                    cs.cancer_study_identifier,
                    cs.name,
                    cs.description,
                    cs.type_of_cancer_id,
                    COUNT(DISTINCT cd.sample_unique_id) as sample_count
                FROM cancer_study cs
                LEFT JOIN clinical_data_derived cd ON cs.cancer_study_identifier = cd.cancer_study_identifier
                GROUP BY cs.cancer_study_identifier, cs.name, cs.description, cs.type_of_cancer_id
                ORDER BY sample_count DESC
                LIMIT {safe_limit}
            """

        results = run_select_query(query)

        # Add has_guide field
        for study in results:
            study_id = study.get("cancer_study_identifier", "")
            study["has_guide"] = study_id in available_guides

        return results

    except Exception as e:
        logger.error(f"list_studies error: {e}")
        return [{"error": str(e)}]


@mcp.tool()
def list_study_guides() -> list[str]:
    """List all studies that have pre-generated guides available.

    Returns:
        List of study identifiers that have curated guides in resources/study-guides/
    """
    return _list_available_study_guides()


@mcp.tool()
def search_oncotree(search_term: str) -> list[dict]:
    """Search OncoTree cancer types by code, name, or tissue.

    Use this BEFORE querying cancer type data to resolve abbreviations and
    find the correct OncoTree codes used in the type_of_cancer table.

    Handles deprecated codes (e.g. "ALL" → BLL + TLL via revocations).
    Returns up to 25 results ranked by relevance.

    Args:
        search_term: Cancer type code, name, or abbreviation to search for
    """
    entries = _load_oncotree_data()
    if not entries:
        return [{"error": "OncoTree data not available"}]

    term_lower = search_term.strip().lower()
    if not term_lower:
        return [{"error": "search_term cannot be empty"}]

    entries_by_code = {e["code"]: e for e in entries if "code" in e}

    scored: list[tuple[int, dict]] = []
    for entry in entries:
        code = entry.get("code", "")
        code_lower = code.lower()
        name = entry.get("name", "")
        name_lower = name.lower()
        main_type = entry.get("mainType", "")
        main_type_lower = main_type.lower()
        tissue = entry.get("tissue", "")
        tissue_lower = tissue.lower()
        revocations = [r.lower() for r in entry.get("revocations", [])]
        precursors = [p.lower() for p in entry.get("precursors", [])]

        score = 0

        # Exact code match (highest priority)
        if term_lower == code_lower:
            score = 100
        # Revoked/deprecated code match
        elif term_lower in revocations or term_lower in precursors:
            score = 90
        # Exact name match
        elif term_lower == name_lower:
            score = 80
        # Code starts with search term
        elif code_lower.startswith(term_lower):
            score = 70
        # Exact mainType match
        elif term_lower == main_type_lower:
            score = 65
        # Name starts with search term
        elif name_lower.startswith(term_lower):
            score = 60
        # Partial name/mainType match
        elif term_lower in name_lower:
            score = 50
        elif term_lower in main_type_lower:
            score = 45
        # Tissue match
        elif term_lower in tissue_lower:
            score = 40

        if score > 0:
            result = {
                "code": code,
                "name": name,
                "score": score,
            }
            if main_type:
                result["mainType"] = main_type
            if tissue:
                result["tissue"] = tissue

            # Build hierarchy path
            path = _build_hierarchy_path(code, entries_by_code)
            if path:
                result["hierarchy"] = path

            # Show what deprecated codes this replaces
            replaced = entry.get("revocations", []) + entry.get("precursors", [])
            if replaced:
                result["replacedCodes"] = replaced

            scored.append((score, result))

    # Sort by score desc, then by code for stability
    scored.sort(key=lambda x: (-x[0], x[1]["code"]))
    return [item for _, item in scored[:25]]


# --- Survival / Kaplan-Meier UI app -----------------------------------------


@mcp.resource(
    uri=ui.SURVIVAL_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Kaplan-Meier Survival Widget",
    description="Self-contained HTML widget that renders a Kaplan-Meier survival curve.",
)
def survival_widget() -> str:
    return ui.load_widget("survival.html")


@mcp.tool(
    app=ui.app_config(ui.SURVIVAL_UI_URI),
    description="""
    Generate an interactive Kaplan-Meier survival curve for one cBioPortal study,
    several studies, or a named study set (e.g. the TCGA PanCancer Atlas cohort).

    Returns structured survival data AND renders an embedded KM chart in
    supporting clients (MCP Apps / io.modelcontextprotocol/ui extension).

    Survival is computed at the **patient** level. Split the cohort in ONE way:
    group_by_gene (one gene, altered vs wild-type), group_by_clinical (an
    attribute's values), groups (2-4 groups defined by alteration conditions:
    co-mutation, codon ranges, exclusions, wild-type) or group_by_expression
    (quantile groups of a gene's mRNA level). If a request fits none of these,
    say so rather than running a different comparison.

    Args:
        study_id: One study identifier (e.g. "brca_tcga_pan_can_atlas_2018").
        endpoint: Survival endpoint — one of OS, PFS, DFS, DSS (default: OS)
        group_by_gene: A single Hugo gene symbol: altered vs wild-type.
        alteration_types: What counts as altered for group_by_gene. Subset of:
                          mutation, amplification, deep_deletion,
                          structural_variant (default: mutation).
        group_by_clinical: Clinical attribute name to split the cohort
                           (e.g. "SUBTYPE", "ER_STATUS").
        groups: 2-4 group definitions, each {"name": str, "altered": OQL | [OQL],
                "unaltered": OQL | [OQL]}. A patient is in a group when EVERY
                "altered" track has a qualifying event in one of their samples and
                NO "unaltered" track does ("unaltered" also requires the patient to
                have been profiled for that gene). Patients matching several groups
                are excluded from all of them. One OQL track per string:
                "TP53: MUT", "KRAS: MUT = G12D", "TP53: MUT = (1-40)" (changes
                overlapping codons 1-40), "TP53: MUT = (41-)", "TP53: TRUNC",
                "EGFR: MUT != T790M", "CDKN2A: HOMDEL", "[BRCA1 BRCA2]" (either).
                Co-mutation vs single mutation:
                  [{"name": "TP53+KRAS", "altered": ["TP53: MUT", "KRAS: MUT"]},
                   {"name": "KRAS only", "altered": "KRAS: MUT", "unaltered": "TP53: MUT"}]
                Codon region vs rest vs wild-type:
                  [{"name": "TP53 codons 1-40", "altered": "TP53: MUT = (1-40)"},
                   {"name": "TP53 other codons", "altered": "TP53: MUT = (41-)"},
                   {"name": "TP53 wild-type", "unaltered": "TP53: MUT"}]
        group_by_expression: {"gene": "EGFR", "split": "top_vs_bottom_quartile",
                "profile": optional profile_type}. split is one of median,
                tertiles, quartiles, top_vs_bottom_quartile, top_quartile_vs_rest.
                Cut-offs are computed within each study over the patients with both
                expression and survival data and returned in grouping.cutoffs; the
                profile defaults to continuous RNA-seq mRNA, else z-scores
                (grouping.profiles says which was used).
        stratify_by: Clinical attribute (e.g. "CANCER_TYPE") or "STUDY". Runs a
                stratified log-rank test, comparing groups only within each stratum,
                which adjusts for that factor. Use it whenever the cohort mixes
                cancer types or studies. 'stats' then holds the stratified test,
                'stats_unstratified' the plain one, and 'stratification' says how.
                Without it the test is NOT adjusted, and 'stratification' is null.
        cohort: Optional cohort filter restricting WHICH patients are analysed,
                as one attribute mapped to the values to match, e.g.
                {"CANCER_TYPE": ["Breast Cancer"]}. Matching is
                case-insensitive. Use this for questions about a subset of a
                multi-cancer study ("breast cancer in MSK-CHORD") - without it
                the analysis covers the whole study.
        studies: Several study identifiers analysed together (patients pooled).
        preference: A named study set, e.g. "pan_cancer_tcga" (the 32 TCGA
                PanCancer Atlas studies) or "all_studies_non_redundant". Pass
                study_id OR studies/preference.

    Returns:
        Structured JSON with per-group KM curves, medians, patient/event counts,
        at-risk tables, a log-rank test result when 2+ groups are present, a
        'grouping' block describing exactly how groups were formed, a 'scope'
        and 'cohort' block stating which patients the curves describe, and a
        'provenance' block with the SQL that produced them.

        Each curve point carries ci_lower/ci_upper, a 95% POINTWISE confidence
        band (Greenwood variance, log-log transform), shaded in the chart. The
        band is not simultaneous over time, and two curves whose bands overlap
        have NOT thereby been tested for a difference - report the log-rank
        p-value for that. Bands widen sharply once few patients remain at risk,
        so curves that separate only in the tail are usually noise; say so
        rather than reading the separation as a finding.

        Long curves are binned down to 200 points for transport and flagged
        with 'curve_binned'. Survival/CI/at-risk values stay exact at each
        reported time and 'events'/'censored' are summed over the preceding
        interval, so per-instant event counts are not recoverable from a binned
        curve. Medians, at-risk tables and the log-rank p-value always come
        from the full data.
""",
)
def survival_curve(
    study_id: str | None = None,
    endpoint: str = "OS",
    group_by_gene: str | None = None,
    alteration_types: list[str] | None = None,
    group_by_clinical: str | None = None,
    cohort: dict[str, list[str]] | None = None,
    groups: list[dict] | None = None,
    group_by_expression: dict | None = None,
    stratify_by: str | None = None,
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    # Error returns keep the contract shape (endpoint + empty groups) so the
    # widget recognizes and renders them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "endpoint": (endpoint or "OS").upper(), "groups": []}

    try:
        if study_id and not (studies or preference):
            study_id = _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))

    try:
        return _with_provenance(
            lambda: _build_survival_payload(
                study_id=_resolve_scope(study_id, studies, preference),
                endpoint=endpoint,
                group_by_gene=group_by_gene,
                alteration_types=alteration_types or ["mutation"],
                group_by_clinical=group_by_clinical,
                cohort=cohort,
                groups=groups,
                group_by_expression=group_by_expression,
                stratify_by=stratify_by,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("survival_curve error: %s", e)
        return _error(f"Unexpected error computing survival curve: {e}")


# --- OncoPrint UI app --------------------------------------------------------


@mcp.resource(
    uri=ui.ONCOPRINT_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="OncoPrint Widget",
    description="Self-contained HTML widget that renders an OncoPrint alteration matrix.",
)
def oncoprint_widget() -> str:
    return ui.load_widget("oncoprint.html")


@mcp.tool(
    app=ui.app_config(ui.ONCOPRINT_UI_URI),
    description="""
    Generate an interactive OncoPrint (gene x sample alteration matrix) for one
    cBioPortal study, several studies, or a named study set.

    Returns structured alteration data AND renders an embedded OncoPrint in
    supporting clients

    OncoPrint is computed at the **sample** level (columns = samples). Each cell
    shows that sample's alteration in the row: mutation (colored by class, or one
    colour with mutation_classes="collapsed"), copy-number amplification or deep
    deletion, and/or structural variant. A gray cell marks a sample not profiled
    for that gene (gene-panel coverage).

    Rows are either plain genes (genes + alteration_types) or tracks written in
    cBioPortal's Onco Query Language (oql), which adds merged tracks, mutation
    classes, protein changes, codon ranges and exclusions:
      "TP53 KRAS"                          two genes, all alteration types
      "EGFR: MUT != T790M MUT != L858R"    every EGFR mutation except T790M and L858R
      "[\"SWI/SNF\" SMARCA4 SMARCB1 ARID1A]" one merged row: any of the three altered
      "[SMARCA4: TRUNC; SMARCB1: TRUNC]"   merged truncating mutations (TRUNC = nonsense,
                                           frameshift, splice, nonstop, nonstart)
      "TP53: MISSENSE; KRAS: MUT = G12D"   mutation class / protein change
      "EGFR: MUT = (712-979)"              changes overlapping codons 712-979
    Separate gene lines with ";" or newlines. DRIVER / _DRIVER is accepted only for
    studies that ship driver annotations (almost none do; the tool errors instead of
    returning an empty "driver" track). EXP / PROT / GAIN / HETLOSS are not supported.
    The payload echoes the parsed 'query', and 'exclusions' reports how many events
    and samples each != removed -- quote those counts rather than asserting that an
    exclusion was applied.

    Args:
        study_id: One study identifier (e.g. "brca_tcga_pan_can_atlas_2018").
        genes: Hugo gene symbols for the matrix rows. Optional; defaults to the
               study's most-altered genes. Clamped to 25 rows. Not with oql.
        alteration_types: Which alterations to include with genes. Subset of:
                          mutation, amplification, deep_deletion,
                          structural_variant (default: all four).
        clinical_tracks: Clinical attribute names shown as annotation tracks below
                         the matrix (e.g. "CANCER_TYPE", "SAMPLE_TYPE"). Optional;
                         pass [] for none.
        max_samples: Max sample columns to render (default and hard cap 500).
                     Altered samples are prioritized; the view is truncated for
                     larger cohorts (per-gene % still reflects all samples).
        cohort: Optional cohort filter restricting WHICH samples are analysed, as
                one attribute mapped to the values to match, e.g.
                {"CANCER_TYPE": ["Breast Cancer"]}. Matching is case-insensitive.
                Applied to the profiled denominator as well as the alterations,
                so per-gene percentages stay correct. Note this filters the
                cohort; it is not the same as clinical_tracks, which only
                display an attribute.
        oql: OQL query defining the rows (see above), instead of genes /
             alteration_types. At most 25 tracks; order is kept.
        mutation_classes: "detailed" (default: missense / truncating / inframe /
             other) or "collapsed" (one 'mutation' class, for a simpler legend).
        studies: Several study identifiers analysed together.
        preference: A named study set, e.g. "pan_cancer_tcga" (TCGA PanCancer
             Atlas, 32 studies) or "all_studies_non_redundant". Pass study_id OR
             studies/preference.

    Returns:
        Structured JSON: row labels ('genes'), sample columns (MemoSort order), a
        sparse alteration-cell map, not-profiled cells, per-row frequency stats,
        clinical tracks, the parsed 'query' / 'tracks' / 'exclusions' when oql is
        used, 'scope' and 'cohort' blocks stating which samples the matrix
        describes, and a 'provenance' block with the SQL that produced it.
""",
)
def oncoprint(
    study_id: str | None = None,
    genes: list[str] | None = None,
    alteration_types: list[str] | None = None,
    clinical_tracks: list[str] | None = None,
    max_samples: int | None = None,
    cohort: dict[str, list[str]] | None = None,
    oql: str | None = None,
    mutation_classes: str = "detailed",
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    # Error returns keep the contract shape (study_id + empty genes/samples) so
    # the widget recognizes and renders them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "study_id": study_id, "genes": [], "samples": []}

    try:
        if study_id and not (studies or preference):
            _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))

    try:
        return _with_provenance(
            lambda: _build_oncoprint_payload(
                study_id=_resolve_scope(study_id, studies, preference),
                genes=genes,
                alteration_types=alteration_types,
                clinical_tracks=clinical_tracks,
                max_samples=max_samples,
                cohort=cohort,
                oql=oql,
                mutation_classes=mutation_classes,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("oncoprint error: %s", e)
        return _error(f"Unexpected error computing OncoPrint: {e}")


# --- Mutation lollipop UI app ------------------------------------------------


@mcp.resource(
    uri=ui.LOLLIPOP_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Mutation Lollipop Widget",
    description="HTML widget that renders a mutation lollipop diagram for one gene.",
)
def lollipop_widget() -> str:
    return ui.load_widget("lollipop.html")


@mcp.tool(
    app=ui.app_config(ui.LOLLIPOP_UI_URI, connect_domains=[ui.GENOME_NEXUS_ORIGIN]),
    description="""
    Generate an interactive mutation lollipop diagram for a single gene in one
    cBioPortal study, several studies, or a named study set (e.g. the TCGA
    PanCancer Atlas cohort or the non-redundant study set).

    Returns structured per-mutation data AND renders an embedded lollipop plot in
    supporting clients.

    A lollipop plot shows each distinct protein change as a "stick" at its codon
    position along the protein; the head is sized/labeled by how many samples carry
    it (recurrence) and colored by mutation class (missense, truncating, inframe,
    other). The protein backbone and its Pfam domains are drawn from data the
    widget fetches live from Genome Nexus.

    Mutations are counted at the **sample** level (one count per sample carrying the
    change; somatic + germline, excluding UNCALLED). Positions are parsed from the
    protein-change notation; changes without a codon position (e.g. some splice
    variants) are summarized in `unmapped_count` but not plotted.

    To count or list only mutations inside a protein domain ("EGFR mutations in the
    tyrosine kinase domain"), pass domain: the server resolves it against the Pfam
    domains of the gene's canonical transcript (the same source the widget draws)
    and the payload's 'region' block reports the codons used and how many mutated
    samples fall inside vs anywhere in the gene. Nucleotide / codon-level detail is
    not in this tool: use nucleotide_variants.

    Args:
        study_id: One study identifier (e.g. "brca_tcga_pan_can_atlas_2018").
        gene: A single Hugo gene symbol (e.g. "TP53", "PIK3CA", "EGFR").
        cohort: Optional cohort filter restricting WHICH samples are counted, as
                one attribute mapped to the values to match, e.g.
                {"CANCER_TYPE": ["Breast Cancer"]}. Matching is case-insensitive.
        domain: Optional Pfam domain to restrict to: an accession ("PF07714"), a
                Pfam name ("PK_Tyr_Ser-Thr"), or words from its description
                ("tyrosine kinase"). Ambiguous or unknown names return an error
                listing the gene's domains and their codons.
        protein_range: Optional [start_codon, end_codon] to restrict to instead of
                a domain. A change counts when its codon span overlaps the range.
        studies: Several study identifiers analysed together.
        preference: A named study set, e.g. "pan_cancer_tcga" (TCGA PanCancer
                Atlas, 32 studies) or "all_studies_non_redundant" (cBioPortal's
                curated non-redundant set). Pass study_id OR studies/preference.

    Returns:
        Structured JSON: the gene, a list of distinct protein changes
        (protein_change, position, count, class, types), per-class counts, the
        number of mutated samples, the highest observed position, the count of
        unplottable mutations, 'region' (null unless domain/protein_range),
        'by_study' for multi-study scopes, 'scope' and 'cohort' blocks stating
        which samples the counts describe, and a 'provenance' block with the SQL
        that produced them.
""",
)
def mutation_diagram(
    study_id: str | None = None,
    gene: str | None = None,
    cohort: dict[str, list[str]] | None = None,
    domain: str | None = None,
    protein_range: list[int] | None = None,
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    # Error returns keep the contract shape (study_id + gene + empty mutations) so
    # the widget recognizes and renders them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "study_id": study_id, "gene": gene, "mutations": []}

    try:
        if study_id and not (studies or preference):
            _validate_study_id(study_id)
        if not gene:
            raise ValueError("gene is required (one Hugo gene symbol, e.g. 'TP53').")
        validated_gene = _validate_gene_symbol(gene)
    except ValueError as e:
        return _error(str(e))

    try:
        return _with_provenance(
            lambda: _build_lollipop_payload(
                _resolve_scope(study_id, studies, preference),
                validated_gene,
                cohort,
                domain=domain,
                protein_range=protein_range,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("mutation_diagram error: %s", e)
        return _error(f"Unexpected error computing mutation lollipop: {e}")


# --- Alteration co-occurrence UI app -----------------------------------------


@mcp.resource(
    uri=ui.COOCCURRENCE_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Alteration Co-occurrence Widget",
    description="HTML widget that renders a pairwise alteration co-occurrence heatmap.",
)
def cooccurrence_widget() -> str:
    return ui.load_widget("cooccurrence.html")


@mcp.tool(
    app=ui.app_config(ui.COOCCURRENCE_UI_URI),
    description="""
    Analyze pairwise alteration co-occurrence and mutual exclusivity among genes,
    or among merged gene sets such as pathways, in a cBioPortal study, several
    studies, or a named study set.

    Returns structured per-pair statistics AND renders an embedded co-occurrence
    heatmap in supporting clients.

    For every pair (of genes, or of OQL tracks), a 2x2 contingency table is built
    over the samples profiled for both (both altered / only one altered /
    neither), scored with a two-sided Fisher exact test and a log2 odds ratio.
    Positive = the alterations tend to co-occur; negative = mutually exclusive.
    P-values are Benjamini-Hochberg corrected to q-values across all pairs, and a
    pair is flagged significant at q < 0.05.

    Confounding: in a cohort mixing tumour types, two genes common in the same
    cancer type look co-occurring and genes common in different cancer types look
    mutually exclusive with no real interaction. Pass stratify_by="CANCER_TYPE" to
    adjust: each pair then gets one 2x2 table per cancer type, tested with the exact
    conditional test (Fisher's test generalized to strata) or the Cochran-Mantel-
    Haenszel test for large tables, with a Mantel-Haenszel odds ratio. The payload's
    'stratification' block is null when results are NOT adjusted; never describe an
    unstratified result as adjusted for tumour type.

    Pathways: pass tracks with merged gene sets, e.g.
      tracks=['["Cell cycle" CDKN2A CDK4 CDK6 CCND1 RB1]',
              '["HR repair" BRCA1 BRCA2 PALB2 ATM RAD51C]']
    A merged track is altered in a sample when any of its genes is, and is tested
    only on samples profiled for all of its genes. Pathway membership is whatever
    you pass -- state the gene lists you used when reporting.

    Args:
        study_id: One study identifier (e.g. "brca_tcga_pan_can_atlas_2018").
        genes: Optional list of Hugo gene symbols (2-12). If omitted (and no
            tracks), the most-altered genes are used -- that is NOT a genome-wide
            search (use alteration_enrichment for that).
        alteration_types: Optional alteration types to count as "altered" with
            genes (any of "mutation", "amplification", "deep_deletion",
            "structural_variant"). Defaults to all four.
        cohort: Optional cohort filter restricting WHICH samples are tested, as
            one attribute mapped to the values to match, e.g.
            {"CANCER_TYPE": ["Breast Cancer"]}. Matching is case-insensitive.
            Applied to each pair's profiled denominator too, so the contingency
            tables stay correct.
        tracks: 2-12 OQL tracks instead of genes, as a list of strings (or one
            ";"-separated string): merged pathway tracks "[...]", or gene lines
            such as "TP53: MUT", "KRAS: MUT = G12D", "CDKN2A: HOMDEL".
        stratify_by: Clinical attribute to adjust for (normally "CANCER_TYPE"),
            or "STUDY" for a multi-study scope.
        studies: Several study identifiers analysed together.
        preference: A named study set, e.g. "pan_cancer_tcga". Pass study_id OR
            studies/preference.

    Returns:
        Structured JSON: the gene/track labels, per-label altered/profiled counts,
        a list of pairs (gene_a, gene_b, n_both, n_a_only, n_b_only, n_neither,
        n_profiled, log2_odds_ratio, p_value, q_value, tendency, significant; with
        stratify_by also test, n_strata_informative and the unadjusted 'crude'
        result), 'stratification' (null = unadjusted), 'query'/'tracks' when
        tracks are used, 'scope' and 'cohort' blocks stating which samples the
        tests describe, and a 'provenance' block with the SQL that produced them.
""",
)
def alteration_cooccurrence(
    study_id: str | None = None,
    genes: list[str] | None = None,
    alteration_types: list[str] | None = None,
    cohort: dict[str, list[str]] | None = None,
    tracks: list[str] | str | None = None,
    stratify_by: str | None = None,
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    # Error returns keep the contract shape (study_id + empty genes/pairs) so the
    # widget recognizes and renders them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "study_id": study_id, "genes": [], "pairs": []}

    try:
        if study_id and not (studies or preference):
            _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))

    try:
        return _with_provenance(
            lambda: _build_cooccurrence_payload(
                _resolve_scope(study_id, studies, preference),
                genes,
                alteration_types,
                cohort,
                tracks=tracks,
                stratify_by=stratify_by,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("alteration_cooccurrence error: %s", e)
        return _error(f"Unexpected error computing co-occurrence: {e}")


# --- Genome-wide alteration enrichment ---------------------------------------


@mcp.tool(
    description="""
    Genome-wide scan: which genes are altered more often in one group of samples
    than in another. Use it for "genes mutated in TP53 wild-type but not TP53-mutant
    tumours", "alterations enriched in metastases vs primaries", "what co-occurs with
    or is mutually exclusive with KRAS across all genes" -- any question asking for
    a list of genes that has to come from a scan rather than a named gene list.

    For every gene altered in at least min_altered samples, a 2x2 table (altered /
    not, group A / group B) is built over the samples of each group profiled for
    that gene (gene-panel aware), tested with a two-sided Fisher exact test, and the
    p-values are Benjamini-Hochberg corrected across all tested genes. The genes that
    define the groups are not tested. Report hits with their counts, frequencies and
    q-values, and keep the caveats: an enrichment is an association, not evidence of
    synthetic lethality; pooled cancer types confound it (use stratify_by); a group
    with a higher mutation burden makes long passenger genes look enriched
    (alteration_burden + warnings).

    Args:
        group_a: Group A as an OQL alteration ("TP53: MUT", "KRAS: MUT = G12C",
            "[BRCA1 BRCA2]"), a list of OQL strings that must all hold, or a
            clinical filter {"SAMPLE_TYPE": ["Metastasis"]}.
        study_id: One study identifier.
        group_b: Group B in the same forms. Default: for an OQL group A, samples
            profiled for its genes that do NOT meet it (e.g. TP53 wild-type); for a
            clinical group A, samples with any other value of that attribute.
        alteration: What is counted per gene: mutation (default), amplification,
            deep_deletion or structural_variant.
        stratify_by: "CANCER_TYPE" (or another clinical attribute, or "STUDY") to
            adjust every gene's test for that factor (exact conditional / CMH test,
            Mantel-Haenszel odds ratio). Strongly advised for pan-cancer cohorts.
        cohort: Optional filter restricting both groups, e.g.
            {"CANCER_TYPE": ["Non-Small Cell Lung Cancer"]}.
        min_altered: Genes altered in fewer samples (both groups) are not tested
            (default 5).
        max_results: How many of the most significant genes to return (default 50,
            max 200); n_significant always counts every tested gene.
        direction: "any" (default), "A" (only genes more often altered in group A)
            or "B" (only genes more often altered in group B), e.g. "B" for genes
            enriched in TP53 wild-type when group_a is "TP53: MUT".
        genes: Optional gene symbols to test instead of every gene, e.g. ["TP53"]
            for "is TP53 mutated more often in metastases than primaries?". Named
            genes are always reported (min_altered is ignored); BH runs over them.
        studies: Several study identifiers analysed together.
        preference: A named study set, e.g. "pan_cancer_tcga". Pass study_id OR
            studies/preference.

    Returns:
        JSON: groups (definition, n_samples), genes[] (gene, altered_a, profiled_a,
        pct_a, altered_b, profiled_b, pct_b, log2_odds_ratio, p_value, q_value,
        enriched_in; with stratify_by also test and crude_* fields), n_genes_tested,
        n_significant (+ by direction), excluded_genes, alteration_burden,
        stratification (null = unadjusted), scope, cohort, warnings, notes and
        provenance with the SQL that ran.
""",
)
def alteration_enrichment(
    group_a: str | list[str] | dict,
    study_id: str | None = None,
    group_b: str | list[str] | dict | None = None,
    alteration: str = "mutation",
    stratify_by: str | None = None,
    cohort: dict[str, list[str]] | None = None,
    min_altered: int = ENRICHMENT_DEFAULT_MIN_ALTERED,
    max_results: int = 50,
    direction: str = "any",
    genes: list[str] | None = None,
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": ENRICHMENT_KIND, "genes": []}

    try:
        if study_id and not (studies or preference):
            _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))
    try:
        return _with_provenance(
            lambda: _build_enrichment_payload(
                _resolve_scope(study_id, studies, preference),
                group_a,
                group_b,
                alteration,
                stratify_by,
                cohort,
                min_altered,
                max_results,
                direction,
                genes,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("alteration_enrichment error: %s", e)
        return _error(f"Unexpected error computing enrichment: {e}")


# --- Cross-study alteration frequency (meta-analysis) UI app -----------------


@mcp.resource(
    uri=ui.FOREST_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Cross-Study Forest Plot Widget",
    description=(
        "HTML widget that renders a forest plot of a gene's alteration frequency "
        "across studies with the pooled random-effects estimate."
    ),
)
def forest_widget() -> str:
    return ui.load_widget("forest.html")


@mcp.tool(
    app=ui.app_config(ui.FOREST_UI_URI),
    description="""
    Compare a gene's alteration frequency ACROSS several cBioPortal studies
    (cross-study meta-analysis), e.g. "TP53 in lung adenocarcinoma across
    MSK-CHORD and TCGA" or "KRAS in all lung adenocarcinoma studies".

    Returns structured per-study statistics AND renders an embedded forest plot
    in supporting clients (MCP Apps / io.modelcontextprotocol/ui extension).

    Use this instead of running one query per study and combining the numbers
    yourself. Returns one row per study (its own cohort, its own panel-aware
    denominator, Wilson 95% CI), then a DerSimonian-Laird random-effects pooled
    frequency with heterogeneity (Q, I², τ²) and a test of whether the studies
    differ (k×2 chi-square, or Fisher's exact for two small studies). It never
    sums counts across studies. Studies that share patients (MSK-CHORD is a
    subset of MSK-IMPACT-50k; the TCGA releases of one cohort overlap) are
    detected from the data: the smaller study is kept out of the pooling and
    the pair is reported.

    Args:
        gene: Hugo gene symbol (e.g. "TP53").
        studies: Study identifiers to compare (resolve names with list_studies).
            "TCGA" for one disease normally means its *_tcga_pan_can_atlas_2018
            study; do not pass several releases of the same cohort.
        preference: A named study set from cancer_study_query_preferences
            (e.g. "pan_cancer_tcga", "all_studies_non_redundant"); unioned with
            `studies`. At least one of `studies` / `preference` is required.
        cancer_type: OncoTree code(s) to restrict every study to, e.g. "LUAD"
            (resolve with search_oncotree first). Matched per sample on
            ONCOTREE_CODE, with CANCER_TYPE_DETAILED and study-level fallbacks
            reported per row as `cohort_key`. Without it each study is taken
            whole, and multi-cancer studies are flagged in `warnings`.
        include_subtypes: Expand the code(s) to their OncoTree descendants
            (default True; "NSCLC" then covers LUAD, LUSC, ...).
        cohort: Optional extra filter, one clinical attribute mapped to the
            values to match, e.g. {"SAMPLE_TYPE": ["Primary"]}; applied inside
            every study and ANDed with cancer_type.
        alteration: One of mutation (default), amplification, deep_deletion,
            structural_variant. Denominators use the matching profiling type.
        unit: "sample" (default; matches cBioPortal's study view) or "patient"
            (use for prevalence / fraction-of-patients questions). Both grains
            are always returned per study.
        min_profiled: Studies with fewer profiled samples/patients are shown
            but excluded from pooling and the test (default 10).
        pool: Set False to withhold the pooled estimate (rows, heterogeneity and
            the test are still returned).

    Returns:
        Structured JSON: `studies[]` (study_id, name, cohort_key, samples{cohort,
        profiled, altered}, patients{...}, frequency_pct, ci95, weight_pct, panels,
        sample_types, status ∈ included | below_min_profiled | not_covered |
        overlap), `studies_without_cohort[]`, `pooled` (random-effects
        frequency_pct + ci95, fixed-effect for reference, crude n_altered /
        n_profiled), `heterogeneity` (q, df, p_value, i2_pct, tau2),
        `difference_test` (test, statistic, df, p_value), `overlap` (pairs,
        excluded), `warnings`, `notes`, and `provenance` with the SQL that ran.
        Report per-study rates with counts as the headline; the pooled value is
        a meta-analytic summary, never a sum.
""",
)
def cross_study_alteration_frequency(
    gene: str,
    studies: list[str] | None = None,
    preference: str | None = None,
    cancer_type: str | list[str] | None = None,
    include_subtypes: bool = True,
    cohort: dict[str, list[str]] | None = None,
    alteration: str = "mutation",
    unit: str = "sample",
    min_profiled: int = CROSS_STUDY_DEFAULT_MIN_PROFILED,
    pool: bool = True,
) -> dict:
    # Error returns keep the contract shape (kind + gene + empty studies) so a
    # widget can recognize and render them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "kind": CROSS_STUDY_KIND, "gene": gene, "studies": []}

    try:
        return _with_provenance(
            lambda: _build_cross_study_payload(
                gene=gene,
                studies=studies,
                preference=preference,
                cancer_type=cancer_type,
                include_subtypes=include_subtypes,
                cohort=cohort,
                alteration=alteration,
                unit=unit,
                min_profiled=min_profiled,
                pool=pool,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("cross_study_alteration_frequency error: %s", e)
        return _error(f"Unexpected error computing cross-study frequency: {e}")


# --- Generic chart UI apps (pie / bar / line) -------------------------------
#
# Unlike the survival/oncoprint apps, these do NOT query the database. The caller
# (the model) supplies the data to plot directly, so the tools only validate and
# normalize it into each widget's data contract. That makes them generic
# visualization primitives the model can point at any data it already has (for
# example, counts it computed from another cBioPortal tool).

# Caller-supplied data is clamped so payloads and the rendered widget stay bounded.
MAX_CHART_SLICES = 50
MAX_CHART_CATEGORIES = 50
MAX_CHART_SERIES = 12
MAX_CHART_POINTS = 500

# Colors accepted from callers for chart elements. Restricted to a safe subset
# (hex, rgb()/rgba(), or a basic CSS color name) because the widget assigns them
# to SVG fill attributes; anything else is dropped so a caller-supplied string
# cannot inject styling or markup.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_COLOR_RE = re.compile(
    r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*(?:0|1|0?\.\d+)\s*)?\)$"
)
_CSS_COLOR_NAMES = frozenset(
    {
        "black",
        "white",
        "red",
        "green",
        "blue",
        "yellow",
        "orange",
        "purple",
        "pink",
        "brown",
        "gray",
        "grey",
        "cyan",
        "magenta",
        "teal",
        "navy",
        "olive",
        "maroon",
        "lime",
        "aqua",
        "fuchsia",
        "silver",
        "gold",
        "indigo",
        "violet",
        "coral",
        "salmon",
        "khaki",
        "crimson",
        "turquoise",
        "tomato",
        "steelblue",
        "seagreen",
        "darkorange",
        "transparent",
    }
)


def _coerce_number(value) -> float | None:
    """Best-effort float coercion; returns None for non-numeric/NaN/inf/bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if f != f or f == float("inf") or f == float("-inf"):  # NaN / ±inf
        return None
    return f


def _label_of(value) -> str:
    """String label for an x value; integer-valued floats render without a decimal
    (so a numeric x forced to categorical matches the numeric axis formatting)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _safe_color(value) -> str | None:
    """Return a sanitized color string, or None if missing/unrecognized."""
    if not isinstance(value, str):
        return None
    c = value.strip()
    if not c:
        return None
    if _HEX_COLOR_RE.match(c) or _RGB_COLOR_RE.match(c):
        return c
    if c.lower() in _CSS_COLOR_NAMES:
        return c.lower()
    return None


def _clamp_seq(seq: list, limit: int, warnings: list, noun: str) -> list:
    """Truncate seq to limit, recording a warning when truncation happens."""
    if len(seq) > limit:
        warnings.append(f"Showing the first {limit} {noun} of {len(seq)}.")
        return seq[:limit]
    return seq


def _opt_str(value) -> str | None:
    """Normalize an optional text field: non-empty string or None."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _build_pie_payload(
    slices: list,
    title=None,
    subtitle=None,
    donut: bool = False,
    show_values: bool = True,
    show_percent: bool = True,
) -> dict:
    """Normalize caller-supplied slices into the pie widget's data contract."""
    if not isinstance(slices, list) or not slices:
        raise ValueError("pie_chart requires a non-empty 'slices' list.")

    warnings: list = []
    slices = _clamp_seq(slices, MAX_CHART_SLICES, warnings, "slices")

    clean: list = []
    for i, s in enumerate(slices):
        if not isinstance(s, dict):
            raise ValueError(f"slices[{i}] must be an object with 'label' and 'value'.")
        label = s.get("label")
        label = str(label) if label is not None else f"Slice {i + 1}"
        value = _coerce_number(s.get("value"))
        if value is None:
            warnings.append(f"Dropped slice '{label}' (non-numeric value).")
            continue
        if value < 0:
            warnings.append(f"Dropped slice '{label}' (negative value).")
            continue
        item = {"label": label, "value": value}
        color = _safe_color(s.get("color"))
        if color:
            item["color"] = color
        elif s.get("color") is not None:
            warnings.append(f"Ignored unrecognized color for slice '{label}'.")
        clean.append(item)

    if not clean:
        raise ValueError("pie_chart: no valid slices after parsing values.")

    return {
        "kind": "pie",
        "title": _opt_str(title),
        "subtitle": _opt_str(subtitle),
        "donut": bool(donut),
        "show_values": bool(show_values),
        "show_percent": bool(show_percent),
        "slices": clean,
        "total": sum(item["value"] for item in clean),
        "warnings": warnings,
    }


def _build_bar_payload(
    categories: list,
    series: list,
    title=None,
    subtitle=None,
    x_label=None,
    y_label=None,
    orientation: str = "vertical",
    stacked: bool = False,
    show_values: bool = False,
) -> dict:
    """Normalize caller-supplied categories/series into the bar widget's contract."""
    if not isinstance(categories, list) or not categories:
        raise ValueError("bar_chart requires a non-empty 'categories' list.")
    if not isinstance(series, list) or not series:
        raise ValueError("bar_chart requires a non-empty 'series' list.")
    orientation = (orientation or "vertical").lower()
    if orientation not in ("vertical", "horizontal"):
        raise ValueError("bar_chart 'orientation' must be 'vertical' or 'horizontal'.")

    warnings: list = []
    categories = [
        str(c) for c in _clamp_seq(categories, MAX_CHART_CATEGORIES, warnings, "categories")
    ]
    series = _clamp_seq(series, MAX_CHART_SERIES, warnings, "series")
    n = len(categories)

    clean_series: list = []
    for i, s in enumerate(series):
        if not isinstance(s, dict):
            raise ValueError(f"series[{i}] must be an object with 'name' and 'values'.")
        name = s.get("name")
        name = str(name) if name is not None else f"Series {i + 1}"
        raw_values = s.get("values")
        if not isinstance(raw_values, list):
            raise ValueError(f"series '{name}' must have a 'values' list.")
        coerced = [_coerce_number(v) for v in raw_values]
        if any(v is None for v in coerced):
            warnings.append(f"Series '{name}': some non-numeric values replaced with 0.")
        values = [v if v is not None else 0.0 for v in coerced]
        if len(values) < n:
            warnings.append(f"Series '{name}' padded with zeros to {n} categories.")
            values = values + [0.0] * (n - len(values))
        elif len(values) > n:
            warnings.append(f"Series '{name}' truncated to {n} categories.")
            values = values[:n]
        item = {"name": name, "values": values}
        color = _safe_color(s.get("color"))
        if color:
            item["color"] = color
        elif s.get("color") is not None:
            warnings.append(f"Ignored unrecognized color for series '{name}'.")
        clean_series.append(item)

    return {
        "kind": "bar",
        "title": _opt_str(title),
        "subtitle": _opt_str(subtitle),
        "x_label": _opt_str(x_label),
        "y_label": _opt_str(y_label),
        "orientation": orientation,
        "stacked": bool(stacked),
        "show_values": bool(show_values),
        "categories": categories,
        "series": clean_series,
        "warnings": warnings,
    }


def _build_line_payload(
    series: list,
    x=None,
    title=None,
    subtitle=None,
    x_label=None,
    y_label=None,
    markers: bool = True,
    smooth: bool = False,
) -> dict:
    """Normalize caller-supplied series into the line widget's data contract.

    Each series carries its own x; a shared ``x`` fills in for series that omit
    one, and point indices are the final fallback. If any x is non-numeric the
    whole chart is treated as categorical (all x coerced to strings).
    """
    if not isinstance(series, list) or not series:
        raise ValueError("line_chart requires a non-empty 'series' list.")

    warnings: list = []
    series = _clamp_seq(series, MAX_CHART_SERIES, warnings, "series")
    shared_x = x if isinstance(x, list) else None

    clean_series: list = []
    all_numeric = True
    for i, s in enumerate(series):
        if not isinstance(s, dict):
            raise ValueError(f"series[{i}] must be an object with a 'y' list.")
        name = s.get("name")
        name = str(name) if name is not None else f"Series {i + 1}"
        raw_y = s.get("y")
        if not isinstance(raw_y, list) or not raw_y:
            raise ValueError(f"series '{name}' must have a non-empty 'y' list.")
        coerced_y = [_coerce_number(v) for v in raw_y]
        if any(v is None for v in coerced_y):
            warnings.append(f"Series '{name}': some non-numeric y values replaced with 0.")
        y = [v if v is not None else 0.0 for v in coerced_y]
        y = _clamp_seq(y, MAX_CHART_POINTS, warnings, "points")

        raw_x = s.get("x")
        x_vals = raw_x if isinstance(raw_x, list) else shared_x
        if isinstance(x_vals, list):
            if len(x_vals) < len(y):
                warnings.append(f"Series '{name}': x shorter than y; remainder filled by index.")
                x_vals = list(x_vals) + list(range(len(x_vals), len(y)))
            elif len(x_vals) > len(y):
                x_vals = x_vals[: len(y)]
            coerced_x = [_coerce_number(v) for v in x_vals]
            if all(v is not None for v in coerced_x):
                out_x = coerced_x
            else:
                out_x = [_label_of(v) for v in x_vals]
                all_numeric = False
        else:
            out_x = list(range(len(y)))

        item = {"name": name, "x": out_x, "y": y}
        color = _safe_color(s.get("color"))
        if color:
            item["color"] = color
        elif s.get("color") is not None:
            warnings.append(f"Ignored unrecognized color for series '{name}'.")
        clean_series.append(item)

    if not all_numeric:
        for item in clean_series:
            item["x"] = [_label_of(v) for v in item["x"]]

    return {
        "kind": "line",
        "title": _opt_str(title),
        "subtitle": _opt_str(subtitle),
        "x_label": _opt_str(x_label),
        "y_label": _opt_str(y_label),
        "markers": bool(markers),
        "smooth": bool(smooth),
        "x_is_numeric": all_numeric,
        "series": clean_series,
        "warnings": warnings,
    }


MAX_HISTOGRAM_VALUES = 100_000
_REFERENCE_STATS = ("mean", "median", "q1", "q3", "min", "max")


def _build_histogram_payload(
    values: list,
    bins=None,
    value_range=None,
    title=None,
    subtitle=None,
    x_label=None,
    y_label=None,
    reference_lines=None,
) -> dict:
    """Bin caller-supplied values; every summary is computed here, never supplied."""
    if not isinstance(values, list) or not values:
        raise ValueError("histogram_chart requires a non-empty 'values' list of numbers.")
    if len(values) > MAX_HISTOGRAM_VALUES:
        raise ValueError(f"histogram_chart accepts at most {MAX_HISTOGRAM_VALUES} values.")
    warnings: list = []
    numbers = [_coerce_number(v) for v in values]
    clean = [v for v in numbers if v is not None]
    if len(clean) < len(numbers):
        warnings.append(f"Dropped {len(numbers) - len(clean)} non-numeric value(s).")
    if not clean:
        raise ValueError("histogram_chart: no numeric values to plot.")
    vr = None
    if value_range is not None:
        if not isinstance(value_range, list) or len(value_range) != 2:
            raise ValueError("value_range must be [low, high].")
        lo, hi = _coerce_number(value_range[0]), _coerce_number(value_range[1])
        if lo is None or hi is None:
            raise ValueError("value_range must be two numbers.")
        vr = (lo, hi)
    hist = dstats.histogram(clean, bins=bins, value_range=vr)
    stats = dstats.describe(clean)
    lines = []
    for item in ["mean", "median"] if reference_lines is None else reference_lines:
        if isinstance(item, str):
            key = item.strip().lower()
            if key not in _REFERENCE_STATS:
                raise ValueError(
                    f"Unknown reference line '{item}'; use {', '.join(_REFERENCE_STATS)} or "
                    '{"label": ..., "value": ...}.'
                )
            lines.append({"label": key, "value": stats[key]})
        elif isinstance(item, dict) and _coerce_number(item.get("value")) is not None:
            label = str(item.get("label") or "reference")
            lines.append({"label": label, "value": _coerce_number(item["value"])})
        else:
            raise ValueError("reference_lines entries must be a stat name or {label, value}.")
    if hist["n_outside"]:
        warnings.append(f"{hist['n_outside']} value(s) outside value_range were not binned.")
    return {
        "kind": "histogram",
        "title": _opt_str(title),
        "subtitle": _opt_str(subtitle),
        "x_label": _opt_str(x_label),
        "y_label": _opt_str(y_label) or "Count",
        "bins": [
            {
                "start": round(hist["edges"][i], 6),
                "end": round(hist["edges"][i + 1], 6),
                "count": c,
            }
            for i, c in enumerate(hist["counts"])
        ],
        "stats": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in stats.items()},
        "reference_lines": [
            {"label": ln["label"], "value": round(ln["value"], 6)} for ln in lines
        ],
        "warnings": warnings,
    }


@mcp.resource(
    uri=ui.PIE_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Pie Chart Widget",
    description="Self-contained HTML widget that renders a pie or donut chart.",
)
def pie_chart_widget() -> str:
    return ui.load_widget("charts.html")


@mcp.resource(
    uri=ui.BAR_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Bar Chart Widget",
    description="Self-contained HTML widget that renders a bar chart.",
)
def bar_chart_widget() -> str:
    return ui.load_widget("charts.html")


@mcp.resource(
    uri=ui.LINE_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Line Chart Widget",
    description="Self-contained HTML widget that renders a line chart.",
)
def line_chart_widget() -> str:
    return ui.load_widget("charts.html")


@mcp.tool(
    app=ui.app_config(ui.PIE_UI_URI),
    description="""
    Render a generic pie (or donut) chart from data you supply.

    Generic visualization tool: it does NOT query cBioPortal. Pass the values you
    want to plot (e.g. counts you already computed) and the host renders an
    interactive pie chart; the same data is also returned as structured JSON.

    Args:
        slices: The wedges, as a list of objects, e.g.
                [{"label": "Missense", "value": 42, "color": "#2e8b57"},
                 {"label": "Truncating", "value": 18}].
                "value" must be a non-negative number; "color" is optional (hex
                like "#2e8b57", "rgb(...)", or a basic CSS color name).
        title: Optional chart title.
        subtitle: Optional secondary line under the title.
        donut: Render as a donut (hole in the middle) instead of a full pie.
        show_values: Show each slice's raw value in the legend (default True).
        show_percent: Show each slice's percentage (default True).

    Returns:
        Structured JSON: {kind, title, slices:[{label,value,color?}], total, ...}.
""",
)
def pie_chart(
    slices: list,
    title: str | None = None,
    subtitle: str | None = None,
    donut: bool = False,
    show_values: bool = True,
    show_percent: bool = True,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": "pie", "slices": []}

    try:
        return _build_pie_payload(
            slices=slices,
            title=title,
            subtitle=subtitle,
            donut=donut,
            show_values=show_values,
            show_percent=show_percent,
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("pie_chart error: %s", e)
        return _error(f"Unexpected error building pie chart: {e}")


@mcp.tool(
    app=ui.app_config(ui.BAR_UI_URI),
    description="""
    Render a generic bar chart from data you supply.

    Generic visualization tool: it does NOT query cBioPortal. Supports one or
    several series (grouped or stacked), vertical or horizontal bars.

    Args:
        categories: X-axis category labels, e.g. ["TP53", "KRAS", "PIK3CA"].
        series: One or more series, as a list of objects, e.g.
                [{"name": "Mutated %", "values": [40, 25, 18], "color": "#1f77b4"}].
                Each "values" list lines up with "categories" (shorter/longer
                lists are padded with zeros / truncated). "color" is optional.
        title: Optional chart title.
        subtitle: Optional secondary line under the title.
        x_label: Optional x-axis label.
        y_label: Optional y-axis label.
        orientation: "vertical" (default) or "horizontal".
        stacked: Stack series instead of grouping them side by side.
        show_values: Draw the numeric value on each bar (default False).

    Returns:
        Structured JSON: {kind, categories, series:[{name,values,color?}], ...}.
""",
)
def bar_chart(
    categories: list,
    series: list,
    title: str | None = None,
    subtitle: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    orientation: str = "vertical",
    stacked: bool = False,
    show_values: bool = False,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": "bar", "categories": [], "series": []}

    try:
        return _build_bar_payload(
            categories=categories,
            series=series,
            title=title,
            subtitle=subtitle,
            x_label=x_label,
            y_label=y_label,
            orientation=orientation,
            stacked=stacked,
            show_values=show_values,
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("bar_chart error: %s", e)
        return _error(f"Unexpected error building bar chart: {e}")


@mcp.tool(
    app=ui.app_config(ui.LINE_UI_URI),
    description="""
    Render a generic line chart from data you supply.

    Generic visualization tool: it does NOT query cBioPortal. Supports one or
    several lines over a shared or per-series x-axis (numeric or categorical).

    Args:
        series: One or more lines, as a list of objects, e.g.
                [{"name": "OS", "y": [100, 82, 61, 40], "x": [0, 12, 24, 36],
                  "color": "#1f77b4"}].
                "y" is required (numbers). "x" is optional per series; if omitted
                the shared "x" (below) is used, else point indices 0,1,2,...
                x values may be numbers (e.g. months) or strings (categories).
        x: Optional shared x-axis values for series that don't supply their own,
           e.g. [0, 12, 24, 36] or ["Q1", "Q2", "Q3"].
        title: Optional chart title.
        subtitle: Optional secondary line under the title.
        x_label: Optional x-axis label.
        y_label: Optional y-axis label.
        markers: Draw a marker at each data point (default True).
        smooth: Draw smoothed/curved lines instead of straight segments.

    Returns:
        Structured JSON: {kind, series:[{name,x,y,color?}], x_is_numeric, ...}.
""",
)
def line_chart(
    series: list,
    x: list | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    markers: bool = True,
    smooth: bool = False,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": "line", "series": []}

    try:
        return _build_line_payload(
            series=series,
            x=x,
            title=title,
            subtitle=subtitle,
            x_label=x_label,
            y_label=y_label,
            markers=markers,
            smooth=smooth,
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("line_chart error: %s", e)
        return _error(f"Unexpected error building line chart: {e}")


@mcp.resource(
    uri=ui.HISTOGRAM_UI_URI,
    mime_type=UI_MIME_TYPE,
    name="Histogram Widget",
    description="Self-contained HTML widget that renders a histogram with reference lines.",
)
def histogram_chart_widget() -> str:
    return ui.load_widget("charts.html")


@mcp.tool(
    app=ui.app_config(ui.HISTOGRAM_UI_URI),
    description="""
    Render a histogram (distribution) of numeric values you supply, with reference
    lines such as the mean and median.

    Generic visualization tool: it does NOT query cBioPortal. The bins, counts and
    every statistic drawn or returned (n, mean, sd, quartiles, median) are computed
    by the tool from the values -- never pass your own mean/median numbers. For
    variant allele frequencies of a gene's mutations use mutation_allele_frequency,
    which fetches the values itself. Use this rather than bar_chart for any
    distribution: a bar chart of hand-binned counts is not a histogram.

    Args:
        values: The raw numbers (up to 100,000).
        bins: Number of equal-width bins (default: Freedman-Diaconis, 5-60).
        value_range: Optional [low, high] to bin over (values outside are counted
            in warnings, not drawn).
        title / subtitle / x_label / y_label: Optional text.
        reference_lines: Vertical lines to draw: stat names ("mean", "median",
            "q1", "q3", "min", "max") and/or {"label": str, "value": number}.
            Default ["mean", "median"]; [] for none.

    Returns:
        Structured JSON: {kind: "histogram", bins:[{start,end,count}], stats,
        reference_lines:[{label,value}], ...}.
""",
)
def histogram_chart(
    values: list,
    bins: int | None = None,
    value_range: list | None = None,
    title: str | None = None,
    subtitle: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    reference_lines: list | None = None,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": "histogram", "bins": []}

    try:
        return _build_histogram_payload(
            values, bins, value_range, title, subtitle, x_label, y_label, reference_lines
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("histogram_chart error: %s", e)
        return _error(f"Unexpected error building histogram: {e}")


@mcp.tool(
    app=ui.app_config(ui.HISTOGRAM_UI_URI),
    description="""
    Distribution of variant allele frequency (VAF = tumor alt reads / (alt + ref))
    for a gene's mutations, as a histogram with the mean and median marked. Works on
    one study, several studies, or a named study set such as "pan_cancer_tcga".

    The values come from the mutation read counts in cBioPortal; the tool computes
    the bins, mean, median and quartiles itself. Optionally keep only mutations in
    samples with a given copy-number state of that gene (GISTIC discrete call:
    "diploid" = 0, "gain" = 1, "amplification" = 2, "shallow_deletion" = -1,
    "deep_deletion" = -2). This is gene-level copy number, which is what "diploid
    samples" means in cBioPortal's plots; whole-genome ploidy is not available --
    say so if the user means that. Mutations without read counts or without a
    copy-number call are excluded and counted in 'counts'. Each mutation event is one
    value. group_by adds per-group summaries (e.g. by CANCER_TYPE) to the payload;
    violin or jitter plots are not rendered.

    Args:
        alteration: One OQL mutation track, e.g. "TP53: MISSENSE",
            "KRAS: MUT = G12D", "PIK3CA: MUT" (mutations only).
        study_id: One study identifier.
        copy_number: Optional copy-number state of the gene to require (see above).
        cohort: Optional cohort filter, e.g. {"CANCER_TYPE": ["Breast Cancer"]}.
        bins: Number of bins over [0, 1] (default 20).
        group_by: Optional clinical attribute (or "STUDY") for per-group summaries.
        studies: Several study identifiers analysed together.
        preference: A named study set, e.g. "pan_cancer_tcga" (TCGA PanCancer Atlas).
            Pass study_id OR studies/preference.

    Returns:
        JSON with kind "histogram": bins, stats (n, mean, sd, min, q1, median, q3,
        max), reference_lines (mean, median), counts (matched, excluded, plotted),
        filters (including the copy-number definition and profile used), optional
        groups, scope, cohort, warnings, notes, provenance.
""",
)
def mutation_allele_frequency(
    alteration: str,
    study_id: str | None = None,
    copy_number: str | None = None,
    cohort: dict[str, list[str]] | None = None,
    bins: int = 20,
    group_by: str | None = None,
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": "histogram", "bins": []}

    try:
        if study_id and not (studies or preference):
            _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))
    try:
        return _with_provenance(
            lambda: _build_allele_frequency_payload(
                _resolve_scope(study_id, studies, preference),
                alteration,
                copy_number,
                cohort,
                bins,
                group_by,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("mutation_allele_frequency error: %s", e)
        return _error(f"Unexpected error computing allele frequencies: {e}")


@mcp.tool(
    description="""
    Nucleotide-level detail behind a gene's mutations: the genomic position, the
    reference and tumor alleles and the codon change for each variant, with how many
    samples carry it. Use it for "which codon changes produce BRAF V600E", "how many
    V600E calls are GTG>GAG vs GTG>GAA", or "cases where a GAG codon changed to GAA
    in EGFR". The lollipop (mutation_diagram) is protein-level only.

    Synonymous changes (e.g. GAG>GAA, both glutamate) are usually filtered out of
    cBioPortal studies before loading; when nothing matches, the error says so and
    reports how many silent calls the scope holds, so "0" is not reported as absence.

    Args:
        gene: Hugo gene symbol.
        protein_change: Optional protein change / codon / range / class, e.g. "V600E",
            "p.Val600Glu", "V600", "(712-979)", "MISSENSE".
        codon_change: Optional codon change as "REF>ALT" (e.g. "GAG>GAA"); "GAG>" or
            ">GAA" match one side.
        study_id: One study identifier.
        cohort: Optional cohort filter, e.g. {"CANCER_TYPE": ["Melanoma"]}.
        max_rows: Distinct variants to return (default 50, max 200).
        studies: Several study identifiers analysed together.
        preference: A named study set, e.g. "pan_cancer_tcga". Pass study_id OR
            studies/preference.

    Returns:
        JSON: variants[] (protein_change, codon_change, chromosome, start_position,
        end_position, reference_allele, tumor_allele, variant_class, mutation_type,
        genome_build, refseq_mrna_id, n_samples, n_events, n_studies, pct_of_samples),
        codon_changes[] (codon_change, n_samples, pct_of_samples), n_samples,
        n_events, filters, scope, cohort, warnings, notes, provenance. Genomic alleles
        are on the + strand; codon_change is on the transcript.
""",
)
def nucleotide_variants(
    gene: str,
    protein_change: str | None = None,
    codon_change: str | None = None,
    study_id: str | None = None,
    cohort: dict[str, list[str]] | None = None,
    max_rows: int = 50,
    studies: list[str] | None = None,
    preference: str | None = None,
) -> dict:
    def _error(message: str) -> dict:
        return {"error": message, "kind": NUCLEOTIDE_KIND, "gene": gene, "variants": []}

    try:
        if study_id and not (studies or preference):
            _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))
    try:
        return _with_provenance(
            lambda: _build_nucleotide_payload(
                _resolve_scope(study_id, studies, preference),
                gene,
                protein_change,
                codon_change,
                cohort,
                max_rows,
            )
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("nucleotide_variants error: %s", e)
        return _error(f"Unexpected error computing nucleotide variants: {e}")


if __name__ == "__main__":
    main()
