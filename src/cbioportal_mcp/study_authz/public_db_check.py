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

    try:
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
        if found("authorities", "EXISTS TABLE authorities") and found(
            "authorities", "SELECT EXISTS(SELECT 1 FROM authorities)"
        ):
            raise PublicDatabaseError("authorities contains rows")
    finally:
        with suppress(Exception):
            client.close()
