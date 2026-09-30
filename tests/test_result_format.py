"""Unit tests for the compact tool-result formatter."""

import datetime
import decimal
import json
import logging
import re

import pydantic_core
import pytest

from cbioportal_mcp import result_format as rf


def _select(columns, rows, max_rows=100):
    return rf.select_result(columns, rows, max_rows=max_rows, hard_max_rows=10000)


def test_empty_result():
    assert _select(["a", "b"], []) == {"columns": ["a", "b"], "rows": [], "row_count": 0}


def test_empty_result_without_columns():
    assert _select([], []) == {"columns": [], "rows": [], "row_count": 0}


def test_nulls_stay_positional():
    out = _select(["a", "b", "c"], [[None, 1, None], ["", None, 0]])
    assert out["rows"] == [[None, 1, None], ["", None, 0]]


def test_dates_and_decimals_become_json_strings():
    row = [
        datetime.date(2026, 9, 29),
        datetime.datetime(2026, 9, 29, 13, 5, 0),
        decimal.Decimal("12.3450"),
        1.5,
        2**64,
    ]
    out = _select(["d", "dt", "dec", "f", "big"], [row])
    assert out["rows"] == [["2026-09-29", "2026-09-29T13:05:00", "12.3450", 1.5, 2**64]]
    # Serializes the way FastMCP does, without a str() fallback.
    pydantic_core.to_json(out)


def test_clickhouse_json_values_pass_through():
    """mcp-clickhouse already renders dates/decimals/UInt64 as strings (default=str)."""
    row = ["2026-09-29 13:05:00", "12.345", "18446744073709551615", ["a", "b"], {"k": 1}, True]
    assert _select(list("abcdef"), [row])["rows"] == [row]


def test_unicode_is_kept_and_counted_in_characters():
    out = rf.compact_table(["t"], [["Ménétrier–病"], ["é" * 6]], cell_limit=5)
    assert out["rows"] == [["Ménét…[+6 chars]"], ["ééééé…[+1 chars]"]]
    assert "Ménétrier" in pydantic_core.to_json(
        rf.compact_table(["t"], [["Ménétrier"]])
    ).decode()


def test_long_cells_are_cut_and_counted(monkeypatch):
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, "8")
    out = _select(["t", "n"], [["abcdefghij", 123456789012], ["short", 1]])
    assert out["rows"] == [["abcdefgh…[+2 chars]", 123456789012], ["short", 1]]
    assert out["cut_cells"] == {"t": 1}
    assert "8 chars in column(s) t" in out["cell_note"]
    assert "substringUTF8(t, 9, 8)" in out["cell_note"]
    assert "substringUTF8(t, 17, 8)" in out["cell_note"]
    assert "lengthUTF8" in out["cell_note"]


def test_numbers_are_never_cut(monkeypatch):
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, "3")
    row = [123456789012, 3.14159265358979, 2**64, True]
    assert _select(list("abcd"), [row])["rows"] == [row]


@pytest.mark.parametrize(
    "value",
    [
        list(range(200)),
        (1, "x" * 50, 3),
        {"k": "v" * 50, "n": 12345678},
        [["nested", list(range(40))], {"m": [1.5, None]}],
    ],
)
def test_structured_cells_are_sent_intact(monkeypatch, value):
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, "10")
    out = _select(["s"], [[value]])
    expected = list(value) if isinstance(value, tuple) else value
    assert out["rows"] == [[expected]]
    assert "cut_cells" not in out and "cell_note" not in out


def test_cut_cells_are_counted_per_column(monkeypatch):
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, "4")
    out = _select(["a", "b"], [["xxxxx", "ok"], ["yyyyy", "zzzzz"], ["ok", "ok"]])
    assert out["cut_cells"] == {"a": 2, "b": 1}
    assert "column(s) a, b" in out["cell_note"]


def _substring_utf8(text, offset, length):
    """ClickHouse substringUTF8: 1-based, counts characters."""
    return text[offset - 1 : offset - 1 + length]


