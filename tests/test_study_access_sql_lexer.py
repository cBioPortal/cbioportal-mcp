"""Adversarial inputs for the app-level SQL guard's tokenizer.

Each bypass here reached ClickHouse unchanged before the guard tokenized
queries: comment stripping ran over string literals (so `'--'` hid the rest of
the line) and quoted identifiers weren't decoded.
"""

from unittest.mock import patch

import pytest

from cbioportal_mcp.authentication.study_access import (
    AuthorizationError,
    _lex_sql,
    _normalize_sql,
    guard_query_study_access,
    references_protected_study_data,
)
from cbioportal_mcp.env import McpConfig

ALICE = {"x-user-id": "alice", "x-cbioportal-allowed-studies": "study_alpha"}


@pytest.fixture(params=[True, False], ids=["row-policies", "guard-only"])
def restricted(request, monkeypatch):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv(
        "CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "true" if request.param else "false"
    )
    with patch("fastmcp.server.dependencies.get_http_headers", return_value=ALICE):
        yield McpConfig()


SETTING_OVERRIDES = [
    # The reported bypass: '--' inside a string literal used to be treated as
    # a comment, removing the SETTINGS clause from inspection.
    "SELECT *, '--' AS marker FROM mutation SETTINGS SQL_cbiomcp_allowed_studies='*'",
    "SELECT *, '/*' AS a FROM mutation SETTINGS SQL_cbiomcp_allowed_studies='*' -- */",
    "SELECT * FROM mutation SETTINGS sql_CBIOMCP_allowed_studies = '*'",
    "SELECT * FROM mutation SETTINGS `SQL_cbiomcp_allowed_studies` = '*'",
    "SELECT * FROM mutation SETTINGS \"SQL_cbiomcp_allowed_studies\" = '*'",
    r"SELECT * FROM mutation SETTINGS `\x53QL_cbiomcp_allowed_studies` = '*'",
    r"SELECT * FROM mutation SETTINGS `SQL\_cbiomcp\_allowed\_studies` = '*'",
    "SET SQL_cbiomcp_allowed_studies = '*'",
    "SELECT getSetting('SQL_cbiomcp_allowed_studies')",
    "SELECT * FROM mutation SETTINGS SQL_cbiomcp_some_future_setting = 1",
    "SELECT 1 # comment\nSETTINGS SQL_cbiomcp_allowed_studies = '*'",
    "SELECT $$--$$ AS x FROM mutation SETTINGS SQL_cbiomcp_allowed_studies = '*'",
    "SELECT * FROM mutation /* SQL_cbiomcp_allowed_studies */",
]


@pytest.mark.parametrize("query", SETTING_OVERRIDES)
def test_internal_setting_is_rejected_anywhere(restricted, query):
    with pytest.raises(AuthorizationError, match="internal ClickHouse study allowlist"):
        guard_query_study_access(query, restricted)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM mutation WHERE x = 'unterminated",
        "SELECT * FROM `mutation",
        "SELECT * FROM mutation /* never closed",
        "SELECT $tag$ open FROM mutation",
    ],
)
def test_unterminated_tokens_are_rejected(restricted, query):
    with pytest.raises(AuthorizationError, match="unterminated"):
        guard_query_study_access(query, restricted)


@pytest.mark.parametrize(
    "query",
    [
        # Comment markers inside literals used to hide the protected table.
        "SELECT '--' AS a, * FROM mutation",
        "SELECT '/*' AS a, * FROM mutation, (SELECT '*/')",
        # Quoted / escaped identifiers are decoded before matching.
        "SELECT * FROM `mutation`",
        'SELECT * FROM "structural_variant"',
        r"SELECT * FROM `mut\x61tion`",
    ],
)
def test_protected_tables_hidden_by_quoting_are_detected(query):
    assert references_protected_study_data(query)


@pytest.mark.parametrize(
    "query",
    [
        # Alias that looks like a study filter once quotes are stripped.
        "SELECT 1 AS `cancer_study_identifier = 'study_alpha'` FROM mutation",
        # Comment that looks like a study filter.
        "SELECT * FROM mutation -- WHERE cancer_study_identifier = 'study_alpha'",
        "SELECT * FROM mutation /* cancer_study_identifier = 'study_alpha' */",
        "SELECT * FROM mutation # cancer_study_identifier = 'study_alpha'",
        # Escaped quote keeps the whole tail inside one literal.
        r"SELECT * FROM sample_derived WHERE cancer_study_identifier = 'x\' OR 1 = 1 --'",
    ],
)
def test_fake_study_filters_do_not_satisfy_guard_only_mode(monkeypatch, query):
    monkeypatch.setenv("CBIOPORTAL_MCP_STUDY_ACCESS_MODE", "restricted")
    monkeypatch.setenv("CBIOPORTAL_MCP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED", "false")
    with patch("fastmcp.server.dependencies.get_http_headers", return_value=ALICE):
        with pytest.raises(AuthorizationError):
            guard_query_study_access(query, McpConfig())


@pytest.mark.parametrize(
    "query",
    [
        "-- top genes\nSELECT * FROM genomic_event_derived "
        "WHERE cancer_study_identifier = 'study_alpha' /* note */",
        "SELECT `hugo_gene_symbol` FROM sample_derived "
        "WHERE cancer_study_identifier IN ('study_alpha')",
        "SELECT 'it''s' AS a, * FROM clinical_data_derived "
        "WHERE cancer_study_identifier = 'study_alpha'",
    ],
)
def test_legitimate_queries_with_comments_and_quotes_still_pass(restricted, query):
    guard_query_study_access(query, restricted)


def test_explicit_denied_study_in_quoted_column_is_still_rejected(restricted):
    with pytest.raises(AuthorizationError, match="study_beta"):
        guard_query_study_access(
            "SELECT * FROM sample_derived WHERE `cancer_study_identifier` = 'study_beta'",
            restricted,
        )


def test_lexer_follows_clickhouse_rules():
    kinds = [t.kind for t in _lex_sql("SELECT 'a--b' /* x /* y */ z */ #! c\n`q`")]
    assert kinds == ["code", "string", "code", "comment", "code", "comment", "code", "identifier"]
    # `#x` is not a comment in ClickHouse (it is a syntax error), so it stays code.
    assert [t.kind for t in _lex_sql("SELECT 1 #x")] == ["code"]
    assert _normalize_sql(r"SELECT 'a\'b', `x\x41`") == "SELECT 'a''b', xA"
