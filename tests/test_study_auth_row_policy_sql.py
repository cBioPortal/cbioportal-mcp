from pathlib import Path


def _policy_sql() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "sql" / "6-study-auth-row-policies.sql").read_text()


def test_policy_sql_uses_fail_closed_setting_defaults():
    sql = _policy_sql()

    assert "getSettingOrDefault('cbioportal_allow_all', '0')" in sql
    assert "getSettingOrDefault('cbioportal_allowed_studies', '')" in sql
    assert "getSetting('cbioportal_allow_all')" not in sql
    assert "getSetting('cbioportal_allowed_studies')" not in sql


def test_policy_sql_covers_required_study_scoped_tables():
    sql = _policy_sql()

    required_tables = [
        "cancer_study",
        "cancer_study_query_preferences",
        "clinical_data_derived",
        "genomic_event_derived",
        "sample_to_gene_panel_derived",
        "genetic_alteration_derived",
        "clinical_event_derived",
        "genetic_profile",
        "patient",
        "sample",
    ]

    for table in required_tables:
        assert f"ON {table}" in sql


def test_policy_sql_targets_runtime_user():
    sql = _policy_sql()

    assert "TO app_user" in sql
