"""CLI: ``uv run python -m cbioportal_mcp.study_authz <command> --help``.

Commands: generate, render, apply, user-sql, check, make-test-db. Commands that
talk to ClickHouse use ADMIN credentials from CLICKHOUSE_HOST, CLICKHOUSE_PORT
(HTTP, default 8443), CLICKHOUSE_SECURE (default true), CLICKHOUSE_ADMIN_USER
and CLICKHOUSE_ADMIN_PASSWORD - never the MCP runtime user.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cbioportal_mcp.study_authz import sqlgen
from cbioportal_mcp.study_authz.catalog import ROW_POLICY_ROLE

PASSWORD_ENV = "CBIOPORTAL_MCP_RESTRICTED_PASSWORD"


def _client():
    import clickhouse_connect

    missing = [
        v
        for v in ("CLICKHOUSE_HOST", "CLICKHOUSE_ADMIN_USER", "CLICKHOUSE_ADMIN_PASSWORD")
        if os.getenv(v) is None
    ]
    if missing:
        raise SystemExit(f"error: set {', '.join(missing)} (admin credentials)")
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.getenv("CLICKHOUSE_PORT", "8443")),
        username=os.environ["CLICKHOUSE_ADMIN_USER"],
        password=os.environ["CLICKHOUSE_ADMIN_PASSWORD"],
        secure=os.getenv("CLICKHOUSE_SECURE", "true").lower() in {"1", "true", "yes"},
    )


def _params(args) -> sqlgen.Params:
    if "public" in args.database.lower() and not args.allow_public_database_name:
        raise SystemExit(
            f"error: {args.database} looks like the public clone, where filtering stays off; "
            "pass --allow-public-database-name if this really is a restricted database"
        )
    try:
        return sqlgen.Params(
            database=args.database,
            public_group=args.public_group,
            app_name=args.app_name,
            refresh_minutes=args.refresh_minutes,
            private_grace_minutes=args.private_grace_minutes,
            policy_target=args.policy_user or args.policy_role,
            target_is_user=bool(args.policy_user),
        )
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e


def _add_params(p: argparse.ArgumentParser) -> None:
    p.add_argument("--database", required=True, help="target (blue/green) database")
    p.add_argument("--public-group", required=True, help="cBioPortal always_show_study_group")
    p.add_argument("--app-name", required=True, help="cBioPortal app.name")
    p.add_argument("--refresh-minutes", type=int, default=5)
    p.add_argument("--private-grace-minutes", type=int, default=15)
    p.add_argument("--allow-public-database-name", action="store_true")
    target = p.add_mutually_exclusive_group()
    target.add_argument(
        "--policy-role",
        default=ROW_POLICY_ROLE,
        help="attach policies TO this role (default: %(default)s)",
    )
    target.add_argument(
        "--policy-user",
        metavar="USER",
        help="attach policies TO this existing user instead of a role",
    )


def cmd_generate(args) -> int:
    expected = sqlgen.row_policies_template()
    path = sqlgen.ROW_POLICIES_TEMPLATE
    if path.exists() and path.read_text() == expected:
        return 0
    if args.check:
        print(f"{path.name} is out of date: run python -m cbioportal_mcp.study_authz generate")
        return 1
    path.write_text(expected)
    print(f"wrote {path}")
    return 0


def cmd_render(args) -> int:
    for name, sql in sqlgen.rendered_files(_params(args)):
        print(f"-- ===== {name} =====\n{sql}")
    return 0


def cmd_apply(args) -> int:
    try:
        sqlgen.apply(_client(), _params(args), migrate=args.migrate)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e
    print("done; now run `check` for this database")
    return 0


def cmd_user_sql(args) -> int:
    password = os.getenv(PASSWORD_ENV)
    if not password:
        raise SystemExit(f"error: put the new user's password in {PASSWORD_ENV}")
    role = None if args.user_target else args.policy_role
    try:
        sql = sqlgen.restricted_user_sql(args.user, password, args.database, role=role)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e
    if not args.apply:
        print(sql)
        return 0
    client = _client()
    for statement in sqlgen.statements(sql):
        client.command(statement)
    if role is None:
        print(f"user {args.user} created with no grants; now run apply --policy-user {args.user}")
    else:
        print(f"user {args.user} now reads {args.database} only through {role}")
    return 0


def cmd_check(args) -> int:
    from cbioportal_mcp.study_authz.coverage import fetch_and_evaluate

    report = fetch_and_evaluate(_client(), args.database, args.target)
    for error in report.errors:
        print(f"ERROR  {error}")
    for name in report.unclassified:
        print(f"UNCLASSIFIED  {name} (classify it in study_authz/catalog.py, then regenerate)")
    print("OK" if report.ok else "FAILED")
    return 0 if report.ok else 1


def cmd_make_test_db(args) -> int:
    from cbioportal_mcp.study_authz import testdb

    gated = {}
    for item in args.gate:
        study, _, group = item.partition("=")
        gated[study] = group or f"PRIVATE_{study.upper()}"
    grants = testdb.read_grants_csv(Path(args.grants_csv)) if args.grants_csv else []
    try:
        testdb.make_test_db(
            _client(),
            args.source_database,
            _params(args),
            gated,
            args.public,
            grants,
            replace=args.replace,
        )
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e
    print(f"done; groups: {gated} (others: {args.public_group})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cbioportal_mcp.study_authz",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="regenerate templates/1-row-policies.sql.tmpl")
    p.add_argument("--check", action="store_true", help="exit 1 on drift instead of writing")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("render", help="print the rendered SQL (for clickhouse-client)")
    _add_params(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("apply", help="apply user_study + row policies to one database")
    _add_params(p)
    p.add_argument(
        "--migrate",
        action="store_true",
        help="replace the previous user_study layout (plain table + user_study_refresh): "
        "drops both, keeps user_study_manual; filtered queries fail until user_study's "
        "first refresh, so run it only on a copy that serves no traffic",
    )
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser(
        "user-sql",
        help=f"SQL for a runtime user that reads only via the role (password from {PASSWORD_ENV})",
    )
    p.add_argument("--user", required=True)
    p.add_argument("--database", required=True, help="the user's default database")
    who = p.add_mutually_exclusive_group()
    who.add_argument(
        "--policy-role",
        default=ROW_POLICY_ROLE,
        help="the role the policies are TO; the user gets only this role (default: %(default)s)",
    )
    who.add_argument(
        "--user-target",
        action="store_true",
        help="the user itself will be the policy target (apply --policy-user): no role "
        "grant, and refuse to replace an existing user",
    )
    p.add_argument("--apply", action="store_true", help="execute instead of printing")
    p.set_defaults(func=cmd_user_sql)

    p = sub.add_parser("check", help="fail if any table/view lacks the expected policy")
    p.add_argument("--database", required=True)
    p.add_argument(
        "--target",
        default=ROW_POLICY_ROLE,
        help="role or user the policies apply TO (default: %(default)s)",
    )
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("make-test-db", help="small test database from a few studies")
    _add_params(p)
    p.add_argument("--source-database", required=True)
    p.add_argument(
        "--gate",
        action="append",
        default=[],
        metavar="STUDY[=GROUP]",
        help="study to make private (default group PRIVATE_<STUDY>); repeatable",
    )
    p.add_argument(
        "--public",
        action="append",
        default=[],
        metavar="STUDY",
        help="study to keep public; repeatable",
    )
    p.add_argument("--grants-csv", help="CSV email,grant_key[,valid_until] for user_study_manual")
    p.add_argument(
        "--replace",
        action="store_true",
        help="drop the target first (only if this tool created it)",
    )
    p.set_defaults(func=cmd_make_test_db)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