@pytest.mark.parametrize("limit", [8, rf.DEFAULT_MAX_CELL_CHARS])
def test_following_the_cell_note_recovers_the_whole_text(monkeypatch, limit):
    """Fetch the tail the way the note says; no chunk is cut again."""
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, str(limit))
    original = "".join(chr(0x61 + i % 26) if i % 7 else "é" for i in range(limit * 3 + 5))
    out = _select(["description"], [[original]])
    head = out["rows"][0][0]
    assert head.endswith(f"…[+{len(original) - limit} chars]")
    note = out["cell_note"]
    first = re.search(r"substringUTF8\(description, (\d+), (\d+)\)", note)
    offset, length = int(first.group(1)), int(first.group(2))
    assert (offset, length) == (limit + 1, limit)

    recovered = head[:limit]
    while offset <= len(original):  # lengthUTF8(description) tells the model when to stop
        chunk = _select(["chunk"], [[_substring_utf8(original, offset, length)]])
        assert "cut_cells" not in chunk
        recovered += chunk["rows"][0][0]
        offset += length
    assert recovered == original


def test_cell_limit_zero_disables_cutting(monkeypatch):
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, "0")
    out = _select(["t"], [["x" * 5000]])
    assert out["rows"] == [["x" * 5000]] and "cut_cells" not in out


def test_default_cell_limit():
    assert rf.DEFAULT_MAX_CELL_CHARS == 2000
    assert "cut_cells" not in _select(["t"], [["x" * 2000]])
    assert _select(["t"], [["x" * 2001]])["cut_cells"] == {"t": 1}


def test_truncation_reports_returned_rows_without_inventing_a_total():
    rows = [[i] for i in range(11)]  # capped query returns max_rows + 1
    out = _select(["i"], rows, max_rows=10)
    assert out["rows"] == [[i] for i in range(10)]
    assert out["row_count"] == 10 and out["truncated"] is True
    assert "total_rows" not in out
    assert "10 of more than 10 rows" in out["note"]
    assert "aggregate" in out["note"] and "up to 10000" in out["note"]


def test_exactly_max_rows_is_not_truncated():
    out = _select(["i"], [[i] for i in range(10)], max_rows=10)
    assert out["row_count"] == 10 and "truncated" not in out


def test_records_round_trip_with_explicit_columns():
    records = [{"a": 1, "b": "x"}, {"a": 2}]
    table = rf.records_to_table(records, ("a", "b"))
    assert table == {"columns": ["a", "b"], "rows": [[1, "x"], [2, None]]}
    assert rf.table_to_records(table) == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]


def test_records_columns_inferred_in_first_appearance_order():
    assert rf.records_to_table([{"a": 1}, {"b": 2, "a": 3}])["columns"] == ["a", "b"]


def test_table_to_records_passes_legacy_rows_through():
    rows = [{"a": 1}]
    assert rf.table_to_records({"rows": rows}) is rows


@pytest.mark.parametrize(
    "value, expected",
    [(None, "compact"), ("compact", "compact"), (" ROWS ", "rows"), ("bogus", "compact")],
)
def test_result_format_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(rf.RESULT_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(rf.RESULT_FORMAT_ENV, value)
    assert rf.result_format() == expected


@pytest.mark.parametrize("value", ["abc", "-1", "", "   "])
def test_invalid_int_env_warns_and_falls_back_to_default(monkeypatch, caplog, value):
    monkeypatch.setenv(rf.MAX_CELL_CHARS_ENV, value)
    monkeypatch.setenv(rf.MAX_RESULT_ROWS_ENV, value)
    with caplog.at_level(logging.WARNING, logger=rf.logger.name):
        assert rf.max_cell_chars() == rf.DEFAULT_MAX_CELL_CHARS
        assert rf.default_max_rows() == rf.DEFAULT_MAX_RESULT_ROWS
    warned = [r.getMessage() for r in caplog.records]
    assert any(rf.MAX_CELL_CHARS_ENV in m for m in warned)
    assert any(rf.MAX_RESULT_ROWS_ENV in m for m in warned)


def test_compact_is_smaller_than_list_of_dicts():
    columns = ["hugo_gene_symbol", "altered_samples", "profiled_samples", "frequency_pct"]
    rows = [[f"GENE{i}", i, 1000, i / 10] for i in range(20)]
    records = [dict(zip(columns, r, strict=True)) for r in rows]
    legacy = json.dumps({"rows": records}, separators=(",", ":"))
    compact = json.dumps(_select(columns, rows), separators=(",", ":"))
    assert len(compact) < 0.6 * len(legacy)
