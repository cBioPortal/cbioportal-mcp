import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SQL_4 = ROOT / "sql" / "4-mutation-frequency-views.sql"
GUIDE = ROOT / "src" / "cbioportal_mcp" / "resources" / "mutation-frequency-guide.md"


def _view_body(view: str) -> str:
    """One CREATE VIEW statement from sql/4, comments stripped."""
    sql = re.sub(r"--[^\n]*", "", SQL_4.read_text())
    return sql[sql.index(f"CREATE VIEW {view} AS") :].split(";", 1)[0]


def test_sv_views_exclude_uncalled():
    for view in ("gene_alteration_frequency_by_cancer_type", "top_sv_genes_in_study"):
        body = _view_body(view)
        sv = body[body.index("'structural_variant'") :]
        assert "mutation_status != 'UNCALLED'" in sv[:300], view


def test_guide_shows_uncalled_filter_on_structural_variant():
    lines = GUIDE.read_text().splitlines()
    row = next(ln for ln in lines if ln.startswith("| `structural_variant`"))
    assert "mutation_status != 'UNCALLED'" in row


@pytest.mark.parametrize(
    "view,panel,wes",
    [
        ("top_mutated_genes_in_cohort", "mutation_panel_gene_coverage", "mutation_wes_coverage"),
        ("top_mutated_genes_in_study", "mutation_panel_gene_coverage", "mutation_wes_coverage"),
        ("top_cna_genes_in_study", "cna_panel_gene_coverage", "cna_wes_coverage"),
        ("top_sv_genes_in_study", "sv_panel_gene_coverage", "sv_wes_coverage"),
    ],
)
def test_top_views_do_not_double_count_wes_plus_panel(view, panel, wes):
    body = _view_body(view)
    panel_cte = body[body.index("panel_profiled_per_gene AS") : body.index("altered_per_gene AS")]
    assert panel in panel_cte and "NOT IN" in panel_cte and wes in panel_cte


def test_co_altered_panel_sizes_leave_out_wes_samples():
    body = _view_body("co_altered_genes_in_study")
    panel_cte = body[body.index("panel_group_sizes AS") : body.index("altered_per_gene AS")]
    assert "NOT IN (SELECT sample_unique_id FROM wes_samples)" in panel_cte
