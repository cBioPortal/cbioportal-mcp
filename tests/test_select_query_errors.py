import re

import mcp_clickhouse.mcp_server as ch_mcp_server
import pytest
from fastmcp.exceptions import ToolError

from cbioportal_mcp import query_errors, server

# Messages as mcp-clickhouse 0.5.0 raises them, captured from ClickHouse 25.8.
_HEADER = (
    "Query execution failed: HTTPDriver for http://ch.internal:8123 received ClickHouse error code"
)
_VERSION = " (version 25.8.33.6 (official build))\n"


def _ch_error(code: int, text: str, name: str) -> str:
    return f"{_HEADER} {code}\n Code: {code}. DB::Exception: {text}. ({name}){_VERSION}"


UNKNOWN_IDENTIFIER = _ch_error(
    47,
    "Unknown expression identifier `foo` in scope SELECT foo FROM clinical_data_derived",
    "UNKNOWN_IDENTIFIER",
)
ILLEGAL_AGGREGATION = _ch_error(
    184,
    "Aggregate function count() is found inside another aggregate function in query",
    "ILLEGAL_AGGREGATION",
)
CANNOT_PARSE = _ch_error(
    27,
    "Cannot parse NaN: Cannot parse Float64 from String: while executing "
    "'FUNCTION toFloat64(__table1.attribute_value :: 1) -> toFloat64(__table1.attribute_value) "
    "Float64 : 0'",
    "CANNOT_PARSE_INPUT_ASSERTION_FAILED",
)
MEMORY_LIMIT = _ch_error(
    241,
    "(total) memory limit exceeded: would use 1.75 GiB (attempt to allocate chunk of 1.00 GiB "
    "bytes), current RSS: 1008.56 MiB, maximum: 1.80 GiB",
    "MEMORY_LIMIT_EXCEEDED",
)
# clickhouse-connect cuts messages at 1024 characters, which drops the error
# name and version from syntax errors that list every expected token.
TRUNCATED_SYNTAX_ERROR = (
    f"{_HEADER} 62\n Code: 62. DB::Exception: Syntax error: failed at position 1 (SELEC) "
    "(line 1, col 1): SELEC 1\n FORMAT Native. Expected one of: Query, Query with output, "
    "EXPLAIN, SELECT query, possibly with UNION, list of union elements, SHOW, "
)


def _run_failing_query(monkeypatch, message: str) -> dict:
    def fail(query):
        raise ToolError(message)

    monkeypatch.setattr(ch_mcp_server, "run_query", fail)
    return server.clickhouse_run_select_query.fn.__wrapped__(
        "SELECT foo FROM clinical_data_derived"
    )


@pytest.mark.parametrize(
    ("message", "hint_phrase"),
    [
        (UNKNOWN_IDENTIFIER, "clickhouse_list_table_columns"),
        (
            _ch_error(60, "Unknown table expression identifier 'nope'", "UNKNOWN_TABLE"),
            "clickhouse_list_tables",
        ),
        (
            _ch_error(
                46,
                "Function with name `median_exact` does not exist. Maybe you meant: "
                "['medianExact','medianExactLow']",
                "UNKNOWN_FUNCTION",
            ),
            "Maybe you meant",
        ),
        (
            _ch_error(
                42,
                "Aggregate function quantile requires single argument",
                "NUMBER_OF_ARGUMENTS_DOESNT_MATCH",
            ),
            "quantile(0.5)(x)",
        ),
        (ILLEGAL_AGGREGATION, "HAVING"),
        (
            _ch_error(
                215,
                "Column 'attribute_value' is not under aggregate function and not in "
                "GROUP BY keys",
                "NOT_AN_AGGREGATE",
            ),
            "GROUP BY",
        ),
        (CANNOT_PARSE, "toFloat64OrNull"),
        (
            _ch_error(
                43,
                "Illegal type String of argument for aggregate function avg",
                "ILLEGAL_TYPE_OF_ARGUMENT",
            ),
            "toFloat64OrNull",
        ),
        (
            _ch_error(386, "There is no supertype for types String, UInt8", "NO_COMMON_TYPE"),
            "toFloat64OrNull",
        ),
        (MEMORY_LIMIT, "sample_unique_id"),
        (_ch_error(164, "mcp: Cannot execute query in readonly mode", "READONLY"), "SETTINGS"),
        (TRUNCATED_SYNTAX_ERROR, "single SELECT"),
        ("Query timed out after 30 seconds", "retry"),
    ],
)
def test_common_errors_get_a_hint(monkeypatch, message, hint_phrase):
    result = _run_failing_query(monkeypatch, message)

    assert set(result) == {"error_message", "hint"}
    assert hint_phrase in result["hint"]


