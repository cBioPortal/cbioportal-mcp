"""Tests for the OQL-subset parser and its two evaluators (Python and compiled SQL).

The Python evaluator decides event membership where rows are fetched anyway; the SQL
compiler is used where they are too many to fetch. The two must agree, so the parity
test runs the compiled predicate in sqlite3 -- with the handful of ClickHouse string
functions it uses re-implemented in Python -- over generated rows and compares the
selected events with the Python evaluator's answer.
"""

import random
import re
import sqlite3

import pytest

from cbioportal_mcp import alteration_query as aq

# --- parsing -----------------------------------------------------------------


def _one(text):
    return aq.parse_single_track(text)


def test_plain_genes_are_separate_tracks_with_default_commands():
    tracks = aq.parse_alteration_query("SMARCA4 SMARCB1 ARID1A")
    assert [t.label for t in tracks] == ["SMARCA4", "SMARCB1", "ARID1A"]
    assert all(not t.merged for t in tracks)
    assert [c.alteration for c in tracks[0].lines[0].effective_commands] == [
        "MUT",
        "AMP",
        "HOMDEL",
        "FUSION",
    ]


def test_merged_track_with_label_and_per_gene_commands():
    t = _one('["Truncating" SMARCA4: MUT = TRUNC; SMARCB1: TRUNC ARID1A: TRUNC]')
    assert t.merged and t.label == "Truncating"
    assert t.genes == ("SMARCA4", "SMARCB1", "ARID1A")
    assert [ln.commands[0].text for ln in t.lines] == ["MUT = TRUNC"] * 3
    assert (
        t.text == '["Truncating" SMARCA4: MUT = TRUNC; SMARCB1: MUT = TRUNC; ARID1A: MUT = TRUNC]'
    )


def test_unlabelled_merged_track_gets_a_descriptive_label():
    t = _one("[CDKN2A CDK4 CDK6 CCND1 RB1]")
    assert t.label == "CDKN2A / CDK4 / CDK6 / CCND1 +1"
    assert t.text == "[CDKN2A CDK4 CDK6 CCND1 RB1]"


def test_single_gene_labels_carry_their_commands_and_stay_unique():
    labels = [
        t.label for t in aq.parse_alteration_query("TP53: MUT = (1-40); TP53: (41-); TP53; TP53")
    ]
    assert labels == ["TP53: MUT = (1-40)", "TP53: MUT = (41-)", "TP53", "TP53 (2)"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("BRAF: V600E", "BRAF: MUT = V600E"),
        ("BRAF: MUT = V600", "BRAF: MUT = V600"),
        ("TP53: TRUNC", "TP53: MUT = TRUNC"),
        ("BRCA1: GERMLINE_TRUNC", "BRCA1: MUT = TRUNC_GERMLINE"),
        ("BRCA1: NONSENSE_DRIVER_GERMLINE", "BRCA1: MUT = NONSENSE_GERMLINE_DRIVER"),
        ("KRAS: DRIVER_MUT", "KRAS: MUT_DRIVER"),
        ("EGFR: MUT = (712-979)_DRIVER", "EGFR: MUT = (712-979)_DRIVER"),
        ("EGFR: (712-979*)", "EGFR: MUT = (712-979*)"),
        ("TP53: MUT = (-40)", "TP53: MUT = (-40)"),
        ("SEPHS1: p.Arg371Trp", "SEPHS1: MUT = R371W"),
        ("TP53: MUT = p.Arg175His", "TP53: MUT = R175H"),
        ("EGFR: E746_A750del", "EGFR: MUT = E746_A750DEL"),
        ("TP53: X125_splice", "TP53: MUT = X125_SPLICE"),
        ("TERT: PROMOTER", "TERT: MUT = PROMOTER"),
        ("BRCA2: GERMLINE", "BRCA2: MUT_GERMLINE"),
        ("EGFR: AMP_DRIVER FUSION", "EGFR: AMP_DRIVER FUSION"),
    ],
)
def test_normalized_command_text(text, expected):
    assert _one(text).text == expected


def test_standalone_driver_covers_every_alteration():
    cmds = _one("CDKN2A: DRIVER").lines[0].commands
    assert [(c.alteration, c.driver) for c in cmds] == [
        ("MUT", True),
        ("AMP", True),
        ("HOMDEL", True),
        ("FUSION", True),
    ]


