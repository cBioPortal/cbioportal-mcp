"""Keep caller privileges on every ordinary view, including portal-specific SQL."""

import re
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
# Mask comments and quoted text before inspecting DDL. A comment, column name,
# or string containing SQL SECURITY INVOKER must not satisfy the requirement.
QUOTED_OR_COMMENT = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`",
    re.DOTALL,
)
CREATE_VIEW = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+)?VIEW\b(?P<header>.*?)\bAS\b",
    re.IGNORECASE | re.DOTALL,
)


def _view_headers(sql):
    masked = QUOTED_OR_COMMENT.sub(lambda m: " " * len(m[0]), sql)
    return [match["header"] for match in CREATE_VIEW.finditer(masked)]


def _is_invoker(header):
    security = re.findall(r"\bSQL\s+SECURITY\s+(\w+)", header, re.IGNORECASE)
    return [value.upper() for value in security] == ["INVOKER"]


def test_every_non_materialized_view_uses_invoker():
    views = 0
    for path in sorted(SQL_DIR.rglob("*.sql")):
        for header in _view_headers(path.read_text()):
            views += 1
            assert _is_invoker(header), f"{path.relative_to(SQL_DIR)}: CREATE VIEW {header.strip()}"
    assert views, "No CREATE VIEW declarations found"


@pytest.mark.parametrize(
    ("ddl", "expected"),
    [
        ("CREATE VIEW v SQL SECURITY INVOKER AS SELECT 1", True),
        ("create or replace view v\nsql security invoker\nas select {n:UInt32}", True),
        ("CREATE VIEW v AS SELECT 'SQL SECURITY INVOKER'", False),
        ("CREATE VIEW v /* SQL SECURITY INVOKER */ AS SELECT 1", False),
        ("CREATE VIEW v -- SQL SECURITY INVOKER\nAS SELECT 1", False),
        ("CREATE VIEW v (`SQL SECURITY INVOKER` String) AS SELECT 1", False),
        ("CREATE VIEW v SQL SECURITY DEFINER AS SELECT 1", False),
        ("CREATE VIEW v SQL SECURITY NONE AS SELECT 1", False),
    ],
)
def test_security_check_inspects_the_ddl_clause(ddl, expected):
    assert _is_invoker(_view_headers(ddl)[0]) is expected


def test_materialized_views_and_commented_ddl_are_excluded():
    assert not _view_headers(
        "-- CREATE VIEW example AS SELECT 1;\n"
        "CREATE MATERIALIZED VIEW mv SQL SECURITY DEFINER AS SELECT 1;"
    )
