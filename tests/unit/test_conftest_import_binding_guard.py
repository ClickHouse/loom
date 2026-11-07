"""The unit conftest finds a server module first imported while a test patched a name
it binds at import, and drops it after that test, so no later test sees the double."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.code.store.index import CodeRepo
from tests.unit.conftest import _double_bound_at_import

pytest_plugins = ["pytester"]

_REPO = Path(__file__).resolve().parents[2]
_PROBE = "server._import_binding_probe"


@pytest.fixture
def probe() -> Iterator[types.ModuleType]:
    module = types.ModuleType(_PROBE)
    yield module
    sys.modules.pop(_PROBE, None)


def _fake_first_import(module: types.ModuleType, **names: object) -> set[str]:
    """Put `module` in `sys.modules` holding `names`, as a first import would."""
    before = set(sys.modules)
    vars(module).update(names)
    sys.modules[_PROBE] = module
    return before


def test_a_mock_bound_at_first_import_is_found(probe) -> None:
    before = _fake_first_import(probe, CodeRepo=MagicMock(return_value=object()))
    assert _double_bound_at_import(before)


def test_a_lambda_the_test_defined_is_found(probe) -> None:
    before = _fake_first_import(probe, CodeRepo=lambda: object())
    assert _double_bound_at_import(before)


def test_the_real_class_is_not_found(probe) -> None:
    before = _fake_first_import(probe, CodeRepo=CodeRepo)
    assert not _double_bound_at_import(before)


def test_a_blocked_import_is_skipped() -> None:
    before = set(sys.modules)
    sys.modules[_PROBE] = None  # type: ignore[assignment]
    try:
        assert not _double_bound_at_import(before)
    finally:
        sys.modules.pop(_PROBE, None)


def test_a_module_imported_before_the_test_is_not_checked(probe) -> None:
    before = _fake_first_import(probe, CodeRepo=MagicMock()) | {_PROBE}
    assert not _double_bound_at_import(before)


# The fixture itself, in a fresh process: scip_delta binds CodeRepo when first imported,
# and scip_full holds scip_delta's apply_delta, which reads CodeRepo from scip_delta's
# globals, so scip_full must go too; both were first imported in the leaking test, and
# the fixture drops every such module.
_INNER_CONFTEST = """
import server.code.app.index  # noqa: F401 (the package is loaded before the tests)
from tests.unit.conftest import _test_double_bound_at_import_dropped  # noqa: F401
"""
_INNER_TESTS = """
import sys
from unittest.mock import patch


def test_first_import_under_a_patch():
    with patch("server.code.store.index.CodeRepo", lambda: None):
        import server.code.app.index.scip_full  # noqa: F401


def test_a_patch_on_the_module_itself(monkeypatch):
    # Runs after the test above: the fixture dropped both modules and the package's
    # attribute for each, so `from package import` runs them again and they bind the
    # real class. The patch below is undone before the fixture looks.
    assert "server.code.app.index.scip_delta" not in sys.modules
    assert "server.code.app.index.scip_full" not in sys.modules
    from server.code.app.index import scip_delta as sd
    from server.code.app.index import scip_full as full
    import server.code.store.index as index

    assert sd.CodeRepo is index.CodeRepo
    assert full.apply_delta.__globals__["CodeRepo"] is index.CodeRepo
    monkeypatch.setattr(sd, "CodeRepo", lambda: None)


def test_the_patched_module_is_kept():
    # The monkeypatch above was undone before the fixture looked, so nothing was dropped.
    assert "server.code.app.index.scip_delta" in sys.modules
"""


def test_a_module_imported_under_a_patch_is_imported_again_with_the_real_names(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(_REPO))
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=3)
