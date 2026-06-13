#!/usr/bin/env python3
"""cBioPortal MCP Server - FastMCP implementation."""

import json
import logging
import re
import sys
from functools import lru_cache
from importlib import resources as importlib_resources
from importlib.resources.abc import Traversable
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.apps import UI_MIME_TYPE


from cbioportal_mcp.env import get_mcp_config, TransportType
from cbioportal_mcp.authentication.permissions import ensure_db_permissions
from cbioportal_mcp import ui
from cbioportal_mcp.survival_stats import kaplan_meier, logrank_test

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
        # Both pathlib.Path and importlib.resources Traversable expose iterdir()/name
        return [
            f.name.removesuffix(".md")
            for f in study_guides_path.iterdir()
            if f.name.endswith(".md") and not f.name.startswith("_")
        ]
    except Exception as e:
        logger.error(f"Error listing study guides: {e}")
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
    study_id: str, endpoint: str
) -> tuple[dict[str, tuple[float, int]], int]:
    """Fetch per-patient (time, event) observations for an endpoint.

    Survival is per-patient, so this aggregates to ``patient_unique_id``.

    Returns:
        (observations keyed by patient, count of patients dropped for
        missing/unparseable time or status).
    """
    rows = run_select_query(f"""
        SELECT
            patient_unique_id,
            MAX(CASE WHEN attribute_name = '{endpoint}_MONTHS'
                THEN toFloat64OrNull(attribute_value) END) AS time,
            MAX(CASE WHEN attribute_name = '{endpoint}_STATUS'
                THEN attribute_value END) AS status
        FROM clinical_data_derived
        WHERE cancer_study_identifier = '{study_id}'
            AND attribute_name IN ('{endpoint}_MONTHS', '{endpoint}_STATUS')
        GROUP BY patient_unique_id
    """)
    observations: dict[str, tuple[float, int]] = {}
    n_dropped = 0
    for row in rows:
        pid = row.get("patient_unique_id")
        if not pid:
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


def _altered_patients(study_id: str, gene: str, alteration_types: list[str]) -> set[str]:
    """Return the set of patients with a qualifying alteration in ``gene``.

    A patient is "altered" if any of their samples carries one of the requested
    alteration types (alterations are per-sample; aggregated to the patient).
    """
    filters = []
    for alt in alteration_types:
        cfg = _validate_alteration_type(alt)
        filters.append(f"({cfg['event_filter']})")
    combined = " OR ".join(filters)
    rows = run_select_query(f"""
        SELECT DISTINCT patient_unique_id
        FROM genomic_event_derived
        WHERE cancer_study_identifier = '{study_id}'
            AND hugo_gene_symbol = '{gene}'
            AND ({combined})
    """)
    altered: set[str] = set()
    for row in rows:
        pid = row.get("patient_unique_id")
        if pid:
            altered.add(pid)
    return altered


