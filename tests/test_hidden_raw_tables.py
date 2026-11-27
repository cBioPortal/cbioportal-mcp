import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def _hidden_tables():
    text = (SQL_DIR / "9-hide-raw-tables.sql").read_text()
    return set(re.findall(r"^DROP TABLE IF EXISTS (\w+) SYNC;", text, re.M))


def _view_bodies():
    for path in sorted(SQL_DIR.glob("[0-8]-*.sql")):
        text = re.sub(r"--[^\n]*", "", path.read_text())
        for statement in text.split(";"):
            if re.search(r"CREATE\s+VIEW", statement):
                yield path.name, statement


def test_no_view_reads_a_hidden_table():
    hidden = _hidden_tables()
    assert hidden
    offenders = []
    for name, statement in _view_bodies():
        for table in re.findall(r"(?:FROM|JOIN)\s+(\w+)", statement):
            if table in hidden:
                offenders.append((name, table))
    assert not offenders


def test_hidden_list_keeps_the_tables_views_and_guides_rely_on():
    keep = {
        "cancer_study",
        "type_of_cancer",
        "genetic_profile",
        "clinical_attribute_meta",
        "gene",
        "resource",
        "resource_definition",
        "gene_panel_to_gene_derived",
    }
    assert not keep & _hidden_tables()
