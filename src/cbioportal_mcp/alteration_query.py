"""Alteration queries: a documented subset of cBioPortal's Onco Query Language (OQL).

The data apps used to take a flat gene list plus an ``alteration_types`` switch, which
cannot say what researchers actually ask for: "a merged track for SMARCA4, SMARCB1 and
ARID1A", "all EGFR mutations except T790M and L858R", "TP53 mutations in codons 1-40",
"truncating mutations only". This module parses the portal's own syntax for those
requests -- so the tools accept what users already paste from cbioportal.org -- and
evaluates it two ways that must agree:

- :func:`track_wants_event` decides in Python whether one ``genomic_event_derived`` row
  is wanted (used where the rows are fetched anyway: OncoPrint cells, co-occurrence
  tracks, survival groups);
- :func:`track_sql_predicate` compiles the same decision to a ClickHouse boolean over the
  same column names (used where the rows are too many to fetch, e.g. a genome-wide
  enrichment).

Pure standard library and no database knowledge, so both paths are unit-testable in
isolation. Anything outside the supported subset raises ``ValueError`` naming the
construct and the alternative: an unsupported filter must never be silently dropped,
because the result would look filtered and not be.

Supported syntax (keywords are case-insensitive; see https://docs.cbioportal.org/user-guide/oql/)::

    query     := track ((";" | newline | whitespace) track)*
    track     := gene_line | "[" ['"label"'] gene_line (";"? gene_line)* "]"   # merged
    gene_line := GENE [":" command+]            # no commands = MUT FUSION AMP HOMDEL
    command   := MUT [("=" | "!=") value] | AMP | HOMDEL | FUSION
               | DRIVER | GERMLINE | SOMATIC
               | value                           # shorthand: "BRAF: V600E" = MUT = V600E
    value     := MISSENSE | NONSENSE | NONSTART | NONSTOP | FRAMESHIFT | INFRAME
               | SPLICE | TRUNC | PROMOTER
               | <protein change: V600E, E746_A750del, p.Arg371Trp> | <codon: V600>
               | (start-end) | (start-) | (-end)  # overlaps the range; "(a-b*)" = contained
    Any keyword or value may carry DRIVER / GERMLINE / SOMATIC joined by "_" on either
    side: MUT_DRIVER, DRIVER_MUT, TRUNC_GERMLINE, (712-979)_DRIVER.

Semantics follow the portal, with one documented difference. A gene line wants an event
when at least one of its commands matches it; a sample is altered in a track when any of
its events is wanted by any line of the track. The portal evaluates each ``MUT != X``
command independently as "any mutation but X", so a second exclusion on the same line
re-admits what the first removed (its docs warn ``!=`` "will only work to exclude a single
event"). Here every ``!=`` on a line is a veto on that line's mutations:
``EGFR: MUT != T790M MUT != L858R`` excludes both, and ``TP53: MISSENSE MUT != R175H`` is
missense except R175H. Only when a line has no positive mutation command does ``MUT != X``
also admit every other mutation of the gene -- which is what the syntax reads as.

Gene lines should be separated by ";" or a newline once they carry commands. Within a
line, a bare word shaped like a protein change (V600E, C5) is read as a value, so
``TP53: MUT A2M`` means TP53 mutations plus TP53 A2M; write ``TP53: MUT; A2M`` for two
genes. Every tool echoes the parsed query, so a misreading is visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# MAF Variant_Classification values behind each OQL mutation class. TRUNC is the same
# set the OncoPrint colours as "truncating", so a track filtered to TRUNC and the cell
# colour always agree.
TRUNCATING_TYPES = frozenset(
    {
        "Nonsense_Mutation",
        "Frame_Shift_Del",
        "Frame_Shift_Ins",
        "Splice_Site",
        "Splice_Region",
        "Nonstop_Mutation",
        "Translation_Start_Site",
    }
)
MUTATION_CLASSES: dict[str, frozenset[str]] = {
    "MISSENSE": frozenset({"Missense_Mutation"}),
    "NONSENSE": frozenset({"Nonsense_Mutation"}),
    "NONSTART": frozenset({"Translation_Start_Site"}),
    "NONSTOP": frozenset({"Nonstop_Mutation"}),
    "FRAMESHIFT": frozenset({"Frame_Shift_Del", "Frame_Shift_Ins"}),
    "INFRAME": frozenset({"In_Frame_Del", "In_Frame_Ins"}),
    "SPLICE": frozenset({"Splice_Site", "Splice_Region"}),
    "TRUNC": TRUNCATING_TYPES,
}
# TERT promoter calls are stored with the protein change "Promoter" (mutation_type
# 5'Flank), so PROMOTER is a protein-change value rather than a MAF class.
PROMOTER = "PROMOTER"

ALTERATIONS = ("MUT", "AMP", "HOMDEL", "FUSION")
# The portal's default when a gene is named without commands.
DEFAULT_ALTERATIONS = ALTERATIONS
MODIFIERS = ("DRIVER", "GERMLINE", "SOMATIC")

# genomic_event_derived.variant_type behind each alteration keyword.
VARIANT_TYPE = {"MUT": "mutation", "AMP": "cna", "HOMDEL": "cna", "FUSION": "structural_variant"}
# sample_to_gene_panel_derived.alteration_type deciding whether a sample was profiled.
PROFILING_TYPE = {
    "MUT": "MUTATION_EXTENDED",
    "AMP": "COPY_NUMBER_ALTERATION",
    "HOMDEL": "COPY_NUMBER_ALTERATION",
    "FUSION": "STRUCTURAL_VARIANT",
}

# Constructs from full OQL this subset refuses, with the reason and the route.
_UNSUPPORTED = {
    "EXP": "mRNA expression thresholds are not alteration events here; for survival by "
    "expression use survival_curve(group_by_expression=...)",
    "PROT": "protein-level thresholds are not supported",
    "GAIN": "shallow copy-number gains are not stored as events (only AMP and HOMDEL are)",
    "HETLOSS": "shallow copy-number losses are not stored as events (only AMP and HOMDEL are)",
    "CNA": "copy-number comparisons (CNA >= GAIN) are not supported; use AMP or HOMDEL",
    "DATATYPES": "DATATYPES is not supported; write the commands on each gene line "
    "(e.g. CDKN2A: AMP HOMDEL; MDM2: AMP HOMDEL)",
}

MAX_TRACKS = 50
MAX_GENES_PER_TRACK = 50
MAX_QUERY_CHARS = 4000

_AA = "ACDEFGHIKLMNPQRSTVWYX*"
_GENE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PROTEIN_CHANGE_RE = re.compile(r"^[A-Za-z0-9_*?.>+=-]+$")
_CODON_RE = re.compile(r"^[A-Za-z*]?(\d+)$")
_RANGE_RE = re.compile(r"^\(\s*(\d*)\s*-\s*(\d*)\s*(\*?)\s*\)$")
# A bare word after "GENE:" is a shorthand value only when it is shaped like a protein
# change; anything else (KRAS, CDKN2A) starts the next gene.
_PROTEIN_SHAPE_RE = re.compile(
    rf"^(?:p\.)?[{_AA}]\d+(?:_[{_AA}]\d+)?"
    rf"(?:[{_AA}?=]|[{_AA}]?fs\*?\d*|delins[{_AA}]+|del[{_AA}]*|ins[{_AA}]+|dup[{_AA}]*"
    r"|ext\*?\d*|_?splice)?$",
    re.IGNORECASE,
)
_FIRST_NUMBER_RE = re.compile(r"\d+")
# End codon of a ranged change such as E746_A750del (the number after "_").
_END_NUMBER_RE = re.compile(r"_[A-Za-z*]*(\d+)")

# HGVS three-letter residues -> one-letter codes (the data stores one-letter changes).
_THREE_LETTER = {
    "Ala": "A", "Arg": "R", "Asn": "N", "Asp": "D", "Cys": "C", "Gln": "Q", "Glu": "E",
    "Gly": "G", "His": "H", "Ile": "I", "Leu": "L", "Lys": "K", "Met": "M", "Phe": "F",
    "Pro": "P", "Ser": "S", "Thr": "T", "Trp": "W", "Tyr": "Y", "Val": "V", "Ter": "*",
    "Sec": "U", "Xaa": "X",
}  # fmt: skip
_THREE_LETTER_RE = re.compile("|".join(_THREE_LETTER))

_TOKEN_RE = re.compile(
    r"""
    (?P<nl>[\r\n]+)
  | (?P<ws>[^\S\r\n]+)
  | (?P<sep>;)
  | (?P<lbr>\[)
  | (?P<rbr>\])
  | (?P<label>"[^"\r\n]*")
  | (?P<neq>!=)
  | (?P<eq>=)
  | (?P<colon>:)
  | (?P<range>(?:[A-Za-z]+_)*\(\s*\d*\s*-\s*\d*\s*\*?\s*\)(?:_[A-Za-z]+)*)
  | (?P<word>[^\s;\[\]":=!()]+)
    """,
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MutationFilter:
    """What a ``MUT = ...`` / ``MUT != ...`` value selects."""

    kind: str  # "class" | "protein_change" | "position" | "range"
    value: object  # class name | upper-cased change | int | (start, end, contained)
    text: str

    def matches(self, row: dict) -> bool:
        if self.kind == "class":
            return str(row.get("mutation_type") or "") in MUTATION_CLASSES[str(self.value)]
        if self.kind == "protein_change":
            return normalize_protein_change(row.get("mutation_variant")) == self.value
        start, end = protein_range(row.get("mutation_variant"))
        if start is None or end is None:
            return False
        if self.kind == "position":
            return start == self.value
        lo, hi, contained = self.value  # type: ignore[misc]
        lo = 1 if lo is None else lo
        hi = 10**9 if hi is None else hi
        if contained:
            return start >= lo and end <= hi
        return start <= hi and end >= lo


@dataclass(frozen=True)
class Command:
    """One alteration command of a gene line, e.g. ``MUT != T790M`` or ``AMP_DRIVER``."""

    alteration: str  # one of ALTERATIONS
    negate: bool = False
    mutation: MutationFilter | None = None
    driver: bool = False
    germline: bool | None = None  # True germline only, False somatic only, None any
    text: str = ""

    def base_matches(self, row: dict) -> bool:
        """The alteration (with its modifiers) matches, ignoring the value."""
        if row.get("variant_type") != VARIANT_TYPE[self.alteration]:
            return False
        if self.alteration == "MUT":
            status = str(row.get("mutation_status") or "").lower()
            if status == "uncalled":
                return False
            if self.germline is True and status != "germline":
                return False
            if self.germline is False and status == "germline":
                return False
        elif self.alteration in ("AMP", "HOMDEL"):
            raw = row.get("cna_alteration")
            try:
                cna = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                cna = None
            if cna != (2 if self.alteration == "AMP" else -2):
                return False
        if self.driver and str(row.get("driver_filter") or "").lower() != "putative_driver":
            return False
        return True

    def wants(self, row: dict) -> bool:
        """Positive match: ``MUT = X`` needs X; ``MUT != X`` admits any mutation."""
        if not self.base_matches(row):
            return False
        if self.mutation is None or self.negate:
            return True
        return self.mutation.matches(row)

    def excludes(self, row: dict) -> bool:
        """``MUT != X`` vetoes a qualifying mutation that is X."""
        return (
            self.negate
            and self.mutation is not None
            and self.base_matches(row)
            and self.mutation.matches(row)
        )


@dataclass(frozen=True)
class GeneLine:
    gene: str
    commands: tuple[Command, ...]
    text: str

    @property
    def effective_commands(self) -> tuple[Command, ...]:
        if self.commands:
            return self.commands
        return tuple(Command(alteration=a, text=a) for a in DEFAULT_ALTERATIONS)


@dataclass(frozen=True)
class Track:
    label: str
    lines: tuple[GeneLine, ...]
    merged: bool
    text: str
    genes: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Protein-change helpers (kept identical to the SQL compiled below)
# ---------------------------------------------------------------------------


def normalize_protein_change(value) -> str | None:
    """Upper-cased protein change without a leading ``p.`` (``p.V600E`` -> ``V600E``)."""
    if value is None:
        return None
    s = str(value).strip()
    if s[:2].lower() == "p.":
        s = s[2:]
    return s.upper() or None


def protein_range(value) -> tuple[int | None, int | None]:
    """``(start, end)`` codons of a protein change; ``(None, None)`` when unplaceable.

    Start is the first number (``V600E`` -> 600, ``E746_A750del`` -> 746); end is the
    number after an underscore when present (750), else the start.
    """
    if value is None:
        return None, None
    s = str(value)
    m = _FIRST_NUMBER_RE.search(s)
    if not m:
        return None, None
    start = int(m.group())
    if start <= 0:
        return None, None
    e = _END_NUMBER_RE.search(s)
    end = int(e.group(1)) if e else start
    return start, max(start, end)


def one_letter_protein_change(value: str) -> str:
    """``p.Arg371Trp`` -> ``R371W``: the query side of a comparison against one-letter data."""
    s = str(value).strip()
    if s[:2].lower() == "p.":
        s = s[2:]
    return _THREE_LETTER_RE.sub(lambda m: _THREE_LETTER[m.group()], s)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(
                f"Could not read the alteration query near {text[pos:pos + 20]!r}. "
                "Parentheses are only valid around a codon range such as MUT = (1-40)."
            )
        kind = m.lastgroup or ""
        if kind == "nl":
            tokens.append(("sep", "\n"))
        elif kind != "ws":
            tokens.append((kind, m.group()))
        pos = m.end()
    return tokens


def _split_modifiers(word: str) -> tuple[str, list[str]]:
    """Peel DRIVER/GERMLINE/SOMATIC joined by "_" off either end of a word."""
    parts = word.split("_")
    mods: list[str] = []
    while parts and parts[0].upper() in MODIFIERS:
        mods.append(parts.pop(0).upper())
    while parts and parts[-1].upper() in MODIFIERS:
        mods.append(parts.pop().upper())
    return "_".join(parts), mods


def _is_command_token(kind: str, word: str) -> bool:
    """Whether a token after ``GENE:`` continues that gene line."""
    if kind == "range":
        return True
    if kind != "word":
        return False
    base, mods = _split_modifiers(word)
    if not base:
        return bool(mods)
    upper = base.upper()
    return (
        upper in ALTERATIONS
        or upper in _UNSUPPORTED
        or upper in MUTATION_CLASSES
        or upper == PROMOTER
        or bool(_PROTEIN_SHAPE_RE.match(one_letter_protein_change(base)))
    )


def _parse_range(token: str) -> tuple[MutationFilter, list[str]]:
    head, _, rest = token.partition("(")
    body, _, suffix = rest.partition(")")
    body = "(" + body + ")"
    mods = [m.upper() for m in (head.split("_") + suffix.split("_")) if m]
    bad = [m for m in mods if m not in MODIFIERS]
    if bad:
        raise ValueError(f"Unknown modifier {bad[0]!r} on codon range {body}.")
    range_match = _RANGE_RE.match(body)
    if not range_match:
        raise ValueError(f"Could not read the codon range {body!r}; write it as (start-end).")
    lo = int(range_match.group(1)) if range_match.group(1) else None
    hi = int(range_match.group(2)) if range_match.group(2) else None
    if lo is None and hi is None:
        raise ValueError(f"The codon range {body!r} needs a start and/or an end.")
    if (lo is not None and lo < 1) or (hi is not None and hi < 1):
        raise ValueError(f"Codon positions start at 1; got {body!r}.")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError(f"The codon range {body!r} starts after it ends.")
    contained = bool(range_match.group(3))
    text = f"({'' if lo is None else lo}-{'' if hi is None else hi}{'*' if contained else ''})"
    return MutationFilter("range", (lo, hi, contained), text), mods


def _parse_value(kind: str, word: str) -> tuple[MutationFilter, list[str]]:
    if kind == "range":
        return _parse_range(word)
    base, mods = _split_modifiers(word)
    if not base:
        raise ValueError(f"{word!r} is a modifier, not a mutation value.")
    upper = base.upper()
    if upper in MUTATION_CLASSES:
        return MutationFilter("class", upper, upper), mods
    if upper == PROMOTER:
        return MutationFilter("protein_change", PROMOTER, PROMOTER), mods
    change = one_letter_protein_change(base)
    codon = _CODON_RE.match(change)
    if codon:
        position = int(codon.group(1))
        if position < 1:
            raise ValueError(f"Codon positions start at 1; got {base!r}.")
        return MutationFilter("position", position, change.upper()), mods
    if not change or not _PROTEIN_CHANGE_RE.match(change):
        raise ValueError(f"{word!r} is not a mutation class, protein change or codon range.")
    normalized = normalize_protein_change(change) or change
    return MutationFilter("protein_change", normalized, normalized), mods


def _apply_modifiers(alteration: str, mods: list[str], negate=False, mutation=None) -> Command:
    mods = list(dict.fromkeys(mods))
    driver = "DRIVER" in mods
    germline: bool | None = None
    if "GERMLINE" in mods and "SOMATIC" in mods:
        raise ValueError("A command cannot be both GERMLINE and SOMATIC.")
    if "GERMLINE" in mods:
        germline = True
    elif "SOMATIC" in mods:
        germline = False
    if germline is not None and alteration != "MUT":
        raise ValueError(f"GERMLINE/SOMATIC apply to mutations only, not {alteration}.")
    text = alteration
    if mutation is not None:
        text += f" {'!=' if negate else '='} {mutation.text}"
    # Canonical modifier order, so equivalent spellings normalize to the same text.
    text += "".join(f"_{m}" for m in ("GERMLINE", "SOMATIC", "DRIVER") if m in mods)
    return Command(alteration, negate, mutation, driver, germline, text)


class _Parser:
    def __init__(self, text: str):
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def parse(self) -> list[Track]:
        tracks: list[Track] = []
        while (tok := self.peek()) is not None:
            kind, value = tok
            if kind == "sep":
                self.take()
            elif kind == "lbr":
                tracks.append(self.parse_merged())
            elif kind == "word":
                line = self.parse_gene_line()
                tracks.append(Track("", (line,), False, line.text, (line.gene,)))
            elif kind == "label":
                raise ValueError(
                    f"The label {value} must open a merged track: " '["label" GENE1 GENE2].'
                )
            else:
                raise ValueError(f"Unexpected {value!r} in the alteration query.")
        return tracks

    def parse_merged(self) -> Track:
        self.take()  # "["
        label = None
        if (tok := self.peek()) is not None and tok[0] == "label":
            label = self.take()[1].strip('"').strip() or None
        lines: list[GeneLine] = []
        while True:
            tok = self.peek()
            if tok is None:
                raise ValueError("A merged track opened with '[' is missing its ']'.")
            if tok[0] == "rbr":
                self.take()
                break
            if tok[0] == "sep":
                self.take()
                continue
            if tok[0] == "lbr":
                raise ValueError("Merged tracks cannot be nested.")
            if tok[0] != "word":
                raise ValueError(f"Unexpected {tok[1]!r} inside a merged track.")
            lines.append(self.parse_gene_line())
        if not lines:
            raise ValueError("A merged track needs at least one gene.")
        genes = tuple(dict.fromkeys(ln.gene for ln in lines))
        if len(genes) > MAX_GENES_PER_TRACK:
            raise ValueError(f"A merged track may hold at most {MAX_GENES_PER_TRACK} genes.")
        joiner = "; " if any(ln.commands for ln in lines) else " "
        body = joiner.join(ln.text for ln in lines)
        text = "[" + (f'"{label}" ' if label else "") + body + "]"
        return Track(label or "", tuple(lines), True, text, genes)

    def parse_gene_line(self) -> GeneLine:
        _, gene = self.take()
        base, mods = _split_modifiers(gene)
        if mods or base.upper() in ALTERATIONS or base.upper() in _UNSUPPORTED:
            if base.upper() in _UNSUPPORTED:
                raise ValueError(
                    f"{base.upper()} is not supported in alteration queries: "
                    f"{_UNSUPPORTED[base.upper()]}."
                )
            raise ValueError(f"Expected a gene symbol but found the keyword {gene!r}.")
        if not _GENE_RE.match(gene):
            raise ValueError(f"Invalid gene symbol {gene!r} in the alteration query.")
        commands: list[Command] = []
        has_colon = (tok := self.peek()) is not None and tok[0] == "colon"
        if has_colon:
            self.take()
            while (tok := self.peek()) is not None:
                if tok[0] in ("sep", "rbr", "lbr", "label"):
                    break
                nxt = self.peek(1)
                if tok[0] == "word" and nxt is not None and nxt[0] == "colon":
                    break  # "KRAS:" starts the next gene line
                if not _is_command_token(*tok):
                    break
                commands.extend(self.parse_command())
            if not commands:
                raise ValueError(
                    f"{gene}: expected alteration commands after ':' (e.g. MUT, AMP, V600E)."
                )
        text = gene + (": " + " ".join(c.text for c in commands) if commands else "")
        return GeneLine(gene, tuple(commands), text)

    def parse_command(self) -> list[Command]:
        kind, word = self.take()
        if kind == "range":
            value, mods = _parse_range(word)
            return [_apply_modifiers("MUT", mods, False, value)]
        base, mods = _split_modifiers(word)
        upper = base.upper()
        if not base:
            # Standalone DRIVER / GERMLINE / SOMATIC (possibly combined).
            if "GERMLINE" in mods or "SOMATIC" in mods:
                return [_apply_modifiers("MUT", mods)]
            return [_apply_modifiers(a, mods) for a in ALTERATIONS]
        if upper in _UNSUPPORTED:
            raise ValueError(
                f"{upper} is not supported in alteration queries: {_UNSUPPORTED[upper]}."
            )
        op = self.peek()
        has_op = op is not None and op[0] in ("eq", "neq")
        if upper in ALTERATIONS:
            if not has_op:
                return [_apply_modifiers(upper, mods)]
            if upper != "MUT":
                raise ValueError(f"Only MUT takes a value; {upper} = ... is not supported.")
            negate = self.take()[0] == "neq"
            tok = self.peek()
            if tok is None or tok[0] not in ("word", "range"):
                raise ValueError(
                    "MUT = / MUT != must be followed by a value (e.g. MISSENSE, V600E, (1-40))."
                )
            value, value_mods = _parse_value(*self.take())
            return [_apply_modifiers("MUT", mods + value_mods, negate, value)]
        if has_op:
            raise ValueError(
                f"{word!r} followed by = / != is not valid; only MUT takes a value "
                f"(write MUT = {word})."
            )
        value, value_mods = _parse_value(kind, word)
        return [_apply_modifiers("MUT", mods + value_mods, False, value)]


def _label_for(track: Track) -> str:
    if track.label:
        return track.label
    if not track.merged:
        return track.lines[0].text
    genes = list(track.genes)
    shown = " / ".join(genes[:4]) + (f" +{len(genes) - 4}" if len(genes) > 4 else "")
    if any(ln.commands for ln in track.lines):
        cmds = sorted({c.text for ln in track.lines for c in ln.commands})
        shown += f" ({', '.join(cmds[:2])}{'…' if len(cmds) > 2 else ''})"
    return shown


def _normalize_input(text: str) -> str:
    # Chat clients love typographic quotes and non-breaking spaces.
    for fancy in ("\u201c", "\u201d", "\u201e"):
        text = text.replace(fancy, '"')
    return text.replace("\u00a0", " ")


def parse_alteration_query(text: str) -> list[Track]:
    """Parse an alteration query into tracks with unique display labels.

    Raises ValueError for malformed or unsupported syntax.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("The alteration query is empty.")
    if len(text) > MAX_QUERY_CHARS:
        raise ValueError(f"The alteration query is longer than {MAX_QUERY_CHARS} characters.")
    tracks = _Parser(_normalize_input(text)).parse()
    if not tracks:
        raise ValueError("The alteration query names no genes.")
    if len(tracks) > MAX_TRACKS:
        raise ValueError(
            f"The alteration query has {len(tracks)} tracks; the limit is {MAX_TRACKS}."
        )
    out: list[Track] = []
    seen: dict[str, int] = {}
    for t in tracks:
        label = _label_for(t)
        n = seen.get(label, 0) + 1
        seen[label] = n
        if n > 1:
            label = f"{label} ({n})"
        out.append(Track(label, t.lines, t.merged, t.text, t.genes))
    return out


def parse_track_list(items) -> list[Track]:
    """Parse a list of track strings (or one query string) into labelled tracks."""
    if isinstance(items, str):
        return parse_alteration_query(items)
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("Give the tracks as an OQL string or a non-empty list of OQL strings.")
    texts = [str(item) for item in items]
    if any(not t.strip() for t in texts):
        raise ValueError("Track definitions cannot be empty strings.")
    return parse_alteration_query("\n".join(texts))


def parse_single_track(text: str) -> Track:
    """Parse text that must describe exactly one track (a gene line or a merged track)."""
    tracks = parse_alteration_query(text)
    if len(tracks) != 1:
        raise ValueError(
            f"{text!r} describes {len(tracks)} tracks; give one gene line per entry "
            '(e.g. "TP53: MUT") or wrap several genes in one merged track ("[BRCA1 BRCA2]").'
        )
    return tracks[0]


def format_query(tracks: list[Track]) -> str:
    """The normalized OQL for parsed tracks, one track per line."""
    return "\n".join(t.text for t in tracks)


# ---------------------------------------------------------------------------
# Evaluation (Python)
# ---------------------------------------------------------------------------


def _admitting_commands(line: GeneLine) -> tuple[Command, ...]:
    """Commands that can admit an event: a ``!=`` admits only on a line without a
    positive mutation command (see the module docstring)."""
    commands = line.effective_commands
    positive_mut = any(c.alteration == "MUT" and not c.negate for c in commands)
    return tuple(c for c in commands if not (c.negate and positive_mut))


def line_wants_event(line: GeneLine, row: dict) -> bool:
    """True when ``row`` (a genomic_event_derived row) is wanted by ``line``."""
    if row.get("hugo_gene_symbol") != line.gene:
        return False
    if any(c.excludes(row) for c in line.effective_commands):
        return False
    return any(c.wants(row) for c in _admitting_commands(line))


def track_wants_event(track: Track, row: dict) -> bool:
    return any(line_wants_event(line, row) for line in track.lines)


def track_exclusions(track: Track) -> list[tuple[GeneLine, Command]]:
    """Every ``!=`` command in the track, with its gene line."""
    return [(ln, c) for ln in track.lines for c in ln.effective_commands if c.negate and c.mutation]


def exclusion_vetoes(line: GeneLine, command: Command, row: dict) -> bool:
    """Whether this ``!=`` command removes ``row`` (a mutation of the line's gene)."""
    return row.get("hugo_gene_symbol") == line.gene and command.excludes(row)


def track_variant_types(track: Track) -> set[str]:
    return {VARIANT_TYPE[c.alteration] for ln in track.lines for c in ln.effective_commands}


def track_profiling_types(track: Track) -> set[str]:
    return {PROFILING_TYPE[c.alteration] for ln in track.lines for c in ln.effective_commands}


def track_uses_driver(track: Track) -> bool:
    return any(c.driver for ln in track.lines for c in ln.effective_commands)


def track_has_exclusions(track: Track) -> bool:
    return bool(track_exclusions(track))


def track_is_mutation_only(track: Track) -> bool:
    return track_variant_types(track) == {"mutation"}


def describe_track(track: Track) -> dict:
    """JSON-ready description of a track for payloads."""
    return {
        "label": track.label,
        "genes": list(track.genes),
        "merged": track.merged,
        "oql": track.text,
        "lines": [
            {
                "gene": ln.gene,
                "commands": [c.text for c in ln.effective_commands],
                "default_commands": not ln.commands,
            }
            for ln in track.lines
        ],
    }


# ---------------------------------------------------------------------------
# Compilation (ClickHouse SQL over genomic_event_derived column names)
# ---------------------------------------------------------------------------


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# SQL twins of normalize_protein_change / protein_range. Character classes instead of
# backslash escapes, so no layer of string escaping can change the pattern.
_SQL_CHANGE = "upper(replaceRegexpOne(trimBoth(mutation_variant), '^[pP][.]', ''))"
_SQL_START = "toInt64OrZero(extract(mutation_variant, '[0-9]+'))"
_SQL_END = (
    "greatest(" + _SQL_START + ", if(match(mutation_variant, '_[A-Za-z*]*[0-9]+'), "
    "toInt64OrZero(extract(mutation_variant, '_[A-Za-z*]*([0-9]+)')), 0))"
)


def _sql_mutation_filter(f: MutationFilter) -> str:
    if f.kind == "class":
        types = ", ".join(_sql_str(t) for t in sorted(MUTATION_CLASSES[str(f.value)]))
        return f"mutation_type IN ({types})"
    if f.kind == "protein_change":
        return f"{_SQL_CHANGE} = {_sql_str(str(f.value))}"
    if f.kind == "position":
        return f"{_SQL_START} = {int(f.value)}"  # type: ignore[call-overload]
    lo, hi, contained = f.value  # type: ignore[misc]
    lo = 1 if lo is None else int(lo)
    hi = 10**9 if hi is None else int(hi)
    if contained:
        return f"({_SQL_START} >= {lo} AND {_SQL_END} <= {hi})"
    return f"({_SQL_START} > 0 AND {_SQL_START} <= {hi} AND {_SQL_END} >= {lo})"


def _sql_base(c: Command) -> str:
    parts = [f"variant_type = {_sql_str(VARIANT_TYPE[c.alteration])}"]
    if c.alteration == "MUT":
        parts.append("lower(mutation_status) != 'uncalled'")
        if c.germline is True:
            parts.append("lower(mutation_status) = 'germline'")
        elif c.germline is False:
            parts.append("lower(mutation_status) != 'germline'")
    elif c.alteration == "AMP":
        parts.append("cna_alteration = 2")
    elif c.alteration == "HOMDEL":
        parts.append("cna_alteration = -2")
    if c.driver:
        parts.append("lower(driver_filter) = 'putative_driver'")
    return "(" + " AND ".join(parts) + ")"


def line_sql_predicate(line: GeneLine) -> str:
    commands = line.effective_commands
    wants = []
    for c in _admitting_commands(line):
        if c.mutation is None or c.negate:
            wants.append(_sql_base(c))
        else:
            wants.append(f"({_sql_base(c)} AND {_sql_mutation_filter(c.mutation)})")
    predicate = f"hugo_gene_symbol = {_sql_str(line.gene)} AND ({' OR '.join(wants)})"
    vetoes = [
        f"({_sql_base(c)} AND {_sql_mutation_filter(c.mutation)})"
        for c in commands
        if c.negate and c.mutation is not None
    ]
    if vetoes:
        predicate += f" AND NOT ({' OR '.join(vetoes)})"
    return f"({predicate})"


def track_sql_predicate(track: Track) -> str:
    """ClickHouse boolean over genomic_event_derived columns selecting the wanted events."""
    return "(" + " OR ".join(line_sql_predicate(ln) for ln in track.lines) + ")"


def track_wants_event_ignoring_exclusions(track: Track, row: dict) -> bool:
    """Whether ``row`` would be wanted if the track's ``!=`` vetoes were removed.

    Lets a tool report what each exclusion took away rather than applying it
    silently.
    """
    return any(
        row.get("hugo_gene_symbol") == ln.gene
        and any(c.wants(row) for c in _admitting_commands(ln))
        for ln in track.lines
    )


def parse_mutation_value(text: str) -> MutationFilter:
    """Parse one mutation value (class, protein change, codon or range) on its own.

    Accepts what follows ``MUT =`` in a query: ``V600E``, ``p.Val600Glu``, ``V600``,
    ``MISSENSE``, ``(1-40)``. Modifiers are not allowed here.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("The mutation value is empty.")
    kind = "range" if raw.startswith("(") else "word"
    value, mods = _parse_value(kind, raw)
    if mods:
        raise ValueError(f"{raw!r}: modifiers such as _DRIVER are not accepted here.")
    return value


def mutation_filter_sql(f: MutationFilter) -> str:
    """ClickHouse boolean for one mutation value over mutation_variant / mutation_type."""
    return _sql_mutation_filter(f)
