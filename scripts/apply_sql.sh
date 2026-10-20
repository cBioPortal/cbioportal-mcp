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
#   APPLY_STUDY_AUTHZ_SQL       default false; true also applies the opt-in
#                               study-level row policies in sql/study-authz/
#                               (restricted deployments only; needs CREATE
#                               ROLE / CREATE ROW POLICY rights)
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

# Apply order:
#   1. Portable files: SQL_DIR/*.sql in numeric order (0-, 1-, ...).
#   2. Portal-specific files: SQL_DIR/portal-specific/<portal>/*.sql.
#      Subdirectories iterate alphabetically, then numerically within each.
#      Files without a leading digit are treated as docs (e.g. README.md).
shopt -s nullglob

apply_one() {
    local f="$1"
    local rel="${f#${SQL_DIR}/}"
    local base
    base=$(basename "$f")
    [[ "$base" =~ ^[0-9]+- ]] || { echo "skip   $rel (no numeric prefix)"; return; }
    echo "apply  $rel"
    clickhouse-client \
        --host "$CLICKHOUSE_HOST" \
        --port "$CLICKHOUSE_PORT" \
        ${secure_flag[@]+"${secure_flag[@]}"} \
        --user "$CLICKHOUSE_ADMIN_USER" \
        --password "$CLICKHOUSE_ADMIN_PASSWORD" \
        --database "$CLICKHOUSE_DATABASE" \
        --multiquery \
        --queries-file "$f"
}

# Phase 1: portable files at the top level.
# Bash globs are lexicographically sorted, which matches numeric order for
# our 0-N prefix scheme.
for f in "$SQL_DIR"/*.sql; do
    apply_one "$f"
done

# Phase 2: portal-specific files. Each subdir under portal-specific/ is one
# deployment's set; iterate them all so a deployer image can include
# multiple subdirs if needed.
for d in "$SQL_DIR"/portal-specific/*/; do
    [[ -d "$d" ]] || continue
    for f in "$d"*.sql; do
        apply_one "$f"
    done
done

# Phase 3 (opt-in): study-level row policies for restricted deployments.
# These files qualify every table with __CLICKHOUSE_DATABASE__ (unqualified
# names in a row policy's subquery resolve against the *querying* session's
# database), so they are rendered for the target database first.
if [[ "${APPLY_STUDY_AUTHZ_SQL:-false}" == "true" || "${APPLY_STUDY_AUTHZ_SQL:-}" == "1" ]]; then
    if [[ ! "$CLICKHOUSE_DATABASE" =~ ^[A-Za-z0-9_]+$ ]]; then
        echo "error: CLICKHOUSE_DATABASE must match ^[A-Za-z0-9_]+$ to render study-authz SQL" >&2
        exit 1
    fi
    rendered_dir=$(mktemp -d)
    trap 'rm -rf "$rendered_dir"' EXIT
    for f in "$SQL_DIR"/study-authz/*.sql; do
        base=$(basename "$f")
        [[ "$base" =~ ^[0-9]+- ]] || continue
        mkdir -p "$rendered_dir/study-authz"
        sed "s/__CLICKHOUSE_DATABASE__/${CLICKHOUSE_DATABASE}/g" "$f" > "$rendered_dir/study-authz/$base"
        echo "render study-authz/$base -> $CLICKHOUSE_DATABASE"
        SQL_DIR="$rendered_dir" apply_one "$rendered_dir/study-authz/$base"
    done
else
    echo "skip   study-authz/ (set APPLY_STUDY_AUTHZ_SQL=true for restricted deployments)"
fi

echo
echo "Done."