def _clinical_patient_values(study_id: str, attribute: str) -> tuple[dict[str, str], int]:
    """Map each patient to a single value of ``attribute``.

    Patients with conflicting values across samples are excluded (and counted),
    since a single grouping value cannot be assigned.
    """
    rows = run_select_query(f"""
        SELECT DISTINCT patient_unique_id, attribute_value
        FROM clinical_data_derived
        WHERE cancer_study_identifier = '{study_id}'
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


def _build_survival_payload(
    study_id: str,
    endpoint: str,
    group_by_gene: str | None,
    alteration_types: list[str] | None,
    group_by_clinical: str | None,
) -> dict:
    """Assemble the Kaplan-Meier data contract consumed by the survival widget.

    Returns a dict with study/endpoint metadata, one entry per group (curve,
    counts, median), the log-rank result when 2+ groups exist, and warnings.
    """
    endpoint = _validate_endpoint(endpoint)
    warnings: list[str] = []

    observations, n_dropped = _fetch_survival_observations(study_id, endpoint)
    payload: dict = {
        "study_id": study_id,
        "endpoint": endpoint,
        "endpoint_label": SURVIVAL_ENDPOINTS[endpoint],
        "time_unit": "months",
        "grouping": {"type": "none"},
        "groups": [],
        "time_ticks": [],
        "stats": None,
        "warnings": warnings,
        "notes": (
            "Survival is per-patient; alteration status is aggregated to the "
            "patient (any altered sample => altered). 'Wild-type' means no "
            "qualifying alteration among this study's patients with survival "
            "data and is not adjusted for gene-panel coverage."
        ),
    }
    if not observations:
        payload["error"] = (
            f"No {endpoint} survival data found for study '{study_id}'. "
            f"Expected clinical attributes '{endpoint}_MONTHS' and '{endpoint}_STATUS'."
        )
        return payload
    if n_dropped:
        warnings.append(
            f"{n_dropped} patient(s) excluded for missing or unparseable "
            f"{endpoint}_MONTHS / {endpoint}_STATUS."
        )

    # Decide how to split the cohort into groups (gene alteration takes
    # precedence over a clinical attribute; both unset => whole cohort).
    grouped: list[tuple[str, list[tuple[float, int]]]] = []
    if group_by_gene and group_by_clinical:
        warnings.append("Both group_by_gene and group_by_clinical were given; grouping by gene.")
    if group_by_gene:
        gene = _validate_gene_symbol(group_by_gene)
        alt_types = alteration_types or ["mutation"]
        altered = _altered_patients(study_id, gene, alt_types)
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
    elif group_by_clinical:
        attribute = _validate_attribute_name(group_by_clinical)
        pat_values, n_ambiguous = _clinical_patient_values(study_id, attribute)
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
        grouped = [(f"{attribute}: {val}", obs) for val, obs in ordered]
        payload["grouping"] = {"type": "clinical", "attribute": attribute}
    else:
        grouped = [("All patients", list(observations.values()))]

    # Drop empty groups; bail out if nothing is left to plot.
    grouped = [(name, obs) for name, obs in grouped if obs]
    if not grouped:
        payload["error"] = "No patients remained after grouping; nothing to plot."
        return payload

    max_time = max(t for _, obs in grouped for t, _ in obs)
    time_ticks = _survival_time_ticks(max_time)
    payload["time_ticks"] = time_ticks

    groups_out = []
    for name, obs in grouped:
        km = kaplan_meier(obs, time_ticks=time_ticks)
        groups_out.append(
            {
                "name": name,
                "n_patients": km["n_patients"],
                "n_events": km["n_events"],
                "n_censored": km["n_censored"],
                "median_survival": (
                    round(km["median_survival"], 2) if km["median_survival"] is not None else None
                ),
                "curve": [
                    {
                        "time": round(p["time"], 3),
                        "survival": round(p["survival"], 5),
                        "at_risk": p["at_risk"],
                        "events": p["events"],
                        "censored": p["censored"],
                    }
                    for p in km["curve"]
                ],
                "at_risk_at_ticks": km["at_risk_at_ticks"],
            }
        )
    payload["groups"] = groups_out

    if len(grouped) >= 2:
        lr = logrank_test({name: obs for name, obs in grouped})
        if lr.get("p_value") is not None:
            lr["p_value"] = round(lr["p_value"], 6)
            lr["chi_square"] = round(lr["chi_square"], 4)
        payload["stats"] = lr

    return payload


# --- OncoPrint app helpers ---------------------------------------------------

# Max sample columns rendered in the OncoPrint matrix. Studies can have tens of
# thousands of samples; the widget shows altered samples first, then fills with
# unaltered profiled samples up to this cap. Per-gene frequencies in gene_stats
# are computed over the full profiled set, not just the shown columns.
MAX_ONCOPRINT_SAMPLES = 200
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
# carries multiple mutations.
_MUT_CLASS_PRIORITY = {"truncating": 3, "missense": 2, "inframe": 1, "other": 0}


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


def _resolve_oncoprint_genes(study_id: str, genes: list[str] | None) -> list[str]:
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
    rows = run_select_query(f"""
        SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS altered_samples
        FROM genomic_event_derived
        WHERE cancer_study_identifier = '{study_id}'
            AND ({combined})
        GROUP BY hugo_gene_symbol
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
        LIMIT {DEFAULT_ONCOPRINT_GENES}
    """)
    return [r["hugo_gene_symbol"] for r in rows if r.get("hugo_gene_symbol")]


def _fetch_oncoprint_events(
    study_id: str, genes: list[str], alteration_types: list[str]
) -> tuple[dict[str, dict[str, dict]], dict[str, str]]:
    """Fetch per-sample alterations for ``genes`` (study-wide, not just shown).

    Returns ``(cells, sample_to_patient)`` where ``cells[gene][sample]`` is
    ``{"cna": "amp"|"deepdel"|None, "mut": <class>|None, "sv": bool}`` for every
    altered (gene, sample) pair, and ``sample_to_patient`` maps each altered
    sample to its patient (for clinical-track grain resolution).
    """
    gene_list = ", ".join(f"'{g}'" for g in genes)
    combined = _oncoprint_event_filter(alteration_types)
    rows = run_select_query(f"""
        SELECT sample_unique_id, patient_unique_id, hugo_gene_symbol,
               variant_type, mutation_type, cna_alteration
        FROM genomic_event_derived
        WHERE cancer_study_identifier = '{study_id}'
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
        vt = row.get("variant_type")
        if vt == "structural_variant":
            cell["sv"] = True
        elif vt == "cna":
            try:
                cna_val = int(row.get("cna_alteration"))
            except (TypeError, ValueError):
                cna_val = None
            if cna_val == 2:
                cell["cna"] = "amp"
            elif cna_val == -2:
                cell["cna"] = "deepdel"
        elif vt == "mutation":
            cell["mut"] = _more_severe_mut(cell["mut"], _mutation_class(row.get("mutation_type")))
    return cells, sample_to_patient


def _fetch_profiled_samples(study_id: str, genes: list[str]) -> dict[str, set[str]]:
    """Return, per gene, the set of samples profiled for that gene.

    Uses the canonical coverage views: ``mutation_panel_gene_coverage`` (panel
    gene membership) plus ``mutation_wes_coverage`` (WES samples, profiled for
    every gene). This is the documented way to avoid the >100% frequency trap
    from treating WES as a named panel. Covers MUTATION_EXTENDED profiling.
    """
    gene_list = ", ".join(f"'{g}'" for g in genes)
    profiled: dict[str, set[str]] = {g: set() for g in genes}

    panel_rows = run_select_query(f"""
        SELECT sample_unique_id, hugo_gene_symbol
        FROM mutation_panel_gene_coverage
        WHERE cancer_study_identifier = '{study_id}'
            AND hugo_gene_symbol IN ({gene_list})
    """)
    for row in panel_rows:
        sid = row.get("sample_unique_id")
        gene = row.get("hugo_gene_symbol")
        if sid and gene in profiled:
            profiled[gene].add(sid)

    wes_rows = run_select_query(f"""
        SELECT sample_unique_id
        FROM mutation_wes_coverage
        WHERE cancer_study_identifier = '{study_id}'
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
    study_id: str,
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
        rows = run_select_query(f"""
            SELECT sample_unique_id, patient_unique_id, attribute_value, type
            FROM clinical_data_derived
            WHERE cancer_study_identifier = '{study_id}'
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
    study_id: str,
    genes: list[str] | None,
    alteration_types: list[str] | None,
    clinical_tracks: list[str] | None,
    max_samples: int | None,
) -> dict:
    """Assemble the OncoPrint data contract consumed by the widget."""
    warnings: list[str] = []
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
        "study_id": study_id,
        "genes": [],
        "samples": [],
        "alteration_types": alt_types,
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
            "is truncated."
        ),
    }

    genes_resolved = _resolve_oncoprint_genes(study_id, genes)
    if not genes_resolved:
        payload["error"] = (
            f"No genes to display for study '{study_id}'. The study may have no "
            "genomic events, or the named genes have no alterations."
        )
        return payload

    cells, sample_to_patient = _fetch_oncoprint_events(study_id, genes_resolved, alt_types)
    profiled = _fetch_profiled_samples(study_id, genes_resolved)

    # Order rows most-altered first (the staircase); ties keep input order.
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
        payload["error"] = (
            f"No samples found for study '{study_id}' with the selected genes and alteration types."
        )
        return payload

    selected, n_total, truncated = _select_oncoprint_samples(profiled_union, altered_samples, cap)
    selected_set = set(selected)
    payload["n_samples_total"] = n_total
    payload["n_samples_shown"] = len(selected)
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
        study_id, payload["samples"], sample_to_patient, track_attrs
    )
    return payload


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

    transport = config.mcp_server_transport

    try:
        # For HTTP and SSE transports, we need to specify host and port
        http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
        if transport in http_transports:
            # Use the configured bind host (defaults to 127.0.0.1, can be set to 0.0.0.0)
            # and bind port (defaults to 8000)
            mcp.run(transport=transport, host=config.mcp_bind_host, port=config.mcp_bind_port)
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


