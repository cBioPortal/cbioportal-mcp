import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SQL_4 = REPO / "sql" / "4-mutation-frequency-views.sql"
GUIDE = REPO / "src" / "cbioportal_mcp" / "resources" / "mutation-frequency-guide.md"
SQL_README = REPO / "sql" / "README.md"


def _views():
    return re.findall(r"^CREATE VIEW (\w+) AS", SQL_4.read_text(), re.M)


def test_every_view_is_dropped_before_create():
    sql = SQL_4.read_text()
    for view in _views():
        assert f"DROP VIEW IF EXISTS {view};" in sql, view


def test_every_view_is_documented_in_guide():
    guide = GUIDE.read_text()
    missing = [v for v in _views() if f"`{v}" not in guide]
    assert not missing, f"views missing from mutation-frequency-guide.md: {missing}"


def test_every_view_is_listed_in_sql_readme():
    readme = SQL_README.read_text()
    missing = [v for v in _views() if f"`{v}`" not in readme]
    assert not missing, f"views missing from sql/README.md: {missing}"


def test_views_do_not_reference_database_names():
    sql = re.sub(r"--[^\n]*", "", SQL_4.read_text())
    assert not re.search(r"\b(FROM|JOIN)\s+\w+\.\w+", sql)
