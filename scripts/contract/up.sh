#!/usr/bin/env bash
# Bring up the hermetic MCP contract environment (see docs/contract-tests.md):
#
#   1. a throwaway ClickHouse container with the repo mounted read-only;
#   2. the vendored cBioPortal schema + tiny fixture data (tests/contract/fixtures);
#   3. every sql/*.sql via scripts/apply_sql.sh, run INSIDE the ClickHouse
#      container (so the host needs no clickhouse-client);
#   4. a read-only app_user with only `GRANT SELECT ON <db>.*`, like prod;
#   5. the MCP image under test, with prod's chat env (http transport at /db/mcp).
#
# Writes the variables the contract tests need to $CONTRACT_ENV_FILE. No secrets.
#
# Env (all optional):
#   CONTRACT_IMAGE        image to test (default cbioportal-mcp:contract; must exist)
#   CONTRACT_PREFIX       container/network name prefix (default mcp-contract)
#   CONTRACT_MCP_PORT     host port for the MCP server (default 18080)
#   CONTRACT_CH_PORT      host port for ClickHouse HTTP (default 18123)
#   CLICKHOUSE_IMAGE      default clickhouse/clickhouse-server:25.8
#   CONTRACT_ENV_FILE     default <repo>/.contract.env
#   CONTRACT_MCP_EXTRA_ENV  extra "-e K=V" flags for the MCP container (used to
#                           prove the tier fails on a broken config)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${CONTRACT_IMAGE:-cbioportal-mcp:contract}"
P="${CONTRACT_PREFIX:-mcp-contract}"
MCP_PORT="${CONTRACT_MCP_PORT:-18080}"
CH_PORT="${CONTRACT_CH_PORT:-18123}"
CH_IMAGE="${CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:25.8}"
ENV_FILE="${CONTRACT_ENV_FILE:-$REPO/.contract.env}"
DB=cbioportal_contract
ADMIN_USER="admin"
ADMIN_PASSWORD="admin"
APP_USER=app_user
APP_PASSWORD=app_password
MCP_PATH=/db/mcp

log() { echo "[contract] $*"; }

"$REPO/scripts/contract/down.sh" >/dev/null 2>&1 || true

docker image inspect "$IMAGE" >/dev/null || {
    echo "image $IMAGE not found; build it first: docker build -f docker/Dockerfile -t $IMAGE ." >&2
    exit 1
}

log "network + ClickHouse ($CH_IMAGE)"
docker network create "$P-net" >/dev/null
docker run -d --name "$P-ch" --network "$P-net" \
    -p "127.0.0.1:$CH_PORT:8123" \
    -e CLICKHOUSE_USER="$ADMIN_USER" -e CLICKHOUSE_PASSWORD="$ADMIN_PASSWORD" \
    -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
    -v "$REPO:/repo:ro" \
    "$CH_IMAGE" >/dev/null

# No -i: with stdin attached, clickhouse-client may wait on it for more rows
# after a trailing `INSERT ... VALUES` in a --queries-file (the vendored schema
# ends with one), hanging forever. Only ch_stdin reads stdin.
ch() {
    docker exec "$P-ch" clickhouse-client --user "$ADMIN_USER" --password "$ADMIN_PASSWORD" "$@" </dev/null
}
ch_stdin() {
    docker exec -i "$P-ch" clickhouse-client --user "$ADMIN_USER" --password "$ADMIN_PASSWORD" "$@"
}

for _ in $(seq 1 60); do
    ch -q "SELECT 1" >/dev/null 2>&1 && break
    sleep 1
done
ch -q "SELECT version()" | sed 's/^/[contract] clickhouse /'

log "schema + fixture data into $DB"
ch -q "CREATE DATABASE $DB"
ch -d "$DB" --multiquery --queries-file /repo/tests/contract/fixtures/cbioportal_schema.sql
python3 "$REPO/tests/contract/fixtures/seed.py" | ch_stdin -d "$DB" --multiquery

log "sql/*.sql via scripts/apply_sql.sh"
docker exec \
    -e CLICKHOUSE_HOST=localhost -e CLICKHOUSE_PORT=9000 -e CLICKHOUSE_SECURE=false \
    -e CLICKHOUSE_DATABASE="$DB" \
    -e CLICKHOUSE_ADMIN_USER="$ADMIN_USER" -e CLICKHOUSE_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
    -e SQL_DIR=/repo/sql \
    "$P-ch" bash /repo/scripts/apply_sql.sh </dev/null

log "read-only $APP_USER (GRANT SELECT ON $DB.* only)"
ch --multiquery -q "
    CREATE USER $APP_USER IDENTIFIED WITH sha256_password BY '$APP_PASSWORD';
    GRANT SELECT ON $DB.* TO $APP_USER;"

log "MCP server ($IMAGE) at $MCP_PATH"
# Prod chat env (knowledgesystems-k8s-deployment cbioagent-clickhouse-mcp).
# shellcheck disable=SC2086
docker run -d --name "$P-mcp" --network "$P-net" \
    -p "127.0.0.1:$MCP_PORT:8000" \
    -e CLICKHOUSE_HOST="$P-ch" -e CLICKHOUSE_PORT=8123 \
    -e CLICKHOUSE_SECURE=false -e CLICKHOUSE_VERIFY=false \
    -e CLICKHOUSE_USER="$APP_USER" -e CLICKHOUSE_PASSWORD="$APP_PASSWORD" \
    -e CLICKHOUSE_DATABASE="$DB" \
    -e CLICKHOUSE_MCP_SERVER_TRANSPORT=http \
    -e CLICKHOUSE_MCP_BIND_HOST=0.0.0.0 -e CLICKHOUSE_MCP_BIND_PORT=8000 \
    -e CLICKHOUSE_MCP_HTTP_PATH="$MCP_PATH" \
    -e CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=4 \
    -e CBIOPORTAL_MCP_STUDY_FILTERING=off \
    -e CBIOPORTAL_MCP_AUTH_ENFORCEMENT=off \
    -e CBIOPORTAL_MCP_PUBLIC_DB_CHECK=off \
    ${CONTRACT_MCP_EXTRA_ENV:-} \
    "$IMAGE" >/dev/null

# Wait for the port, but don't hide a crash: the tests assert the container
# is still running ~30s after start, and print its logs if not.
for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Running}}' "$P-mcp")" = "true" ] || break
    curl -s -o /dev/null "http://127.0.0.1:$MCP_PORT/" && break
    sleep 1
done

cat > "$ENV_FILE" <<EOF
CBIOPORTAL_MCP_CONTRACT_URL=http://127.0.0.1:$MCP_PORT$MCP_PATH
CBIOPORTAL_MCP_CONTRACT_CONTAINER=$P-mcp
CBIOPORTAL_MCP_CONTRACT_CLICKHOUSE_URL=http://127.0.0.1:$CH_PORT
CBIOPORTAL_MCP_CONTRACT_DATABASE=$DB
CBIOPORTAL_MCP_CONTRACT_APP_USER=$APP_USER
CBIOPORTAL_MCP_CONTRACT_APP_PASSWORD=$APP_PASSWORD
EOF
log "ready; env written to $ENV_FILE"
