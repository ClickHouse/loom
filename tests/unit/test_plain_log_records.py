"""Plain-text logs keep one physical line per record."""

import logging

from server.core.logging_config import _TextFormatter


def test_text_formatter_escapes_line_breaks_in_a_record() -> None:
    record = logging.LogRecord(
        "loom",
        logging.WARNING,
        __file__,
        1,
        "Message: %s",
        ("first line\r\nsecond line",),
        None,
    )

    assert _TextFormatter("%(message)s").format(record) == r"Message: first line\r\nsecond line"
