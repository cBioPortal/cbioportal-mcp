#!/usr/bin/env bash
# Tear down what scripts/contract/up.sh started. Set CONTRACT_LOG_DIR to keep
# the containers' logs first (CI uploads them on failure).
set -uo pipefail

P="${CONTRACT_PREFIX:-mcp-contract}"

if [ -n "${CONTRACT_LOG_DIR:-}" ]; then
    mkdir -p "$CONTRACT_LOG_DIR"
    for c in mcp ch; do
        docker logs "$P-$c" >"$CONTRACT_LOG_DIR/$c.log" 2>&1 || true
    done
fi
docker rm -f "$P-mcp" "$P-ch" >/dev/null 2>&1
docker network rm "$P-net" >/dev/null 2>&1
exit 0
