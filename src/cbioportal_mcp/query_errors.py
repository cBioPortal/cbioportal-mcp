"""Error replies for failed clickhouse_run_select_query calls.

mcp-clickhouse reports a failed query with clickhouse-connect's exception text:

    Query execution failed: HTTPDriver for http://<host>:8123 received ClickHouse error code 184
     Code: 184. DB::Exception: Aggregate function count() is found in WHERE in query.
     (ILLEGAL_AGGREGATION) (version 25.8.33.6 (official build))

The transport prefix names the internal ClickHouse host and the version suffix
is noise; agents relay this text to users verbatim, and neither part says how
to fix the query. select_query_error() keeps the ClickHouse message itself and,
for the errors agent-written SQL commonly hits, adds a "hint" naming the fix.

Hints are keyed by the numeric error code rather than the trailing
"(ERROR_NAME)", because clickhouse-connect cuts long messages (syntax errors
list every expected token) before the name, while the code is always in the
header. Codes and names: ClickHouse src/Common/ErrorCodes.cpp.
"""

import difflib
import re
from collections.abc import Callable

_HTTP_ERROR_HEADER = re.compile(
    r"^Query execution failed: HTTPDriver for \S+ received ClickHouse error code \d+\s+"
    r"(?=Code: \d+\.)"
)
_HTTP_DRIVER = re.compile(r"HTTPDriver for \S+")
_VERSION_SUFFIX = re.compile(r"\s*\(version [^()]*(?:\([^()]*\)[^()]*)*\)\s*$")
_ERROR_CODE = re.compile(r"\bCode: (\d+)\.|received ClickHouse error code (\d+)")
# mcp-clickhouse's own timeout. It also covers time spent waiting for a free
# query slot (see query_concurrency), so it does not prove the query is slow.
_CLIENT_TIMEOUT = re.compile(r"^Query timed out after ")

_MISSING_COLUMN = (
    "A column or alias in the query does not exist where it is used. Look up the table's "
    "real columns with clickhouse_list_table_columns instead of guessing. Clinical "
    "attributes such as AGE or SAMPLE_TYPE are rows of clinical_data_derived "
    "(attribute_name / attribute_value), not columns."
)
_MISSING_TABLE = (
    "The query names a table or database that does not exist. List the real tables with "
    "clickhouse_list_tables, and leave the database name out of table references."
)
_WRONG_TYPE = (
    "A value has the wrong type for how it is used. clinical_data_derived.attribute_value "
    "is a String for every attribute, numeric ones included: convert it with "
    "toFloat64OrNull(attribute_value), which returns NULL for values such as 'NA' "
    "(CAST and toFloat64 fail on them). Those rows then drop out of filters and "
    "averages silently, so also count them with countIf(toFloat64OrNull(attribute_value) "
    "IS NULL) and tell the user how many were excluded. Both branches of if/CASE, and "
    "both sides of a comparison or JOIN key, need compatible types."
)
_TOO_EXPENSIVE = (
    "Filter by cancer_study_identifier (and gene or attribute) as early as possible, "
    "aggregate in SQL instead of returning raw rows, and filter large tables before "
    "joining them. For a cohort defined by several criteria (e.g. KRAS-mutant, TP53 "
    "wild-type, high MYC expression), select each criterion's sample_unique_id set in its "
    "own subquery and combine them with IN / NOT IN, or run the criteria as separate "
    "queries and tell the user which step failed."
)

