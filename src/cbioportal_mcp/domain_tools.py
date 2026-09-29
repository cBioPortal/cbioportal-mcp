"""Purpose-built tools for the recurring gene-frequency / cohort-count questions.

Each tool answers one question template in a single call, reading the
precomputed tables from sql/final/0-precomputed-aggregates.sql. When a table is
missing, empty, or has no row for the request, the tool runs the live recipe
SQL instead (the sql/4-mutation-frequency-views.sql recipes) and says
so with ``source: "live"`` plus a ``fallback_reason``.

run_select_query() only accepts a SQL string (mcp_clickhouse has no bind
parameters), so every value that reaches SQL goes through an allow-list
validator and then _sql_str(), which re-checks the value before quoting it.
Nothing user-supplied is interpolated any other way.
"""

import logging
import re

from cbioportal_mcp import server

logger = logging.getLogger(__name__)

ALTERATION_TYPES = ("any", "mutation", "amplification", "deep_deletion", "structural_variant")

# alteration_type -> sample_to_gene_panel_derived.alteration_type(s) that
# make a sample "profiled" for it. 'any' = profiled under at least one.
_PROFILE_TYPES = {
    "mutation": ("MUTATION_EXTENDED",),
    "amplification": ("COPY_NUMBER_ALTERATION",),
    "deep_deletion": ("COPY_NUMBER_ALTERATION",),
    "structural_variant": ("STRUCTURAL_VARIANT",),
    "any": ("MUTATION_EXTENDED", "COPY_NUMBER_ALTERATION", "STRUCTURAL_VARIANT"),
}

# Numerator filters on genomic_event_derived, the same in every sql/4 view:
# UNCALLED mutations and UNCALLED structural variants are not called
# events. 'any' = any of the four.
_EVENT_FILTERS = {
    "mutation": "(variant_type = 'mutation' AND mutation_status != 'UNCALLED')",
    "amplification": "(variant_type = 'cna' AND cna_alteration = 2)",
    "deep_deletion": "(variant_type = 'cna' AND cna_alteration = -2)",
    "structural_variant": "(variant_type = 'structural_variant' AND mutation_status != 'UNCALLED')",
}
_EVENT_FILTERS["any"] = "(" + " OR ".join(_EVENT_FILTERS.values()) + ")"

# Same threshold as the by-cancer-type recipe views.
MIN_PROFILED_SAMPLES = 50
MAX_TOP_N = 100

# HGNC symbols: letters, digits, '-', '.', '_' (e.g. NKX2-1, C1orf112).
VALID_GENE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VALID_PREFERENCE_PATTERN = re.compile(r"^[a-z0-9_]{1,128}$")
# Every literal _sql_str() will quote: the union of the patterns above plus
# the fixed tokens in this module. No quote or backslash can get through.
_SAFE_LITERAL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_PROVENANCE = (
    "numerator: distinct samples with the alteration (off-panel calls, UNCALLED "
    "mutations and UNCALLED SVs excluded); denominator: distinct samples profiled for "
    "this gene (gene on the sample's panel, or WES = all genes)"
)
_STUDY_PROVENANCE = _PROVENANCE + "; CNA denominator: discrete CNA profiles only"
_COHORT_PROVENANCE = _PROVENANCE + (
    "; CNA denominator: every copy-number profile incl. log2, as "
    "gene_alteration_frequency_by_cancer_type"
)


def _sql_str(value: str) -> str:
    """Quote an already-validated value as a ClickHouse string literal."""
    if not _SAFE_LITERAL_PATTERN.match(value):
        raise ValueError(f"refusing to quote unvalidated value {value!r}")
    return f"'{value}'"


def _sql_list(values) -> str:
    return "(" + ", ".join(_sql_str(v) for v in values) + ")"


def _validate_gene(gene: str) -> str:
    gene = (gene or "").strip()
    if not VALID_GENE_PATTERN.match(gene):
        raise ValueError(
            f"Invalid gene '{gene}'. Use a HUGO symbol such as TP53 "
            "(letters, digits, '-', '.', '_')."
        )
    return gene


