#!/usr/bin/env bash
# Render one sql/study-authz/**/*.sql file for a target deployment and print it
# to stdout. Used by scripts/apply_sql.sh and the local e2e init script, so
# both substitute exactly the same placeholders with the same validation.
#
#   CLICKHOUSE_DATABASE=<db> sql/study-authz/render.sh <file.sql> | clickhouse-client ...
#
# Env (defaults match the MCP server's own defaults, so the policies always
# read the setting the server sends):
#   CLICKHOUSE_DATABASE                                required, ^[A-Za-z0-9_]+$
#   CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING  default SQL_cbiomcp_allowed_studies (acl)
#   CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING             default SQL_cbio_user (authorities)
#   STUDY_AUTHZ_REFRESH_MINUTES                        default 5, 1..1440 (authorities)
#   STUDY_AUTHZ_MAX_STALENESS_SECONDS                  default 2 x the refresh interval; a
#                                                      user_study refresh older than this
#                                                      grants nothing (authorities); must
#                                                      exceed the refresh interval
#   STUDY_AUTHZ_APP_NAME                               default cbioportal; cBioPortal's app.name
#   STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME              default true; cBioPortal's filter_groups_by_appname
#   STUDY_AUTHZ_PUBLIC_GROUP                           default PUBLIC; cBioPortal's
#                                                      always_show_study_group ('' disables)
#   STUDY_AUTHZ_GRANT_ROLE                             default true; false leaves out the
#                                                      "GRANT SELECT ON <db>.* TO <role>" line,
#                                                      for a clone that is not live yet (the
#                                                      running MCP's startup/periodic check
#                                                      refuses a role granted on two
#                                                      databases); see sql/README.md
set -euo pipefail

f="${1:?usage: render.sh <file.sql>}"
db="${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
studies_setting="${CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING:-SQL_cbiomcp_allowed_studies}"
user_setting="${CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING:-SQL_cbio_user}"
refresh_minutes="${STUDY_AUTHZ_REFRESH_MINUTES:-5}"
app_name="${STUDY_AUTHZ_APP_NAME:-cbioportal}"
filter_by_app="${STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME:-true}"
public_group="${STUDY_AUTHZ_PUBLIC_GROUP-PUBLIC}"
grant_role="${STUDY_AUTHZ_GRANT_ROLE:-true}"
max_staleness="${STUDY_AUTHZ_MAX_STALENESS_SECONDS:-}"

die() { echo "render.sh: $*" >&2; exit 1; }

[[ -f "$f" ]] || die "no such file: $f"

[[ "$db" =~ ^[A-Za-z0-9_]+$ ]] || die "CLICKHOUSE_DATABASE must match ^[A-Za-z0-9_]+$"
for name in "$studies_setting" "$user_setting"; do
    # Custom settings need the server's custom_settings_prefixes (default SQL_).
    [[ "$name" =~ ^SQL_[A-Za-z0-9_]+$ ]] || die "setting name '$name' must match ^SQL_[A-Za-z0-9_]+$"
done
[[ "$refresh_minutes" =~ ^[0-9]+$ ]] && (( refresh_minutes >= 1 && refresh_minutes <= 1440 )) \
    || die "STUDY_AUTHZ_REFRESH_MINUTES must be an integer in 1..1440"
refresh_seconds=$(( refresh_minutes * 60 ))
if [[ -z "$max_staleness" ]]; then
    max_staleness=$(( 2 * refresh_seconds ))
fi
[[ "$max_staleness" =~ ^[0-9]+$ ]] \
    && (( max_staleness > refresh_seconds && max_staleness <= 7 * 86400 )) \
    || die "STUDY_AUTHZ_MAX_STALENESS_SECONDS must be an integer greater than the refresh interval (${refresh_seconds}s) and at most 604800"
[[ "$app_name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "STUDY_AUTHZ_APP_NAME must match ^[A-Za-z0-9_.-]+$"
[[ -z "$public_group" || "$public_group" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || die "STUDY_AUTHZ_PUBLIC_GROUP must be empty or match ^[A-Za-z0-9_.-]+$"
case "$(echo "$filter_by_app" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) filter_by_app=1 ;;
    false|0|no) filter_by_app=0 ;;
    *) die "STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME must be true or false" ;;
esac

case "$(echo "$grant_role" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes) grant_filter='^$x' ;;  # matches nothing
    false|0|no) grant_filter="^GRANT SELECT ON ${db}\\.\\* TO cbioportal_mcp_study_restricted;\$" ;;
    *) die "STUDY_AUTHZ_GRANT_ROLE must be true or false" ;;
esac

rendered=$(sed \
    -e "s/__CLICKHOUSE_DATABASE__/${db}/g" \
    -e "s/__CBIOMCP_ALLOWED_STUDIES_SETTING__/${studies_setting}/g" \
    -e "s/__CBIOMCP_USER_SETTING__/${user_setting}/g" \
    -e "s/__CBIOMCP_REFRESH_MINUTES__/${refresh_minutes}/g" \
    -e "s/__CBIOMCP_APP_NAME__/${app_name}/g" \
    -e "s/__CBIOMCP_FILTER_GROUPS_BY_APP_NAME__/${filter_by_app}/g" \
    -e "s/__CBIOMCP_PUBLIC_GROUP__/${public_group}/g" \
    -e "s/__CBIOMCP_MAX_STALENESS_SECONDS__/${max_staleness}/g" \
    "$f" | grep -v -e "$grant_filter" || true)
if grep -q "__CBIOMCP_[A-Z_]*__\|__CLICKHOUSE_DATABASE__" <<<"$rendered"; then
    die "unrendered placeholder left in $f"
fi
printf '%s\n' "$rendered"
