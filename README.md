# cBioPortal MCP Server

> **WARNING ⚠️: This is still under construction**

A wrapper around the [mcp-clickhouse server](https://github.com/ClickHouse/mcp-clickhouse) adding a [cBioPortal-specific system prompt](https://github.com/cBioPortal/cbioportal-mcp/blob/main/src/cbioportal_mcp/resources/system-prompt.md).

## Installation

```bash
# Navigate to the project directory
cd cbioportal-mcp

# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
# venv\Scripts\activate

# Upgrade pip
pip install --upgrade pip

# Install the package in development mode
pip install -e .

# Or install with development dependencies
pip install -e "."
```

## Configuration

Set the same environment variables used by mcp-clickhouse:

```bash
export CLICKHOUSE_HOST=your-clickhouse-host
export CLICKHOUSE_PORT=9000
export CLICKHOUSE_USER=your-username
export CLICKHOUSE_PASSWORD=your-password
export CLICKHOUSE_DATABASE=your-cbioportal-database  # see "Preparing the database" below
export CLICKHOUSE_SECURE=true  # or false for insecure connections
export CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio # or http or sse
# Optional: mount the HTTP endpoint under a sub-path (default: /mcp).
# Set when reverse-proxied behind a prefix so trailing-slash redirects
# include it, e.g. /db/mcp when served at https://host/db/mcp.
# export CLICKHOUSE_MCP_HTTP_PATH=/db/mcp
# Optional: how long table lists, column descriptions, and generated study
# guides are cached in-process (default: 3600; 0 disables the cache).
# export CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS=3600
# Optional: most ClickHouse queries one server process runs at once, across
# concurrent tool calls (default: 4). Sets mcp-clickhouse's query pool
# (CLICKHOUSE_MCP_MAX_WORKERS, which it overrides); may also be set in the .env
# mcp-clickhouse loads. A query waiting for a free slot counts against
# CLICKHOUSE_MCP_QUERY_TIMEOUT, so a saturated pool yields a timeout error
# rather than a hung request. If mcp_clickhouse is imported before
# cbioportal_mcp with a different pool size, the server refuses to start.
# export CBIOPORTAL_MCP_MAX_CONCURRENT_QUERIES=4
```

### Study-Level Access Control

Public deployments default to unrestricted study access:

```bash
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=public
```

Internal deployments can enable per-request study authorization:

```bash
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted
export CBIOPORTAL_MCP_AUTH_REQUIRED=true
export CBIOPORTAL_MCP_AUTH_PROXY_SECRET=shared-secret-known-only-to-the-proxy
```

The MCP server expects authentication to be handled by a trusted reverse proxy
or gateway backed by Keycloak. The proxy should validate the user session or
token, strip spoofable inbound identity headers, and inject trusted headers such
as:

```text
x-user-id: <keycloak-sub-or-username>
x-user-email: <email>
x-forwarded-groups: /group/a,/group/b
x-cbioportal-allowed-studies: study_a,study_b
```

The server reads identity only from the configured header names (defaults
above); if your proxy uses others, set them explicitly, e.g. for
oauth2-proxy:

```bash
export CBIOPORTAL_MCP_AUTH_USER_ID_HEADERS=x-auth-request-user
export CBIOPORTAL_MCP_AUTH_EMAIL_HEADERS=x-auth-request-email
export CBIOPORTAL_MCP_AUTH_GROUPS_HEADERS=x-auth-request-groups
```

The proxy must drop every one of these headers - plus the allowed-studies
header and the proxy-secret header - from inbound client requests before
injecting its own values; otherwise a client can add its own groups header.
`authz_header_names()` in `study_access.py` lists the full set, and the
reference proxy (`local_keycloak_proxy.py`) strips exactly that.

If your proxy can inject allowed studies directly, no ACL file is required. If
you prefer to manage access in the MCP deployment, configure a JSON ACL:

```bash
export CBIOPORTAL_MCP_STUDY_ACL_FILE=/etc/cbioportal-mcp/study-acl.json
```

```json
{
  "users": {
    "alice@example.org": ["brca_msk_2024"],
    "keycloak-subject-id": ["study_a", "study_b"]
  },
  "groups": {
    "/cbioportal/admins": ["*"],
    "/research/brca": ["brca_msk_2024"]
  },
  "default": []
}
```

`CBIOPORTAL_MCP_AUTH_PROXY_SECRET` (set above) is **required** in restricted
mode, not optional: it is the only thing that lets the server verify that
`x-user-id` / `x-forwarded-groups` / `x-cbioportal-allowed-studies` actually
came from your trusted proxy rather than a direct caller who can reach this
process on the network. The server refuses to start in restricted mode
without it (`validate_study_access_startup_config`, called from `main()`)
rather than silently trusting unverified headers. Only the header name
carrying the secret is configurable:

```bash
# optional; default is x-cbioportal-mcp-proxy-secret
export CBIOPORTAL_MCP_AUTH_PROXY_SECRET_HEADER=x-cbioportal-mcp-proxy-secret
```

In restricted mode, `list_studies`, study guide access, and arbitrary ClickHouse
queries are checked against the current user's allowed studies. Arbitrary SQL is
intentionally conservative: study-scoped queries must include literal
`cancer_study_identifier` filters, or parameterized view arguments such as
`study='...'` / `studies=['...']`, so the server can verify the requested study
IDs before ClickHouse executes the query.

For stronger isolation, enable ClickHouse row-policy enforcement:

```bash
export CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true
# optional; ClickHouse custom settings generally need an allowed prefix.
export CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING=SQL_cbiomcp_allowed_studies
```

In this mode, the MCP server passes the resolved study allowlist to ClickHouse
as a query setting on every SELECT, and ClickHouse row policies filter every
study-scoped table by following its real foreign-key path back to
`cancer_study.cancer_study_identifier`. The production policies live in
[`sql/study-authz/0-row-policies.sql`](sql/study-authz/0-row-policies.sql),
generated from
[`row_policy_catalog.py`](src/cbioportal_mcp/authentication/row_policy_catalog.py)
by `scripts/generate_row_policy_sql.py` (CI fails on drift), with the setting
name left as a placeholder that the apply script renders from the same
`CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING` the server reads. They
are **opt-in** (restricted deployments only):

```bash
APPLY_STUDY_AUTHZ_SQL=true ./scripts/apply_sql.sh   # see sql/README.md
# then, once per runtime user:
#   GRANT cbioportal_mcp_study_restricted TO <mcp_user>;
#   SET DEFAULT ROLE cbioportal_mcp_study_restricted TO <mcp_user>;
```

They cover the cBioPortal ClickHouse schema (3.1.0 and the legacy schema
cloned deployments use): direct `cancer_study_identifier` tables
(`clinical_data_derived`, `genomic_event_derived`, the other `*_derived`
tables, `cancer_study_query_preferences`), `cancer_study_id` tables
(`patient`, `genetic_profile`, `sample_list`, `copy_number_seg`, ...),
tables reached through `patient` (`sample`, `clinical_event`,
`clinical_patient`) or `sample` (`clinical_sample`), every per-profile table
through `genetic_profile` (`mutation`, `genetic_alteration`,
`structural_variant`, `sample_cna_event`, ...), multi-hop tables
(`clinical_event_data` -> `clinical_event` -> `patient`), and the shared
`mutation_event` / `cna_event` dimensions (visible only when observed in an
allowed study). The classification is in
[`row_policy_catalog.py`](src/cbioportal_mcp/authentication/row_policy_catalog.py).
Views (`sql/4`-`sql/7`, including parameterized views) get a `USING 1`
pass-through policy and inherit filtering from their underlying tables
because every view is `SQL SECURITY INVOKER`: declared in each `CREATE VIEW`
in `sql/4`, `sql/5`, `sql/7` (so the daily DROP + CREATE stays INVOKER) and
set again by the policy file for older views. A `DEFINER` or `NONE` view, a
materialized view or a dictionary would bypass the caller's row policies; the
coverage check below and the server's startup check reject them, and in
row-policy mode the server re-runs that check every
`CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS` (default 300; must be 1-86400 -
anything else, including 0, NaN or inf, stops the server from starting) and
refuses study-data queries while it fails, while the re-check thread isn't
running, or when its last completed check is older than three intervals. Apply the policy file after the
portable `sql/*.sql` files on every run.
This is the recommended defense-in-depth mode for restricted deployments; the
app-level guard still rejects explicit requests for denied study IDs and
rejects any query mentioning an internal `SQL_cbiomcp_*` setting (tokenized,
so quoting, escapes and comments don't hide it). ClickHouse enforces the same
thing independently: the server sends `readonly=1` with every row-policy
query, which makes ClickHouse reject any `SETTINGS`/`SET` inside the query
while still applying the allowlist sent with the request. This only holds if
the runtime user's settings profile leaves `readonly` at its default (0) and
does not mark the setting `CHANGEABLE_IN_READONLY`; at startup the server
checks both directions (`verify_allowlist_lockdown`) and refuses to start
otherwise. Profiles with `readonly=1` or `readonly=2` are therefore
unsupported for row-policy mode. The same startup check refuses to run if
any view in the database is not explicitly `SQL SECURITY INVOKER`, if the
database holds a materialized view or dictionary, if the runtime user's
effective grants (`SHOW GRANTS FINAL`) are anything beyond
`SELECT ON <db>.*`, or if it can use `url()` / `remote()` / `remoteSecure()` /
`cluster()` / `file()` / `input()` or the external-source table functions
`mysql()` / `postgresql()` / `s3()` / `hdfs()` / `azureBlobStorage()` /
`mongodb()` / `redis()` / `sqlite()` / `odbc()` / `jdbc()`, or read
`system.processes` / `system.dictionaries` / `system.row_policies` (other
requests' SQL carries their allowlists or identities). Only ClickHouse error
497 (`ACCESS_DENIED`) counts as denied - or, for a table function the server
doesn't have at all, error 46 (`UNKNOWN_FUNCTION`); any other error fails the
check as inconclusive. Keep the server's `select_from_system_db_requires_grant`
enabled (verified on ClickHouse 25.8). Running restricted
mode without it logs a startup warning: the app-level guard is a heuristic
pattern check on arbitrary SELECT queries, not a real SQL parser, so it's the
weaker of the two supported modes.

Per-table grants (no wildcard) would mechanically close the "forgotten
table" gap on their own - ClickHouse just denies an ungranted table
outright, which is fail-closed too. Two things rule that out here,
independent of whether it would work: the MCP server's own startup
permission check requires the literal wildcard-scoped
`GRANT SELECT ON db.* TO <restricted-role>` (per-table grants don't satisfy
that check even when every existing table is individually granted), and
`SHOW TABLES` / schema discovery is grant-scoped in ClickHouse, so per-table
grants would make an unscoped table invisible to `clickhouse_list_tables()`,
not just inaccessible. Keeping the wildcard grant for those two reasons
means it says yes to every table, forgotten or not, by construction - there
is no room left in the grant layer to say no to one specific table. Instead,
pair it with a database-wide **default-deny row policy**, which is the only
layer left that can say no:

```sql
CREATE ROW POLICY cbioportal_mcp_default_deny
ON <db>.* USING 0 TO <restricted-role>;
```

This auto-covers tables created after the policy, the same way the wildcard
grant does. ClickHouse combines multiple `PERMISSIVE` row policies on one
table with `OR`, so a table with no policy of its own is stuck at `0`
(nothing visible), while a table with its own policy gets
`0 OR <real condition>` = `<real condition>`. A newly added table is
therefore unreadable by default until someone adds a policy deciding how it
maps back to a study (or explicitly allowlists it as study-agnostic
reference data, e.g. `gene`/`type_of_cancer`, with its own `USING 1` policy).
The default-deny also matches views, which is why every view needs its own
pass-through policy.

For a runnable local Keycloak + ClickHouse + MCP authz stack see
[`docker/local-e2e/`](docker/local-e2e/). Its ClickHouse fixture mirrors the
production linkage (no study column on `sample`/`patient`) and has the repo's
real `sql/*.sql` plus the production row-policy file applied on top, and the
verifier runs unfiltered `SELECT *` as restricted users against every
study-scoped table and several views. `tests/test_row_policy_sql.py` keeps the
policy file, the catalog, the views defined in `sql/` and the fixture in sync
without needing ClickHouse.

Before enabling `CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true` against a
real database - and periodically after, since an unrelated cBioPortal schema
migration can add a table nobody's scoped yet - run
[`scripts/verify_row_policy_coverage.py`](scripts/verify_row_policy_coverage.py)
against the live `system.tables` / `system.row_policies`. It fails if the
default-deny is missing, if a study-scoped table has only the default-deny
(fully denied by accident) or an unfiltered policy, if a reference table or
view has no policy, on `SQL SECURITY DEFINER` views, materialized views and
dictionaries, and on unclassified tables. It takes
**admin** credentials (`CLICKHOUSE_ADMIN_USER`/`CLICKHOUSE_ADMIN_PASSWORD`,
the same split [`scripts/apply_sql.sh`](scripts/apply_sql.sh) uses), not the
MCP's runtime SELECT-only user: reading `system.row_policies` requires a
grant that is *not* scoped to the querying user's own database access, so
granting it to the runtime role would leak policy metadata for every
database on the cluster through the `clickhouse_run_select_query`
arbitrary-SQL tool. The admin identity only needs `SHOW TABLES` /
`SHOW DICTIONARIES` on the target database (metadata, not row data) plus
`SELECT ON system.row_policies` and `system.dictionaries`.

### Study access from cBioPortal's authorities table (option A, off by default)

`CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=authorities` keeps **one shared read-only
ClickHouse user** but stops resolving study lists in the MCP. Each query
carries only the caller's **verified email**, and ClickHouse decides which
studies that is from cBioPortal's own `authorities` / `users` /
`cancer_study.groups`:

```bash
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted
export CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=authorities
export CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true      # required in this mode
export CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY=oauth             # or proxy
# optional; must match what the policies were rendered with
export CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING=SQL_cbio_user
```

| Flag | Default | Meaning |
|------|---------|---------|
| `CBIOPORTAL_MCP_STUDY_ACCESS_MODE` | `public` | `restricted` turns study authz on at all |
| `CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE` | `acl` | `acl`: #175 allowlist (ACL file / headers / admin groups); `authorities`: ClickHouse resolves studies from the caller's email |
| `CBIOPORTAL_MCP_STUDY_ACCESS_IDENTITY` | `oauth` | authorities mode: `oauth` = the MCP's own validated OAuth token (`sub`/`email` claims, the same ones telemetry reads); `proxy` = the trusted proxy's email header, only with `CBIOPORTAL_MCP_AUTH_PROXY_SECRET` |
| `CBIOPORTAL_MCP_CLICKHOUSE_USER_SETTING` | `SQL_cbio_user` | custom setting carrying the lower-cased email |
| `CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED` | `false` | send settings + rely on row policies (required for `authorities`) |
| `CBIOPORTAL_MCP_CLICKHOUSE_ALLOWED_STUDIES_SETTING` | `SQL_cbiomcp_allowed_studies` | acl mode's allowlist setting |
| `CBIOPORTAL_MCP_ROW_POLICY_RECHECK_SECONDS` | `300` | periodic safety re-check |
| `APPLY_STUDY_AUTHZ_SQL` (apply_sql.sh) | `false` | apply `sql/study-authz/` at all |
| `STUDY_AUTHZ_SOURCE` (apply_sql.sh) | `$CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE` or `acl` | which policy set to apply |
| `STUDY_AUTHZ_REFRESH_MINUTES` (apply_sql.sh) | `5` | `user_study` refresh interval (1-1440) |
| `STUDY_AUTHZ_MAX_STALENESS_SECONDS` (apply_sql.sh) | 2 × the refresh interval (600) | `user_study` rows from an older refresh grant nothing; must exceed the interval |
| `STUDY_AUTHZ_APP_NAME` / `STUDY_AUTHZ_FILTER_GROUPS_BY_APP_NAME` / `STUDY_AUTHZ_PUBLIC_GROUP` (apply_sql.sh) | `cbioportal` / `true` / `PUBLIC` | cBioPortal's `app.name` / `filter_groups_by_appname` / `always_show_study_group` |
| `STUDY_AUTHZ_GRANT_ROLE` (apply_sql.sh) | `true` | `false` for a clone that isn't live yet (see sql/README.md) |

With every flag at its default the server behaves exactly as before: no
setting is sent, the guard rejects nothing, the query-cache path is
unchanged, and the policy SQL is not applied
(`tests/test_study_authz_default_off.py`).

How it works:

- **Identity.** `oauth` reads identity and verification from the same
  token FastMCP validated for this request, and requires positive proof:
  FastMCP's `GoogleTokenVerifier` stores Google's raw responses in the
  claims (`google_user_data` = v2 userinfo, `google_token_info` = v1
  tokeninfo), and one of them must say `verified_email: true` (a JSON
  boolean, as Google returns it) for that same email. Missing, null, `0`,
  `"true"`, `false`, unexpected values, malformed claims and token-read
  errors are all refused; `proxy` reads
  the configured email header and requires the proxy secret on every request.
  Tool arguments, the prompt and other headers are never consulted. A
  missing, malformed or unverified email is rejected before any query runs;
  there is no unrestricted (`*`) value in this mode at all.
- **Entitlements.** `sql/study-authz/authorities/0-user-study.sql` creates
  `user_study (email, cancer_study_identifier, cancer_study_id)` as a
  **refreshable materialized view** over `authorities` + `users` +
  `cancer_study`, mirroring cBioPortal's `CancerStudyPermissionEvaluator`:
  `<app>:<STUDY_ID>` grants that study, `<app>:<GROUP>` grants every study
  whose `;`-separated `groups` contains it (case-insensitive), `ALL` /
  `ALL_TCGA` / `ALL_NCI_TARGET` work as in cBioPortal, and every *known*
  (present and enabled in `users`) user gets the studies in the PUBLIC group.
  It is recomputed every `STUDY_AUTHZ_REFRESH_MINUTES` and swapped in
  atomically, so a revoked authority stops granting rows within one interval.
  Every row carries `refreshed_at`, and every policy accepts only rows newer
  than `STUDY_AUTHZ_MAX_STALENESS_SECONDS`: if the refresh stops or keeps
  failing, all study access is denied once the bound passes (enforced in
  ClickHouse, not the MCP), so a revocation can never outlive it.
- **Policies.** `sql/study-authz/authorities/1-row-policies.sql` (generated
  from the same catalog) resolves every study-scoped table through
  `user_study` for `email = getSetting('<user setting>')`, following the same
  foreign-key paths as the acl set (numeric `cancer_study_id` paths end at
  `user_study.cancer_study_id`). `user_study` itself has a policy limiting it
  to the caller's own rows, so neither the caller's SQL nor the other
  policies' subqueries (which run as the caller) can see anyone else's
  entitlements. `authorities`, `users`, `data_access_tokens` and the refresh
  MV object have no policy and stay at the default-deny (zero rows).
- **App-level checks** (guide access, `list_studies`, the literal-ID guard)
  ask ClickHouse for the caller's studies through the same policy
  (`SELECT cancer_study_identifier FROM user_study`), memoized per request
  only. `list_studies` reads `cancer_study` per request instead of the shared
  unrestricted snapshot.

#### Query cache isolation in row-policy mode

One shared ClickHouse user plus identity-dependent row policies means any
result cache keyed without the identity could hand caller A's rows to caller
B. In row-policy mode (either source):

- the MCP itself keeps **no query-result cache** (beta removed the ClickHouse
  query-cache pilot); every row-policy query is sent with `use_query_cache=0`
  and the startup/periodic check confirms the server honors it, so a
  ClickHouse profile that turns the cache on can't make results shared;
- ClickHouse 25.8 does key cache entries on the request's changed settings
  (verified in the local e2e: alice's and bob's cached results differ), but
  that is version behavior the server doesn't rely on;
- the MCP's own dynamic study-guide cache is keyed by the caller's access
  partition (verified email, or resolved allowlist in acl mode) and stores the
  canonical study id; every hit re-checks the row-policy safety gate and
  re-authorizes that id against the caller's *current* entitlements (a live
  `user_study` read in authorities mode), so revoked, disabled or stale users
  never get a cached guide. Public mode keeps the plain study-id key;
- `list_studies` filters the shared snapshot per request (acl) or reads
  `cancer_study` per request (authorities);
- the schema cache (`SHOW TABLES` / `DESCRIBE` results) holds no row data and
  is the same for every caller of the shared role, so it stays shared. The
  remaining `lru_cache`s hold files baked into the image (guides, OncoTree,
  pitfalls) and the ACL file.

### Login provider per deployment

Each deployment picks exactly one login provider with
`CBIOPORTAL_MCP_AUTH_PROVIDER` (default `google`). The provider decides who
the caller is; `CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE` decides which studies
they may read. One resolver (`authentication/caller.py:resolve_caller_identity`)
feeds authentication, study access and telemetry, so they never disagree.
With every new setting at its default the server behaves, and tags Datadog
spans, exactly as before (`tests/test_telemetry_golden.py`).

| Deployment | `AUTH_PROVIDER` | `STUDY_ACCESS_MODE` | `STUDY_ACCESS_SOURCE` | ClickHouse policy set |
| --- | --- | --- | --- | --- |
| Public | `google` | `public` | – | none |
| Private, IdP speaks OIDC | `oidc` | `restricted` | `jwt` (studies in a token claim), `authorities`, or `acl` | `jwt`/`acl`: `sql/study-authz/0-row-policies.sql`; `authorities`: `sql/study-authz/authorities/` |
| Private, portal uses SAML | `portal_dat` | `restricted` | `portal_dat` | `sql/study-authz/0-row-policies.sql` |

`jwt` and `portal_dat` resolve the study list in the MCP, validate every ID
with `validate_study_ids()` and send it as the existing allowlist setting
(`SQL_cbiomcp_allowed_studies`), so they use the **acl policy variant**:
render and apply `sql/study-authz/0-row-policies.sql` exactly as for
`acl` (see "Study-Level Access Control" above), with
`CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true` - the server refuses to
start with either source otherwise. Neither source ever sends `*`, and
neither merges in `CBIOPORTAL_MCP_STUDY_ACL_FILE`,
`CBIOPORTAL_MCP_ALLOWED_STUDIES` or admin groups (startup refuses those
combinations). Cache isolation is unchanged: every query carries the
caller's allowlist setting with `use_query_cache=0`, and dynamic study guides
are cached per resolved allowlist and re-authorized on every hit. Guide
section queries run on worker threads in a copy of the request context, so
they resolve the same caller (`tests/test_caller_context_threads.py`).

The server refuses to start (exit 2) on any unsafe combination, for example
`oidc` without an issuer, client, base URL or audience; `portal_dat` without
a portal URL or HMAC secret; a non-https URL without the explicit local-test
override; `oidc`/`portal_dat` on a public deployment; `jwt` without
`oidc`; `portal_dat` with a source other than `portal_dat`; `authorities`
with `portal_dat` (no verified email); and `jwt`/`portal_dat` without row
policies. The full list is `tests/test_auth_provider_config.py::INVALID`.

#### Public: Google (default, unchanged)

```bash
export CBIOPORTAL_MCP_GOOGLE_CLIENT_ID=123-abc.apps.googleusercontent.com
export CBIOPORTAL_MCP_GOOGLE_CLIENT_SECRET=...
export CBIOPORTAL_MCP_GOOGLE_BASE_URL=https://mcp.cbioportal.org
export REDIS_URL=redis://:password@host:6379/1
# study access stays off (CBIOPORTAL_MCP_STUDY_ACCESS_MODE=public)
```

#### Private: OIDC (any standards-based IdP)

Generic OpenID Connect through FastMCP's `OIDCProxy`: discovery from
`<issuer>/.well-known/openid-configuration` (or
`CBIOPORTAL_MCP_OIDC_CONFIG_URL`), keys from the discovered JWKS. Every
upstream access token must carry a valid signature (one pinned asymmetric
algorithm, `CBIOPORTAL_MCP_OIDC_SIGNING_ALGORITHM`, default `RS256`), `iss`
equal to the configured issuer, an `aud` containing
`CBIOPORTAL_MCP_OIDC_AUDIENCE` (comma-separated list allowed), and an
unexpired `exp`; anything else is rejected with 401. Clients then use the
token the MCP itself issues after login, exactly as with Google. There is
nothing IdP-specific in the code; Keycloak and Okta below differ only in
configuration.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CBIOPORTAL_MCP_OIDC_ISSUER_URL` | – (required) | issuer, compared verbatim with `iss` |
| `CBIOPORTAL_MCP_OIDC_CLIENT_ID` / `_CLIENT_SECRET` | – (required) | confidential client registered at the IdP |
| `CBIOPORTAL_MCP_OIDC_BASE_URL` | – (required) | this server's public origin; register `<base>/auth/callback` at the IdP |
| `CBIOPORTAL_MCP_OIDC_AUDIENCE` | – (required) | accepted `aud` value(s) of the IdP's access tokens |
| `CBIOPORTAL_MCP_OIDC_SCOPES` | `openid email profile` | scopes requested and required |
| `CBIOPORTAL_MCP_OIDC_USER_ID_CLAIM` | `sub` | `enduser.id` |
| `CBIOPORTAL_MCP_OIDC_EMAIL_CLAIM` | `email` | email (authorities source, `enduser.email`) |
| `CBIOPORTAL_MCP_OIDC_EMAIL_VERIFIED_CLAIM` | `email_verified` | must be JSON `true` for the email to count |
| `CBIOPORTAL_MCP_OIDC_GROUPS_CLAIM` | – | groups (acl source) |
| `CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM` | – (required for `jwt`) | array of study IDs |
| `CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM_PREFIX` | – | keep only values with this prefix, prefix removed |
| `CBIOPORTAL_MCP_OIDC_REQUIRE_CONSENT` | `true` | FastMCP's consent screen before the IdP login |

Claim paths are an exact top-level claim name first (so namespaced claims
such as `https://example.org/studies` work), otherwise dot-separated
(`realm_access.roles`). With `STUDY_ACCESS_SOURCE=jwt` a token with no
studies claim, a non-array claim or an invalid study ID is **denied**; it
never falls back to ACL entries. An empty array means no studies.

**Email verification.** For `oidc` an email counts only when the token's
`email_verified` is exactly `true`. Missing, `false`, `"true"`, `1` or an
error reading the token all fail closed (the authorities source denies;
telemetry omits `enduser.email`). Some IdPs never emit `email_verified` in
access tokens; `CBIOPORTAL_MCP_OIDC_TRUST_EMAIL_WITHOUT_VERIFIED_CLAIM=true`
then accepts an email whose claim is *absent* (never one that says `false`).
That is a trust decision: enable it only if the IdP itself only issues
verified addresses (e.g. directory-managed accounts). Google deployments are
unaffected: they still require Google's own `verified_email`.

Keycloak (MSK's existing realm; studies as realm roles `cbioportal:<id>`):

```bash
export CBIOPORTAL_MCP_AUTH_PROVIDER=oidc
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted
export CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=jwt
export CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true
export CBIOPORTAL_MCP_OIDC_ISSUER_URL=https://keycloak.example.org/realms/cbioportal
export CBIOPORTAL_MCP_OIDC_CLIENT_ID=cbioportal-mcp
export CBIOPORTAL_MCP_OIDC_CLIENT_SECRET=...
export CBIOPORTAL_MCP_OIDC_BASE_URL=https://mcp.example.org
export CBIOPORTAL_MCP_OIDC_AUDIENCE=cbioportal-mcp   # add an audience mapper to the client
export CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM=realm_access.roles
export CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM_PREFIX=cbioportal:
export CBIOPORTAL_MCP_OIDC_GROUPS_CLAIM=groups
export REDIS_URL=redis://:password@host:6379/1
```

Okta (custom authorization server; login in `sub`, user id in `uid`, a
custom `cbio_studies` claim, no `email_verified` in access tokens):

```bash
export CBIOPORTAL_MCP_AUTH_PROVIDER=oidc
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted
export CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=jwt
export CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true
export CBIOPORTAL_MCP_OIDC_ISSUER_URL=https://example.okta.com/oauth2/default
export CBIOPORTAL_MCP_OIDC_CLIENT_ID=0oa...
export CBIOPORTAL_MCP_OIDC_CLIENT_SECRET=...
export CBIOPORTAL_MCP_OIDC_BASE_URL=https://mcp.example.org
export CBIOPORTAL_MCP_OIDC_AUDIENCE=api://default
export CBIOPORTAL_MCP_OIDC_USER_ID_CLAIM=uid
export CBIOPORTAL_MCP_OIDC_STUDIES_CLAIM=cbio_studies
export CBIOPORTAL_MCP_OIDC_GROUPS_CLAIM=groups
```

**Base URL behind Traefik / a path prefix.** No new setting is needed, but
the OAuth routes FastMCP adds (`/authorize`, `/token`, `/register`,
`/auth/callback`, `/consent`, `/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource...`) are mounted at the server's
root, not under `CLICKHOUSE_MCP_HTTP_PATH`. Set
`CBIOPORTAL_MCP_OIDC_BASE_URL` to the origin with no path
(`https://mcp.example.org`) and route those paths to the pod as well as the
MCP path; a base URL with a path prefix only works if the proxy strips that
prefix before the pod. Behind a TLS-terminating proxy also set
`CLICKHOUSE_MCP_FORWARDED_ALLOW_IPS`, as for Google. This is the same rule
that applies to `CBIOPORTAL_MCP_GOOGLE_BASE_URL` today.

**Redis.** OIDC uses the same `REDIS_URL` with its own key namespace
(collections prefixed `cbioportal-mcp-oidc`, encrypted under a key derived
from the OIDC client secret). Google's keys and paths are unchanged, so a
deployment can switch provider and back without invalidating either set of
sessions.

#### Private: SAML portals, pasted Data Access Token (`portal_dat`)

The MCP cannot perform a portal's SAML login, and nothing is changed on the
portal side. Instead each user downloads a Data Access Token from the portal
(`dat.method=uuid` or `oauth2`) and configures their MCP client to send it in
an `x-auth-token` header. This is a deliberate manual step.

```bash
export CBIOPORTAL_MCP_AUTH_PROVIDER=portal_dat
export CBIOPORTAL_MCP_STUDY_ACCESS_MODE=restricted
export CBIOPORTAL_MCP_STUDY_ACCESS_SOURCE=portal_dat
export CBIOPORTAL_MCP_CLICKHOUSE_ROW_POLICY_ENABLED=true
export CBIOPORTAL_MCP_PORTAL_URL=https://portal.example.org         # https only
export CBIOPORTAL_MCP_PORTAL_DAT_HMAC_SECRET=$(openssl rand -hex 32) # >= 32 chars, stable
export CBIOPORTAL_MCP_PORTAL_CONFIRM_SHOW_UNAUTHORIZED_STUDIES_OFF=true
# optional
export CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS=30     # 0..300
export CBIOPORTAL_MCP_PORTAL_TIMEOUT_SECONDS=10
export CBIOPORTAL_MCP_PORTAL_USERNAMES_ARE_EMAILS=false
```

What the MCP does, and nothing else (it only ever makes GET requests, to the
configured portal URL, never one from the client):

- **Studies**: `GET /api/studies?projection=SUMMARY` with
  `Authorization: Bearer <DAT>`. The allowed studies are exactly the
  returned list; `readPermission` is ignored. At cBioPortal 5677496 (and
  v7.0.0 through v7.2.4) this route is filtered to studies the token can READ
  (`@PostFilter`). Newer portals also list unreadable studies when
  `skin.home_page.show_unauthorized_studies=true`, so the deployment must
  confirm that is off. **Minimum portal version: v7.0.0.** Before trusting a
  portal the MCP checks that it refuses an anonymous study list; a portal
  with `authenticate=false` (or `optional_oauth2` without method
  authorization) serves `/api` unfiltered and every token is denied.
- **Owner (telemetry only, never access)**: `GET /api/data-access-tokens`
  with the token in the header; only `username` is kept and the rest -
  including every token secret in that response - is discarded at once.
  `enduser.id` becomes `portal:<host>:<username>`. When that fails (a portal
  that restricts token listing, or an oauth2 portal, which doesn't list
  tokens), an oauth2 (JWT-shaped) token
  yields its unverified `sub` as a hint (`portal:<host>:sub:<sub>`),
  otherwise an HMAC fingerprint (`portal:<host>:fp:<hmac>`). `enduser.email`
  is set only with `CBIOPORTAL_MCP_PORTAL_USERNAMES_ARE_EMAILS=true`. The MCP
  never calls `/api/data-access-tokens/{token}` and never POSTs or DELETEs.
- **Cache**: the lookup is cached per token for
  `CBIOPORTAL_MCP_PORTAL_DAT_CACHE_SECONDS` (default 30), keyed by portal
  plus an HMAC of the token, and never past the token's known expiry. A
  revoked token stops working within that window; a portal error, timeout,
  redirect or malformed response denies.
- **Secrets**: the raw token never appears in logs, span tags, LLMObs
  metadata, exception messages or cache keys (`tests/test_portal_dat.py`
  checks success and failure paths with DEBUG logging on).

**A DAT is a full portal credential.** Anyone holding it can read
everything its owner can read in the portal, through the portal's own API,
until it expires - not just through the MCP. Treat it like a password: keep
it only in the MCP client's secret storage, never in a prompt or a shared
config. If a token may have leaked, **revoke all of your tokens** in the
portal (and download a new one), not just the one you think leaked: a uuid
portal can hold several live tokens per user, and you cannot tell which copy
was exposed. Note also that the portal itself may log rejected or invalid
tokens on its side (e.g. failed-authentication logs); that is outside the
MCP's control.

### Datadog Tool Metrics

The server emits one OpenTelemetry span per MCP tool call and can also emit
DogStatsD metrics for dashboard-level aggregates:

| Metric | Type | Purpose |
|---|---|---|
| `cbioportal_mcp.tool.calls` | counter | Tool-call volume by `tool`, `success`, `client_kind`, and `client_name` |
| `cbioportal_mcp.tool.duration_ms` | distribution | Tool latency, including p50/p95/p99 by tool |
| `cbioportal_mcp.tool.errors` | counter | Tool-call failures by tool/client |

DogStatsD metrics are enabled by default when `DD_AGENT_HOST` or
`DD_DOGSTATSD_HOST` is configured:

```bash
export DD_AGENT_HOST=<datadog-agent-host>
# Optional overrides:
export DD_DOGSTATSD_HOST=<dogstatsd-host>
export DD_DOGSTATSD_PORT=8125
export DD_SERVICE=cbioportal-mcp
export DD_ENV=prod
export CBIOPORTAL_MCP_DD_METRICS_ENABLED=true
export CBIOPORTAL_MCP_DD_METRIC_PREFIX=cbioportal_mcp
```

Set `CBIOPORTAL_MCP_DD_METRICS_ENABLED=false` to disable DogStatsD metrics.
The checked-in dashboard definition at
[`datadog/cbioagent-tool-metrics-dashboard.json`](datadog/cbioagent-tool-metrics-dashboard.json)
can be imported into Datadog or used as the source for updating the existing
cBioAgent dashboard.

## Preparing the database

**We strongly recommend pointing the MCP at a *separate* ClickHouse database, not your production cBioPortal database directly.** Two reasons:

1. **LLM-friendly fixes are destructive.** The agent works much better against a schema that's been cleaned up (misleading columns dropped, column comments added, OncoTree fields denormalized, named cohorts materialized). Applying those changes to your production database would interfere with the cBioPortal application.
2. **Isolation.** A separate database with a read-only user (`SELECT`-only) means agent traffic — including pathological queries — can't degrade production performance or accidentally expose data your portal users shouldn't see.

The recommended pattern is a periodic clone job: copy your production cBioPortal database into a separate ClickHouse database, then apply the SQL files in [`sql/`](sql/) — these add column comments, drop misleading columns, denormalize OncoTree, and materialize the `cancer_study_query_preferences` table the agent uses for cohort lookups. Point the MCP at this cloned-and-prepped database. See [`sql/README.md`](sql/README.md) for the full schema-prep contract and how to add deployment-specific preferences.

To apply the SQL files manually (e.g. for ad-hoc testing), use the helper script:

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_DATABASE=your-prepped-db
export CLICKHOUSE_ADMIN_USER=...  CLICKHOUSE_ADMIN_PASSWORD=...
./scripts/apply_sql.sh
```

Note the deliberately separate `CLICKHOUSE_ADMIN_*` env vars — admin credentials with DDL rights are kept out of the MCP server's runtime environment (which only ever needs `SELECT`).

For an end-to-end reference deployment (Kubernetes CronJob that handles the clone + SQL apply + atomic pointer-flip), see the cBioPortal team's daily clone CronJob in [knowledgesystems-k8s-deployment](https://github.com/knowledgesystems/knowledgesystems-k8s-deployment).

## Development

### Inspecting the Server with MCP Inspector

To connect to the MCP server and see requests and replies, use MCP Inspector.
You can run it with:
```bash
fastmcp dev inspector src/cbioportal_mcp/server.py
```

### Running the Server
```bash
# For development
python -m cbioportal_mcp.server

# Or using the installed script
cbioportal-mcp
```

### Running in Docker
```bash
# Build the image
docker build -t cbioportal-mcp -f docker/Dockerfile .
docker run -i -p 8000:8000 cbioportal-mcp
```

## License

Apache License 2.0 - see [LICENSE](LICENSE) for details.

## Related Projects

- [cBioPortal](https://github.com/cBioPortal/cbioportal) - The main cBioPortal platform
- [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) - ClickHouse MCP server
