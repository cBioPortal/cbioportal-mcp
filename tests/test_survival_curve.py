"""Tests for the survival_curve tool, its helpers, and the data contract.

The database is faked by monkeypatching ``run_select_query`` on the server
module, so these run without a live ClickHouse instance.
"""

import pytest

from cbioportal_mcp import server

# --- status parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1:DECEASED", 1),
        ("0:LIVING", 0),
        ("1", 1),
        ("0", 0),
        ("DECEASED", 1),
        ("LIVING", 0),
        ("1:Recurred", 1),
        ("0:DiseaseFree", 0),
        ("Progression Free", 0),
        ("Progressed", 1),
        ("", None),
        (None, None),
        ("unknown-label", None),
    ],
)
def test_parse_survival_status(value, expected):
    assert server._parse_survival_status(value) == expected


# --- endpoint validation -----------------------------------------------------


def test_validate_endpoint_normalizes_case():
    assert server._validate_endpoint("os") == "OS"
    assert server._validate_endpoint(" PFS ") == "PFS"


def test_validate_endpoint_rejects_unknown():
    with pytest.raises(ValueError):
        server._validate_endpoint("ZZZ")


def test_survival_time_ticks_span_range():
    ticks = server._survival_time_ticks(100)
    assert ticks[0] == 0
    assert ticks[-1] >= 100
    assert ticks == sorted(ticks)


# --- fake DB helpers ---------------------------------------------------------


def _survival_rows(data):
    """data: list of (pid, time, event). event=1 deceased, 0 living."""
    return [
        {
            "patient_unique_id": pid,
            "time": float(t),
            "status": "1:DECEASED" if e else "0:LIVING",
        }
        for pid, t, e in data
    ]


def _make_fake(survival_data, altered=None, clinical=None):
    altered = altered or []
    clinical = clinical or {}

    def fake_query(q):
        if "_MONTHS" in q:
            return _survival_rows(survival_data)
        if "genomic_event_derived" in q:
            return [{"patient_unique_id": p} for p in altered]
        if "attribute_name = '" in q:  # single-attribute clinical grouping
            return [
                {"patient_unique_id": pid, "attribute_value": val} for pid, val in clinical.items()
            ]
        return []

    return fake_query


SIX = [("p1", 10, 1), ("p2", 20, 0), ("p3", 5, 1), ("p4", 30, 0), ("p5", 15, 1), ("p6", 25, 0)]


# --- _build_survival_payload -------------------------------------------------


def test_payload_whole_cohort(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake(SIX))
    p = server._build_survival_payload("study", "OS", None, None, None)
    assert p["endpoint"] == "OS"
    assert p["endpoint_label"] == "Overall Survival"
    assert p["grouping"] == {"type": "none"}
    assert len(p["groups"]) == 1
    g = p["groups"][0]
    assert g["name"] == "All patients"
    assert g["n_patients"] == 6
    assert g["n_events"] == 3
    assert p["stats"] is None  # single group => no log-rank
    assert p["time_ticks"][0] == 0


def test_payload_group_by_gene(monkeypatch):
    monkeypatch.setattr(
        server,
        "run_select_query",
        _make_fake(SIX, altered=["p1", "p3", "p5"]),
    )
    p = server._build_survival_payload("study", "OS", "TP53", ["mutation"], None)
    assert p["grouping"] == {"type": "alteration", "gene": "TP53", "alteration_types": ["mutation"]}
    names = {g["name"]: g for g in p["groups"]}
    assert "TP53 altered" in names and "TP53 wild-type" in names
    assert names["TP53 altered"]["n_patients"] == 3
    assert names["TP53 altered"]["n_events"] == 3
    assert names["TP53 wild-type"]["n_events"] == 0
    assert p["stats"]["test"] == "log-rank"
    assert p["stats"]["df"] == 1
    assert p["stats"]["p_value"] is not None


def test_payload_group_by_clinical(monkeypatch):
    clinical = {
        "p1": "LumA",
        "p2": "LumA",
        "p3": "LumA",
        "p4": "Basal",
        "p5": "Basal",
        "p6": "Basal",
    }
    monkeypatch.setattr(server, "run_select_query", _make_fake(SIX, clinical=clinical))
    p = server._build_survival_payload("study", "OS", None, None, "SUBTYPE")
    assert p["grouping"] == {"type": "clinical", "attribute": "SUBTYPE"}
    names = {g["name"] for g in p["groups"]}
    assert names == {"SUBTYPE: LumA", "SUBTYPE: Basal"}


