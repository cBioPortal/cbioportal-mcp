import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_row_policy_coverage.py"
_spec = importlib.util.spec_from_file_location("verify_row_policy_coverage", SCRIPT)
verify = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = verify  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(verify)

Policy = verify.Policy
DB = "cbio"
SETTING = "SQL_cbiomcp_allowed_studies"
STUDY_FILTER = (
    f"(getSetting('{SETTING}') = '*') OR (patient_id IN (SELECT internal_id FROM {DB}.patient "
    f"WHERE cancer_study_id IN (SELECT cancer_study_id FROM {DB}.cancer_study WHERE "
    f"has(splitByChar(',', getSetting('{SETTING}')), cancer_study_identifier))))"
)


def _table(engine="MergeTree", query=""):
    return (engine, query)


def _good():
    objects = {
        "sample": _table(),
        "gene": _table(),
        "users": _table(),
        "mutation_panel_gene_coverage": _table(
            "View", "CREATE VIEW cbio.x SQL SECURITY INVOKER AS SELECT 1"
        ),
    }
    policies = [
        Policy("cbioportal_mcp_default_deny", "", "0"),
        Policy("cbioportal_mcp_study_sample", "sample", STUDY_FILTER),
        Policy("cbioportal_mcp_reference_gene", "gene", "1"),
        Policy("cbioportal_mcp_view_x", "mutation_panel_gene_coverage", "1"),
    ]
    return objects, policies


def _evaluate(objects, policies):
    return verify.evaluate_coverage(objects, policies, DB, SETTING)


def test_correct_coverage_passes():
    report = _evaluate(*_good())
    assert report.ok, report


def test_missing_default_deny_is_an_error():
    objects, policies = _good()
    policies = [p for p in policies if p.table != ""]
    assert any("default-deny" in e for e in _evaluate(objects, policies).errors)


def test_open_wildcard_policy_is_an_error():
    objects, policies = _good()
    policies.append(Policy("oops", "", "1"))
    assert any("oops" in e for e in _evaluate(objects, policies).errors)


def test_study_scoped_table_with_only_default_deny_is_fully_denied():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "sample"]
    errors = _evaluate(objects, policies).errors
    assert any("sample" in e and "fully denied by accident" in e for e in errors), errors


def test_study_scoped_table_with_using_1_is_unfiltered():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "sample"] + [Policy("p", "sample", "1")]
    assert any("unfiltered" in e for e in _evaluate(objects, policies).errors)


def test_study_scoped_policy_must_use_the_setting():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "sample"] + [
        Policy("p", "sample", "patient_id > 0")
    ]
    assert any("does not filter" in e for e in _evaluate(objects, policies).errors)


def test_reference_table_without_policy_is_fully_denied():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "gene"]
    assert any("gene" in e and "fully denied" in e for e in _evaluate(objects, policies).errors)


def test_view_without_pass_through_is_fully_denied():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "mutation_panel_gene_coverage"]
    errors = _evaluate(objects, policies).errors
    assert any("mutation_panel_gene_coverage" in e and "pass-through" in e for e in errors)


def test_definer_view_is_an_error():
    objects, policies = _good()
    objects["mutation_panel_gene_coverage"] = _table(
        "View", "CREATE VIEW cbio.x DEFINER = admin SQL SECURITY DEFINER AS SELECT 1"
    )
    assert any("SQL SECURITY DEFINER" in e for e in _evaluate(objects, policies).errors)


@pytest.mark.parametrize("engine", ["MaterializedView", "Dictionary"])
def test_materialized_views_and_dictionaries_are_errors(engine):
    objects, policies = _good()
    objects["thing"] = _table(engine)
    assert any("thing" in e and engine in e for e in _evaluate(objects, policies).errors)


def test_denied_table_with_policy_is_an_error():
    objects, policies = _good()
    policies.append(Policy("p", "users", "1"))
    assert any("users" in e and "denied" in e for e in _evaluate(objects, policies).errors)


def test_unsubstituted_placeholder_or_foreign_db_is_an_error():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "sample"] + [
        Policy("p", "sample", STUDY_FILTER.replace(f"{DB}.patient", "__CLICKHOUSE_DATABASE__.x")),
        Policy("q", "gene", "1 AND x IN (SELECT 1 FROM otherdb.t)"),
    ]
    errors = _evaluate(objects, policies).errors
    assert any("__CLICKHOUSE_DATABASE__" in e for e in errors), errors
    assert any("otherdb" in e for e in errors), errors


def test_unclassified_tables_and_views_are_reported():
    objects, policies = _good()
    objects["brand_new_table"] = _table()
    objects["brand_new_view"] = _table("View", "CREATE VIEW cbio.v AS SELECT 1")
    report = _evaluate(objects, policies)
    assert report.unclassified == ["brand_new_table", "brand_new_view"]
    assert not report.ok


def test_restrictive_policies_do_not_count_as_coverage():
    objects, policies = _good()
    policies = [p for p in policies if p.table != "sample"] + [
        Policy("r", "sample", STUDY_FILTER, is_restrictive=True)
    ]
    assert any("fully denied" in e for e in _evaluate(objects, policies).errors)


def test_unknown_view_with_a_policy_is_still_unclassified():
    objects, policies = _good()
    objects["surprise_view"] = _table("View", "CREATE VIEW cbio.s AS SELECT * FROM sample")
    policies.append(Policy("cbioportal_mcp_view_surprise", "surprise_view", "1"))
    report = _evaluate(objects, policies)
    assert report.unclassified == ["surprise_view"]
    assert not report.ok


def test_unknown_table_with_a_policy_is_still_unclassified():
    objects, policies = _good()
    objects["surprise_table"] = _table()
    policies.append(Policy("p", "surprise_table", "1"))
    assert _evaluate(objects, policies).unclassified == ["surprise_table"]


@pytest.mark.parametrize("security", ["NONE", "DEFINER", "none"])
def test_none_and_definer_views_are_errors(security):
    objects, policies = _good()
    objects["mutation_panel_gene_coverage"] = _table(
        "View", f"CREATE VIEW cbio.x SQL SECURITY {security} AS SELECT 1"
    )
    errors = _evaluate(objects, policies).errors
    assert any(f"SQL SECURITY {security.upper()}" in e for e in errors), errors


def test_cataloged_view_without_explicit_invoker_is_an_error():
    objects, policies = _good()
    objects["mutation_panel_gene_coverage"] = _table("View", "CREATE VIEW cbio.x AS SELECT 1")
    errors = _evaluate(objects, policies).errors
    assert any("not explicitly SQL SECURITY INVOKER" in e for e in errors), errors


def test_unclassified_none_view_is_an_error_too():
    objects, policies = _good()
    objects["sneaky"] = _table("View", "CREATE VIEW cbio.s SQL SECURITY NONE AS SELECT 1")
    report = _evaluate(objects, policies)
    assert any("sneaky" in e and "NONE" in e for e in report.errors)
    assert "sneaky" in report.unclassified


def test_quoted_column_named_like_the_clause_does_not_mask_none():
    objects, policies = _good()
    objects["mutation_panel_gene_coverage"] = _table(
        "View",
        "CREATE VIEW cbio.x (`SQL SECURITY INVOKER` String, y String COMMENT "
        "'SQL SECURITY INVOKER') SQL SECURITY NONE AS SELECT 'SQL SECURITY INVOKER', 1",
    )
    errors = _evaluate(objects, policies).errors
    assert any("SQL SECURITY NONE" in e for e in errors), errors
