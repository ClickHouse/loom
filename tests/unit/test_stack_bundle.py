from __future__ import annotations

import hashlib
import importlib.util
import tarfile
from pathlib import Path


def _module():
    path = Path(__file__).parents[2] / "scripts" / "release" / "build_stack_bundle.py"
    spec = importlib.util.spec_from_file_location("build_stack_bundle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_stack_bundle_is_complete_and_checksummed(tmp_path: Path) -> None:
    archive, checksum = _module().build("1.2.3", tmp_path)
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