def _gene_candidates(gene: str) -> tuple[str, ...]:
    """Exact symbol, plus its upper-case form ('tp53' -> TP53) when different.

    Not blindly upper-cased: some HGNC symbols contain lower case (C1orf112).
    """
    upper = gene.upper()
    return (gene,) if upper == gene else (gene, upper)


def _study_candidates(study_id: str) -> tuple[str, ...]:
    study_id = server._validate_study_id(study_id)
    lower = study_id.lower()
    return (study_id,) if lower == study_id else (study_id, lower)


def _validate_alteration_type(alteration_type: str, allowed=ALTERATION_TYPES) -> str:
    value = (alteration_type or "").strip().lower()
    if value not in allowed:
        raise ValueError(
            f"Invalid alteration_type '{alteration_type}'. Use one of: {', '.join(allowed)}."
        )
    return value


def _validate_preference(preference: str) -> str:
    if not VALID_PREFERENCE_PATTERN.match(preference or ""):
        raise ValueError(
            f"Invalid preference '{preference}'. Use a cancer_study_query_preferences "
            "preference_name such as pan_cancer_tcga."
        )
    return preference


def _clamp_top_n(top_n) -> int:
    return max(1, min(int(top_n), MAX_TOP_N))


def _study_profile_filter(profile_types, prefix: str = "") -> str:
    """sample_to_gene_panel_derived rows that make a sample profiled, single-study rules.

    Same as the {mutation,cna,sv}_*_coverage views: CNA rows count only for
    DISCRETE profiles (continuous log2 profiles share the alteration_type but
    carry no AMP / HOMDEL calls).
    """
    parts = []
    other = [t for t in profile_types if t != "COPY_NUMBER_ALTERATION"]
    if other:
        parts.append(f"{prefix}alteration_type IN {_sql_list(other)}")
    if "COPY_NUMBER_ALTERATION" in profile_types:
        parts.append(
            f"({prefix}alteration_type = 'COPY_NUMBER_ALTERATION' AND {prefix}genetic_profile_id "
            "IN (SELECT stable_id FROM genetic_profile WHERE datatype = 'DISCRETE'))"
        )
    return "(" + " OR ".join(parts) + ")"


def _frequency_pct(altered, profiled):
    """Exactly the recipes' ROUND(altered * 100.0 / profiled, 1).

    ClickHouse rounds a Float64 to N digits as nearbyint(x * 10^N) / 10^N:
    half-to-even on the SCALED double. Python's round(x, 1) rounds the exact
    binary value instead, and the two disagree at halves (1/2000: ClickHouse
    0.0, round() 0.1). Python's round(float) with no digits is also
    half-to-even, so this reproduces ClickHouse bit for bit.
    """
    if not profiled:
        return None
    return round(int(altered) * 100.0 / int(profiled) * 10) / 10


def _run_with_fallback(label: str, precomputed_sql: str, live_sql_factory):
    """Run the precomputed query; fall back to live SQL if it errors or is empty.

    Returns (rows, meta) where meta carries source/fallback_reason/built_at.
    live_sql_factory is only called on fallback so its SQL is never built
    on the fast path.
    """
    try:
        rows = server.run_select_query(precomputed_sql, query_label=f"{label}.precomputed")
    except Exception as e:  # table missing (fresh DB, mid-rebuild) or query error
        logger.warning(f"{label}: precomputed query failed, falling back to live SQL: {e}")
        reason = f"precomputed table unavailable ({str(e)[:160]})"
    else:
        if rows:
            built_at = rows[0].get("built_at")
            return rows, {"source": "precomputed", "built_at": str(built_at) if built_at else None}
        reason = "no precomputed rows (table empty, or nothing matched)"

    rows = server.run_select_query(live_sql_factory(), query_label=f"{label}.live")
    return rows, {"source": "live", "fallback_reason": reason}


def _finish(result: dict, meta: dict) -> dict:
    result["source"] = meta["source"]
    if meta.get("built_at"):
        result["built_at"] = meta["built_at"]
    if meta.get("fallback_reason"):
        result["fallback_reason"] = meta["fallback_reason"]
    return result


# ---------------------------------------------------------------------------
# SQL builders (pure functions — unit-tested without a database)
# ---------------------------------------------------------------------------


