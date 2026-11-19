"""H&E slide questions must reach the external-resources guide, and its
discovery query must find H&E slide resources (issue #161).

Agents asked "Any studies with H&E data?" answered that the database has
none: no guide description named H&E, and the guide's discovery query only
matched minerva/image/pathology/histology, so an 'H&E Slide' resource
definition never matched it.
"""

import re
import sqlite3

from cbioportal_mcp import server


def _discovery_query() -> str:
    guide = server._external_resources_guide_text()
    section = guide.split("## Discovery Query", 1)[1]
    return section.split("```sql", 1)[1].split("```", 1)[0]


def _run_discovery_query(definitions):
    # SQLite runs the relational part of the query; the ClickHouse string
    # functions it uses are registered with ClickHouse's semantics.
    with sqlite3.connect(":memory:") as db:
        db.create_function("match", 2, lambda text, pattern: re.search(pattern, text) is not None)
        db.create_function("concat", -1, lambda *parts: "".join(parts))
        db.executescript(
            """
            CREATE TABLE cancer_study (cancer_study_id INTEGER, cancer_study_identifier TEXT);
            CREATE TABLE resource_definition (
                resource_id TEXT, display_name TEXT, description TEXT,
                resource_type TEXT, cancer_study_id INTEGER
            );
            CREATE TABLE resource (
                resource_id TEXT, cancer_study_id INTEGER, resource_scope TEXT,
                sample_id TEXT, patient_id TEXT, url TEXT
            );
            INSERT INTO cancer_study VALUES (1, 'crc_hta11_htan_2021');
            """
        )
        db.executemany("INSERT INTO resource_definition VALUES (?, ?, ?, 'SAMPLE', 1)", definitions)
        db.executemany(
            "INSERT INTO resource VALUES (?, 1, 'SAMPLE', 'S1', NULL, 'https://example.org')",
            [(resource_id,) for resource_id, _, _ in definitions],
        )
        return [row[1] for row in db.execute(_discovery_query())]


def test_discovery_query_finds_h_and_e_slides():
    found = _run_discovery_query(
        [
            ("HE_SLIDE", "H&E Slide", None),
            ("MINERVA", "Minerva Story", "Multiplexed tissue imaging viewer"),
            ("HE", "Hematoxylin and eosin", "H&E stained section"),
            ("PDF", "Pathology Report", None),
            ("CLINICAL_TRIAL", "Clinical trial", "Trial registration page"),
        ]
    )

    assert sorted(found) == [
        "H&E Slide",
        "Hematoxylin and eosin",
        "Minerva Story",
        "Pathology Report",
    ]


def test_h_and_e_questions_route_to_the_external_resources_guide():
    guides = {g["uri"]: g["description"] for g in server.list_guides.fn.__wrapped__()}
    assert "H&E" in guides["cbioportal://external-resources-guide"]

    query_doc = server.clickhouse_run_select_query.description
    assert re.search(r"cbioportal://external-resources-guide - .*H&E", query_doc)

    prompt = server._load_resource("system-prompt.md")
    assert re.search(r"H&E.*cbioportal://external-resources-guide", prompt)

    assert "H&E" in server._external_resources_guide_text().split("## Core Rule", 1)[0]
