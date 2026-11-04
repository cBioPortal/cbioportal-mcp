"""Run the real cbioportal_mcp server (server.main) for integration tests.

Only ClickHouse is faked: queries return the studies the request's
SQL_cbiomcp_allowed_studies setting allows (like the acl row policies), and
the startup ClickHouse checks are skipped. Everything else - provider
selection, OIDC discovery, OIDCProxy, PortalDatAuthMiddleware, the portal
HTTP calls, telemetry middleware - is the production code path, configured
only through environment variables set by the test.
"""

from __future__ import annotations

import json
import os
import sys

STUDIES = ["study_alpha", "study_beta", "study_public"]
LOG = os.environ.get("INTEGRATION_CLICKHOUSE_LOG")


def fake_clickhouse(query, settings):
    allowed = settings.get("SQL_cbiomcp_allowed_studies")
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(settings)) + "\n")
    if allowed is None:
        visible = []
    elif allowed == "*":
        visible = STUDIES
    else:
        visible = [s for s in STUDIES if s in allowed.split(",")]
    cols = ["cancer_study_identifier", "name", "description", "type_of_cancer_id", "sample_count"]
    return {"columns": cols, "rows": [[s, s, "", "brca", 1] for s in visible]}


def main() -> None:
    from cbioportal_mcp import clickhouse_query_settings as cqs
    from cbioportal_mcp import server

    cqs.run_query_with_settings = fake_clickhouse
    server.run_query_with_settings = fake_clickhouse
    cqs.current_surface_problem = lambda: None
    server.current_surface_problem = lambda: None
    server.ensure_db_permissions = lambda **_kw: None
    server.verify_allowlist_lockdown = lambda *_a, **_kw: None
    server.verify_restricted_surface = lambda *_a, **_kw: None
    server.start_surface_monitor = lambda *_a, **_kw: None
    server.query_concurrency.verify_query_pool = lambda *_a, **_kw: None
    server._list_available_study_guides = lambda: []
    server.main()


if __name__ == "__main__":
    sys.exit(main())