def build_alteration_frequency_precomputed_sql(genes, studies, alteration_types) -> str:
    return f"""
        SELECT hugo_gene_symbol, cancer_study_identifier, alteration_type,
               altered_samples, profiled_samples, built_at
        FROM study_gene_alteration_counts
        WHERE cancer_study_identifier IN {_sql_list(studies)}
          AND hugo_gene_symbol IN {_sql_list(genes)}
          AND alteration_type IN {_sql_list(alteration_types)}
    """


def build_alteration_frequency_live_sql(genes, studies) -> str:
    """One wide row: altered_<type> / profiled_<profile type> for one gene in one study.

    Mirrors gene_mutation_frequency_in_study (COUNT(DISTINCT) over panel-with-gene
    UNION ALL WES) with the single-study CNA / SV rules of top_{cna,sv}_genes_in_study,
    without the CANCER_TYPE split. Always returns exactly one row, so an unaltered
    gene reports 0 / N.
    """
    s, g = _sql_list(studies), _sql_list(genes)
    f = _EVENT_FILTERS
    return f"""
        WITH
        events AS (
            SELECT sample_unique_id,
                   {f['mutation']} AS is_mutation,
                   {f['amplification']} AS is_amplification,
                   {f['deep_deletion']} AS is_deep_deletion,
                   {f['structural_variant']} AS is_structural_variant
            FROM genomic_event_derived
            WHERE cancer_study_identifier IN {s}
              AND hugo_gene_symbol IN {g}
              AND off_panel = 0
        ),
        profiled AS (
            SELECT stgp.sample_unique_id AS sample_unique_id,
                   stgp.alteration_type AS alteration_type
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene ge ON gpl.gene_id = ge.entrez_gene_id
            WHERE stgp.cancer_study_identifier IN {s}
              AND ge.hugo_gene_symbol IN {g}
              AND {_study_profile_filter(_PROFILE_TYPES["any"], "stgp.")}
            UNION ALL
            SELECT sample_unique_id, alteration_type
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier IN {s}
              AND gene_panel_id = 'WES'
              AND {_study_profile_filter(_PROFILE_TYPES["any"])}
        )
        SELECT *
        FROM (
            SELECT groupUniqArray(hugo_gene_symbol) AS matched_genes
            FROM gene WHERE hugo_gene_symbol IN {g}
        ) AS gene_match
        CROSS JOIN (
            SELECT groupUniqArray(cancer_study_identifier) AS matched_studies
            FROM cancer_study WHERE cancer_study_identifier IN {s}
        ) AS study_match
        CROSS JOIN (
            SELECT uniqExactIf(sample_unique_id, is_mutation) AS altered_mutation,
                   uniqExactIf(sample_unique_id, is_amplification) AS altered_amplification,
                   uniqExactIf(sample_unique_id, is_deep_deletion) AS altered_deep_deletion,
                   uniqExactIf(sample_unique_id, is_structural_variant)
                       AS altered_structural_variant,
                   uniqExactIf(sample_unique_id, is_mutation OR is_amplification
                       OR is_deep_deletion OR is_structural_variant) AS altered_any
            FROM events
        ) AS altered
        CROSS JOIN (
            SELECT uniqExactIf(sample_unique_id, alteration_type = 'MUTATION_EXTENDED')
                       AS profiled_MUTATION_EXTENDED,
                   uniqExactIf(sample_unique_id, alteration_type = 'COPY_NUMBER_ALTERATION')
                       AS profiled_COPY_NUMBER_ALTERATION,
                   uniqExactIf(sample_unique_id, alteration_type = 'STRUCTURAL_VARIANT')
                       AS profiled_STRUCTURAL_VARIANT,
                   uniqExact(sample_unique_id) AS profiled_ANY
            FROM profiled
        ) AS profiled_counts
    """


def _profiled_key(alteration_type: str) -> str:
    profile_types = _PROFILE_TYPES[alteration_type]
    return f"profiled_{'ANY' if len(profile_types) > 1 else profile_types[0]}"


def build_top_altered_genes_precomputed_sql(studies, alteration_type, top_n) -> str:
    return f"""
        SELECT hugo_gene_symbol, altered_samples, profiled_samples, built_at
        FROM study_gene_alteration_counts
        WHERE cancer_study_identifier IN {_sql_list(studies)}
          AND alteration_type = {_sql_str(alteration_type)}
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
        LIMIT {int(top_n)}
    """