def test_newline_and_semicolon_end_a_gene_line():
    assert [t.label for t in aq.parse_alteration_query("TP53: MUT\nA2M")] == ["TP53: MUT", "A2M"]
    assert [t.label for t in aq.parse_alteration_query("TP53: MUT; A2M")] == ["TP53: MUT", "A2M"]
    assert [t.label for t in aq.parse_alteration_query("TP53: MUT KRAS: AMP")] == [
        "TP53: MUT",
        "KRAS: AMP",
    ]
    # Documented ambiguity: a protein-change-shaped word on the same line is a value.
    assert aq.parse_alteration_query("TP53: MUT A2M")[0].text == "TP53: MUT MUT = A2M"


def test_parse_track_list_joins_entries_as_separate_tracks():
    tracks = aq.parse_track_list(["TP53: MUT", "[BRCA1 BRCA2]"])
    assert [t.label for t in tracks] == ["TP53: MUT", "BRCA1 / BRCA2"]
    assert aq.format_query(tracks) == "TP53: MUT\n[BRCA1 BRCA2]"


def test_typographic_quotes_are_accepted():
    assert _one("[“HRR” BRCA1 BRCA2]").label == "HRR"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "empty"),
        ("EGFR: EXP > 2", "EXP is not supported"),
        ("EGFR: PROT > 1", "PROT is not supported"),
        ("CCNE1: CNA >= GAIN", "CNA is not supported"),
        ("KRAS: GAIN", "GAIN is not supported"),
        ("DATATYPES: AMP; TP53", "DATATYPES is not supported"),
        ("TP53:", "expected alteration commands"),
        ("TP53: MUT = (40-1)", "starts after it ends"),
        ("TP53: MUT = (0-40)", "start at 1"),
        ("TP53: MUT = (-)", "needs a start and/or an end"),
        ("TP53: AMP = V600E", "Only MUT takes a value"),
        ("TP53: TRUNC = X", "only MUT takes a value"),
        ('["x" TP53', "missing its ']'"),
        ("[TP53 [KRAS]]", "cannot be nested"),
        ("[]", "at least one gene"),
        ('"label" TP53', "must open a merged track"),
        ("TP53: MUT_GERMLINE_SOMATIC", "both GERMLINE and SOMATIC"),
        ("TP53: AMP_GERMLINE", "mutations only"),
        ("MUT: TP53", "keyword"),
        ("TP53: MUT = ", "must be followed by a value"),
        ("TP53 (1-40)", "Unexpected"),
        ("TP53: MUT = (1-40)_BOGUS", "Unknown modifier"),
        ("TP53$: MUT", "Invalid gene symbol"),
        ("TP53! MUT", "Could not read"),
    ],
)
def test_malformed_or_unsupported_queries_raise(text, fragment):
    with pytest.raises(ValueError, match=re.escape(fragment)):
        aq.parse_alteration_query(text)


def test_track_and_query_size_limits():
    with pytest.raises(ValueError, match="limit is"):
        aq.parse_alteration_query(" ".join(f"G{i}" for i in range(aq.MAX_TRACKS + 1)))
    with pytest.raises(ValueError, match="longer than"):
        aq.parse_alteration_query("TP53 " * 1000)


def test_parse_single_track_rejects_several_tracks():
    with pytest.raises(ValueError, match="describes 2 tracks"):
        aq.parse_single_track("TP53 KRAS")


# --- protein-change helpers --------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("V600E", (600, 600)),
        ("p.E746_A750del", (746, 750)),
        ("V600_K601delinsE", (600, 601)),
        ("A289Vfs*12", (289, 289)),
        ("X125_splice", (125, 125)),
        ("*307L", (307, 307)),
        ("Promoter", (None, None)),
        ("NA", (None, None)),
        ("p.0?", (None, None)),
        (None, (None, None)),
    ],
)
def test_protein_range(value, expected):
    assert aq.protein_range(value) == expected


def test_one_letter_conversion():
    assert aq.one_letter_protein_change("p.Glu746_Ala750del") == "E746_A750del"
    assert aq.one_letter_protein_change("Arg213Ter") == "R213*"
    assert aq.one_letter_protein_change("V600E") == "V600E"


