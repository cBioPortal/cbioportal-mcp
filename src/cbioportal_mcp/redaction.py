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
# A closing quote only ends a value when a delimiter (or the end) follows it;
# otherwise the quoting is malformed and the value fails closed.
_AFTER_QUOTE = r"[\s,;&)}\]]|\Z"
_AFTER_QUOTE_CHARS = " \t\r\n,;&)}]"
# Optional quote around the key ("password", 'password', password), then = or :,
# then a quoted or bare value. A quote that never closes fails closed: the
# value runs to the end of the WHOLE text, across every following line.
_SECRET_KV_RE = re.compile(
    rf"""(?ix)
    (?P<q>["']?)\b(?P<key>{_KEY})(?P=q)
    (?P<sep>\s*[=:]\s*)
    (?P<value>"(?:[^"\\]|\\.)*"(?={_AFTER_QUOTE})|'(?:[^'\\]|\\.)*'(?={_AFTER_QUOTE})
              |["'][\s\S]*|[^\s,;&)}}\]]+)
    """
)
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@")
# An Authorization / Proxy-Authorization header (or dict/JSON entry) and a bare
# Digest parameter list: the WHOLE value goes, whatever the scheme. The end of
# the value is found by _auth_value_end (fail closed on unterminated quotes).
_AUTH_HEADER_RE = re.compile(
    r"""(?ix)
    (?P<q>["']?)(?P<key>\b(?:proxy-)?authorization)(?P=q)
    (?P<sep>\s*[=:]\s*)
    """
)
_DIGEST_RE = re.compile(r"(?i)(?P<key>\bdigest)(?P<sep>\s+)")
# Bare token credentials: "Bearer x", "Basic x".
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_RE = re.compile(r"[^\s@'\"`<>]+(?:@|%40)[^\s@'\"`<>]+", re.IGNORECASE)
# A ClickHouse server error code ("Code: 497." / DatabaseError.code).
_CH_CODE_RE = re.compile(r"\bCode:\s*(\d{1,4})\b")

MAX_LINE = 300
REDACTED = "<redacted>"


def _auth_value_end(text: str, start: int) -> int:
    """End of an Authorization value starting at ``start``. Fail closed.

    - Quoted value: ends at the matching closing quote (backslash escapes
      honoured) when a delimiter or the end follows it; if it never closes, or
      closes mid-token (``"Digest username="u"...``), at the end of the text.
    - Unquoted value: runs to the end of the line, and also absorbs folded
      continuation lines (next line starts with space/tab) and any quote opened
      inside it; a quote that never closes takes the rest of the text.
    """
    n = len(text)
    if start < n and text[start] in "\"'":
        quote, j = text[start], start + 1
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text[j] == quote:
                end = j + 1
                if end == n or text[end] in _AFTER_QUOTE_CHARS:
                    return end
                return n  # malformed quoting: fail closed
            else:
                j += 1
        return n
    j, quote = start, None
    while j < n:
        c = text[j]
        if quote is not None:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "\r\n":
            k = j + 2 if text.startswith("\r\n", j) else j + 1
            if k < n and text[k] in " \t":
                j = k
                continue
            return j
        j += 1
    return n


def _redact_auth_values(text: str, pattern: re.Pattern) -> str:
    out, pos = [], 0
    while True:
        m = pattern.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        q = m.groupdict().get("q") or ""
        out.append(text[pos : m.start()])
        out.append(f"{q}{m['key']}{q}{m['sep']}{REDACTED}")
        pos = _auth_value_end(text, m.end())


def redact_credentials(text) -> str:
    """Remove credential values; keeps everything else (including newlines)."""
    text = str(text)
    text = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    # Whole header values first, then Digest lists, bare tokens, then key=value.
    text = _redact_auth_values(text, _AUTH_HEADER_RE)
    text = _redact_auth_values(text, _DIGEST_RE)
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