def build_top_altered_genes_live_sql(studies, alteration_type, top_n) -> str:
    """Top-N like top_{mutated,cna,sv}_genes_in_study, for any alteration type.

    Denominator = WES samples + panel samples NOT also WES for these profile
    types — disjoint sets, so this equals the COUNT(DISTINCT) of the union
    (the top_*_genes_in_study views add the two without the NOT IN, and so
    count a sample that is both WES and on a panel twice).
    """
    s = _sql_list(studies)
    types = _PROFILE_TYPES[alteration_type]
    return f"""
        WITH
        altered AS (
            SELECT hugo_gene_symbol, COUNT(DISTINCT sample_unique_id) AS altered_samples
            FROM genomic_event_derived
            WHERE cancer_study_identifier IN {s}
              AND off_panel = 0
              AND {_EVENT_FILTERS[alteration_type]}
            GROUP BY hugo_gene_symbol
            ORDER BY altered_samples DESC, hugo_gene_symbol ASC
            LIMIT {int(top_n)}
        ),
        wes_samples AS (
            SELECT DISTINCT sample_unique_id
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier IN {s}
              AND gene_panel_id = 'WES'
              AND {_study_profile_filter(types)}
        ),
        panel_profiled AS (
            SELECT ge.hugo_gene_symbol AS hugo_gene_symbol,
                   COUNT(DISTINCT stgp.sample_unique_id) AS n
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene ge ON gpl.gene_id = ge.entrez_gene_id
            WHERE stgp.cancer_study_identifier IN {s}
              AND {_study_profile_filter(types, "stgp.")}
              AND stgp.sample_unique_id NOT IN (SELECT sample_unique_id FROM wes_samples)
              AND ge.hugo_gene_symbol IN (SELECT hugo_gene_symbol FROM altered)
            GROUP BY ge.hugo_gene_symbol
        )
        SELECT a.hugo_gene_symbol AS hugo_gene_symbol,
               a.altered_samples AS altered_samples,
               (SELECT count() FROM wes_samples) + COALESCE(p.n, 0) AS profiled_samples
        FROM altered a
        LEFT JOIN panel_profiled p ON a.hugo_gene_symbol = p.hugo_gene_symbol
        ORDER BY altered_samples DESC, hugo_gene_symbol ASC
    """


def build_frequency_by_cancer_type_precomputed_sql(genes, alteration_type, preference) -> str:
    """Every stored cancer type for the gene, below the threshold too (one row each).

    The >= 50 threshold, order and limit are applied by _select_cancer_types(),
    so an empty result means the gene has no altered sample in the cohort (or
    the cohort was not built) rather than "nothing reached 50 profiled".
    """
    return f"""
        SELECT cancer_type, hugo_gene_symbol, altered_samples, profiled_samples, built_at
        FROM cancer_type_gene_alteration_counts
        WHERE preference_name = {_sql_str(preference)}
          AND hugo_gene_symbol IN {_sql_list(genes)}
          AND alteration_type = {_sql_str(alteration_type)}
    """


def _select_cancer_types(rows, top_n):
    """The recipe's WHERE profiled_samples >= 50, ORDER BY and LIMIT, on stored rows.

    Same order as the live SQL: altered / profiled as a double DESC, then
    altered DESC, then cancer_type ASC (UTF-8 byte order == code point order).
    """
    kept = [r for r in rows if int(r["profiled_samples"]) >= MIN_PROFILED_SAMPLES]
    kept.sort(
        key=lambda r: (
            -(int(r["altered_samples"]) / int(r["profiled_samples"])),
            -int(r["altered_samples"]),
            r["cancer_type"],
        )
    )
    return kept[:top_n]


