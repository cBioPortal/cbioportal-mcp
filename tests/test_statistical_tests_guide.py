import re

from cbioportal_mcp import server

NATIVE_TESTS = (
    "mannWhitneyUTest",
    "welchTTest",
    "studentTTest",
    "kolmogorovSmirnovTest",
    "anova",
    "proportionsZTest",
)
NOT_NATIVE = ("Fisher's exact", "chi-squared", "Kruskal-Wallis", "log-rank", "Cox")


def _guide():
    return server._load_resource("statistical-tests-guide.md")


def test_guide_lists_the_tests_clickhouse_runs_and_the_ones_it_does_not():
    guide = _guide()
    for name in NATIVE_TESTS:
        assert name in guide
    section = guide.split("Not available in ClickHouse, so hand off", 1)[1].split("\n\n", 1)[0]
    for name in NOT_NATIVE:
        assert name in section


def test_guide_no_longer_claims_clickhouse_cannot_run_any_test():
    for text in (_guide(), server._load_resource("system-prompt.md")):
        assert "ClickHouse cannot run statistical tests" not in text
        assert "ClickHouse cannot compute statistical tests" not in text
        assert "does NOT have built-in statistical test functions" not in text


def test_p_values_are_only_allowed_from_clickhouse_test_functions():
    guide = _guide()
    assert "allowed only when a ClickHouse test function returned it" in guide
    prompt = server._load_resource("system-prompt.md")
    assert "any p-value that a clickhouse test function didn't return" in prompt.lower()
    assert "report the p-value they return with the test name and group sizes" in prompt


def test_tuple_results_are_selected_as_scalars_in_every_example():
    guide = _guide()
    assert "unrecognized data found in stream" in guide
    for call in re.findall(r"(mannWhitneyUTest|welchTTest|studentTTest)\(expr, grp\)", guide):
        assert f"tupleElement({call}(expr, grp)" in guide


def test_example_uses_a_real_expression_profile_type():
    guide = _guide()
    assert "g.profile_type = 'rna_seq_v2_mrna'" in guide
    assert "'mrna_seq'" not in guide
