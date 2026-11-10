"""The code tests' shared timing helpers (`tests/unit/code_fixtures.py`)."""

from __future__ import annotations

import gc

from tests.unit.code_fixtures import untraced


def test_untraced_leaves_the_collector_as_it_found_it() -> None:
    with untraced():
        assert not gc.isenabled()
    assert gc.isenabled()
    gc.disable()
    try:
        with untraced():
            pass
        assert not gc.isenabled()
    finally:
        gc.enable()