def build_frequency_by_cancer_type_live_sql(genes, alteration_type, preference, top_n) -> str:
    """The recipe view itself for the four concrete types; an equivalent union for 'any'."""
    order_limit = f"""
        ORDER BY altered_samples / profiled_samples DESC, altered_samples DESC, cancer_type ASC
        LIMIT {int(top_n)}
    """
    if alteration_type != "any":
        # One recipe call per candidate spelling; at most one matches a real gene.
        parts = [
            f"""
            SELECT cancer_type, {_sql_str(g)} AS hugo_gene_symbol,
                   altered_samples, profiled_samples
            FROM gene_alteration_frequency_by_cancer_type(
                preference={_sql_str(preference)}, gene={_sql_str(g)},
                alteration={_sql_str(alteration_type)})
            """
            for g in genes
        ]
        return "SELECT * FROM (" + " UNION ALL ".join(parts) + ")" + order_limit

    g = _sql_list(genes)
    types = _sql_list(_PROFILE_TYPES["any"])
    return f"""
        WITH
        cohort AS (
            SELECT cancer_study_identifier
            FROM cancer_study_query_preferences
            WHERE preference_name = {_sql_str(preference)}
        ),
        sample_cancer_type AS (
            SELECT cd.sample_unique_id AS sample_unique_id, cd.attribute_value AS cancer_type
            FROM clinical_data_derived cd
            JOIN cohort c USING (cancer_study_identifier)
            WHERE cd.attribute_name = 'CANCER_TYPE'
        ),
        altered AS (
            SELECT sct.cancer_type AS cancer_type, ged.hugo_gene_symbol AS hugo_gene_symbol,
                   COUNT(DISTINCT ged.sample_unique_id) AS altered_samples
            FROM genomic_event_derived ged
            JOIN cohort c USING (cancer_study_identifier)
            JOIN sample_cancer_type sct USING (sample_unique_id)
            WHERE ged.hugo_gene_symbol IN {g}
              AND ged.off_panel = 0
              AND {_EVENT_FILTERS['any']}
            GROUP BY sct.cancer_type, ged.hugo_gene_symbol
        ),
        profiled_samples_for_gene AS (
            SELECT stgp.sample_unique_id AS sample_unique_id,
                   stgp.cancer_study_identifier AS cancer_study_identifier,
                   ge.hugo_gene_symbol AS hugo_gene_symbol
            FROM sample_to_gene_panel_derived stgp
            JOIN gene_panel gp ON stgp.gene_panel_id = gp.stable_id
            JOIN gene_panel_list gpl ON gp.internal_id = gpl.internal_id
            JOIN gene ge ON gpl.gene_id = ge.entrez_gene_id
            WHERE ge.hugo_gene_symbol IN {g}
              AND stgp.alteration_type IN {types}
            UNION ALL
            SELECT w.sample_unique_id, w.cancer_study_identifier, x.hugo_gene_symbol
            FROM sample_to_gene_panel_derived w
            CROSS JOIN (SELECT arrayJoin([{", ".join(_sql_str(v) for v in genes)}])
                        AS hugo_gene_symbol) x
            WHERE w.gene_panel_id = 'WES'
              AND w.alteration_type IN {types}
        ),
        profiled AS (
            SELECT sct.cancer_type AS cancer_type, p.hugo_gene_symbol AS hugo_gene_symbol,
                   COUNT(DISTINCT p.sample_unique_id) AS profiled_samples
            FROM profiled_samples_for_gene p
            JOIN cohort c USING (cancer_study_identifier)
            JOIN sample_cancer_type sct USING (sample_unique_id)
            GROUP BY sct.cancer_type, p.hugo_gene_symbol
        )
        SELECT a.cancer_type AS cancer_type, a.hugo_gene_symbol AS hugo_gene_symbol,
               a.altered_samples AS altered_samples, p.profiled_samples AS profiled_samples
        FROM altered a
        JOIN profiled p ON a.cancer_type = p.cancer_type AND a.hugo_gene_symbol = p.hugo_gene_symbol
        WHERE p.profiled_samples >= {MIN_PROFILED_SAMPLES}
        {order_limit}
    """


def build_profiled_counts_precomputed_sql(studies) -> str:
    return f"""
        SELECT cancer_study_identifier, profile_type, samples, patients, wes_samples, built_at
        FROM study_profiled_counts
        WHERE cancer_study_identifier IN {_sql_list(studies)}
        ORDER BY cancer_study_identifier, profile_type = 'ALL_SAMPLES' DESC,
                 profile_type = 'ANY_MUT_CNA_SV' DESC, samples DESC, profile_type
    """


