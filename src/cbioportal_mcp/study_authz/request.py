"""Request-scoped settings for the pinned mcp-clickhouse 0.5 query pipeline."""

import contextvars
import functools
import logging
import os
import re
import traceback
from contextlib import contextmanager

from clickhouse_connect import common

from cbioportal_mcp import rollout
from cbioportal_mcp.identity import request_decision

_USER_QUERY = contextvars.ContextVar("cbioportal_user_query", default=None)
PUBLIC_ONLY_NOTE = "Results cover public studies only; frequencies are computed over those studies."


def enabled() -> bool:
    # RolloutSettings returned by get() has no filtering field. Read the
    # immutable pin itself without changing it; env is needed only before
    # startup validation (notably to configure clients at import time).
    pin = rollout._PIN
    if pin is not None:
        return pin.filtering == "on"
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


def _decode_sql(value: str) -> str:
    return re.sub(
        r"\\(?:x([0-9a-fA-F]{2})|(.))", lambda m: (chr(int(m[1], 16)) if m[1] else m[2]), value
    )


def _sql_tokens(query: str):
    """Separate strings/identifiers from keywords and ignore SQL comments."""
    pattern = re.compile(
        r"\s+|--[^\r\n]*|\#[^\r\n]*|/\*|"
        r"'(?:(?:\\.|'')|[^'\\])*'|"
        r'"(?:(?:\\.|"")|[^"\\])*"|'
        r"`(?:(?:\\.|``)|[^`\\])*`|[a-zA-Z_][a-zA-Z_0-9]*|.",
        re.DOTALL,
    )
    position = 0
    while position < len(query):
        match = pattern.match(query, position)
        token = match[0]
        position = match.end()
        if token == "/*":
            depth = 1
            while position < len(query) and depth:
                if query.startswith("/*", position):
                    depth += 1
                    position += 2
                elif query.startswith("*/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
            continue
        if token.isspace() or token.startswith(("--", "#")):
            continue
        kind = "word"
        if token[0] in "'\"`":
            kind = "string" if token[0] == "'" else "identifier"
            token = token[1:-1].replace(token[0] * 2, token[0])
        yield kind, _decode_sql(token).lower()


def guard_sql(query: str) -> None:
    tokens = list(_sql_tokens(query))
    # Reserved identity spellings remain forbidden even inside literals.
    normalized = re.sub(r"['`\"\s]", "", _decode_sql(query)).lower()
    forbidden = any(word in normalized for word in ("sql_cbio_user", "getsetting"))
    for index, (kind, value) in enumerate(tokens):
        # Quotes, whitespace and comments cannot hide qualified system tables.
        if value in ("system.settings", "system.settings_changes") or (
            value == "system"
            and index + 2 < len(tokens)
            and tokens[index + 1][1] == "."
            and tokens[index + 2][1] in ("settings", "settings_changes")
        ):
            forbidden = True
        if kind == "word" and value == "settings":
            # Only an actual SETTINGS keyword can start a settings clause;
            # strings such as 'mutation_settings' and 'sql_x' are harmless.
            if any(v.startswith("sql_") for _, v in tokens[index + 1 :]):
                forbidden = True
    if forbidden:
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
