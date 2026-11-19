"""Check the built Langflow extension against its installed distribution."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import sys
import tarfile
import zipfile
from pathlib import Path


def only(directory: Path, pattern: str) -> Path:
    paths = list(directory.glob(pattern))
    assert len(paths) == 1, (directory, pattern, paths)
    return paths[0]


def check_wheel(wheel: Path, expected: dict[str, bytes], license_bytes: bytes) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "Duplicate wheel entries"
        assert {name for name in names if name.startswith("lfx_loom/")} == set(expected)
        for name, content in expected.items():
            assert archive.read(name) == content, name
        licenses = [name for name in names if name.endswith(".dist-info/licenses/LICENSE")]
        assert len(licenses) == 1
        assert archive.read(licenses[0]) == license_bytes


def check_sdist(sdist: Path, source: Path) -> None:
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        assert len({member.name for member in members}) == len(members), "Duplicate sdist entries"
        for relative in (
            "loom_component.py",
            "extension.json",
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "src/lfx_loom/__init__.py",
        ):
            matches = [member for member in members if member.name.split("/", 1)[-1] == relative]
            assert len(matches) == 1 and matches[0].isfile(), relative
            stream = archive.extractfile(matches[0])
            assert stream is not None
            with stream:
                assert stream.read() == (source / relative).read_bytes(), relative


def check_installed(expected: dict[str, bytes], prefix: Path) -> metadata.Distribution:
    installed = metadata.distribution("lfx-loom")
    assert installed.version == "0.1.0"
    entries = list(installed.entry_points.select(group="langflow.extensions"))
    assert [(entry.name, entry.value) for entry in entries] == [("lfx-loom", "lfx_loom")]
    for name, content in expected.items():
        path = Path(installed.locate_file(name)).resolve()
        assert path.is_relative_to(prefix), path
        assert path.read_bytes() == content, name
    return installed


def check_sdk(sdk_wheel: Path, prefix: Path) -> str:
    import loom

    sdk = metadata.distribution("clickhouse-loom")
    assert Path(loom.__file__).resolve().is_relative_to(prefix)
    with zipfile.ZipFile(sdk_wheel) as archive:
        for name in archive.namelist():
            if name.startswith("loom/") and not name.endswith("/"):
                path = Path(sdk.locate_file(name)).resolve()
                assert path.is_relative_to(prefix), path
                assert path.read_bytes() == archive.read(name), name
    return sdk.version


def check_discovery(installed: metadata.Distribution, component_bytes: bytes) -> str:
    from lfx.custom.custom_component.component import Component
    from lfx.extension.discovery import discover_installed_extensions
    from lfx.extension.loader import load_installed_extensions

    discoveries, errors = discover_installed_extensions()
    assert not errors, errors
    matching = [record for record in discoveries if record.extension_id == "lfx-loom"]
    assert len(matching) == 1, matching
    assert matching[0].source == "lfx-loom" and matching[0].bundle_names == ("loom",)
    results = load_installed_extensions(distributions=[installed])
    assert len(results) == 1 and results[0].ok, results
    assert not results[0].warnings, results[0].warnings
    components = results[0].components
    assert len(components) == 1, components
    component = components[0]
    assert component.namespaced_id == "ext:loom:LoomComponent@official"
    assert component.distribution == "lfx-loom"
    assert issubclass(component.klass, Component)
    assert callable(getattr(component.klass, "produce_result", None))
    assert any(output.method == "produce_result" for output in component.klass.outputs)
    assert component.file_path.read_bytes() == component_bytes
    return component.namespaced_id


def main() -> None:
    root, extension_dist, rebuilt_dist, sdk_dist = (Path(value).resolve() for value in sys.argv[1:])
    assert not Path.cwd().is_relative_to(root), "Run outside the source checkout"
    prefix = Path(sys.prefix).resolve()
    assert not prefix.is_relative_to(root), "Use a separate virtual environment"
    assert metadata.version("lfx") == "1.13.3"
    source = root / "integrations/langflow"
    component_bytes = (source / "loom_component.py").read_bytes()
    expected = {
        "lfx_loom/__init__.py": (source / "src/lfx_loom/__init__.py").read_bytes(),
        "lfx_loom/extension.json": (source / "extension.json").read_bytes(),
        "lfx_loom/components/loom/loom_component.py": component_bytes,
    }
    original_wheel = only(extension_dist, "*.whl")
    wheel = only(rebuilt_dist, "*.whl")
    license_bytes = (root / "LICENSE").read_bytes()
    assert (source / "LICENSE").read_bytes() == license_bytes
    for built_wheel in (original_wheel, wheel):
        check_wheel(built_wheel, expected, license_bytes)
    check_sdist(only(extension_dist, "*.tar.gz"), source)
    installed = check_installed(expected, prefix)
    sdk_wheel = only(sdk_dist, "*.whl")
    sdk_version = check_sdk(sdk_wheel, prefix)
    component_id = check_discovery(installed, component_bytes)
    print(
        json.dumps(
            {
                "lfx": metadata.version("lfx"),
                "clickhouse-loom": sdk_version,
                "extension": installed.version,
                "component": component_id,
                "component_sha256": hashlib.sha256(component_bytes).hexdigest(),
                "extension_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "original_wheel_sha256": hashlib.sha256(original_wheel.read_bytes()).hexdigest(),
                "sdk_wheel_sha256": hashlib.sha256(sdk_wheel.read_bytes()).hexdigest(),
                "scope": "installed discovery and class loading; no tool calls or live flow",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
