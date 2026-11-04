"""Exercise the production resource CTE with study-scoped stable IDs."""

import sqlite3
from pathlib import Path

SQL = (Path(__file__).parent.parent / "sql" / "6-add-study-data-type-counts.sql").read_text()


def test_resource_counts_resolve_stable_ids_within_study_and_preserve_scope_counts():
    # SQLite executes this relational CTE without requiring a live ClickHouse.
    resource_rows = SQL.split("    resource_rows AS (", 1)[1].split("    mrna AS (", 1)[0]
    resource_rows = resource_rows.rsplit("),", 1)[0]
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            """
            CREATE TABLE patient (internal_id INTEGER, stable_id TEXT, cancer_study_id INTEGER);
            CREATE TABLE sample (internal_id INTEGER, stable_id TEXT, patient_id INTEGER);
            CREATE TABLE resource_definition (
                resource_id TEXT, display_name TEXT, cancer_study_id INTEGER
            );
            CREATE TABLE resource (
                internal_id INTEGER, resource_id TEXT, cancer_study_id INTEGER,
                resource_scope TEXT, sample_id TEXT, patient_id TEXT
            );
            INSERT INTO patient VALUES (1, 'P1', 10), (2, 'P1', 20), (3, 'P2', 10);
            INSERT INTO sample VALUES
                (101, 'S1', 1), (102, 'S2', 1), (201, 'S1', 2), (301, 'S3', 3);
            INSERT INTO resource_definition VALUES
                ('slide', 'Slide', 10), ('other', 'Slide', 10),
                ('slide', 'Study 20 Slide', 20);
            INSERT INTO resource VALUES
                (9001, 'slide', 10, 'SAMPLE', 'S1', NULL),
                (9002, 'slide', 10, 'SAMPLE', 'S1', NULL),
                (9003, 'other', 10, 'SAMPLE', 'S1', NULL),
                (9004, 'slide', 10, 'PATIENT', NULL, 'P1'),
                (9005, 'slide', 10, 'PATIENT', NULL, 'P1'),
                (9006, 'slide', 10, 'STUDY', NULL, NULL),
                (9007, 'slide', 20, 'SAMPLE', 'S1', NULL),
                (9008, 'slide', 20, 'PATIENT', NULL, 'P1'),
                (9009, 'slide', 10, 'SAMPLE', 'missing', NULL),
                (9010, 'slide', 10, 'PATIENT', NULL, 'missing');
            """
        )
        counts = db.execute(
            f"""WITH resource_rows AS ({resource_rows})
            SELECT cancer_study_id, display_name, sum(n)
            FROM resource_rows GROUP BY cancer_study_id, display_name
            ORDER BY cancer_study_id"""
        ).fetchall()
    # Scope contributions stay additive; reused resource IDs keep their study's label.
    assert counts == [(10, "Slide", 3), (20, "Study 20 Slide", 2)]
