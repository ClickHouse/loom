"""The SDK wheel smoke test fails loudly: a wheel missing py.typed or the
license, or a `loom` imported from the source tree, is exit 1, and the
mocked-transport exercise passes against the in-repo SDK."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from scripts.gates import sdk_wheel_smoke as smoke


def _wheel(tmp_path: Path, *names: str) -> str:
    path = tmp_path / "clickhouse_loom-0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        for name in names:
            z.writestr(name, "")
    return str(path)


def test_complete_wheel_passes(tmp_path: Path) -> None:
    smoke.check_wheel(
        _wheel(tmp_path, "loom/py.typed", "clickhouse_loom-0.dist-info/licenses/LICENSE")
    )


@pytest.mark.parametrize(
    "names,missing",
    [
        (("clickhouse_loom-0.dist-info/licenses/LICENSE",), "py.typed"),
        (("loom/py.typed",), "LICENSE"),
    ],
)
def test_incomplete_wheel_fails(tmp_path: Path, names: tuple[str, ...], missing: str) -> None:
    with pytest.raises(smoke.SmokeFailure, match=missing):
        smoke.check_wheel(_wheel(tmp_path, *names))


def test_source_tree_import_is_refused(capsys) -> None:
    # tests import `loom` from sdk/python, not from site-packages
    assert "site-packages" not in smoke.loom.__file__
    assert smoke.main([]) == 1
    assert "not the installed wheel" in capsys.readouterr().err


def test_exercise_passes_against_the_sdk() -> None:
    asyncio.run(smoke.exercise())


def _fail_import(monkeypatch: pytest.MonkeyPatch, error: ImportError) -> None:
    real = smoke.importlib.import_module

    def fake(name: str) -> object:
        if name == "loom.integrations.langchain":
            raise error
        return real(name)

    monkeypatch.setattr(smoke.importlib, "import_module", fake)


def test_an_adapter_whose_error_names_its_extra_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_import(monkeypatch, ImportError("needs it: pip install 'clickhouse-loom[langchain]'"))
    smoke.import_submodules()


def test_any_other_import_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_import(monkeypatch, ImportError("No module named 'httpx'"))
    with pytest.raises(smoke.SmokeFailure, match="httpx"):
        smoke.import_submodules()
