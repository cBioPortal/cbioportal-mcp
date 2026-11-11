#!/usr/bin/env bash
# Run the MCP contract tier end to end: build the image (unless
# CONTRACT_SKIP_BUILD=1), bring up ClickHouse + the image (up.sh), run
# tests/contract, tear down. Extra args go to pytest, e.g.
#
#   scripts/contract/run.sh                    # full run
#   UPDATE_SNAPSHOT=1 scripts/contract/run.sh  # rewrite the tools/list snapshot
#   CONTRACT_KEEP=1 scripts/contract/run.sh -k views   # leave containers up
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export CONTRACT_IMAGE="${CONTRACT_IMAGE:-cbioportal-mcp:contract}"
export CONTRACT_ENV_FILE="${CONTRACT_ENV_FILE:-$REPO/.contract.env}"

cd "$REPO"
if [ "${CONTRACT_SKIP_BUILD:-0}" != "1" ]; then
    docker build -f docker/Dockerfile -t "$CONTRACT_IMAGE" .
fi

if [ "${CONTRACT_KEEP:-0}" != "1" ]; then
    trap '"$REPO/scripts/contract/down.sh"' EXIT
fi
"$REPO/scripts/contract/up.sh"

set -a
# shellcheck disable=SC1090
. "$CONTRACT_ENV_FILE"
set +a
uv run --frozen --extra dev pytest tests/contract -v -p no:cacheprovider "$@"
