"""The unit conftest names a server module first imported while a test patched a
name it binds at import, so the test that leaks fails instead of a later one."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from server.code.store.index import CodeRepo
from tests.unit.conftest import _doubles_bound_at_import

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


def test_the_real_class_and_a_bound_module_are_not_named(probe) -> None:
    before = _import(probe, CodeRepo=CodeRepo, mod=sys)
    assert _doubles_bound_at_import(before) == []


def test_a_module_imported_before_the_test_is_not_checked(probe) -> None:
    before = _import(probe, CodeRepo=MagicMock()) | {_PROBE}
    assert _doubles_bound_at_import(before) == []
