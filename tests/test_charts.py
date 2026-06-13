"""Tests for the generic chart tools (pie / bar / line), their payload builders,
and the shared input-normalization helpers.

These tools do not touch the database — the caller supplies the data — so unlike
``test_oncoprint.py`` there is no ``run_select_query`` to fake.
"""

import pytest

from cbioportal_mcp import server

# --- shared helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (3, 3.0),
        (4.5, 4.5),
        ("4.5", 4.5),
        ("  7 ", 7.0),
        ("-2", -2.0),
        (True, None),
        (False, None),
        (None, None),
        ("x", None),
        ("", None),
        ([], None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
    ],
)
def test_coerce_number(value, expected):
    assert server._coerce_number(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#2e8b57", "#2e8b57"),
        ("#abc", "#abc"),
        ("#abcd", "#abcd"),
        ("#11223344", "#11223344"),
        ("red", "red"),
        ("RED", "red"),
        ("rgb(1,2,3)", "rgb(1,2,3)"),
        ("rgba(1, 2, 3, 0.5)", "rgba(1, 2, 3, 0.5)"),
        ("#12", None),  # too short
        ("notacolor", None),
        ("javascript:alert(1)", None),
        ("url(x)", None),
        ("", None),
        ("   ", None),
        (123, None),
        (None, None),
    ],
)
def test_safe_color(value, expected):
    assert server._safe_color(value) == expected


def test_clamp_seq_records_warning():
    warnings: list = []
    out = server._clamp_seq(list(range(10)), 4, warnings, "items")
    assert out == [0, 1, 2, 3]
    assert warnings and "first 4 items of 10" in warnings[0]

    warnings2: list = []
    assert server._clamp_seq([1, 2], 4, warnings2, "items") == [1, 2]
    assert warnings2 == []


# --- pie ---------------------------------------------------------------------


def test_pie_payload_basic():
    p = server._build_pie_payload(
        [{"label": "A", "value": 3}, {"label": "B", "value": "7", "color": "#abc"}],
        title="T",
        donut=True,
    )
    assert p["kind"] == "pie"
    assert p["title"] == "T"
    assert p["donut"] is True
    assert p["total"] == 10.0
    assert [(s["label"], s["value"]) for s in p["slices"]] == [("A", 3.0), ("B", 7.0)]
    assert p["slices"][1]["color"] == "#abc"
    assert p["slices"][0].get("color") is None


def test_pie_payload_drops_bad_values_and_colors():
    p = server._build_pie_payload(
        [
            {"label": "ok", "value": 5, "color": "lime"},
            {"label": "neg", "value": -1},
            {"label": "nan", "value": "abc"},
            {"label": "badcolor", "value": 2, "color": "drop(me)"},
        ]
    )
    labels = [s["label"] for s in p["slices"]]
    assert labels == ["ok", "badcolor"]  # valid values kept, neg/non-numeric dropped
    by_label = {s["label"]: s for s in p["slices"]}
    assert by_label["ok"]["color"] == "lime"  # recognized color kept
    assert "color" not in by_label["badcolor"]  # unrecognized color stripped
    # negative + non-numeric + unrecognized color each produce a warning
    joined = " ".join(p["warnings"])
    assert "neg" in joined and "nan" in joined and "badcolor" in joined


def test_pie_payload_clamps():
    slices = [{"label": f"s{i}", "value": 1} for i in range(server.MAX_CHART_SLICES + 5)]
    p = server._build_pie_payload(slices)
    assert len(p["slices"]) == server.MAX_CHART_SLICES
    assert any("first" in w for w in p["warnings"])


@pytest.mark.parametrize("bad", [[], [{"label": "x", "value": "nope"}], "notalist"])
def test_pie_payload_invalid_raises(bad):
    with pytest.raises(ValueError):
        server._build_pie_payload(bad)


# --- bar ---------------------------------------------------------------------


def test_bar_payload_basic_multi_series():
    p = server._build_bar_payload(
        ["TP53", "KRAS"],
        [
            {"name": "Mut", "values": [40, 25], "color": "#1f77b4"},
            {"name": "Amp", "values": [5, 9]},
        ],
        orientation="vertical",
        stacked=True,
    )
    assert p["kind"] == "bar"
    assert p["orientation"] == "vertical"
    assert p["stacked"] is True
    assert p["categories"] == ["TP53", "KRAS"]
    assert p["series"][0]["values"] == [40.0, 25.0]
    assert p["series"][0]["color"] == "#1f77b4"
    assert p["series"][1].get("color") is None


def test_bar_payload_pads_and_truncates_to_categories():
    p = server._build_bar_payload(
        ["a", "b", "c"],
        [
            {"name": "short", "values": [1]},
            {"name": "long", "values": [1, 2, 3, 4, 5]},
            {"name": "withbad", "values": [1, "x", 3]},
        ],
    )
    assert p["series"][0]["values"] == [1.0, 0.0, 0.0]
    assert p["series"][1]["values"] == [1.0, 2.0, 3.0]
    assert p["series"][2]["values"] == [1.0, 0.0, 3.0]
    joined = " ".join(p["warnings"])
    assert "padded" in joined and "truncated" in joined and "non-numeric" in joined


