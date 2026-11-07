"""cbioportal_mcp.redaction: the global log-record scrubber.

Every assertion here is on FORMATTED output from a real StreamHandler with a
standard Formatter, as an operator's log pipeline would see it, not on record
fields.
"""

import io
import logging

import pytest

from cbioportal_mcp import redaction

SECRET = "review-secret-0077"
PLACEHOLDER = redaction.UNLOGGABLE


@pytest.fixture
def emit():
    """Log through a real handler on a scrubbed logger; return formatted text."""
    redaction.install()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger = logging.getLogger("mcp-clickhouse")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)

    def log(level, msg, *args, **kwargs):
        logger.log(level, msg, *args, **kwargs)
        handler.flush()
        return stream.getvalue()

    yield log
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


def physical_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line]


# --- 1. Authorization headers: the whole value, any scheme -------------------

AUTH_VALUES = [
    # RFC 7616 Digest, as a client sends it.
    'Authorization: Digest username="mcp_user", realm="ClickHouse", '
    'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", uri="/?query=SELECT%201", '
    f'qop=auth, nc=00000001, cnonce="0a4f113b", response="{SECRET}", '
    'opaque="5ccc069c403ebaf9f0171e9517f40e41"',
    f'Proxy-Authorization: Digest username="mcp_user", response="{SECRET}"',
    f"Authorization: Negotiate {SECRET}",  # unknown to the scheme list
    f"Authorization: X-Custom-Scheme token68 {SECRET} trailing",
    f"Proxy-Authorization: {SECRET}",  # no scheme at all
    f"authorization={SECRET}",
    f'{{"Authorization": "Digest username=\\"u\\", response=\\"{SECRET}\\""}}',
    f"{{'proxy-authorization': 'Digest response=\"{SECRET}\"'}}",
    f'got a bare Digest username="u", response="{SECRET}" from the server',
    f"Authorization: Basic {SECRET}",
    f"Authorization: Bearer {SECRET}",
]


@pytest.mark.parametrize("value", AUTH_VALUES)
def test_authorization_value_fully_redacted(emit, value):
    out = emit(logging.ERROR, "request failed: %s", value)
    assert SECRET not in out
    assert "username=" not in out and "nonce=" not in out
    assert len(physical_lines(out)) == 1


def test_digest_header_through_redact_and_credentials():
    digest = AUTH_VALUES[0]
    for fn in (redaction.redact, redaction.redact_credentials):
        out = fn(digest)
        assert SECRET not in out and "realm=" not in out and "nonce=" not in out
        assert out.startswith("Authorization: <redacted>")


def test_authorization_redaction_keeps_unrelated_text():
    out = redaction.redact_credentials(f'{{"authorization": "Bearer {SECRET}", "status": 401}}')
    assert out == '{"authorization": <redacted>, "status": 401}'


# --- 2. Scrubbing never raises into application code ------------------------


class BadMessage:
    def __str__(self):
        raise RuntimeError(f"cannot render {SECRET}")


class BadArg:
    def __str__(self):
        raise RuntimeError(f"cannot render {SECRET}")


@pytest.mark.parametrize("raise_exceptions", [True, False])
def test_unrenderable_message_becomes_placeholder(emit, monkeypatch, raise_exceptions):
    monkeypatch.setattr(logging, "raiseExceptions", raise_exceptions)
    out = emit(logging.ERROR, BadMessage())  # must not raise
    assert PLACEHOLDER in out
    assert SECRET not in out
    assert len(physical_lines(out)) == 1


@pytest.mark.parametrize("raise_exceptions", [True, False])
def test_unrenderable_argument_becomes_placeholder(emit, monkeypatch, raise_exceptions):
    monkeypatch.setattr(logging, "raiseExceptions", raise_exceptions)
    out = emit(logging.WARNING, "value %s", BadArg())  # must not raise
    # Falls back to the bare format string: never the argument.
    assert "value %s" in out and SECRET not in out


@pytest.mark.parametrize("raise_exceptions", [True, False])
def test_bad_format_args_keep_format_string_only(emit, monkeypatch, raise_exceptions):
    monkeypatch.setattr(logging, "raiseExceptions", raise_exceptions)
    out = emit(logging.ERROR, "count %d password=%s", "not-a-number", SECRET)
    assert "count %d password=<redacted>" in out
    assert SECRET not in out


@pytest.mark.parametrize("raise_exceptions", [True, False])
def test_unrenderable_exception_becomes_placeholder(emit, monkeypatch, raise_exceptions):
    class BadError(Exception):
        def __str__(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(logging, "raiseExceptions", raise_exceptions)
    try:
        raise BadError()
    except BadError:
        out = emit(logging.ERROR, "failed", exc_info=True)  # must not raise
    assert len(physical_lines(out)) == 1
    assert SECRET not in out


def test_scrub_record_never_raises_even_on_broken_record():
    record = logging.LogRecord("mcp-clickhouse", logging.ERROR, __file__, 1, BadMessage(), (), None)
    record.levelno = "not-an-int"  # comparison raises inside scrubbing
    assert redaction.scrub_record(record).msg == PLACEHOLDER


# --- 3. Tracebacks and stacks fold into one physical line --------------------


def test_logger_exception_is_one_formatted_line(emit):
    try:
        try:
            raise ConnectionError(f"password={SECRET}\nretry later")
        except ConnectionError as inner:
            raise RuntimeError(f"wrapped https://mcp_user:{SECRET}@ch.example.org") from inner
    except RuntimeError:
        logger = logging.getLogger("mcp-clickhouse")
        logger.exception("query failed")
        out = emit(logging.DEBUG, "marker")
    lines = physical_lines(out)
    error_lines = [line for line in lines if " - ERROR - " in line]
    assert len(error_lines) == 1
    assert len(lines) == 2  # the ERROR line and the DEBUG marker, nothing else
    line = error_lines[0]
    assert "query failed | Traceback" in line
    assert "ConnectionError" in line and "RuntimeError" in line
    assert SECRET not in out


def test_stack_info_is_folded_into_one_line(emit):
    out = emit(logging.WARNING, "slow query password=%s", SECRET, stack_info=True)
    assert len(physical_lines(out)) == 1
    assert "Stack (most recent call last)" in out and SECRET not in out


def test_below_warning_keeps_multiline_messages(emit):
    out = emit(logging.INFO, "Executing query: SELECT 1\nFROM t WHERE password = 'x'")
    assert "SELECT 1\nFROM t" in out
    assert "password = <redacted>" in out


def test_unscrubbed_loggers_are_untouched():
    redaction.install()
    record = logging.getLogRecordFactory()(
        "some.other.library", logging.ERROR, __file__, 1, "password=%s", (SECRET,), None
    )
    assert record.getMessage() == f"password={SECRET}"
