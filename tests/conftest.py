# Import the package before any test module imports mcp_clickhouse: the
# package sizes mcp-clickhouse's query pool at import, which only works if the
# pool hasn't been created yet (see query_concurrency).
import cbioportal_mcp  # noqa: F401
