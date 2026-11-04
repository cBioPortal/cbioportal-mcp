-- ============================================================================
-- user_study: per-user study allowlist rebuilt from cBioPortal's own tables
-- (CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=authorities)
-- ============================================================================
-- OPT-IN. apply_sql.sh applies this directory only with
-- APPLY_STUDY_AUTHZ_SQL=true and STUDY_AUTHZ_SOURCE=authorities, then
-- 1-row-policies.sql. Render placeholders with sql/study-authz/render.sh:
--   __CLICKHOUSE_DATABASE__                  target database
--   __CBIOMCP_REFRESH_MINUTES__              STUDY_AUTHZ_REFRESH_MINUTES (default 5)
--   __CBIOMCP_APP_NAME__                     STUDY_AUTHZ_APP_NAME (default cbioportal)
--   __CBIOMCP_FILTER_GROUPS_BY_APP_NAME__    STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME (1/0)
--   __CBIOMCP_PUBLIC_GROUP__                 STUDY_AUTHZ_PUBLIC_GROUP (default PUBLIC; '' = off)
--   (1-row-policies.sql also takes __CBIOMCP_MAX_STALENESS_SECONDS__,
--    STUDY_AUTHZ_MAX_STALENESS_SECONDS, default 2 x the refresh interval)
--
-- Semantics mirror cBioPortal's CancerStudyPermissionEvaluator
-- (src/main/java/org/cbioportal/application/security/CancerStudyPermissionEvaluator.java):
--   * a user is "known" only when users has the email with enabled != 0 -
--     cBioPortal refuses the login otherwise (CustomOAuth2AuthorizationConfig:
--     user "either not in db or not authorized"), so their authorities and
--     PUBLIC grant nothing here either;
--   * authorities.authority: a leading ROLE_ is stripped; with
--     filter_groups_by_appname (cBioPortal default true) only
--     '<APP_NAME>:<X>' entries count, compared case-insensitively, and X is
--     what is matched; otherwise the whole authority is X. X is upper-cased;
--   * X grants a study when X = upper(cancer_study_identifier)  (a), or X is
--     one of the study's ';'-separated, upper-cased, non-empty groups  (b);
--     X = ALL grants every study, ALL_TCGA every *_TCGA study, ALL_NCI_TARGET
--     every *_TARGET study plus ALL_TARGET_PHASE1/2;
--   * every known user is also granted the always_show_study_group
--     (__CBIOMCP_PUBLIC_GROUP__), matched like any other group  (c).
-- Emails are lower-cased, as cBioPortal stores them (SecurityMapper.xml
-- inserts LOWER(email)); the MCP sends the caller's email lower-cased too.
--
-- Refresh / revocation latency: a REFRESHABLE materialized view (not an
-- insert-triggered one, which would never see DELETEs from authorities)
-- recomputes the whole table every __CBIOMCP_REFRESH_MINUTES__ minutes and
-- atomically swaps it in. A revoked authority, a disabled user or a study
-- regrouped away from a user's groups stops granting rows within one
-- interval plus the refresh's own run time.
--
-- Freshness (fail closed): every refresh stamps its rows with refreshed_at =
-- now(), and every row policy in 1-row-policies.sql only accepts user_study
-- rows with refreshed_at >= now() - STUDY_AUTHZ_MAX_STALENESS_SECONDS. If the
-- refresh stops or keeps failing (the previous contents would otherwise stay
-- forever, including revoked grants), every caller loses all study access
-- once the bound passes, and gets it back on the next successful refresh.
-- So a revocation is enforced within at most the staleness bound, whatever
-- happens to the refresh. scripts/verify_row_policy_coverage.py
-- --source authorities additionally reports a failing or stale refresh.
--
-- Access: user_study is in the MCP's database, so the restricted role can
-- read it; 1-row-policies.sql gives it a policy limiting rows to
-- email = getSetting('<user setting>'), so a caller (and the other policies'
-- subqueries, which run as the caller) only ever sees their own entitlements.
-- The MV object itself and authorities/users/data_access_tokens get no policy,
-- so the database-wide default-deny returns zero rows for them. The MV runs
-- as its definer (the admin applying this file), which is how it can read
-- authorities and users at all.
--
-- Idempotent and safe to run after every daily clone into a new database:
-- CREATE TABLE IF NOT EXISTS keeps the target; the MV is dropped and
-- recreated so a changed interval/app name takes effect (dropping an MV with
-- a TO target leaves the target's rows in place, so there is no gap), and the
-- file ends by waiting for the first refresh to finish.
--
-- Requires ClickHouse 24.10+ (refreshable materialized views without the
-- experimental flag, SYSTEM WAIT VIEW).
-- ============================================================================

CREATE TABLE IF NOT EXISTS __CLICKHOUSE_DATABASE__.user_study
(
    `email` String COMMENT 'Lower-cased cBioPortal user email (users.email).',
    `cancer_study_identifier` String COMMENT 'Study the user may read (its identifier).',
    `cancer_study_id` Int64 COMMENT 'cancer_study.cancer_study_id of the same study.',
    `refreshed_at` DateTime DEFAULT toDateTime(0)
        COMMENT 'Refresh time of this row; rows past the staleness bound grant nothing.'
)
ENGINE = MergeTree
ORDER BY (email, cancer_study_identifier)
COMMENT 'Per-user study allowlist for MCP row policies; rebuilt by user_study_refresh.';

-- Tables created before refreshed_at existed: the column defaults to the
-- epoch, i.e. stale (deny) until the MV below has refreshed once.
ALTER TABLE __CLICKHOUSE_DATABASE__.user_study
    ADD COLUMN IF NOT EXISTS `refreshed_at` DateTime DEFAULT toDateTime(0)
    COMMENT 'Refresh time of this row; rows past the staleness bound grant nothing.';

DROP VIEW IF EXISTS __CLICKHOUSE_DATABASE__.user_study_refresh SYNC;

CREATE MATERIALIZED VIEW __CLICKHOUSE_DATABASE__.user_study_refresh
REFRESH EVERY __CBIOMCP_REFRESH_MINUTES__ MINUTE
TO __CLICKHOUSE_DATABASE__.user_study
DEFINER = CURRENT_USER SQL SECURITY DEFINER
AS
WITH
    concat(upper('__CBIOMCP_APP_NAME__'), ':') AS app_prefix,
    __CBIOMCP_FILTER_GROUPS_BY_APP_NAME__ AS filter_by_app_name,
    upper('__CBIOMCP_PUBLIC_GROUP__') AS public_group,
    known_users AS (
        SELECT DISTINCT lower(email) AS email
        FROM __CLICKHOUSE_DATABASE__.users
        WHERE enabled != 0 AND email != ''
    )
SELECT DISTINCT
    g.email AS email,
    s.cancer_study_identifier AS cancer_study_identifier,
    s.cancer_study_id AS cancer_study_id,
    now() AS refreshed_at
FROM
(
    -- (a) + (b): the user's own authorities, normalized like
    -- CancerStudyPermissionEvaluator.getGrantedAuthorities().
    SELECT email, granted
    FROM
    (
        SELECT
            lower(email) AS email,
            replaceRegexpOne(authority, '^ROLE_', '') AS stripped,
            if(
                filter_by_app_name,
                if(
                    startsWith(upper(stripped), app_prefix),
                    upper(substring(stripped, length(app_prefix) + 1)),
                    ''
                ),
                upper(stripped)
            ) AS granted
        FROM __CLICKHOUSE_DATABASE__.authorities
    )
    WHERE granted != '' AND email IN (SELECT email FROM known_users)

    UNION ALL

    -- (c): always_show_study_group, granted to every known user.
    SELECT email, public_group AS granted
    FROM known_users
    WHERE public_group != ''
) AS g
INNER JOIN
(
    -- Every key that grants a study: its identifier, each of its groups, and
    -- the ALL / ALL_TCGA / ALL_NCI_TARGET wildcards that cover it.
    SELECT
        cancer_study_id,
        assumeNotNull(cancer_study_identifier) AS cancer_study_identifier,
        arrayJoin(arrayDistinct(arrayConcat(
            [upper(assumeNotNull(cancer_study_identifier)), 'ALL'],
            arrayFilter(
                x -> x != '',
                arrayMap(x -> upper(x), splitByChar(';', ifNull(`groups`, '')))
            ),
            if(endsWith(upper(assumeNotNull(cancer_study_identifier)), '_TCGA'), ['ALL_TCGA'], []),
            if(
                endsWith(upper(assumeNotNull(cancer_study_identifier)), '_TARGET')
                    OR upper(assumeNotNull(cancer_study_identifier))
                        IN ('ALL_TARGET_PHASE1', 'ALL_TARGET_PHASE2'),
                ['ALL_NCI_TARGET'],
                []
            )
        ))) AS match_key
    FROM __CLICKHOUSE_DATABASE__.cancer_study
    WHERE cancer_study_identifier IS NOT NULL AND cancer_study_identifier != ''
) AS s ON g.granted = s.match_key;

SYSTEM REFRESH VIEW __CLICKHOUSE_DATABASE__.user_study_refresh;
SYSTEM WAIT VIEW __CLICKHOUSE_DATABASE__.user_study_refresh;
