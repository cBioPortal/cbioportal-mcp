#!/usr/bin/env bash
# Apply every sql/*.sql file in numeric order against a prepped LLM clone
# database. Uses ADMIN credentials (CREATE/DROP/INSERT), not the MCP's
# SELECT-only runtime user.
#
# Required env:
#   CLICKHOUSE_HOST
#   CLICKHOUSE_DATABASE         target prepped DB (e.g. cbioportal_public_librechat_blue)
#   CLICKHOUSE_ADMIN_USER       NOT CLICKHOUSE_USER — must have DDL/DML rights on the target DB
#   CLICKHOUSE_ADMIN_PASSWORD
#
# Optional env:
#   CLICKHOUSE_PORT             default 9440 (native+TLS)
#   CLICKHOUSE_SECURE           default true
#   SQL_DIR                     default <repo>/sql
#
# Requires the `clickhouse-client` binary on PATH.

set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
: "${CLICKHOUSE_ADMIN_USER:?CLICKHOUSE_ADMIN_USER is required - do not use the MCP SELECT-only user}"
: "${CLICKHOUSE_ADMIN_PASSWORD:?CLICKHOUSE_ADMIN_PASSWORD is required}"

CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-9440}"
CLICKHOUSE_SECURE="${CLICKHOUSE_SECURE:-true}"
SQL_DIR="${SQL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/sql}"

if [[ ! -d "$SQL_DIR" ]]; then
    echo "error: sql dir not found: $SQL_DIR" >&2
    exit 1
fi

if ! command -v clickhouse-client >/dev/null 2>&1; then
    echo "error: clickhouse-client not on PATH (apt install clickhouse-client, or use the clickhouse/clickhouse-client docker image)" >&2
    exit 1
fi

# clickhouse-client TLS flag is a bare --secure (no value)
secure_flag=()
if [[ "$CLICKHOUSE_SECURE" == "true" || "$CLICKHOUSE_SECURE" == "1" ]]; then
    secure_flag+=(--secure)
fi

echo "Target: $CLICKHOUSE_ADMIN_USER@$CLICKHOUSE_HOST:$CLICKHOUSE_PORT/$CLICKHOUSE_DATABASE"
echo "SQL dir: $SQL_DIR"
echo

# Iterate numbered files in numeric order. Files without a leading digit are
# treated as docs (e.g. README.md) and skipped.
shopt -s nullglob
# Bash globs are lexicographically sorted, which matches numeric order for
# our 0-N prefix scheme.
for f in "$SQL_DIR"/*.sql; do
    base=$(basename "$f")
    [[ "$base" =~ ^[0-9]+- ]] || { echo "skip   $base (no numeric prefix)"; continue; }
    echo "apply  $base"
    clickhouse-client \
        --host "$CLICKHOUSE_HOST" \
        --port "$CLICKHOUSE_PORT" \
        "${secure_flag[@]}" \
        --user "$CLICKHOUSE_ADMIN_USER" \
        --password "$CLICKHOUSE_ADMIN_PASSWORD" \
        --database "$CLICKHOUSE_DATABASE" \
        --multiquery \
        --queries-file "$f"
done

echo
echo "Done."
