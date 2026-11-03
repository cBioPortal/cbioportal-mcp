"""read_guide must accept every URI that list_guides advertises.

list_guides lists deployment-specific guides as cbioportal://general-guide/<name>
and study guides as cbioportal://study-guide/{study_id}, and the system prompt
tells the agent to discover deployment-specific guides with list_guides() and
then call read_guide(uri). read_guide used to answer both with "Resource not
found".
"""

import pytest

from cbioportal_mcp import server

LOCAL_GUIDE = "# Local data sources\n\nOnly in this deployment."


@pytest.fixture
def local_general_guide(monkeypatch):
    monkeypatch.setattr(server, "_list_available_general_guides", lambda: ["local-info"])
    monkeypatch.setattr(
        server,
        "_load_general_guide",
        lambda name: LOCAL_GUIDE if name == "local-info" else None,
    )


def test_every_listed_uri_is_readable(local_general_guide):
    for guide in server.list_guides.fn():
        uri = guide["uri"].replace("{study_id}", "msk_chord_2024")
        assert not server.read_guide.fn(uri).startswith("Resource not found"), uri


def test_general_guide_uri_returns_the_deployment_guide(local_general_guide):
    content = server.read_guide.fn("cbioportal://general-guide/local-info")

    assert content == LOCAL_GUIDE
    assert content == server.get_general_guide.fn("local-info")


def test_unknown_general_guide_uri_lists_the_available_guides(local_general_guide):
    content = server.read_guide.fn("cbioportal://general-guide/missing")

    assert "General guide 'missing' not found" in content
    assert "local-info" in content


def test_general_guide_uri_still_rejects_paths(local_general_guide):
    content = server.read_guide.fn("cbioportal://general-guide/../system-prompt")

    assert content.startswith("Error: invalid guide name")


def test_study_guide_uri_returns_the_study_guide():
    content = server.read_guide.fn("cbioportal://study-guide/msk_chord_2024")

    assert content == server.get_study_guide.fn("msk_chord_2024")
    assert "MSK-CHORD" in content


def test_study_guide_uri_still_validates_the_study_id():
    content = server.read_guide.fn("cbioportal://study-guide/x' OR '1'='1")

    assert content.startswith("Error: Invalid study_id")


def test_not_found_message_mentions_the_guide_uri_patterns():
    content = server.read_guide.fn("cbioportal://no-such-guide")

    assert "cbioportal://general-guide/<name>" in content
    assert "cbioportal://study-guide/<study_id>" in content