def _mutation_frequency_guide_text() -> str:
    return _load_resource("mutation-frequency-guide.md")


def _clinical_data_guide_text() -> str:
    return _load_resource("clinical-data-guide.md")


def _sample_filtering_guide_text() -> str:
    return _load_resource("sample-filtering-guide.md")


def _common_pitfalls_guide_text() -> str:
    return _load_resource("common-pitfalls.md")


def _treatment_guide_text() -> str:
    return _load_resource("treatment-guide.md")


def _faq_guide_text() -> str:
    return _load_resource("faq-guide.md")


def _statistical_tests_guide_text() -> str:
    return _load_resource("statistical-tests-guide.md")


def _gene_expression_guide_text() -> str:
    return _load_resource("gene-expression-guide.md")


# --- MCP resources (decorator registers them) --------------------------------
@mcp.resource("cbioportal://mutation-frequency-guide")
def mutation_frequency_guide() -> str:
    return _mutation_frequency_guide_text()


@mcp.resource("cbioportal://clinical-data-guide")
def clinical_data_guide() -> str:
    return _clinical_data_guide_text()


@mcp.resource("cbioportal://sample-filtering-guide")
def sample_filtering_guide() -> str:
    return _sample_filtering_guide_text()


@mcp.resource("cbioportal://common-pitfalls")
def common_pitfalls_guide() -> str:
    return _common_pitfalls_guide_text()