def test_error_message_drops_driver_header_and_version(monkeypatch):
    result = _run_failing_query(monkeypatch, UNKNOWN_IDENTIFIER)

    assert result["error_message"] == (
        "Code: 47. DB::Exception: Unknown expression identifier `foo` in scope "
        "SELECT foo FROM clinical_data_derived. (UNKNOWN_IDENTIFIER)"
    )


def test_truncated_message_keeps_its_text_without_the_header(monkeypatch):
    result = _run_failing_query(monkeypatch, TRUNCATED_SYNTAX_ERROR)

    assert result["error_message"].startswith("Code: 62. DB::Exception: Syntax error")
    assert "HTTPDriver" not in result["error_message"]
    assert "ch.internal" not in result["error_message"]


def test_http_error_without_clickhouse_body_hides_the_host(monkeypatch):
    message = (
        "Query execution failed: HTTPDriver for http://ch.internal:8123 returned response code 502"
    )

    result = _run_failing_query(monkeypatch, message)

    assert result == {
        "error_message": "Query execution failed: ClickHouse returned response code 502"
    }


def test_error_without_clickhouse_text_gets_its_hint_from_the_header(monkeypatch):
    result = _run_failing_query(monkeypatch, f"{_HEADER} 241")

    assert result["error_message"] == (
        "Query execution failed: ClickHouse received ClickHouse error code 241"
    )
    assert result["hint"].startswith("The query ran out of memory.")


def test_unrecognized_clickhouse_error_has_no_hint(monkeypatch):
    message = _ch_error(999, "Something new", "SOMETHING_NEW")

    result = _run_failing_query(monkeypatch, message)

    assert result == {"error_message": "Code: 999. DB::Exception: Something new. (SOMETHING_NEW)"}


def test_non_clickhouse_error_passes_through_unchanged(monkeypatch):
    def fail(query, query_label=None, max_rows=None):
        raise ValueError("bad max_rows")

    monkeypatch.setattr(server, "run_select_query", fail)

    result = server.clickhouse_run_select_query.fn.__wrapped__("SELECT 1")

    assert result == {"error_message": "bad max_rows"}


def test_hints_only_name_tools_the_server_registers():
    hints = set(query_errors._HINTS.values()) | {query_errors._CLIENT_TIMEOUT_HINT}
    named_tools = {name for hint in hints for name in re.findall(r"\bclickhouse_\w+", hint)}

    assert named_tools == {"clickhouse_list_tables", "clickhouse_list_table_columns"}
    for name in named_tools:
        assert hasattr(server, name), name


def test_guide_examples_convert_clinical_values_the_way_the_hint_says():
    """CAST/toFloat64 on attribute_value fail on 'NA'; example SQL must use toFloat64OrNull."""
    unsafe = re.compile(
        r"CAST\(\s*attribute_value\s+AS\s+Float|toFloat64\(\s*attribute_value\s*\)", re.I
    )
    code_block = re.compile(r"```.*?```", re.S)
    resources = server._get_resources_path()

    offenders = [
        path.relative_to(resources).as_posix()
        for path in resources.rglob("*.md")
        for block in code_block.findall(path.read_text(encoding="utf-8"))
        if unsafe.search(block)
    ]

    assert offenders == []
