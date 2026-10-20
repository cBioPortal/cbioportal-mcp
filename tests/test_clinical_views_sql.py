import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SQL = (ROOT / "sql" / "7-clinical-views.sql").read_text()
RESOURCES = ROOT / "src" / "cbioportal_mcp" / "resources"


def _created_views():
    return re.findall(r"^CREATE VIEW (\w+) AS", SQL, flags=re.MULTILINE)


def test_every_view_is_dropped_before_create():
    views = _created_views()
    assert views
    for view in views:
        assert f"DROP VIEW IF EXISTS {view};" in SQL, f"{view} not re-runnable"


def test_tables_are_unqualified():
    assert "cbioportal_public" not in SQL


def test_every_view_is_documented_in_clinical_data_guide():
    guide = (RESOURCES / "clinical-data-guide.md").read_text()
    for view in _created_views():
        assert view in guide, f"{view} not documented in clinical-data-guide.md"


def test_treatment_guide_points_to_treatment_views():
    guide = (RESOURCES / "treatment-guide.md").read_text()
    for view in _created_views():
        if view.startswith("treatment_"):
            assert view in guide, f"{view} not referenced in treatment-guide.md"
