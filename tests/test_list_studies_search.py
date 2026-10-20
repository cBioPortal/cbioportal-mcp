from cbioportal_mcp import server


def _rows():
    studies = [
        ("nbl_target_2018_pub", "Pediatric Neuroblastoma (TARGET, 2018)", "nbl", "Whole genome sequencing of neuroblastoma."),
        ("nbl_target_gdc", "Neuroblastoma (TARGET GDC, 2025)", "nbl", "TARGET Neuroblastoma data from the GDC."),
        ("os_target_gdc", "Osteosarcoma (TARGET GDC, 2025)", "os", "Osteosarcoma data from the GDC."),
        ("brca_tcga", "Breast Invasive Carcinoma (TCGA, Firehose Legacy)", "brca", "TCGA Breast Invasive Carcinoma."),
    ]
    return tuple(
        tuple({"cancer_study_identifier": i, "name": n, "type_of_cancer_id": t, "description": d, "sample_count": 1}.items())
        for i, n, t, d in studies
    )


def _ids(monkeypatch, search):
    monkeypatch.setattr(server, "_all_studies_query", _rows)
    return [r["cancer_study_identifier"] for r in server._filter_studies(search, limit=20, verbose=False)]


def test_search_matches_words_in_any_order_and_field(monkeypatch):
    assert _ids(monkeypatch, "osteosarcoma TARGET") == ["os_target_gdc"]
    assert set(_ids(monkeypatch, "TARGET neuroblastoma")) == {"nbl_target_2018_pub", "nbl_target_gdc"}


def test_whole_phrase_matches_are_listed_first(monkeypatch):
    assert _ids(monkeypatch, "TARGET neuroblastoma") == ["nbl_target_gdc", "nbl_target_2018_pub"]


def test_single_word_and_identifier_searches_still_work(monkeypatch):
    assert _ids(monkeypatch, "breast") == ["brca_tcga"]
    assert _ids(monkeypatch, "nbl_target_2018_pub") == ["nbl_target_2018_pub"]
    assert _ids(monkeypatch, "melanoma") == []


def test_no_match_points_to_study_resolution_guide(monkeypatch):
    server._clear_studies_cache()
    monkeypatch.setattr(server, "_all_studies_query", _rows)
    monkeypatch.setattr(server, "_list_available_study_guides", lambda: [])
    (note,) = server.list_studies.fn(search="OHSU HTAN")
    assert "cbioportal://study-resolution-guide" in note["note"] and "hta9" in note["note"]
    assert "note" not in server.list_studies.fn(search="osteosarcoma")[0]
    assert all("note" not in s for s in server.list_studies.fn())


def test_guide_listing_and_query_tool_route_to_the_right_guides():
    guides = {g["uri"]: g["description"] for g in server.list_guides.fn()}
    assert "OHSU" in guides["cbioportal://study-resolution-guide"]
    assert "cancer types" in guides["cbioportal://faq-guide"]
    assert "ONCOTREE_CODE" in guides["cbioportal://sample-filtering-guide"]
    query_doc = server.clickhouse_run_select_query.description
    assert "cbioportal://faq-guide" in query_doc and "ONCOTREE_CODE" in query_doc