@mcp.resource("cbioportal://treatment-guide")
def treatment_guide() -> str:
    return _treatment_guide_text()


@mcp.resource("cbioportal://faq-guide")
def faq_guide() -> str:
    return _faq_guide_text()


@mcp.resource("cbioportal://statistical-tests-guide")
def statistical_tests_guide() -> str:
    return _statistical_tests_guide_text()


@mcp.resource("cbioportal://gene-expression-guide")
def gene_expression_guide() -> str:
    return _gene_expression_guide_text()


@mcp.tool(
    description="""
    Execute a ClickHouse SQL SELECT query.

    For complex analysis patterns, consult these query guides:
    - cbioportal://mutation-frequency-guide - Gene mutation frequency calculations with proper denominators
    - cbioportal://clinical-data-guide - Patient vs sample-level clinical data queries
    - cbioportal://sample-filtering-guide - Study and sample type filtering strategies
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

    Note: For study-specific guides, use the `get_study_guide(study_id)` tool instead.
    Use `list_studies(search)` to find available studies.
    """
    return [
        {
            "uri": "cbioportal://mutation-frequency-guide",
            "description": "Comprehensive guide for calculating gene mutation frequencies with gene-specific profiling denominators",
        },
        {
            "uri": "cbioportal://clinical-data-guide",
            "description": "Guide for querying clinical data including patient vs sample level considerations",
        },
        {
            "uri": "cbioportal://sample-filtering-guide",
            "description": "Guide for filtering samples and studies in cBioPortal queries",
        },
        {
            "uri": "cbioportal://common-pitfalls",
            "description": "Guide to avoid common mistakes when querying cBioPortal data",
        },
        {
            "uri": "cbioportal://treatment-guide",
            "description": "Guide for querying treatment/clinical event data including drug agents, timelines, and linking to genomic data",
        },
        {
            "uri": "cbioportal://faq-guide",
            "description": "General cBioPortal FAQ: history, how to cite, data types, reference genome, abbreviations, GISTIC thresholds, API access",
        },
        {
            "uri": "cbioportal://statistical-tests-guide",
            "description": "Statistical test selection guide — decision matrix for choosing Fisher's exact, Wilcoxon, chi-squared, t-test, ANOVA, etc. based on data type and group count",
        },
        {
            "uri": "cbioportal://gene-expression-guide",
            "description": "Gene expression / copy-number / methylation analysis. Covers genetic_alteration_derived, profile_type discovery, and the gene_pair_coexpression view for Spearman correlation between two genes",
        },
        {
            "uri": "cbioportal://study-guide/{study_id}",
            "description": "Dynamic study-specific guide - use get_study_guide(study_id) tool to generate",
        },
    ]


@mcp.tool()
def read_guide(uri: str) -> str:
    """Read the content of a specific guide by URI.

    Use this after calling list_guides() to read the detailed content of guides.

    Args:
        uri: The guide URI (e.g., "cbioportal://mutation-frequency-guide")
    """
    # Resource content mapping
    resources = {
        "cbioportal://mutation-frequency-guide": _mutation_frequency_guide_text(),
        "cbioportal://clinical-data-guide": _clinical_data_guide_text(),
        "cbioportal://sample-filtering-guide": _sample_filtering_guide_text(),
        "cbioportal://common-pitfalls": _common_pitfalls_guide_text(),
        "cbioportal://treatment-guide": _treatment_guide_text(),
        "cbioportal://faq-guide": _faq_guide_text(),
        "cbioportal://statistical-tests-guide": _statistical_tests_guide_text(),
        "cbioportal://gene-expression-guide": _gene_expression_guide_text(),
    }

    if uri not in resources:
        available_list = "\n".join(f"  - {r}" for r in resources.keys())
        return (
            f"Resource not found: {uri}.\n"
            f"Available resources:\n{available_list}\n\n"
            "Use list_guides() for descriptions, or get_study_guide(study_id) for study-specific guides."
        )

    return resources[uri]


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
    app=ui.survival_app_config(),
    description="""
    Generate an interactive Kaplan-Meier survival curve for a cBioPortal study.

    Returns structured survival data AND renders an embedded KM chart in
    supporting clients (MCP Apps / io.modelcontextprotocol/ui extension).

    Survival is computed at the **patient** level.
    Groups can be formed by gene-alteration status or a clinical attribute.

    Args:
        study_id: cBioPortal study identifier (e.g. "brca_tcga_pan_can_atlas_2018")
        endpoint: Survival endpoint — one of OS, PFS, DFS, DSS (default: OS)
        group_by_gene: Hugo gene symbol to split the cohort (altered vs wild-type).
                       Requires the study to have genomic data. Optional.
        alteration_types: Which alteration types count as "altered" when using
                          group_by_gene. Subset of: mutation, amplification,
                          deep_deletion, structural_variant (default: mutation).
        group_by_clinical: Clinical attribute name to split the cohort
                           (e.g. "SUBTYPE", "ER_STATUS"). Optional.
                           Ignored when group_by_gene is also given.

    Returns:
        Structured JSON with per-group KM curves, medians, patient/event counts,
        at-risk tables, and a log-rank test result when 2+ groups are present.
""",
)
def survival_curve(
    study_id: str,
    endpoint: str = "OS",
    group_by_gene: str | None = None,
    alteration_types: list[str] | None = None,
    group_by_clinical: str | None = None,
) -> dict:
    # Error returns keep the contract shape (endpoint + empty groups) so the
    # widget recognizes and renders them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "endpoint": (endpoint or "OS").upper(), "groups": []}

    try:
        study_id = _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))

    try:
        return _build_survival_payload(
            study_id=study_id,
            endpoint=endpoint,
            group_by_gene=group_by_gene,
            alteration_types=alteration_types or ["mutation"],
            group_by_clinical=group_by_clinical,
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
    app=ui.oncoprint_app_config(),
    description="""
    Generate an interactive OncoPrint (gene x sample alteration matrix) for a cBioPortal study.

    Returns structured alteration data AND renders an embedded OncoPrint in
    supporting clients

    OncoPrint is computed at the **sample** level (columns = samples). Each cell
    shows that sample's alteration in the gene: mutation (colored by class),
    copy-number amplification or deep deletion, and/or structural variant. A gray
    cell marks a sample not profiled for that gene (gene-panel coverage).

    Args:
        study_id: cBioPortal study identifier (e.g. "brca_tcga_pan_can_atlas_2018").
        genes: Hugo gene symbols for the matrix rows. Optional; defaults to the
               study's most-altered genes. Clamped to 25 rows.
        alteration_types: Which alterations to include. Subset of: mutation,
                          amplification, deep_deletion, structural_variant
                          (default: all four).
        clinical_tracks: Clinical attribute names shown as annotation tracks below
                         the matrix (e.g. "CANCER_TYPE", "SAMPLE_TYPE"). Optional;
                         pass [] for none.
        max_samples: Max sample columns to render (default 200, hard cap 200).
                     Altered samples are prioritized; the view is truncated for
                     larger cohorts (per-gene % still reflects all samples).

    Returns:
        Structured JSON: gene rows, sample columns (MemoSort order), a sparse
        alteration-cell map, not-profiled cells, per-gene frequency stats, and
        clinical tracks.
""",
)
def oncoprint(
    study_id: str,
    genes: list[str] | None = None,
    alteration_types: list[str] | None = None,
    clinical_tracks: list[str] | None = None,
    max_samples: int | None = None,
) -> dict:
    # Error returns keep the contract shape (study_id + empty genes/samples) so
    # the widget recognizes and renders them consistently across host transports.
    def _error(message: str) -> dict:
        return {"error": message, "study_id": study_id, "genes": [], "samples": []}

    try:
        validated_study = _validate_study_id(study_id)
    except ValueError as e:
        return _error(str(e))

    try:
        return _build_oncoprint_payload(
            study_id=validated_study,
            genes=genes,
            alteration_types=alteration_types,
            clinical_tracks=clinical_tracks,
            max_samples=max_samples,
        )
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.error("oncoprint error: %s", e)
        return _error(f"Unexpected error computing OncoPrint: {e}")


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
        "black", "white", "red", "green", "blue", "yellow", "orange", "purple",
        "pink", "brown", "gray", "grey", "cyan", "magenta", "teal", "navy",
        "olive", "maroon", "lime", "aqua", "fuchsia", "silver", "gold", "indigo",
        "violet", "coral", "salmon", "khaki", "crimson", "turquoise", "tomato",
        "steelblue", "seagreen", "darkorange", "transparent",
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
    app=ui.pie_chart_app_config(),
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
    app=ui.bar_chart_app_config(),
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
    app=ui.line_chart_app_config(),
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


if __name__ == "__main__":
    main()
