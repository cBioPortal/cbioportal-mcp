#!/usr/bin/env bash
# Runs inside the clickhouse container's /docker-entrypoint-initdb.d, after
# 00-init.sql has created the production-shaped fixture. Applies the repo's
# real sql/*.sql files (the same ones the daily clone job runs) and then the
# opt-in sql/study-authz/*.sql row policies, so the e2e exercises exactly the
# SQL a restricted deployment would apply - not a hand-maintained copy.
#
# The entrypoint executes this file when it is executable and sources it
# otherwise, so variables are prefixed and it never calls `exit`.
set -euo pipefail

cbiomcp_db=cbioportal_authz_e2e
cbiomcp_client=(clickhouse-client --host 127.0.0.1 --database "$cbiomcp_db" --multiquery)

for cbiomcp_f in /cbioportal-sql/*.sql; do
    echo "init-apply-repo-sql: applying ${cbiomcp_f#/cbioportal-sql/}"
    "${cbiomcp_client[@]}" < "$cbiomcp_f"
done

# sql/3 recreates cancer_study_query_preferences with only pattern-detected
# rows (none match this fixture); add one row per study so the table has
# data to filter.
"${cbiomcp_client[@]}" <<'SQL'
INSERT INTO cancer_study_query_preferences VALUES
    ('local_e2e', 'study_alpha', 'alpha fixture study'),
    ('local_e2e', 'study_beta', 'beta fixture study');
SQL

# STUDY_AUTHZ_SOURCE=acl (default) applies sql/study-authz/*.sql;
# authorities applies sql/study-authz/authorities/*.sql (the user_study
# refreshable MV, then its policies). Rendered by the same script apply_sql.sh
# uses, with the same defaults the server has.
case "${STUDY_AUTHZ_SOURCE:-acl}" in
    acl) cbiomcp_authz_dir=/cbioportal-sql/study-authz ;;
    authorities) cbiomcp_authz_dir=/cbioportal-sql/study-authz/authorities ;;
    *) echo "init-apply-repo-sql: bad STUDY_AUTHZ_SOURCE=${STUDY_AUTHZ_SOURCE}" >&2; false ;;
esac
for cbiomcp_f in "$cbiomcp_authz_dir"/*.sql; do
    echo "init-apply-repo-sql: applying ${cbiomcp_f#/cbioportal-sql/}"
    CLICKHOUSE_DATABASE="$cbiomcp_db" bash /cbioportal-sql/study-authz/render.sh "$cbiomcp_f" \
        | "${cbiomcp_client[@]}"
done
