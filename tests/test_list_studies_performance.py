from cbioportal_mcp import server


def _fake_rows():
    return [
        {
            "cancer_study_identifier": "study_alpha",
            "name": "Alpha Study",
            "description": "Long description",
            "type_of_cancer_id": "brca",
            "sample_count": 10,
        }
    ]


def test_list_studies_default_uses_trimmed_sample_count_query(monkeypatch):
    queries = []

    def fake_run_select_query(query, query_label=None):
        queries.append(query)
        return _fake_rows()

    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn()

    assert rows[0]["cancer_study_identifier"] == "study_alpha"
    assert "description" not in rows[0]
    assert "clinical_data_derived" not in queries[0]
    assert "JOIN" not in queries[0]
    assert "cs.sample_count" in queries[0]


def test_list_studies_refetches_after_ttl_expires(monkeypatch):
    call_count = 0

    def fake_run_select_query(query, query_label=None):
        nonlocal call_count
        call_count += 1
        return _fake_rows()

    fake_now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(server, "STUDIES_CACHE_TTL_SECONDS", 900)

    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    server.list_studies.fn()
    assert call_count == 1

    fake_now[0] += 899  # still within TTL
    server.list_studies.fn()
    assert call_count == 1

    fake_now[0] += 2  # past TTL
    server.list_studies.fn()
    assert call_count == 2


def test_list_studies_caches_repeated_calls_across_search_terms(monkeypatch):
    call_count = 0

    def fake_run_select_query(query, query_label=None):
        nonlocal call_count
        call_count += 1
        return _fake_rows()

    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    assert server.list_studies.fn(limit=20) == server.list_studies.fn(limit=20)
    # A never-before-seen search term still hits the same cached snapshot,
    # unlike the old per-query-shape cache which would have missed here.
    server.list_studies.fn(search="alpha")
    assert call_count == 1


def test_list_studies_verbose_includes_description(monkeypatch):
    queries = []

    def fake_run_select_query(query, query_label=None):
        queries.append(query)
        return _fake_rows()

    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn(verbose=True)

    assert rows[0]["description"] == "Long description"
    assert "cs.description" in queries[0]


def test_background_refresh_updates_cache_without_a_live_caller(monkeypatch):
    call_count = 0

    def fake_run_select_query(query, query_label=None):
        nonlocal call_count
        call_count += 1
        return _fake_rows()

    fake_now = [1000.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: fake_now[0])

    server._clear_studies_cache()
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    server._refresh_studies_cache_once()
    assert call_count == 1

    # Advance well past STUDIES_CACHE_TTL_SECONDS with no caller in between --
    # the on-demand path would refetch here, but nothing calls it.
    fake_now[0] += 2000

    # The background cycle refreshes on its own, with no list_studies() call
    # driving it.
    server._refresh_studies_cache_once()
    assert call_count == 2

    # A caller landing right after sees the already-fresh cache, not a third
    # fetch triggered by its own request.
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    server.list_studies.fn()
    assert call_count == 2


def test_background_refresh_failure_is_logged_not_raised(monkeypatch, caplog):
    def failing_run_select_query(query, query_label=None):
        raise RuntimeError("clickhouse unreachable")

    server._clear_studies_cache()
    monkeypatch.setattr(server, "run_select_query", failing_run_select_query)

    server._refresh_studies_cache_once()

    assert "Background list_studies cache refresh failed" in caplog.text


def test_list_studies_search_filters_in_python(monkeypatch):
    def fake_run_select_query(query, query_label=None):
        return [
            {
                "cancer_study_identifier": "study_alpha",
                "name": "Alpha Study",
                "description": "Long description",
                "type_of_cancer_id": "brca",
                "sample_count": 10,
            },
            {
                "cancer_study_identifier": "study_beta",
                "name": "Beta Study",
                "description": None,
                "type_of_cancer_id": "luad",
                "sample_count": 5,
            },
        ]

    server._clear_studies_cache()
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    rows = server.list_studies.fn(search="beta")

    assert [row["cancer_study_identifier"] for row in rows] == ["study_beta"]


def test_study_count_columns_are_created_by_the_clone_sql():
    """Every column the guides and list_studies read must be created by sql/6."""
    from pathlib import Path

    sql = (Path(__file__).parent.parent / "sql" / "6-add-study-data-type-counts.sql").read_text()
    guide = (
        Path(__file__).parent.parent / "src" / "cbioportal_mcp" / "resources" / "sample-filtering-guide.md"
    ).read_text()
    for column in (
        "sample_count",
        "mutation_sample_count",
        "cna_sample_count",
        "structural_variant_sample_count",
        "rna_seq_sample_count",
        "mrna_microarray_sample_count",
        "mrna_expression_sample_count",
        "mirna_sample_count",
        "rppa_sample_count",
        "mass_spectrometry_sample_count",
        "treatment_patient_count",
        "resource_sample_counts",
    ):
        assert f"ADD COLUMN {column} " in sql, f"{column} not created by sql/6"
        assert column in guide, f"{column} not documented in sample-filtering-guide §4"
