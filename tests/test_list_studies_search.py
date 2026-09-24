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
