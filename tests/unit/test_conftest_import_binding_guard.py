"""The unit conftest names a server module first imported while a test patched a
name it binds at import, so the test that leaks fails instead of a later one."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from server.code.store.index import CodeRepo
from tests.unit.conftest import _doubles_bound_at_import

pytest_plugins = ["pytester"]

_REPO = Path(__file__).resolve().parents[2]
_PROBE = "server._import_binding_probe"


@pytest.fixture
def probe() -> Iterator[types.ModuleType]:
    module = types.ModuleType(_PROBE)
    yield module
    sys.modules.pop(_PROBE, None)


def _import(module: types.ModuleType, **names: object) -> set[str]:
    before = set(sys.modules)
    vars(module).update(names)
    sys.modules[_PROBE] = module
    return before


def test_a_mock_bound_at_first_import_is_named(probe) -> None:
    before = _import(probe, CodeRepo=MagicMock(return_value=object()))
    assert _doubles_bound_at_import(before) == [f"{_PROBE}.CodeRepo"]


def test_a_lambda_the_test_defined_is_named(probe) -> None:
    before = _import(probe, CodeRepo=lambda: object())
    assert _doubles_bound_at_import(before) == [f"{_PROBE}.CodeRepo"]


def test_the_real_class_is_not_named(probe) -> None:
    before = _import(probe, CodeRepo=CodeRepo)
    assert _doubles_bound_at_import(before) == []


def test_a_module_imported_before_the_test_is_not_checked(probe) -> None:
    before = _import(probe, CodeRepo=MagicMock()) | {_PROBE}
    assert _doubles_bound_at_import(before) == []


# The fixture itself, in a fresh process: scip_delta binds CodeRepo when first imported.
_INNER_CONFTEST = """
from tests.unit.conftest import _no_test_double_bound_at_import  # noqa: F401
"""
_INNER_TESTS = """
from unittest.mock import patch


def test_first_import_under_a_patch():
    with patch("server.code.store.index.CodeRepo", lambda: None):
        import server.code.app.index.scip_delta  # noqa: F401


def test_a_patch_on_the_module_itself(monkeypatch):
    # Runs after the test above: the guard dropped the module, so this import binds
    # the real class, and the patch below is undone before the guard looks.
    import server.code.app.index.scip_delta as sd
    import server.code.store.index as index

    assert sd.CodeRepo is index.CodeRepo
    monkeypatch.setattr(sd, "CodeRepo", lambda: None)
"""


def test_the_guard_fails_the_leaking_test_and_not_a_patch_on_the_module(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(_REPO))
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_INNER_TESTS)
    result = pytester.runpytest_subprocess("-p", "no:randomly", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_first_import_under_a_patch*"])
    result.stdout.fnmatch_lines(
        ["*kept the test double: server.code.app.index.scip_delta.CodeRepo *"]
    )
