from __future__ import annotations

import pytest

from server.code.app.read import _parse_range
from server.core.errors import ValidationError


@pytest.mark.parametrize("value", ["../file.py:2", "/file.py:1", "file.py:0", "file.py:9-2"])
def test_read_refuses_invalid_ranges(value: str) -> None:
    with pytest.raises(ValidationError):
        _parse_range(value)