_HINTS: dict[int, str] = {
    16: _MISSING_COLUMN,  # NO_SUCH_COLUMN_IN_TABLE
    47: _MISSING_COLUMN,  # UNKNOWN_IDENTIFIER
    60: _MISSING_TABLE,  # UNKNOWN_TABLE
    81: _MISSING_TABLE,  # UNKNOWN_DATABASE
    46: (  # UNKNOWN_FUNCTION
        "ClickHouse function names differ from other SQL dialects and most are "
        "case-sensitive (e.g. countIf, medianExact, quantile(0.5)(x), rankCorr). Use the "
        "function the message suggests after 'Maybe you meant', if any."
    ),
    42: (  # NUMBER_OF_ARGUMENTS_DOESNT_MATCH
        "A function got the wrong number of arguments; the message says what it expects. "
        "Parametric aggregates take their parameters in a separate pair of parentheses: "
        "quantile(0.5)(x), not quantile(x, 0.5)."
    ),
    62: (  # SYNTAX_ERROR
        "ClickHouse could not parse the query at the reported position. Check for "
        "unbalanced parentheses or quotes, a trailing comma, or syntax from another SQL "
        "dialect, and send a single SELECT statement per call."
    ),
    184: (  # ILLEGAL_AGGREGATION
        "An aggregate function is used where aggregates are not allowed: in WHERE, or "
        "inside another aggregate (including a CASE inside one). Filter on aggregates with "
        "HAVING, or compute the inner aggregate in a subquery and aggregate its result in "
        "the outer query."
    ),
    215: (  # NOT_AN_AGGREGATE
        "Every selected column must be in GROUP BY or wrapped in an aggregate function "
        "such as any(x), max(x) or count()."
    ),
    6: _WRONG_TYPE,  # CANNOT_PARSE_TEXT
    27: _WRONG_TYPE,  # CANNOT_PARSE_INPUT_ASSERTION_FAILED
    38: _WRONG_TYPE,  # CANNOT_PARSE_DATE
    41: _WRONG_TYPE,  # CANNOT_PARSE_DATETIME
    43: _WRONG_TYPE,  # ILLEGAL_TYPE_OF_ARGUMENT
    53: _WRONG_TYPE,  # TYPE_MISMATCH
    70: _WRONG_TYPE,  # CANNOT_CONVERT_TYPE
    72: _WRONG_TYPE,  # CANNOT_PARSE_NUMBER
    386: _WRONG_TYPE,  # NO_COMMON_TYPE
    158: "The query exceeded a result-size limit. " + _TOO_EXPENSIVE,  # TOO_MANY_ROWS
    396: "The query exceeded a result-size limit. " + _TOO_EXPENSIVE,  # TOO_MANY_ROWS_OR_BYTES
    159: "The query ran out of time. " + _TOO_EXPENSIVE,  # TIMEOUT_EXCEEDED
    241: "The query ran out of memory. " + _TOO_EXPENSIVE,  # MEMORY_LIMIT_EXCEEDED
    164: (  # READONLY
        "This connection is read-only: send one SELECT (or WITH ... SELECT) without a "
        "SETTINGS clause, and query tables rather than table functions such as url() or "
        "file()."
    ),
    497: (  # ACCESS_DENIED
        "The database user this server runs as may not read something the query uses. "
        "Query only the tables clickhouse_list_tables returns."
    ),
}

_CLIENT_TIMEOUT_HINT = (
    "The query did not finish in time; the server may also have been busy, so one retry "
    "of the same query is reasonable. If it times out again: " + _TOO_EXPENSIVE
)


MAX_LISTED_COLUMNS = 60
MAX_LISTED_TABLES = 40

_UNKNOWN_IDENTIFIER = (
    re.compile(
        r"Unknown expression(?: or function)?(?: or table expression)? identifier [`']([^`']+)[`']"
    ),
    re.compile(r"Identifier '([^']+)' cannot be resolved"),
    re.compile(r"Missing columns: '([^']+)'"),
)
_UNKNOWN_TABLE = (
    re.compile(r"Unknown table(?: expression)?(?: identifier)? [`']([^`']+)[`']"),
    re.compile(r"Table (?:\w+\.)?(\w+) (?:does not exist|doesn't exist)"),
)
_TABLE_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.IGNORECASE
)
_NOT_AN_ALIAS = frozenset(
    "where on using group order limit left right inner outer cross full join prewhere union "
    "select having settings format final sample array global any all asof semi anti natural".split()
)
_TRANSIENT = ("Connection reset by peer", "Connection aborted", "Remote end closed connection")

_TUPLE_UNREADABLE = (
    "The result holds a tuple (for example a test function's (statistic, p_value)) that the client "
    "cannot read next to other columns. Select its elements as scalars: "
    "tupleElement(test_expression, 2) AS p_value."
)
_CORRELATED_SUBQUERY = (
    "Correlated subqueries (a subquery that reads a column of the outer query) are not supported. "
    "Rewrite with a JOIN or a CTE (WITH ... AS)."
)
_CONNECTION_DROPPED = "The database connection dropped; the same query is safe to run again."

DESCRIBE = Callable[[str], list[str] | None]
LIST_TABLES = Callable[[], list[str] | None]


def is_transient_connection_error(message: str) -> bool:
    """A dropped connection: safe to repeat a SELECT once, unlike a timeout (too heavy a query)."""
    return any(marker in message for marker in _TRANSIENT)


