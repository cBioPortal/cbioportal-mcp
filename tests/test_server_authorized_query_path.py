from pathlib import Path


def test_server_local_run_select_query_uses_authorized_wrapper():
    repo_root = Path(__file__).resolve().parents[1]
    server_source = (repo_root / "src" / "cbioportal_mcp" / "server.py").read_text()

    assert "execute_authorized_select_query(query)" in server_source
    assert "from mcp_clickhouse.mcp_server import run_select_query" not in server_source