def test_payload_no_survival_data(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake([]))
    p = server._build_survival_payload("study", "OS", None, None, None)
    assert "error" in p
    assert p["groups"] == []
    assert p["endpoint"] == "OS"


def test_payload_warns_on_dropped_patients(monkeypatch):
    data = [("p1", 10, 1), ("p2", 20, 0)]
    rows = _survival_rows(data)
    rows.append({"patient_unique_id": "p3"})  # missing time + status

    def fake(q):
        return rows if "_MONTHS" in q else []

    monkeypatch.setattr(server, "run_select_query", fake)
    p = server._build_survival_payload("study", "OS", None, None, None)
    assert any("excluded" in w for w in p["warnings"])
    assert p["groups"][0]["n_patients"] == 2


# --- confidence band in the payload contract ---------------------------------


def test_payload_curve_points_carry_confidence_limits(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake(SIX))
    p = server._build_survival_payload("study", "OS", None, None, None)
    curve = p["groups"][0]["curve"]
    assert curve, "expected a non-empty curve"
    for pt in curve:
        assert "ci_lower" in pt and "ci_upper" in pt
        assert 0.0 <= pt["ci_lower"] <= pt["survival"] <= pt["ci_upper"] <= 1.0


def test_payload_declares_the_band_it_drew(monkeypatch):
    """The widget labels the band off conf_level; it must not guess."""
    monkeypatch.setattr(server, "run_select_query", _make_fake(SIX))
    p = server._build_survival_payload("study", "OS", None, None, None)
    assert p["conf_level"] == 0.95
    assert p["band"]["conf_level"] == 0.95
    assert p["band"]["scope"] == "pointwise"
    assert "Greenwood" in p["band"]["method"]


def test_band_caveat_is_not_duplicated_into_the_rendered_notes(monkeypatch):
    """'notes' renders verbatim under the figure, which already captions itself.

    Pins the split so a future edit does not push the band prose back into the
    string the widget prints.
    """
    monkeypatch.setattr(server, "run_select_query", _make_fake(SIX))
    p = server._build_survival_payload("study", "OS", None, None, None)
    assert "pointwise" not in p["notes"].lower()
    assert "log-rank" in p["band"]["note"].lower()


def test_band_widens_in_the_sparse_tail(monkeypatch):
    """The band must actually fan out, not sit at constant width.

    Uses the Freireich 6-MP arm: events keep coming while censoring thins the
    risk set, so the estimate stays mid-range and the band widens visibly. Made
    up of real times rather than a synthetic ramp -- with no censoring the curve
    reaches S=0 and the band is squeezed against the floor instead, which is
    correct but tests nothing.
    """
    times = [6, 6, 6, 7, 10, 13, 16, 22, 23]
    censored = [6, 9, 10, 11, 17, 19, 20, 25, 32, 32, 34, 35]
    data = [(f"e{i}", t, 1) for i, t in enumerate(times)]
    data += [(f"c{i}", t, 0) for i, t in enumerate(censored)]
    monkeypatch.setattr(server, "run_select_query", _make_fake(data))
    p = server._build_survival_payload("study", "OS", None, None, None)
    curve = [pt for pt in p["groups"][0]["curve"] if pt["events"]]
    early = curve[0]["ci_upper"] - curve[0]["ci_lower"]
    late = curve[-1]["ci_upper"] - curve[-1]["ci_lower"]
    assert late > early * 1.2


# --- survival_curve tool wrapper (error contract) ----------------------------


def test_survival_curve_bad_study_id():
    out = server.survival_curve("bad id!")
    assert "error" in out
    assert out["groups"] == []
    assert out["endpoint"] == "OS"


def test_survival_curve_bad_endpoint():
    out = server.survival_curve("valid_study", endpoint="ZZZ")
    assert "error" in out
    assert out["groups"] == []
    assert out["endpoint"] == "ZZZ"


def test_survival_curve_happy_path(monkeypatch):
    monkeypatch.setattr(server, "run_select_query", _make_fake(SIX))
    out = server.survival_curve("valid_study")
    assert "error" not in out
    assert out["groups"][0]["n_patients"] == 6
