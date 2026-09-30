import pytest

from cbioportal_mcp import result_format


@pytest.fixture(autouse=True)
def _default_result_format(monkeypatch):
    """Tests assume the default result format unless they set one themselves."""
    for name in (
        result_format.RESULT_FORMAT_ENV,
        result_format.MAX_RESULT_ROWS_ENV,
        result_format.MAX_CELL_CHARS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