def build_profiled_counts_live_sql(studies) -> str:
    s = _sql_list(studies)
    return f"""
        WITH
        profiled AS (
            SELECT cancer_study_identifier, alteration_type AS profile_type,
                   sample_unique_id, gene_panel_id
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier IN {s}
            UNION ALL
            SELECT cancer_study_identifier, 'COPY_NUMBER_ALTERATION_DISCRETE' AS profile_type,
                   sample_unique_id, gene_panel_id
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier IN {s}
              AND {_study_profile_filter(("COPY_NUMBER_ALTERATION",))}
            UNION ALL
            SELECT cancer_study_identifier, 'ANY_MUT_CNA_SV' AS profile_type,
                   sample_unique_id, gene_panel_id
            FROM sample_to_gene_panel_derived
            WHERE cancer_study_identifier IN {s}
              AND {_study_profile_filter(_PROFILE_TYPES["any"])}
        ),
        sample_patient AS (
            SELECT sample_unique_id, patient_unique_id
            FROM sample_derived
            WHERE cancer_study_identifier IN {s}
        )
        SELECT * FROM (
            SELECT cancer_study_identifier, 'ALL_SAMPLES' AS profile_type,
                   COUNT(DISTINCT sample_unique_id) AS samples,
                   COUNT(DISTINCT patient_unique_id) AS patients,
                   toUInt64(0) AS wes_samples
            FROM sample_derived
            WHERE cancer_study_identifier IN {s}
            GROUP BY cancer_study_identifier
            UNION ALL
            SELECT p.cancer_study_identifier, p.profile_type,
                   COUNT(DISTINCT p.sample_unique_id),
                   COUNT(DISTINCT nullIf(sp.patient_unique_id, '')),
                   COUNT(DISTINCT if(p.gene_panel_id = 'WES', p.sample_unique_id, NULL))
            FROM profiled p
            LEFT JOIN sample_patient sp ON p.sample_unique_id = sp.sample_unique_id
            GROUP BY p.cancer_study_identifier, p.profile_type
        )
        ORDER BY cancer_study_identifier, profile_type = 'ALL_SAMPLES' DESC,
                 profile_type = 'ANY_MUT_CNA_SV' DESC, samples DESC, profile_type
    """


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _frequency_row(alteration_type, altered, profiled) -> dict:
    return {
        "alteration_type": alteration_type,
        "altered_samples": int(altered or 0),
        "profiled_samples": int(profiled or 0),
        "frequency_pct": _frequency_pct(altered or 0, profiled or 0),
    }