# --- Python evaluation -------------------------------------------------------


def _mut(gene, change, mtype="Missense_Mutation", status="Somatic", driver=""):
    return {
        "hugo_gene_symbol": gene,
        "variant_type": "mutation",
        "mutation_variant": change,
        "mutation_type": mtype,
        "mutation_status": status,
        "driver_filter": driver,
        "cna_alteration": None,
    }


def _cna(gene, value):
    return {
        "hugo_gene_symbol": gene,
        "variant_type": "cna",
        "mutation_variant": "NA",
        "mutation_type": "",
        "mutation_status": "",
        "driver_filter": "",
        "cna_alteration": value,
    }


def _sv(gene):
    row = _cna(gene, None)
    row["variant_type"] = "structural_variant"
    return row


def test_exclusions_veto_every_listed_change():
    t = _one("EGFR: MUT != T790M MUT != L858R")
    assert aq.track_wants_event(t, _mut("EGFR", "G719S"))
    assert not aq.track_wants_event(t, _mut("EGFR", "T790M"))
    assert not aq.track_wants_event(t, _mut("EGFR", "p.L858R"))
    # A != only vetoes mutations; the line has no AMP command, so AMP is not wanted either.
    assert not aq.track_wants_event(t, _cna("EGFR", 2))
    assert [c.text for _, c in aq.track_exclusions(t)] == ["MUT != T790M", "MUT != L858R"]


def test_exclusion_does_not_veto_other_alteration_types():
    t = _one("EGFR: AMP MUT != T790M")
    assert aq.track_wants_event(t, _cna("EGFR", 2))
    assert not aq.track_wants_event(t, _mut("EGFR", "T790M"))


def test_class_minus_specific_change():
    # With a positive mutation command on the line, != only removes.
    t = _one("TP53: MISSENSE MUT != R175H")
    assert aq.track_wants_event(t, _mut("TP53", "R248Q"))
    assert not aq.track_wants_event(t, _mut("TP53", "R175H"))
    assert not aq.track_wants_event(t, _mut("TP53", "R213*", "Nonsense_Mutation"))
    # Alone, MUT != X admits every other mutation of the gene.
    alone = _one("TP53: MUT != R175H")
    assert aq.track_wants_event(alone, _mut("TP53", "R213*", "Nonsense_Mutation"))
    everything_but = _one("BRAF: MUT MUT != V600E")
    assert aq.track_wants_event(everything_but, _mut("BRAF", "K601E"))
    assert not aq.track_wants_event(everything_but, _mut("BRAF", "V600E"))


def test_codon_and_range_semantics():
    codon = _one("BRAF: V600")
    assert aq.track_wants_event(codon, _mut("BRAF", "V600E"))
    assert aq.track_wants_event(codon, _mut("BRAF", "V600K"))
    assert not aq.track_wants_event(codon, _mut("BRAF", "K601E"))

    overlap = _one("EGFR: (745-750)")
    contained = _one("EGFR: (747-750*)")
    deletion = _mut("EGFR", "E746_A750del", "In_Frame_Del")
    assert aq.track_wants_event(overlap, deletion)
    assert not aq.track_wants_event(contained, deletion)  # starts at 746, outside 747-750
    assert aq.track_wants_event(_one("EGFR: (746-750*)"), deletion)

    head = _one("TP53: (1-40)")
    rest = _one("TP53: (41-)")
    assert aq.track_wants_event(head, _mut("TP53", "P36fs", "Frame_Shift_Del"))
    assert not aq.track_wants_event(rest, _mut("TP53", "P36fs", "Frame_Shift_Del"))
    assert aq.track_wants_event(rest, _mut("TP53", "R175H"))
    # An unplaceable change is in no range.
    assert not aq.track_wants_event(head, _mut("TP53", "NA", "Splice_Region"))
    assert not aq.track_wants_event(rest, _mut("TP53", "NA", "Splice_Region"))


def test_protein_change_matching_ignores_prefix_and_case():
    t = _one("BRAF: MUT = p.V600E")
    assert aq.track_wants_event(t, _mut("BRAF", "V600E"))
    assert aq.track_wants_event(t, _mut("BRAF", "p.v600e"))
    assert not aq.track_wants_event(t, _mut("BRAF", "V600K"))
    assert aq.track_wants_event(_one("TERT: PROMOTER"), _mut("TERT", "Promoter", "5'Flank"))


