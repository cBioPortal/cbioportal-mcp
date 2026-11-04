"""Request-scoped settings for the pinned mcp-clickhouse 0.5 query pipeline."""

import contextvars
import functools
import logging
import os
import re
import traceback
from contextlib import contextmanager

from clickhouse_connect import common

from cbioportal_mcp.identity import request_decision

_USER_QUERY = contextvars.ContextVar("cbioportal_user_query", default=None)
PUBLIC_ONLY_NOTE = "Results cover public studies only; frequencies are computed over those studies."


def enabled() -> bool:
    return os.getenv("CBIOPORTAL_MCP_STUDY_FILTERING") == "on"


def configure_client_settings() -> None:
    # Must happen before client construction: unknown custom settings must reach CH.
    if enabled():
        common.set_setting("invalid_setting_action", "send")


def email() -> str:
    decision = request_decision()
    value = (
        decision.identity.email.strip().lower()
        if decision and decision.allowed and decision.identity
        else ""
    )
    if not value:
        raise RuntimeError("Study filtering requires a verified user identity")
    return value


def guard_sql(query: str) -> None:
    # Fail closed even inside quoted identifiers/strings/comments. Decode CH's
    # backslash and doubled-quote escapes before checking reserved spellings.
    normalized = re.sub(
        r"\\(?:x([0-9a-fA-F]{2})|(.))", lambda m: (chr(int(m[1], 16)) if m[1] else m[2]), query
    )
    normalized = re.sub(r"['`\"\s]", "", normalized).lower()
    if any(word in normalized for word in ("sql_cbio_user", "getsetting")) or (
        "settings" in normalized and "sql_" in normalized
    ):
        raise ValueError("User SQL cannot access or override identity settings")


@contextmanager
def user_query():
    if not enabled():
        yield
        return
    configure_client_settings()
    token = _USER_QUERY.set(email())
    try:
        yield
    finally:
        _USER_QUERY.reset(token)


class _RedactEmails(logging.Filter):
    def filter(self, record):
        if enabled():

            def redact(value):
                return re.sub(
                    r"[^\s'\"<>]+(?:@|%40)[^\s'\"<>]+",
                    "[redacted]",
                    value,
                    flags=re.IGNORECASE,
                )

            record.msg = redact(record.getMessage())
            if record.exc_info:
                record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
            record.args = ()
        return True


def install() -> None:
    """Preserve upstream pooling, cancellation and timeouts; extend only settings/context."""
    configure_client_settings()
    import mcp_clickhouse.mcp_server as upstream

    if getattr(upstream, "_cbio_identity_installed", False):
        return
    original_submit = upstream.QUERY_EXECUTOR.submit
    original_settings = upstream.build_query_settings

    @functools.wraps(original_submit)
    def submit(fn, /, *args, **kwargs):
        return original_submit(contextvars.copy_context().run, fn, *args, **kwargs)

    @functools.wraps(original_settings)
    def settings(client):
        result = original_settings(client)
        caller = _USER_QUERY.get()
        if caller is not None:
            result.update(SQL_cbio_user=caller, readonly=1)
        return result

    upstream.QUERY_EXECUTOR.submit = submit
    upstream.build_query_settings = settings
    upstream._cbio_identity_installed = True
    for name in (
        "mcp-clickhouse",
        "cbioportal_mcp.server",
        "clickhouse_connect.driver.httpclient",
        "urllib3.connectionpool",
    ):
        logging.getLogger(name).addFilter(_RedactEmails())
