# Import the package before any test module imports mcp_clickhouse: the
# package sizes mcp-clickhouse's query pool, and refuses to load if the pool
# was already created with a different size (see query_concurrency).
import cbioportal_mcp  # noqa: F401
