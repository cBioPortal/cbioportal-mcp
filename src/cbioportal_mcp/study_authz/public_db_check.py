"""Startup checks for an unfiltered public deployment."""

from contextlib import suppress


class PublicDatabaseError(RuntimeError):
    """A public-database rule failed. Messages contain fixed text only."""


def check(allowed_groups: frozenset[str]) -> None:
    from mcp_clickhouse.mcp_server import clickhouse_connect, get_config

    try:
        # Use the same connection settings as runtime. The upstream factory
        # logs raw connection errors, which can contain secrets.
        client = clickhouse_connect.get_client(**get_config().get_client_config())
    except Exception:
        raise PublicDatabaseError("connection to the configured ClickHouse database") from None

    def found(rule: str, sql: str, parameters: dict[str, list[str]] | None = None) -> bool:
        try:
            value = client.query(sql, parameters=parameters).result_rows[0][0]
            if value not in (0, 1):
                raise ValueError
            return bool(value)
        except Exception:
            raise PublicDatabaseError(f"{rule}: could not verify") from None

    def rows_hidden(table: str) -> None:
        # Row policies (and additional_table_filters) apply to count() but not
        # to system.tables.total_rows. A policy on system.row_policies can hide
        # policies from the catalog read above, so this check does not rely
        # on it. A missing row, NULL total_rows (e.g. a view) or an error
        # fails closed; so does a lightweight-deleted row.
        try:
            meta = client.query(
                "SELECT total_rows FROM system.tables "
                "WHERE database = currentDatabase() AND name = {table:String}",
                parameters={"table": table},
            ).result_rows
            total = meta[0][0] if len(meta) == 1 else None
            count = client.query(f"SELECT count() FROM {table}").result_rows[0][0]
            if not isinstance(total, int) or not isinstance(count, int):
                raise ValueError
        except Exception:
            raise PublicDatabaseError("row policies: could not verify row counts") from None
        if count != total:
            raise PublicDatabaseError("row policies: rows hidden from the check user")

    try:
        # Checked first: a row policy can hide rows from the checks below. The
        # restricted runtime user needs SHOW ROW POLICIES ON <db>.* to read
        # system.row_policies; without it the read fails and so does the check.
        # The catalog can itself be filtered, so an empty result is not proof:
        # rows_hidden() covers every table read here independently.
        if found(
            "row policies",
            "SELECT EXISTS(SELECT 1 FROM system.row_policies WHERE database = currentDatabase())",
        ):
            raise PublicDatabaseError("row policies exist on the database")
        rows_hidden("cancer_study")
        if found(
            "cancer_study.public",
            "SELECT EXISTS(SELECT 1 FROM cancer_study WHERE public = 0)",
        ):
            raise PublicDatabaseError("cancer_study.public = 0")
        if found(
            "cancer_study.groups",
            "SELECT EXISTS(SELECT 1 FROM cancer_study WHERE arrayExists("
            "entry -> entry != '' AND NOT has(arrayMap(group_name -> upperUTF8(group_name), "
            "{allowed:Array(String)}), upperUTF8(entry)), splitByChar(';', ifNull(groups, ''))))",
            {"allowed": sorted(allowed_groups)},
        ):
            raise PublicDatabaseError("cancer_study.groups contains an entry outside the allowlist")
        if found("authorities", "EXISTS TABLE authorities"):
            rows_hidden("authorities")
            if found("authorities", "SELECT EXISTS(SELECT 1 FROM authorities)"):
                raise PublicDatabaseError("authorities contains rows")
    finally:
        with suppress(Exception):
            client.close()
