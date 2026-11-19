"""Startup checks for an unfiltered public deployment."""

import re
import time
from contextlib import suppress


class PublicDatabaseError(RuntimeError):
    """A public-database rule failed. Messages contain fixed text and table names only."""


# These read other tables (checked themselves) or hold no counted rows.
# A materialized view's inner table is listed and checked as a storage table.
NON_STORAGE_ENGINES = frozenset(
    {"View", "MaterializedView", "LiveView", "WindowView", "Dictionary"}
)
# A readonly=1 user cannot set max_execution_time, so the bound is client-side:
# a per-query socket timeout plus a total budget for the row-count pass.
QUERY_TIMEOUT_SECONDS = 60
ROW_COUNT_BUDGET_SECONDS = 300
_PRINTABLE_TABLE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _table_label(name: object) -> str:
    return name if isinstance(name, str) and _PRINTABLE_TABLE.fullmatch(name) else "<table>"


def check(allowed_groups: frozenset[str]) -> None:
    from mcp_clickhouse.mcp_server import clickhouse_connect, get_config

    try:
        # Use the same connection settings as runtime. The upstream factory
        # logs raw connection errors, which can contain secrets.
        settings = get_config().get_client_config()
        settings["send_receive_timeout"] = min(
            settings.get("send_receive_timeout") or QUERY_TIMEOUT_SECONDS, QUERY_TIMEOUT_SECONDS
        )
        client = clickhouse_connect.get_client(**settings)
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

    def rows_hidden() -> None:
        # Row policies and additional_table_filters apply to count() but not
        # to system.tables.total_rows. A policy on system.row_policies can hide
        # policies from the catalog read above, so this pass does not rely on
        # it. It covers every storage table in the database. A policy on
        # system.tables can hide a table from that listing, so the listing is
        # cross-checked against system.columns. NULL total_rows on a storage
        # engine, an error or a timeout fails closed; so does a
        # lightweight-deleted row (still counted in total_rows).
        unverified = "row policies: could not verify row counts"
        deadline = time.monotonic() + ROW_COUNT_BUDGET_SECONDS
        try:
            tables = client.query(
                "SELECT name, engine, total_rows FROM system.tables "
                "WHERE database = currentDatabase()"
            ).result_rows
            listed = {
                row[0]
                for row in client.query(
                    "SELECT DISTINCT table FROM system.columns WHERE database = currentDatabase()"
                ).result_rows
            }
        except Exception:
            raise PublicDatabaseError(unverified) from None
        if listed != {row[0] for row in tables}:
            raise PublicDatabaseError(f"{unverified}: table listings disagree")
        for name, engine, total in tables:
            if engine in NON_STORAGE_ENGINES:
                continue
            label = _table_label(name)
            if not isinstance(total, int) or time.monotonic() > deadline:
                raise PublicDatabaseError(f"{unverified} ({label})")
            try:
                count = client.query(
                    "SELECT count() FROM {table:Identifier}", parameters={"table": name}
                ).result_rows[0][0]
                if not isinstance(count, int):
                    raise ValueError
            except Exception:
                raise PublicDatabaseError(f"{unverified} ({label})") from None
            if count != total:
                raise PublicDatabaseError(
                    f"row policies: rows hidden from the check user ({label})"
                )

    try:
        # Checked first: a row policy can hide rows from the checks below. The
        # restricted runtime user needs SHOW ROW POLICIES ON <db>.* to read
        # system.row_policies; without it the read fails and so does the check.
        # The catalog can itself be filtered, so an empty result is not proof:
        # rows_hidden() checks every table independently.
        if found(
            "row policies",
            "SELECT EXISTS(SELECT 1 FROM system.row_policies WHERE database = currentDatabase())",
        ):
            raise PublicDatabaseError("row policies exist on the database")
        rows_hidden()
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