def test_bar_payload_orientation_normalized():
    p = server._build_bar_payload(["a"], [{"name": "s", "values": [1]}], orientation="HORIZONTAL")
    assert p["orientation"] == "horizontal"


@pytest.mark.parametrize(
    "categories,series,orientation",
    [
        ([], [{"name": "s", "values": [1]}], "vertical"),
        (["a"], [], "vertical"),
        (["a"], [{"name": "s", "values": [1]}], "diagonal"),
        (["a"], [{"name": "s", "values": "notalist"}], "vertical"),
    ],
)
def test_bar_payload_invalid_raises(categories, series, orientation):
    with pytest.raises(ValueError):
        server._build_bar_payload(categories, series, orientation=orientation)


def test_bar_payload_clamps_series_and_categories():
    cats = [f"c{i}" for i in range(server.MAX_CHART_CATEGORIES + 3)]
    series = [{"name": f"s{i}", "values": [1]} for i in range(server.MAX_CHART_SERIES + 4)]
    p = server._build_bar_payload(cats, series)
    assert len(p["categories"]) == server.MAX_CHART_CATEGORIES
    assert len(p["series"]) == server.MAX_CHART_SERIES


# --- line --------------------------------------------------------------------


def test_line_payload_numeric_shared_x():
    p = server._build_line_payload(
        [{"name": "OS", "y": [100, 80, "60"]}],
        x=[0, 12, 24],
    )
    assert p["kind"] == "line"
    assert p["x_is_numeric"] is True
    assert p["series"][0]["x"] == [0.0, 12.0, 24.0]
    assert p["series"][0]["y"] == [100.0, 80.0, 60.0]


def test_line_payload_per_series_x_overrides_shared():
    p = server._build_line_payload(
        [
            {"name": "a", "y": [1, 2], "x": [5, 6]},
            {"name": "b", "y": [3, 4]},
        ],
        x=[0, 1],
    )
    assert p["series"][0]["x"] == [5.0, 6.0]
    assert p["series"][1]["x"] == [0.0, 1.0]


def test_line_payload_defaults_to_indices():
    p = server._build_line_payload([{"name": "a", "y": [9, 8, 7]}])
    assert p["series"][0]["x"] == [0, 1, 2]
    assert p["x_is_numeric"] is True


def test_line_payload_categorical_x():
    p = server._build_line_payload([{"name": "a", "y": [1, 2, 3], "x": ["Q1", "Q2", "Q3"]}])
    assert p["x_is_numeric"] is False
    assert p["series"][0]["x"] == ["Q1", "Q2", "Q3"]


def test_line_payload_mixed_x_forces_categorical_strings():
    # one numeric series, one categorical -> the whole chart becomes categorical
    p = server._build_line_payload(
        [
            {"name": "num", "y": [1, 2], "x": [10, 20]},
            {"name": "cat", "y": [3, 4], "x": ["lo", "hi"]},
        ]
    )
    assert p["x_is_numeric"] is False
    assert p["series"][0]["x"] == ["10", "20"]
    assert p["series"][1]["x"] == ["lo", "hi"]


def test_line_payload_aligns_x_to_y():
    p = server._build_line_payload([{"name": "a", "y": [1, 2, 3, 4], "x": [0, 1]}])
    assert len(p["series"][0]["x"]) == 4
    assert any("x shorter than y" in w for w in p["warnings"])


def test_line_payload_clamps_points():
    y = list(range(server.MAX_CHART_POINTS + 10))
    p = server._build_line_payload([{"name": "a", "y": y}])
    assert len(p["series"][0]["y"]) == server.MAX_CHART_POINTS


@pytest.mark.parametrize(
    "series",
    [[], [{"name": "a"}], [{"name": "a", "y": []}], "notalist"],
)
def test_line_payload_invalid_raises(series):
    with pytest.raises(ValueError):
        server._build_line_payload(series)


# --- tool wrappers (error contract) ------------------------------------------


def test_pie_chart_tool_error_and_happy_path():
    err = server.pie_chart([])
    assert err["error"] and err["kind"] == "pie" and err["slices"] == []
    ok = server.pie_chart([{"label": "a", "value": 1}])
    assert "error" not in ok and ok["kind"] == "pie"


def test_bar_chart_tool_error_and_happy_path():
    err = server.bar_chart(["a"], [{"name": "s", "values": [1]}], orientation="diagonal")
    assert err["error"] and err["kind"] == "bar"
    assert err["categories"] == [] and err["series"] == []
    ok = server.bar_chart(["a", "b"], [{"name": "s", "values": [1, 2]}])
    assert "error" not in ok and ok["kind"] == "bar"


def test_line_chart_tool_error_and_happy_path():
    err = server.line_chart([])
    assert err["error"] and err["kind"] == "line" and err["series"] == []
    ok = server.line_chart([{"name": "s", "y": [1, 2, 3]}])
    assert "error" not in ok and ok["kind"] == "line"


# --- widget resource ---------------------------------------------------------


def test_charts_widget_html_loads():
    html = server.ui.load_widget("charts.html")
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "Widget asset not found" not in html