@server.mcp.tool(
    description="""
    Alteration frequency of ONE gene in ONE study, in one call. Prefer this over
    hand-written SQL for "how often is GENE altered/mutated/amplified in STUDY".

    alteration_type: any (default) | mutation | amplification | deep_deletion |
    structural_variant. 'any' also returns the per-type breakdown.

    Returns rows of {alteration_type, altered_samples, profiled_samples,
    frequency_pct}; profiled_samples is the gene-specific denominator (samples
    whose panel covers the gene, or WES). Report these numbers as-is.
    """
)
def get_alteration_frequency(gene: str, study_id: str, alteration_type: str = "any") -> dict:
    try:
        gene = _validate_gene(gene)
        studies = _study_candidates(study_id)
        alteration_type = _validate_alteration_type(alteration_type)
        genes = _gene_candidates(gene)
        wanted = list(ALTERATION_TYPES) if alteration_type == "any" else [alteration_type]
        label = "domain_tools.alteration_frequency"

        try:
            rows = server.run_select_query(
                build_alteration_frequency_precomputed_sql(genes, studies, wanted),
                query_label=f"{label}.precomputed",
            )
            reason = None if rows else "no precomputed row (gene unaltered in study, or unknown)"
        except Exception as e:
            logger.warning(f"{label}: precomputed query failed, falling back to live SQL: {e}")
            rows, reason = [], f"precomputed table unavailable ({str(e)[:160]})"

        result = {"gene": gene, "study_id": study_id}
        if rows:
            by_type = {r["alteration_type"]: r for r in rows}
            result["gene"] = rows[0].get("hugo_gene_symbol", gene)
            result["study_id"] = rows[0].get("cancer_study_identifier", study_id)
            out = []
            for t in wanted:
                r = by_type.get(t)
                if r is not None:
                    out.append(
                        _frequency_row(t, r.get("altered_samples"), r.get("profiled_samples"))
                    )
                elif t == alteration_type:
                    # Requested type unaltered in this study: denominator still needed.
                    out = None
                    reason = f"no precomputed row for alteration_type={t} (0 altered)"
                    break
            if out is not None:
                result["rows"] = out
                result["provenance"] = _STUDY_PROVENANCE
                built_at = rows[0].get("built_at")
                return _finish(
                    result,
                    {"source": "precomputed", "built_at": str(built_at) if built_at else None},
                )

        live = server.run_select_query(
            build_alteration_frequency_live_sql(genes, studies), query_label=f"{label}.live"
        )
        row = live[0] if live else {}
        matched_studies = row.get("matched_studies") or []
        matched_genes = row.get("matched_genes") or []
        if not matched_studies:
            return {"error_message": server._study_not_in_deployment_message(study_id)}
        if not matched_genes:
            return {
                "error_message": f"Gene '{gene}' is not in the gene table. Check the symbol "
                "(see cbioportal://gene-resolution-guide for aliases)."
            }
        result["gene"] = matched_genes[0]
        result["study_id"] = matched_studies[0]
        out = []
        for t in wanted:
            altered = row.get(f"altered_{t}", 0)
            if t != alteration_type and not altered:
                continue  # breakdown under 'any' lists only types that occur
            out.append(_frequency_row(t, altered, row.get(_profiled_key(t), 0)))
        result["rows"] = out
        result["provenance"] = _STUDY_PROVENANCE
        return _finish(result, {"source": "live", "fallback_reason": reason})
    except Exception as e:
        logger.error(f"get_alteration_frequency: {e}")
        return {"error_message": str(e)}


@server.mcp.tool(
    description="""
    Top-N most frequently altered genes in ONE study, in one call. Prefer this
    over hand-written SQL for "most mutated / amplified / altered genes in STUDY".

    alteration_type: mutation (default) | amplification | deep_deletion |
    structural_variant | any. top_n: 1-100 (default 10).

    Ranked by altered_samples (like cBioPortal). Each row has hugo_gene_symbol,
    altered_samples, profiled_samples (gene-specific denominator), frequency_pct.
    """
)
def get_top_altered_genes(
    study_id: str, alteration_type: str = "mutation", top_n: int = 10
) -> dict:
    try:
        studies = _study_candidates(study_id)
        alteration_type = _validate_alteration_type(alteration_type)
        top_n = _clamp_top_n(top_n)
        rows, meta = _run_with_fallback(
            "domain_tools.top_altered_genes",
            build_top_altered_genes_precomputed_sql(studies, alteration_type, top_n),
            lambda: build_top_altered_genes_live_sql(studies, alteration_type, top_n),
        )
        result = {
            "study_id": study_id,
            "alteration_type": alteration_type,
            "rows": [
                {
                    "hugo_gene_symbol": r.get("hugo_gene_symbol"),
                    "altered_samples": int(r.get("altered_samples", 0)),
                    "profiled_samples": int(r.get("profiled_samples", 0)),
                    "frequency_pct": _frequency_pct(
                        r.get("altered_samples", 0), r.get("profiled_samples", 0)
                    ),
                }
                for r in rows
            ],
            "provenance": _STUDY_PROVENANCE,
        }
        if not rows:
            result["note"] = (
                f"No {alteration_type} events found. The study may lack this data type, or "
                "the identifier may be wrong (check list_studies)."
            )
        return _finish(result, meta)
    except Exception as e:
        logger.error(f"get_top_altered_genes: {e}")
        return {"error_message": str(e)}