def test_status_and_driver_modifiers():
    germ = _one("BRCA1: GERMLINE")
    som = _one("BRCA1: MUT_SOMATIC")
    drv = _one("BRCA1: MUT_DRIVER")
    g = _mut("BRCA1", "E23fs", "Frame_Shift_Del", status="GERMLINE")
    s = _mut("BRCA1", "E23fs", "Frame_Shift_Del", status="Somatic", driver="Putative_Driver")
    unknown = _mut("BRCA1", "E23fs", "Frame_Shift_Del", status="NA", driver="Putative_Passenger")
    assert aq.track_wants_event(germ, g) and not aq.track_wants_event(germ, s)
    assert aq.track_wants_event(som, s) and aq.track_wants_event(som, unknown)
    assert not aq.track_wants_event(som, g)
    assert aq.track_wants_event(drv, s) and not aq.track_wants_event(drv, unknown)


def test_uncalled_mutations_never_count():
    t = _one("TP53")
    assert not aq.track_wants_event(t, _mut("TP53", "R175H", status="UNCALLED"))
    assert aq.track_wants_event(t, _mut("TP53", "R175H", status="uncalled".upper() + "X"))


def test_default_commands_and_cna_types():
    t = _one("CDKN2A")
    assert aq.track_wants_event(t, _cna("CDKN2A", -2))
    assert aq.track_wants_event(t, _cna("CDKN2A", 2))
    assert not aq.track_wants_event(t, _cna("CDKN2A", -1))
    assert aq.track_wants_event(t, _sv("CDKN2A"))
    assert not aq.track_wants_event(_one("CDKN2A: HOMDEL"), _cna("CDKN2A", 2))
    assert not aq.track_wants_event(t, _mut("CDKN2B", "R80*"))


def test_merged_track_is_the_union_of_its_lines():
    t = _one("[KRAS: G12D; NRAS: Q61]")
    assert aq.track_wants_event(t, _mut("KRAS", "G12D"))
    assert aq.track_wants_event(t, _mut("NRAS", "Q61K"))
    assert not aq.track_wants_event(t, _mut("KRAS", "G13D"))


def test_track_introspection():
    t = _one("[EGFR: (712-979)_DRIVER; ERBB2: AMP]")
    assert aq.track_variant_types(t) == {"mutation", "cna"}
    assert aq.track_profiling_types(t) == {"MUTATION_EXTENDED", "COPY_NUMBER_ALTERATION"}
    assert aq.track_uses_driver(t)
    assert not aq.track_has_exclusions(t)
    assert aq.track_is_mutation_only(_one("TP53: MISSENSE"))
    desc = aq.describe_track(_one("TP53"))
    assert desc == {
        "label": "TP53",
        "genes": ["TP53"],
        "merged": False,
        "oql": "TP53",
        "lines": [
            {
                "gene": "TP53",
                "commands": ["MUT", "AMP", "HOMDEL", "FUSION"],
                "default_commands": True,
            }
        ],
    }


# --- SQL compilation: parity with the Python evaluator ------------------------


def _clickhouse_like_sqlite():
    """sqlite3 with the ClickHouse functions the compiler emits."""

    def extract(s, pattern):
        m = re.search(pattern, s or "")
        if not m:
            return ""
        return m.group(1) if m.groups() else m.group(0)

    def to_int_or_zero(s):
        return int(s) if isinstance(s, str) and s.isdigit() else 0

    conn = sqlite3.connect(":memory:")
    conn.create_function("extract", 2, extract)
    conn.create_function("toInt64OrZero", 1, to_int_or_zero)
    conn.create_function("match", 2, lambda s, p: 1 if re.search(p, s or "") else 0)
    conn.create_function("replaceRegexpOne", 3, lambda s, p, r: re.sub(p, r, s or "", count=1))
    conn.create_function("trimBoth", 1, lambda s: (s or "").strip())
    conn.create_function("greatest", 2, max)
    conn.execute(
        "CREATE TABLE ev (id INTEGER, hugo_gene_symbol TEXT, variant_type TEXT, "
        "mutation_variant TEXT, mutation_type TEXT, mutation_status TEXT, "
        "driver_filter TEXT, cna_alteration INTEGER)"
    )
    return conn


