#!/usr/bin/env bash
# Validate study row policies against a prepared ClickHouse MCP database.
#
# Usage:
#   ./scripts/validate_study_auth_policies.sh brca_tcga luad_tcga
#
# Required env:
#   CLICKHOUSE_HOST
#   CLICKHOUSE_DATABASE
#   CLICKHOUSE_USER
#   CLICKHOUSE_PASSWORD
#
# Optional env:
#   CLICKHOUSE_PORT   default 9440
#   CLICKHOUSE_SECURE default true

set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
: "${CLICKHOUSE_USER:?CLICKHOUSE_USER is required}"
: "${CLICKHOUSE_PASSWORD:?CLICKHOUSE_PASSWORD is required}"

AUTHORIZED_STUDY="${1:?first arg must be an authorized study id}"
UNAUTHORIZED_STUDY="${2:?second arg must be an unauthorized study id}"

CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-9440}"
CLICKHOUSE_SECURE="${CLICKHOUSE_SECURE:-true}"

secure_flag=()
if [[ "$CLICKHOUSE_SECURE" == "true" || "$CLICKHOUSE_SECURE" == "1" ]]; then
    secure_flag+=(--secure)
fi

run_query() {
    local query="$1"
    clickhouse-client \
        --host "$CLICKHOUSE_HOST" \
        --port "$CLICKHOUSE_PORT" \
        "${secure_flag[@]}" \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DATABASE" \
        --query "$query"
}

echo "Authorized study should be visible:"
run_query "
SELECT DISTINCT cancer_study_identifier
FROM clinical_data_derived
SETTINGS cbioportal_allowed_studies='${AUTHORIZED_STUDY}', cbioportal_allow_all='0';
"

echo
echo "Unauthorized study should not be visible even with an adversarial WHERE:"
run_query "
SELECT DISTINCT cancer_study_identifier
FROM clinical_data_derived
WHERE cancer_study_identifier = '${UNAUTHORIZED_STUDY}'
SETTINGS cbioportal_allowed_studies='${AUTHORIZED_STUDY}', cbioportal_allow_all='0';
"

echo
echo "No-study scope should return zero rows:"
run_query "
SELECT count()
FROM clinical_data_derived
SETTINGS cbioportal_allowed_studies='', cbioportal_allow_all='0';
"

echo
echo "Allow-all scope should return studies:"
run_query "
SELECT DISTINCT cancer_study_identifier
FROM clinical_data_derived
LIMIT 10
SETTINGS cbioportal_allowed_studies='', cbioportal_allow_all='1';
"

echo
echo "CTE should still be filtered:"
run_query "
WITH x AS (
    SELECT *
    FROM clinical_data_derived
)
SELECT DISTINCT cancer_study_identifier
FROM x
SETTINGS cbioportal_allowed_studies='${AUTHORIZED_STUDY}', cbioportal_allow_all='0';
"
