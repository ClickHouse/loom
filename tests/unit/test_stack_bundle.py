from __future__ import annotations

import hashlib
import importlib.util
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[2] / "scripts" / "release" / "build_stack_bundle.py"
    spec = importlib.util.spec_from_file_location("build_stack_bundle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_stack_bundle_is_complete_and_checksummed(tmp_path: Path) -> None:
    archive, checksum = _module().build(
        "1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11"
    )
    expected = checksum.read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
    with tarfile.open(archive) as bundle:
        names = set(bundle.getnames())
        assert {
            "loom-stack/VERSION",
            "loom-stack/LICENSE",
            "loom-stack/NOTICE",
            "loom-stack/THIRD_PARTY_NOTICES",
            "loom-stack/compose.yaml",
            "loom-stack/env.example",
            "loom-stack/lemmagen3/en.bin",
            "loom-stack/lemmagen3/LICENSE",
            "loom-stack/lemmagen3/README.md",
            "loom-stack/clickhouse-config/named_collections.xml",
        } <= names
        assert bundle.extractfile("loom-stack/VERSION").read() == b"1.2.3\n"
        compose = bundle.extractfile("loom-stack/compose.yaml").read().decode()
        assert "${LOOM_CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:26.9.18.11}" in compose
        assert "clickhouse/clickhouse-server:latest" not in compose
        notices = bundle.extractfile("loom-stack/THIRD_PARTY_NOTICES").read().decode()
        assert "CC BY-SA 4.0" in notices


def test_standalone_compose_uses_release_image_and_embeds_no_secret() -> None:
    root = Path(__file__).parents[2]
    compose = (root / "deploy" / "standalone" / "compose.yaml").read_text(encoding="utf-8")
    env = (root / "deploy" / "standalone" / "env.example").read_text(encoding="utf-8")
    assert "${LOOM_IMAGE_REPOSITORY:-ghcr.io/clickhouse/loom}:${LOOM_VERSION:-latest}" in compose
    assert "build:" not in compose
    assert "127.0.0.1" in env
    assert "LOOM_IDENTITY_PROVIDER=single-user" in env
    assert "__LOOM_JWT_SECRET__" in env
    # ClickHouse shares the host, so Loom does not size itself to all of it.
    assert 'LOOM_WEB_WORKERS: "${LOOM_WEB_WORKERS:-2}"' in compose


@pytest.mark.parametrize(
    "image", ["", "clickhouse/clickhouse-server:latest", "clickhouse/clickhouse-server:26.9"]
)
def test_a_bundle_names_one_exact_clickhouse_release(tmp_path: Path, image: str) -> None:
    with pytest.raises(ValueError, match="not one ClickHouse release"):
        _module().build("1.2.3", tmp_path, image)


def test_a_rerun_reads_the_release_a_published_bundle_names(tmp_path: Path) -> None:
    module = _module()
    archive, _ = module.build("1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11")
    assert module.pinned_image(archive) == "clickhouse/clickhouse-server:26.9.18.11"


def _edited_bundle(tmp_path: Path, edit: Callable[[str], str]) -> Path:
    """A built bundle whose compose.yaml `edit` rewrote."""
    archive, _ = _module().build("1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11")
    stage = tmp_path / "stage"
    with tarfile.open(archive) as bundle:
        bundle.extractall(stage, filter="data")
    compose = stage / "loom-stack" / "compose.yaml"
    compose.write_text(edit(compose.read_text()))
    edited = tmp_path / "edited.tar.gz"
    with tarfile.open(edited, "w:gz") as bundle:
        bundle.add(stage / "loom-stack", arcname="loom-stack")
    return edited


def test_the_pin_is_the_compose_default_not_a_release_a_comment_names(tmp_path: Path) -> None:
    edited = _edited_bundle(
        tmp_path, lambda c: "# e.g. clickhouse/clickhouse-server:26.8.1.1\n" + c
    )
    assert _module().pinned_image(edited) == "clickhouse/clickhouse-server:26.9.18.11"


def test_a_bundle_from_before_the_override_names_its_release_bare(tmp_path: Path) -> None:
    def bare(compose: str) -> str:
        return compose.replace(
            "${LOOM_CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:26.9.18.11}",
            "clickhouse/clickhouse-server:26.5.1.882",
        )

    edited = _edited_bundle(tmp_path, bare)
    assert _module().pinned_image(edited) == "clickhouse/clickhouse-server:26.5.1.882"


def test_the_pin_pattern_reads_what_build_writes() -> None:
    module = _module()
    written = module._image_default("clickhouse/clickhouse-server:26.9.18.11")
    assert module._PINNED.fullmatch(written).group(1) == "clickhouse/clickhouse-server:26.9.18.11"