_GENES = ["TP53", "EGFR", "KRAS"]
_CHANGES = [
    "R175H", "p.R248Q", "T790M", "L858R", "G12D", "G12C", "E746_A750del", "P36fs",
    "R213*", "X125_splice", "NA", "Promoter", "V600_K601delinsE", "A39V", "M1?", "Q61H",
]  # fmt: skip
_TYPES = [
    "Missense_Mutation", "Nonsense_Mutation", "Frame_Shift_Del", "In_Frame_Del",
    "Splice_Site", "Splice_Region", "Translation_Start_Site", "5'Flank",
]  # fmt: skip


def _random_rows(n=600, seed=7):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        gene = rng.choice(_GENES)
        kind = rng.random()
        if kind < 0.7:
            rows.append(
                {
                    "id": i,
                    "hugo_gene_symbol": gene,
                    "variant_type": "mutation",
                    "mutation_variant": rng.choice(_CHANGES),
                    "mutation_type": rng.choice(_TYPES),
                    "mutation_status": rng.choice(["Somatic", "GERMLINE", "UNCALLED", "NA", ""]),
                    "driver_filter": rng.choice(["", "Putative_Driver", "Putative_Passenger"]),
                    "cna_alteration": None,
                }
            )
        elif kind < 0.9:
            rows.append(
                {
                    "id": i,
                    "hugo_gene_symbol": gene,
                    "variant_type": "cna",
                    "mutation_variant": "NA",
                    "mutation_type": "",
                    "mutation_status": "",
                    "driver_filter": rng.choice(["", "Putative_Driver"]),
                    "cna_alteration": rng.choice([-2, 2]),
                }
            )
        else:
            rows.append(
                {
                    "id": i,
                    "hugo_gene_symbol": gene,
                    "variant_type": "structural_variant",
                    "mutation_variant": "NA",
                    "mutation_type": "",
                    "mutation_status": "",
                    "driver_filter": "",
                    "cna_alteration": None,
                }
            )
    return rows


PARITY_QUERIES = [
    "TP53",
    "TP53: MUT",
    "EGFR: MUT != T790M MUT != L858R",
    "TP53: MISSENSE MUT != R175H",
    "TP53: MUT != R175H",
    "TP53: MUT_DRIVER MUT != (1-100)",
    "TP53: (1-40)",
    "TP53: (41-)",
    "EGFR: (745-750*)",
    "EGFR: (-760)",
    "KRAS: G12",
    "KRAS: MUT = G12D",
    "TP53: TRUNC",
    "TP53: SPLICE NONSTART",
    "EGFR: MUT = (712-979)_DRIVER",
    "TP53: GERMLINE",
    "TP53: MUT_SOMATIC",
    "KRAS: AMP HOMDEL_DRIVER",
    "EGFR: FUSION",
    "[TP53: TRUNC; KRAS: AMP; EGFR: MUT != T790M]",
    "TP53: PROMOTER",
    "TP53: DRIVER",
    "EGFR: INFRAME FRAMESHIFT",
]


@pytest.mark.parametrize("query", PARITY_QUERIES)
def test_sql_predicate_selects_exactly_what_python_wants(query):
    conn = _clickhouse_like_sqlite()
    rows = _random_rows()
    conn.executemany(
        "INSERT INTO ev VALUES (:id, :hugo_gene_symbol, :variant_type, :mutation_variant, "
        ":mutation_type, :mutation_status, :driver_filter, :cna_alteration)",
        rows,
    )
    track = aq.parse_single_track(query)
    predicate = aq.track_sql_predicate(track).replace(" if(", " iif(").replace("(if(", "(iif(")
    selected = {r[0] for r in conn.execute(f"SELECT id FROM ev WHERE {predicate}")}
    expected = {r["id"] for r in rows if aq.track_wants_event(track, r)}
    assert selected == expected
    assert expected, f"fixture never exercises {query!r}"


def test_sql_literals_are_escaped():
    t = aq.parse_single_track("TP53: MUT = R175H")
    sql = aq.track_sql_predicate(t)
    assert "hugo_gene_symbol = 'TP53'" in sql
    assert aq._sql_str("O'Brien\\") == "'O''Brien\\\\'"
