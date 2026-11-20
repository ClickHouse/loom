"""Extension archive checks reject broken distributions before class loading."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.gates import langflow_extension_smoke as smoke

_COMPONENT = "lfx_loom/components/loom/loom_component.py"
_PACKAGE_FILES = {
    "lfx_loom/__init__.py": b'"""Extension package."""\n',
    "lfx_loom/extension.json": b'{"id": "lfx-loom"}\n',
    _COMPONENT: b"class LoomComponent: pass\n",
}
_LICENSE_MEMBER = "lfx_loom-0.1.0.dist-info/licenses/LICENSE"
_LICENSE_BYTES = b"license text\n"
_SDIST_FILES = {
    "loom_component.py": _PACKAGE_FILES[_COMPONENT],
    "extension.json": _PACKAGE_FILES["lfx_loom/extension.json"],
    "pyproject.toml": b'[project]\nname = "lfx-loom"\n',
    "README.md": b"# Extension\n",
    "LICENSE": _LICENSE_BYTES,
    "src/lfx_loom/__init__.py": _PACKAGE_FILES["lfx_loom/__init__.py"],
}


@pytest.fixture
def wheel_members() -> list[tuple[str, bytes]]:
    return [*_PACKAGE_FILES.items(), (_LICENSE_MEMBER, _LICENSE_BYTES)]


def _wheel(tmp_path: Path, members: list[tuple[str, bytes]]) -> Path:
    path = tmp_path / "lfx_loom-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return path


def test_complete_wheel_passes(tmp_path: Path, wheel_members: list[tuple[str, bytes]]) -> None:
    smoke.check_wheel(_wheel(tmp_path, wheel_members), _PACKAGE_FILES, _LICENSE_BYTES)


@pytest.mark.parametrize(
    "name,content",
    [
        (_COMPONENT, b"corrupted component\n"),
        ("lfx_loom/unexpected.py", b"unexpected module\n"),
        (_LICENSE_MEMBER, None),
    ],
    ids=["corrupted-component", "unexpected-member", "missing-license"],
)
def test_wheel_rejects_invalid_contents(
    tmp_path: Path, wheel_members: list[tuple[str, bytes]], name: str, content: bytes | None
) -> None:
    members = dict(wheel_members)
    if content is None:
        del members[name]
    else:
        members[name] = content
    wheel = _wheel(tmp_path, list(members.items()))
    with pytest.raises(AssertionError):
        smoke.check_wheel(wheel, _PACKAGE_FILES, _LICENSE_BYTES)


def test_wheel_rejects_duplicate_members(
    tmp_path: Path, wheel_members: list[tuple[str, bytes]]
) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        wheel = _wheel(tmp_path, [*wheel_members, (_COMPONENT, _PACKAGE_FILES[_COMPONENT])])
    with pytest.raises(AssertionError, match="Duplicate wheel entries"):
        smoke.check_wheel(wheel, _PACKAGE_FILES, _LICENSE_BYTES)


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    for relative, content in _SDIST_FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _sdist(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "lfx_loom-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for relative, content in members.items():
            member = tarfile.TarInfo(f"lfx_loom-0.1.0/{relative}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return path


def test_complete_sdist_passes(tmp_path: Path, source: Path) -> None:
    smoke.check_sdist(_sdist(tmp_path, _SDIST_FILES), source)


@pytest.mark.parametrize("content", [None, b"changed build metadata\n"], ids=["missing", "changed"])
def test_sdist_rejects_invalid_build_input(
    tmp_path: Path, source: Path, content: bytes | None
) -> None:
    members = dict(_SDIST_FILES)
    if content is None:
        del members["pyproject.toml"]
    else:
        members["pyproject.toml"] = content
    sdist = _sdist(tmp_path, members)
    with pytest.raises(AssertionError, match="pyproject.toml"):
        smoke.check_sdist(sdist, source)