@server.mcp.tool(
    description="""
    Frequency of ONE gene across cancer types in a cohort, in one call. Prefer
    this over hand-written SQL for "which cancer types have the highest
    GENE mutation/amplification/... frequency".

    alteration_type: mutation (default) | amplification | deep_deletion |
    structural_variant | any. top_n: 1-100 (default 10). preference: a
    cancer_study_query_preferences cohort (default pan_cancer_tcga).

    Cancer types need >= 50 profiled samples (same rule as the recipe views).
    Rows sorted by frequency_pct desc: cancer_type, altered_samples,
    profiled_samples, frequency_pct. CNA denominators here count every
    copy-number profile (incl. log2), as gene_alteration_frequency_by_cancer_type,
    so they can exceed get_alteration_frequency's discrete-only ones.
    """
)
def get_gene_frequency_by_cancer_type(
    gene: str,
    alteration_type: str = "mutation",
    top_n: int = 10,
    preference: str = "pan_cancer_tcga",
) -> dict:
    try:
        gene = _validate_gene(gene)
        alteration_type = _validate_alteration_type(alteration_type)
        preference = _validate_preference(preference)
        top_n = _clamp_top_n(top_n)
        genes = _gene_candidates(gene)
        rows, meta = _run_with_fallback(
            "domain_tools.gene_frequency_by_cancer_type",
            build_frequency_by_cancer_type_precomputed_sql(genes, alteration_type, preference),
            lambda: build_frequency_by_cancer_type_live_sql(
                genes, alteration_type, preference, top_n
            ),
        )
        if meta["source"] == "precomputed":
            rows = _select_cancer_types(rows, top_n)
        result = {
            "gene": rows[0].get("hugo_gene_symbol", gene) if rows else gene,
            "alteration_type": alteration_type,
            "preference": preference,
            "rows": [
                {
                    "cancer_type": r.get("cancer_type"),
                    "altered_samples": int(r.get("altered_samples", 0)),
                    "profiled_samples": int(r.get("profiled_samples", 0)),
                    "frequency_pct": _frequency_pct(
                        r.get("altered_samples", 0), r.get("profiled_samples", 0)
                    ),
                }
                for r in rows
            ],
            "provenance": _COHORT_PROVENANCE + f"; cancer types with < {MIN_PROFILED_SAMPLES} "
            "profiled samples omitted",
        }
        if not rows:
            result["note"] = (
                "No cancer type qualified. Check the gene symbol, and that the preference "
                "exists: SELECT DISTINCT preference_name FROM cancer_study_query_preferences."
            )
        return _finish(result, meta)
    except Exception as e:
        logger.error(f"get_gene_frequency_by_cancer_type: {e}")
        return {"error_message": str(e)}


@server.mcp.tool(
    description="""
    Study-wide sample and patient counts for ONE study: all samples, and samples
    with a profile of each data type. Prefer this over hand-written SQL for "how
    many samples / patients / sequenced samples are in STUDY". These are NOT
    gene-frequency denominators (a panel may not cover a gene): for "% of
    profiled samples with GENE altered" use get_alteration_frequency.

    profile_type: ALL_SAMPLES (every sample; wes_samples is always 0 here);
    ANY_MUT_CNA_SV (mutation, discrete CNA or SV profile);
    COPY_NUMBER_ALTERATION_DISCRETE; and each stored type (MUTATION_EXTENDED,
    COPY_NUMBER_ALTERATION incl. log2, STRUCTURAL_VARIANT, MRNA_EXPRESSION, ...).
    wes_samples = samples whose profile covers all genes (whole exome, or a
    non-panel genome-wide CNA / SV profile). Counts come from sample profiles
    and can differ from the portal's case-list counts (cancer_study.*_sample_count).
    """
)
def get_profiled_counts(study_id: str) -> dict:
    try:
        studies = _study_candidates(study_id)
        rows, meta = _run_with_fallback(
            "domain_tools.profiled_counts",
            build_profiled_counts_precomputed_sql(studies),
            lambda: build_profiled_counts_live_sql(studies),
        )
        if not rows:
            return {"error_message": server._study_not_in_deployment_message(study_id)}
        result = {
            "study_id": rows[0].get("cancer_study_identifier", study_id),
            "rows": [
                {
                    "profile_type": r.get("profile_type"),
                    "samples": int(r.get("samples", 0)),
                    "patients": int(r.get("patients", 0)),
                    "wes_samples": int(r.get("wes_samples", 0)),
                }
                for r in rows
            ],
        }
        return _finish(result, meta)
    except Exception as e:
        logger.error(f"get_profiled_counts: {e}")
        return {"error_message": str(e)}
