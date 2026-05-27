import pytest

from cbioportal_mcp.authentication.study_scope import StudyScope
from cbioportal_mcp.clickhouse_auth import (
    build_clickhouse_auth_settings,
    execute_authorized_select_query,
)
from cbioportal_mcp.env import McpConfig


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    for env_var in [
        "CBIOPORTAL_AUTH_ENABLED",
        "CBIOPORTAL_CLICKHOUSE_ALLOWED_STUDIES_SETTING",
        "CBIOPORTAL_CLICKHOUSE_ALLOW_ALL_SETTING",
    ]:
        monkeypatch.delenv(env_var, raising=False)


def test_generated_settings_for_allowed_studies():
    scope = StudyScope(frozenset({"luad_tcga", "brca_tcga"}), source="test")

    settings = build_clickhouse_auth_settings(scope, McpConfig())

    assert settings == {
        "cbioportal_allowed_studies": "brca_tcga,luad_tcga",
        "cbioportal_allow_all": "0",
    }


def test_generated_settings_for_allow_all():
    scope = StudyScope(frozenset(), allow_all=True, source="test")

    settings = build_clickhouse_auth_settings(scope, McpConfig())

    assert settings == {
        "cbioportal_allowed_studies": "",
        "cbioportal_allow_all": "1",
    }


def test_generated_settings_for_empty_scope():
    scope = StudyScope(frozenset(), allow_all=False, source="test")

    settings = build_clickhouse_auth_settings(scope, McpConfig())

    assert settings == {
        "cbioportal_allowed_studies": "",
        "cbioportal_allow_all": "0",
    }


def test_custom_setting_names_are_validated(monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_CLICKHOUSE_ALLOWED_STUDIES_SETTING", "bad-setting")

    with pytest.raises(ValueError, match="Invalid ClickHouse setting name"):
        build_clickhouse_auth_settings(StudyScope(frozenset({"brca_tcga"})), McpConfig())


def test_no_sql_injection_through_study_id():
    with pytest.raises(ValueError, match="Invalid study_id"):
        StudyScope(frozenset({"brca_tcga'); DROP TABLE cancer_study; --"}))


def test_raw_query_wrapper_includes_auth_settings_when_auth_enabled(monkeypatch):
    captured = {}

    class FakeResult:
        column_names = ["cancer_study_identifier"]
        result_rows = [("brca_tcga",)]

    class FakeClient:
        server_settings = {}

        def query(self, query, settings):
            captured["query"] = query
            captured["settings"] = settings
            return FakeResult()

    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setattr("cbioportal_mcp.clickhouse_auth.create_clickhouse_client", FakeClient)

    result = execute_authorized_select_query(
        "SELECT DISTINCT cancer_study_identifier FROM clinical_data_derived",
        scope=StudyScope(frozenset({"brca_tcga"}), source="test"),
        config=McpConfig(),
    )

    assert result == {
        "columns": ["cancer_study_identifier"],
        "rows": [("brca_tcga",)],
    }
    assert captured["query"] == "SELECT DISTINCT cancer_study_identifier FROM clinical_data_derived"
    assert captured["settings"] == {
        "readonly": "1",
        "cbioportal_allowed_studies": "brca_tcga",
        "cbioportal_allow_all": "0",
    }


def test_raw_query_wrapper_omits_auth_settings_when_auth_disabled(monkeypatch):
    captured = {}

    class FakeResult:
        column_names = ["one"]
        result_rows = [(1,)]

    class FakeClient:
        server_settings = {}

        def query(self, query, settings):
            captured["settings"] = settings
            return FakeResult()

    monkeypatch.setattr("cbioportal_mcp.clickhouse_auth.create_clickhouse_client", FakeClient)

    execute_authorized_select_query(
        "SELECT 1",
        scope=StudyScope(frozenset(), allow_all=True, source="test"),
        config=McpConfig(),
    )

    assert captured["settings"] == {"readonly": "1"}


def test_concurrent_calls_do_not_share_settings(monkeypatch):
    captured_settings = []

    class FakeResult:
        column_names = ["one"]
        result_rows = [(1,)]

    class FakeClient:
        server_settings = {}

        def query(self, query, settings):
            captured_settings.append(dict(settings))
            return FakeResult()

    monkeypatch.setenv("CBIOPORTAL_AUTH_ENABLED", "true")
    monkeypatch.setattr("cbioportal_mcp.clickhouse_auth.create_clickhouse_client", FakeClient)

    execute_authorized_select_query(
        "SELECT 1",
        scope=StudyScope(frozenset({"brca_tcga"}), source="test"),
        config=McpConfig(),
    )
    execute_authorized_select_query(
        "SELECT 1",
        scope=StudyScope(frozenset({"luad_tcga"}), source="test"),
        config=McpConfig(),
    )

    assert captured_settings[0]["cbioportal_allowed_studies"] == "brca_tcga"
    assert captured_settings[1]["cbioportal_allowed_studies"] == "luad_tcga"
