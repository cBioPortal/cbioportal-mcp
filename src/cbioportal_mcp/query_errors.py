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

import re

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
    "(CAST and toFloat64 fail on them). Both branches of if/CASE, and both sides of a "
    "comparison or JOIN key, need compatible types."
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
    code = _error_code(message)
    return _HINTS.get(code) if code is not None else None


def select_query_error(exc: BaseException) -> dict[str, str]:
    """The tool reply for a failed query: {"error_message": ..., "hint": ...}.

    "hint" is present only for recognized errors. Messages that did not come
    from ClickHouse keep their text (only surrounding whitespace is trimmed).
    """
    message = str(exc)
    reply = {"error_message": _clean_message(message)}
    hint = _hint_for(message)
    if hint:
        reply["hint"] = hint
    return reply
