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
  exception text.

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
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic|digest)\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_RE = re.compile(r"[^\s@'\"`<>]+(?:@|%40)[^\s@'\"`<>]+", re.IGNORECASE)
# A ClickHouse server error code ("Code: 497." / DatabaseError.code).
_CH_CODE_RE = re.compile(r"\bCode:\s*(\d{1,4})\b")

MAX_LINE = 300
REDACTED = "<redacted>"


def redact_credentials(text) -> str:
    """Remove credential values; keeps everything else (including newlines)."""
    text = str(text)
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    # Scheme first, so "Authorization: Bearer x" loses the token, not just "Bearer".
    text = _AUTH_SCHEME_RE.sub(rf"\1 {REDACTED}", text)
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


def scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a bad format string must not leak args
        message = str(record.msg)
    message = redact_credentials(message)
    exc_text = None
    if record.exc_info and record.exc_info[0] is not None:
        exc_text = redact_credentials("".join(traceback.format_exception(*record.exc_info)))
    elif record.exc_text:
        exc_text = redact_credentials(record.exc_text)
    if record.levelno >= logging.WARNING:
        message = one_line(message)
        if exc_text is not None:
            exc_text = one_line(exc_text)
    record.msg = message
    record.args = ()
    record.exc_info = None
    record.exc_text = exc_text
    return record


_INSTALLED = False


def install() -> None:
    """Scrub records from the ClickHouse client stack and this package. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if _scrubbed(record.name):
            scrub_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _INSTALLED = True