def tables_in_query(query: str) -> dict[str, str]:
    """Alias (or bare table name) to table name for every FROM/JOIN in the query."""
    found: dict[str, str] = {}
    for match in _TABLE_REFERENCE.finditer(query):
        table = match.group(1).rsplit(".", 1)[-1]
        alias = match.group(2)
        if alias and alias.lower() not in _NOT_AN_ALIAS:
            found.setdefault(alias, table)
        found.setdefault(table, table)
    return found


def _first_match(patterns: tuple[re.Pattern, ...], text: str) -> str | None:
    return next((m.group(1) for p in patterns if (m := p.search(text))), None)


def _identifier_detail(message: str, query: str, describe: DESCRIBE) -> str | None:
    """The real columns of the tables the failing query reads, and the closest name."""
    identifier = _first_match(_UNKNOWN_IDENTIFIER, message)
    if not identifier:
        return None
    qualifier, _, column = identifier.rpartition(".")
    references = tables_in_query(query)
    tables = (
        [references[qualifier]]
        if qualifier in references
        else list(dict.fromkeys(references.values()))
    )
    lines = [f"`{identifier}` does not exist."]
    for table in tables[:4]:
        columns = describe(table)
        if not columns:
            continue
        shown = ", ".join(columns[:MAX_LISTED_COLUMNS]) + (
            " ..." if len(columns) > MAX_LISTED_COLUMNS else ""
        )
        lines.append(f"Columns of `{table}`: {shown}.")
        if column in columns:
            lines.append(f"`{column}` is a column of `{table}`.")
            continue
        close = difflib.get_close_matches(column, columns, n=3, cutoff=0.6)
        if close:
            lines.append(
                f"Closest to `{column}` in `{table}`: {', '.join(f'`{c}`' for c in close)}."
            )
    return " ".join(lines) if len(lines) > 1 else None


def _table_detail(message: str, list_tables: LIST_TABLES) -> str | None:
    table = _first_match(_UNKNOWN_TABLE, message)
    if not table:
        return None
    names = list_tables() or []
    close = difflib.get_close_matches(table.rsplit(".", 1)[-1], names, n=3, cutoff=0.5)
    lines = [f"Table `{table}` does not exist."]
    if close:
        lines.append(f"Closest tables: {', '.join(f'`{c}`' for c in close)}.")
    if names:
        more = " ..." if len(names) > MAX_LISTED_TABLES else ""
        lines.append(f"Tables: {', '.join(names[:MAX_LISTED_TABLES])}{more}.")
    return " ".join(lines)


def _detail_for(
    message: str, query: str, describe: DESCRIBE, list_tables: LIST_TABLES
) -> str | None:
    """Facts about this particular failure; a failing lookup just leaves its part out."""
    for build in (
        lambda: _identifier_detail(message, query, describe),
        lambda: _table_detail(message, list_tables),
    ):
        try:
            detail = build()
        except Exception:
            continue
        if detail:
            return detail
    return None


def _error_code(message: str) -> int | None:
    match = _ERROR_CODE.search(message)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def _clean_message(message: str) -> str:
    """Drop the driver's transport header and the server version; keep the ClickHouse text."""
    cleaned = _HTTP_ERROR_HEADER.sub("", message)
    cleaned = _HTTP_DRIVER.sub("ClickHouse", cleaned)
    return _VERSION_SUFFIX.sub("", cleaned).strip()


def _hint_for(message: str) -> str | None:
    """Return a fix-it hint for a ClickHouse error message, or None for unrecognized errors."""
    if _CLIENT_TIMEOUT.match(message):
        return _CLIENT_TIMEOUT_HINT
    if "unrecognized data found in stream" in message:
        return _TUPLE_UNREADABLE
    if "in parent scope" in message:
        return _CORRELATED_SUBQUERY
    if is_transient_connection_error(message):
        return _CONNECTION_DROPPED
    code = _error_code(message)
    return _HINTS.get(code) if code is not None else None


def select_query_error(
    exc: BaseException,
    query: str | None = None,
    describe: DESCRIBE | None = None,
    list_tables: LIST_TABLES | None = None,
) -> dict[str, str]:
    """The tool reply for a failed query: {"error_message": ..., "hint": ...}.

    "hint" is present only for recognized errors. Messages that did not come
    from ClickHouse keep their text (only surrounding whitespace is trimmed).
    """
    message = str(exc)
    reply = {"error_message": _clean_message(message)}
    hint = _hint_for(message)
    detail = (
        _detail_for(message, query, describe or (lambda _t: None), list_tables or (lambda: None))
        if query
        else None
    )
    if hint or detail:
        reply["hint"] = " ".join(part for part in (detail, hint) if part)
    return reply
