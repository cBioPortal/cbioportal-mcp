"""With every #175 and authorities flag at its default, behave exactly like beta.

- no study/identity setting is sent and the per-request settings hook is not
  involved (queries go straight to mcp-clickhouse's run_query, as on beta);
- the SQL guard never rejects anything (not even SETTINGS that name the
  internal settings), and get_study_guide caches on the plain study id;
- scripts/apply_sql.sh does not apply sql/study-authz/ at all.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cbioportal_mcp import clickhouse_query_settings as cqs
from cbioportal_mcp import server
from cbioportal_mcp.authentication.study_access import (
    clickhouse_study_access_settings,
    get_current_study_access,
    guard_query_study_access,
    study_access_cache_partition,
    validate_study_access_startup_config,
)
from cbioportal_mcp.env import get_mcp_config

REPO = Path(__file__).resolve().parent.parent

FLAGS = (
    "CBIOPORTAL_MCP_STUDY_ACCESS_MODE",
    "CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE",
    "CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY",
    "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED",
    "CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
    "CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING",
    "CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS",
    "CBIOPORTAL_MCP_AUTH_REQUIRED",
    "CBIOPORTAL_MCP_AUTH_PROXY_SECRET",
    "CBIOPORTAL_MCP_STUDY_ACL_FILE",
    "CBIOPORTAL_MCP_ALLOWED_STUDIES",
    "CBIOPORTAL_MCP_AUTH_ADMIN_GROUPS",
    "APPLY_STUDY_AUTHZ_SQL",
    "STUDY_AUTHZ_SOURCE",
)


@pytest.fixture
def defaults(monkeypatch):
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)


def test_flag_defaults(defaults):
    config = get_mcp_config()
    assert config.study_access_mode == "public"
    assert config.study_access_source == "acl"
    assert config.study_access_identity == "oauth"
    assert config.clickhouse_row_policy_enabled is False
    assert config.study_authz_identity_mode is False
    assert config.clickhouse_user_setting == "SQL_cbio_user"
    validate_study_access_startup_config(config)  # no error, no requirement


def test_no_setting_is_sent_and_queries_take_betas_path(defaults, monkeypatch):
    from mcp_clickhouse import mcp_server

    sent = []

    def upstream_run_query(query):
        sent.append((query, cqs._REQUEST_SETTINGS.get()))
        return json.dumps({"columns": ["x"], "rows": [[1]]})

    monkeypatch.setattr(mcp_server, "run_query", upstream_run_query)
    assert clickhouse_study_access_settings() == {}
    rows = server.run_select_query("SELECT 1 AS x", query_label="t")
    assert rows == [{"x": 1}]
    # Exactly the SQL, straight to mcp-clickhouse, with no per-request settings.
    assert sent == [("SELECT 1 AS x", None)]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM mutation",
        "SELECT * FROM clinical_data_derived WHERE cancer_study_identifier = 'anything'",
        "SELECT * FROM cancer_study SETTINGS SQL_cbio_user = 'other@x'",
        "SELECT * FROM cancer_study SETTINGS SQL_cbiomcp_allowed_studies = '*'",
        "SELECT * FROM a UNION ALL SELECT * FROM b WHERE x = 1 OR y = 2",
    ],
)
def test_guard_never_rejects(defaults, query):
    guard_query_study_access(query)
    assert get_current_study_access().all_studies


def test_study_guide_cache_key_is_the_plain_study_id(defaults, monkeypatch):
    server._clear_study_guide_cache()
    assert study_access_cache_partition() is None
    monkeypatch.setattr(server, "_load_study_guide", lambda _: None)
    monkeypatch.setattr(
        server,
        "run_select_query",
        lambda query, *, query_label, **_: (
            [{"name": "T"}] if query_label == "study_guide.study_info" else []
        ),
    )
    try:
        server.get_study_guide.fn.__wrapped__("Alpha")
        assert server._study_guide_cache.get("alpha") is not None
    finally:
        server._clear_study_guide_cache()


def _fake_clickhouse_client(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "applied.log"
    stub = bindir / "clickhouse-client"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--queries-file" ]]; then echo "$2" >> "$CBIOMCP_LOG";'
        ' cp "$2" "$CBIOMCP_LOG.$(basename "$2")"; fi\n'
        "  shift\n"
        "done\n"
    )
    stub.chmod(0o755)
    return bindir, log


def _apply_sql(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    bindir, log = _fake_clickhouse_client(tmp_path)
    full_env = {
        k: v for k, v in os.environ.items() if k not in FLAGS and not k.startswith("STUDY_AUTHZ")
    }
    full_env.update(
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        CBIOMCP_LOG=str(log),
        CLICKHOUSE_HOST="localhost",
        CLICKHOUSE_DATABASE="cbio_new_clone",
        CLICKHOUSE_ADMIN_USER="admin",
        CLICKHOUSE_ADMIN_PASSWORD="pw",
        **env,
    )
    result = subprocess.run(
        ["bash", str(REPO / "scripts" / "apply_sql.sh")],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    applied = log.read_text().splitlines() if log.exists() else []
    return result, applied


def test_apply_sql_skips_study_authz_by_default(tmp_path):
    result, applied = _apply_sql(tmp_path)
    assert result.returncode == 0, result.stderr
    assert applied, "portable sql/*.sql should still be applied"
    assert not [path for path in applied if "/study-authz/" in path]
    assert "skip   study-authz/" in result.stdout


def test_apply_sql_authorities_renders_the_configured_names(tmp_path):
    result, applied = _apply_sql(
        tmp_path,
        APPLY_STUDY_AUTHZ_SQL="true",
        STUDY_AUTHZ_SOURCE="authorities",
        CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING="SQL_portal_user",
        STUDY_AUTHZ_REFRESH_MINUTES="7",
    )
    assert result.returncode == 0, result.stderr
    authz = [Path(p).name for p in applied if "/study-authz/" in p]
    assert authz == ["0-user-study.sql", "1-row-policies.sql"]
    policies = (tmp_path / "applied.log.1-row-policies.sql").read_text()
    assert "getSetting('SQL_portal_user')" in policies
    assert "SQL_cbio_user" not in policies and "__CBIOMCP_" not in policies
    assert "cbio_new_clone.user_study" in policies
    mv = (tmp_path / "applied.log.0-user-study.sql").read_text()
    assert "REFRESH EVERY 7 MINUTE" in mv


def test_apply_sql_acl_renders_the_configured_allowlist_name(tmp_path):
    result, applied = _apply_sql(
        tmp_path,
        APPLY_STUDY_AUTHZ_SQL="true",
        CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING="SQL_custom_studies",
    )
    assert result.returncode == 0, result.stderr
    authz = [Path(p).name for p in applied if "/study-authz/" in p]
    assert authz == ["0-row-policies.sql"]
    policies = (tmp_path / "applied.log.0-row-policies.sql").read_text()
    assert "getSetting('SQL_custom_studies')" in policies
    assert "SQL_cbiomcp_allowed_studies" not in policies


def test_apply_sql_can_leave_the_role_grant_for_switchover(tmp_path):
    # A clone that isn't live yet: policies applied, but the shared role is
    # not granted the new database until the switchover step.
    result, _applied = _apply_sql(
        tmp_path,
        APPLY_STUDY_AUTHZ_SQL="true",
        STUDY_AUTHZ_SOURCE="authorities",
        STUDY_AUTHZ_GRANT_ROLE="false",
    )
    assert result.returncode == 0, result.stderr
    policies = (tmp_path / "applied.log.1-row-policies.sql").read_text()
    assert "GRANT SELECT ON cbio_new_clone.*" not in policies
    assert "CREATE ROW POLICY OR REPLACE cbioportal_mcp_default_deny" in policies


@pytest.mark.parametrize(
    "env",
    [
        {"STUDY_AUTHZ_SOURCE": "bogus"},
        {"STUDY_AUTHZ_SOURCE": "authorities", "STUDY_AUTHZ_REFRESH_MINUTES": "0"},
        {"STUDY_AUTHZ_SOURCE": "authorities", "STUDY_AUTHZ_APP_NAME": "x'; DROP"},
        {"CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING": "not_sql_prefixed"},
        {"STUDY_AUTHZ_GRANT_ROLE": "maybe"},
    ],
)
def test_apply_sql_rejects_bad_study_authz_parameters(tmp_path, env):
    result, applied = _apply_sql(tmp_path, APPLY_STUDY_AUTHZ_SQL="true", **env)
    assert result.returncode != 0
    assert not [path for path in applied if "/study-authz/" in path]
