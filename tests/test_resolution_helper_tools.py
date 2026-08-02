from cbioportal_mcp import server


def test_resolve_gene_symbol_separates_exact_and_family_matches(monkeypatch):
    queries = []

    def fake_run_select_query(query):
        queries.append(query)
        if "upper(hugo_gene_symbol) = 'CD3'" in query:
            return []
        if "upper(hugo_gene_symbol) LIKE 'CD3%'" in query:
            return [
                {"hugo_gene_symbol": "CD3D", "entrez_gene_id": 915},
                {"hugo_gene_symbol": "CD3E", "entrez_gene_id": 916},
                {"hugo_gene_symbol": "CD3G", "entrez_gene_id": 917},
            ]
        return []

    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    result = server._resolve_gene_symbol_impl("CD3")

    assert result["query_term"] == "CD3"
    assert result["is_ambiguous"] is True
    assert result["exact_matches"] == []
    assert [row["hugo_gene_symbol"] for row in result["prefix_matches"]] == [
        "CD3D",
        "CD3E",
        "CD3G",
    ]
    assert "Ask the user to choose from the prefix matches" in result["recommendation"]
    assert len(queries) == 3


def test_find_external_resources_queries_all_resource_scopes(monkeypatch):
    queries = []

    def fake_run_select_query(query):
        queries.append(query)
        if "FROM resource_study" in query:
            return [{"scope": "study", "display_name": "Minerva", "url": "https://example.org"}]
        return []

    monkeypatch.setattr(server, "run_select_query", fake_run_select_query)

    result = server._find_external_resources_impl(
        search="Minerva imaging",
        study_search="HTAN",
    )

    assert result["search_terms"] == ["Minerva", "imaging"]
    assert result["study_search"] == "HTAN"
    assert result["total_rows"] == 1
    assert [section["scope"] for section in result["sections"]] == ["study", "sample", "patient"]
    assert any("FROM resource_study" in query for query in queries)
    assert any("FROM resource_sample" in query for query in queries)
    assert any("FROM resource_patient" in query for query in queries)
    assert all("cancer_study" in query for query in queries)
    assert all("htan" in query.lower() for query in queries)
