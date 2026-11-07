"""Keep credentials (and, in refusal lines, emails) out of logs.

Driver, HTTP and server errors can echo connection URLs, ``password=...``
parameters, JSON bodies such as ``{"password": "..."}`` and Authorization
headers. Two layers use the same rules:

- ``redact`` makes one refusal line: single line, no credentials, no emails.
- ``install`` wraps the log record factory so that every record from the
  ClickHouse client stack (mcp-clickhouse, clickhouse_connect, urllib3) and
  from this package has credentials removed when the record is *created*,
  before any handler, filter or test capture sees it. Records at WARNING and
  above are also collapsed to one line, since those are the ones that carry
  exception text: the traceback and stack are folded into the message, so a
  standard Formatter writes exactly one physical line. A record that cannot be
  rendered becomes a fixed placeholder; scrubbing never raises into the caller.

Over-redaction is acceptable; leaking a credential is not.
"""

from __future__ import annotations

import logging
import re
import traceback

_KEY = (
    r"[A-Za-z0-9_.-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|credentials?)"
    r"|key"
)
# Optional quote around the key ("password", 'password', password), then = or :,
# then a quoted or bare value.
_SECRET_KV_RE = re.compile(
    rf"""(?ix)
    (?P<q>["']?)\b(?P<key>{_KEY})(?P=q)
    (?P<sep>\s*[=:]\s*)
    (?P<value>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,;&)}}\]]+)
    """
)
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@")
# An Authorization / Proxy-Authorization header (or dict/JSON entry): the WHOLE
# value goes, whatever the scheme (Digest carries comma-separated parameters).
# A quoted value ends at its closing quote; an unquoted one runs to end of line.
_AUTH_HEADER_RE = re.compile(
    r"""(?ix)
    (?P<q>["']?)(?P<key>\b(?:proxy-)?authorization)(?P=q)
    (?P<sep>\s*[=:]\s*)
    (?P<value>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\r\n]*)
    """
)
# Credentials outside a header: "Bearer x", "Basic x", or a Digest parameter list.
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+|\b(digest)\s+[^\r\n]*")
_EMAIL_RE = re.compile(r"[^\s@'\"`<>]+(?:@|%40)[^\s@'\"`<>]+", re.IGNORECASE)
# A ClickHouse server error code ("Code: 497." / DatabaseError.code).
_CH_CODE_RE = re.compile(r"\bCode:\s*(\d{1,4})\b")

MAX_LINE = 300
REDACTED = "<redacted>"


def redact_credentials(text) -> str:
    """Remove credential values; keeps everything else (including newlines)."""
    text = str(text)
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    # Whole header values first, then bare scheme credentials, then key=value.
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m['q']}{m['key']}{m['q']}{m['sep']}{REDACTED}", text)
    text = _AUTH_SCHEME_RE.sub(lambda m: f"{m[1] or m[2]} {REDACTED}", text)
    return _SECRET_KV_RE.sub(lambda m: f"{m['q']}{m['key']}{m['q']}{m['sep']}{REDACTED}", text)


def one_line(text) -> str:
    return " ".join(str(text).split())


def redact(text) -> str:
    """One line, no credentials and no email addresses, bounded length."""
    line = redact_credentials(one_line(text))
    return _EMAIL_RE.sub("<email>", line)[:MAX_LINE]


def describe_error(error: BaseException) -> str:
    """Exception class plus the ClickHouse error code, never free-form text."""
    name = type(error).__name__
    code = getattr(error, "code", None)
    if not isinstance(code, int):
        match = _CH_CODE_RE.search(str(error))
        code = int(match.group(1)) if match else None
    return f"{name} (ClickHouse code {code})" if code is not None else name


# Loggers whose records may carry driver/server error text.
SCRUBBED_PREFIXES = (
    "mcp-clickhouse",
    "mcp_clickhouse",
    "clickhouse_connect",
    "urllib3",
    "cbioportal_mcp",
)


def _scrubbed(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in SCRUBBED_PREFIXES)


UNLOGGABLE = "<unloggable message redacted>"


def scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    """Remove credentials in place; never raises.

    At WARNING and above the traceback and stack are folded into the message
    so that a standard Formatter emits exactly one physical line.
    """
    try:
        _scrub(record)
    except Exception:  # noqa: BLE001 - logging must never break the caller
        record.msg = UNLOGGABLE
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
    return record


def _scrub(record: logging.LogRecord) -> None:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - bad args: keep the format string, never the args
        message = str(record.msg)
    message = redact_credentials(message)
    exc_text = None
    if record.exc_info and record.exc_info[0] is not None:
        exc_text = redact_credentials("".join(traceback.format_exception(*record.exc_info)))
    elif record.exc_text:
        exc_text = redact_credentials(record.exc_text)
    stack = redact_credentials(record.stack_info) if record.stack_info else None
    if record.levelno >= logging.WARNING:
        parts = [one_line(message)]
        if exc_text:
            parts.append(one_line(exc_text))
        if stack:
            parts.append(one_line(stack))
        message, exc_text, stack = " | ".join(parts), None, None
    record.msg = message
    record.args = ()
    record.exc_info = None
    record.exc_text = exc_text
    record.stack_info = stack


_INSTALLED = False


def install() -> None:
    """Scrub records from the ClickHouse client stack and this package. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            if _scrubbed(record.name):
                scrub_record(record)
        except Exception:  # noqa: BLE001 - e.g. a name that is not a str
            record.msg, record.args = UNLOGGABLE, ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    logging.setLogRecordFactory(factory)
    _INSTALLED = True
