"""Compact tool-result formatting.

Every tool result stays in the conversation, so each later model round
re-reads it. A list of per-row dicts repeats every column name on every row;
the compact format lists the columns once and sends each row as an array:

    {"columns": ["hugo_gene_symbol", "altered_samples"],
     "rows": [["TP53", 412], ["PIK3CA", 351]],
     "row_count": 2}

Environment:
    CBIOPORTAL_MCP_RESULT_FORMAT: "compact" (default) or "rows" (the previous
        list-of-dicts shape, unchanged).
    CBIOPORTAL_MCP_MAX_RESULT_ROWS: default max_rows for
        clickhouse_run_select_query (default 100).
    CBIOPORTAL_MCP_MAX_CELL_CHARS: longest text cell kept in a compact SELECT
        result before it is cut (default 500; 0 disables the cut).

Settings are read on each call, so they can be changed without a restart in
tests; an invalid value logs a warning and uses the default.
"""

import datetime
import decimal
import json
import logging
import os
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

RESULT_FORMAT_ENV = "CBIOPORTAL_MCP_RESULT_FORMAT"
MAX_RESULT_ROWS_ENV = "CBIOPORTAL_MCP_MAX_RESULT_ROWS"
MAX_CELL_CHARS_ENV = "CBIOPORTAL_MCP_MAX_CELL_CHARS"

COMPACT = "compact"
LEGACY = "rows"
RESULT_FORMATS = (COMPACT, LEGACY)

DEFAULT_MAX_RESULT_ROWS = 100
DEFAULT_MAX_CELL_CHARS = 500


def result_format() -> str:
    value = os.getenv(RESULT_FORMAT_ENV, COMPACT).strip().lower()
    if value not in RESULT_FORMATS:
        logger.warning(
            f"{RESULT_FORMAT_ENV}={value!r} is not one of {RESULT_FORMATS}; using {COMPACT}"
        )
        return COMPACT
    return value


def is_compact() -> bool:
    return result_format() == COMPACT


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if value < minimum:
        logger.warning(f"{name}={raw!r} is not an integer >= {minimum}; using {default}")
        return default
    return value


def default_max_rows() -> int:
    return _int_env(MAX_RESULT_ROWS_ENV, DEFAULT_MAX_RESULT_ROWS, minimum=1)


def max_cell_chars() -> int:
    return _int_env(MAX_CELL_CHARS_ENV, DEFAULT_MAX_CELL_CHARS, minimum=0)


def _jsonable(value: Any) -> Any:
    """Plain JSON value for one cell.

    Rows from mcp-clickhouse are already JSON-decoded (dates, decimals and
    64-bit integers arrive as strings), so this mostly passes values through;
    it also covers native Python values from other callers.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _cut_cell(value: Any, limit: int) -> tuple[Any, bool]:
    """Cut a cell longer than limit characters; returns (value, was_cut).

    Arrays and maps are measured as compact JSON and, when too long, replaced
    by their cut JSON text.
    """
    if not limit or value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return value, False
    return f"{text[:limit]}…[+{len(text) - limit} chars]", True


def compact_table(
    columns: Sequence[str], rows: Iterable[Sequence[Any]], *, cell_limit: int = 0
) -> dict:
    """{"columns": [...], "rows": [[...], ...]} with an optional per-cell cap.

    Adds "cut_cells": n when any cell was cut.
    """
    out_rows = []
    cut = 0
    for row in rows:
        out_row = []
        for value in row:
            value, was_cut = _cut_cell(_jsonable(value), cell_limit)
            cut += was_cut
            out_row.append(value)
        out_rows.append(out_row)
    table = {"columns": list(columns), "rows": out_rows}
    if cut:
        table["cut_cells"] = cut
    return table


def records_to_table(records: Sequence[dict], columns: Sequence[str] | None = None) -> dict:
    """Compact table from a list of dicts. Missing keys become null.

    columns defaults to the keys in order of first appearance, which only
    matches the query's column order when the first row has every key.
    Pass columns explicitly when it is known.
    """
    if columns is None:
        columns = list(dict.fromkeys(key for record in records for key in record))
    return compact_table(columns, ([record.get(c) for c in columns] for record in records))


def table_to_records(table: dict) -> list[dict]:
    """A tool result's "rows" as dicts, in either format (for tests and callers that want dicts)."""
    if "columns" not in table or not isinstance(table["columns"], list):
        return table["rows"]
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


def select_result(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], *, max_rows: int, hard_max_rows: int
) -> dict:
    """Compact clickhouse_run_select_query result.

    rows holds at most max_rows + 1 rows (the query is capped in ClickHouse
    at max_rows + 1), so one extra row means the result was truncated. The
    true total is then unknown: counting it would need a second, uncapped
    query, which defeats stopping early.
    """
    truncated = len(rows) > max_rows
    shown = rows[:max_rows]
    result = compact_table(columns, shown, cell_limit=max_cell_chars())
    result["row_count"] = len(shown)
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"{len(shown)} of more than {len(shown)} rows returned (exact total_rows not "
            "computed). Don't page through rows: aggregate (GROUP BY / COUNT) or filter in "
            f"SQL, or pass a larger max_rows (up to {hard_max_rows}) only if every row is needed."
        )
    if result.get("cut_cells"):
        result["cell_note"] = (
            f"{result['cut_cells']} cell(s) longer than {max_cell_chars()} chars were cut "
            "(marked '…[+N chars]'); select substring(col, 1, N) or a narrower column to see more."
        )
    return result
