"""Google (default) telemetry is identical to the base commit.

tests/golden/telemetry_google_012843f.json was produced by running
tests/telemetry_golden_scenarios.py against the source tree of 012843f (the
head of #182 this work is merged with; it includes upstream beta #181, which
removed the query-cache pilot and with it the query_cache metric tag and
db.query.cache span attribute):

    git archive 012843f src | tar -x -C /tmp/base
    PYTHONPATH=/tmp/base/src uv run --extra dev python tests/telemetry_golden_scenarios.py

The only permitted difference is the two new span attributes, auth.provider
and auth.source. Everything else - every span attribute (enduser.id,
enduser.email, mcp.client_kind, mcp.client.name/version, mcp.session.id,
network.client.ip, mcp.tool.name/success, error.type ...), the LLMObs tool
span arguments (usr_id, user_email metadata, ...) and every DogStatsD
packet with its tags (tool, success, client_kind, client_name, query_label)
- must match exactly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "telemetry_google_012843f.json"
NEW_SPAN_ATTRIBUTES = {"auth.provider": "google", "auth.source": "public"}


def _current() -> dict:
    # A fresh interpreter: the scenarios set a global tracer provider and
    # clear deployment env vars, neither of which may leak into other tests.
    out = subprocess.run(
        [sys.executable, str(HERE / "telemetry_golden_scenarios.py")],
        check=True,
        capture_output=True,
        text=True,
        cwd=HERE.parent,
    )
    return json.loads(out.stdout)


def test_google_path_emits_the_base_commit_telemetry():
    golden = json.loads(GOLDEN.read_text())
    current = _current()
    assert current.keys() == golden.keys()
    for scenario, expected in golden.items():
        got = current[scenario]
        assert got["llmobs"] == expected["llmobs"], scenario
        assert got["metrics"] == expected["metrics"], scenario
        assert [s["name"] for s in got["spans"]] == [s["name"] for s in expected["spans"]]
        for got_span, want_span in zip(got["spans"], expected["spans"], strict=False):
            attrs = dict(got_span["attributes"])
            added = {k: attrs.pop(k) for k in NEW_SPAN_ATTRIBUTES if k in attrs}
            assert attrs == want_span["attributes"], (scenario, got_span["name"])
            # Identity spans gain exactly the two new tags; db.query spans none.
            expected_added = {} if got_span["name"].startswith("db.query/") else NEW_SPAN_ATTRIBUTES
            assert added == expected_added, (scenario, got_span["name"])


def test_golden_covers_every_identity_tag():
    text = GOLDEN.read_text()
    for tag in (
        "enduser.id",
        "enduser.email",
        "mcp.client_kind",
        "mcp.client.name",
        "mcp.client.version",
        "mcp.session.id",
        "network.client.ip",
        "mcp.tool.name",
        "mcp.tool.success",
        "user_email",
        "usr.id",
        "usr_id",
        "client_kind:",
        "client_name:",
        "query_label:",
    ):
        assert tag in text, tag


def test_studies_and_groups_never_tagged():
    current = json.dumps(_current())
    for word in ("studies", "groups", "study_"):
        assert word not in current
