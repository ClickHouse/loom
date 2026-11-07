from __future__ import annotations

import hashlib
import importlib.util
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit import git_repo


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
            "loom-stack/deploy/lemmagen3/en.bin",
            "loom-stack/deploy/lemmagen3/LICENSE",
            "loom-stack/deploy/lemmagen3/README.md",
            "loom-stack/deploy/clickhouse-config/named_collections.xml",
        } <= names
        assert bundle.extractfile("loom-stack/VERSION").read() == b"1.2.3\n"
        compose = bundle.extractfile("loom-stack/compose.yaml").read().decode()
        assert "${LOOM_CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:26.9.18.11}" in compose
        assert "clickhouse/clickhouse-server:latest" not in compose
        notices = bundle.extractfile("loom-stack/THIRD_PARTY_NOTICES").read().decode()
        assert "CC BY-SA 4.0" in notices


def test_standalone_compose_uses_release_image_and_embeds_no_secret() -> None:
    root = Path(__file__).parents[2]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    env = (root / "deploy" / "standalone" / "env.example").read_text(encoding="utf-8")
    assert "${LOOM_IMAGE_REPOSITORY:-ghcr.io/clickhouse/loom}:${LOOM_VERSION:-latest}" in compose
    assert "build:" not in compose
    assert "127.0.0.1" in env
    assert "LOOM_IDENTITY_PROVIDER=single-user" in env
    assert "__LOOM_JWT_SECRET__" in env
    assert "LOOM_DOCUMENT_EXTRACTOR_TOKEN=__LOOM_DOCUMENT_EXTRACTOR_TOKEN__" in env
    image = "${LOOM_EXTRACTOR_IMAGE_REPOSITORY:-ghcr.io/clickhouse/loom-extractor}:${LOOM_VERSION:-latest}"
    assert image in compose
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


@pytest.mark.parametrize(
    ("image", "older"),
    [
        ("clickhouse/clickhouse-server:26.9.18.11", False),  # a re-pin of the same release
        ("clickhouse/clickhouse-server:26.10.1.5", False),  # 10 > 9, not "1" < "9"
        ("clickhouse/clickhouse-server:26.9.17.30", True),
        ("clickhouse/clickhouse-server:25.12.4.2", True),
    ],
)
def test_a_new_bundle_never_names_an_older_clickhouse_than_the_last(
    tmp_path: Path, image: str, older: bool
) -> None:
    """An upgrading install would start an older server on data a newer one wrote."""
    module = _module()
    published, _ = module.build("1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11")
    if older:
        with pytest.raises(ValueError, match="older than clickhouse/clickhouse-server:26.9.18.11"):
            module.refuse_older(image, [published])
    else:
        module.refuse_older(image, [published])


def test_a_new_bundle_is_checked_against_the_newest_of_several(tmp_path: Path) -> None:
    """A prerelease can name a newer ClickHouse than the latest final release."""
    module = _module()
    final, _ = module.build("1.2.3", tmp_path / "a", "clickhouse/clickhouse-server:26.8.2.1")
    rc, _ = module.build("1.3.0-rc.1", tmp_path / "b", "clickhouse/clickhouse-server:26.9.18.11")
    with pytest.raises(ValueError, match="older than clickhouse/clickhouse-server:26.9.18.11"):
        module.refuse_older("clickhouse/clickhouse-server:26.9.1.5", [final, rc])


def _release_workflow() -> dict:
    import yaml

    return yaml.safe_load(
        (Path(__file__).parents[2] / ".github" / "workflows" / "release.yml").read_text()
    )


def test_the_release_warns_when_a_published_bundle_keeps_its_own_release() -> None:
    (resolve,) = [
        s for s in _release_workflow()["jobs"]["clickhouse"]["steps"] if s.get("id") == "resolve"
    ]
    assert "scripts/release/release_clickhouse.sh" in resolve["run"]
    script = Path(__file__).parents[2] / "scripts" / "release" / "release_clickhouse.sh"
    assert "::warning::clickhouse_image=" in script.read_text()


# `gh` as a release step sees it: v1.1.0 is not published yet, and v1.2.0's
# bundle (STUB_BUNDLE) was published by another tag's run.
STUB_GH = """#!/usr/bin/env bash
if [ "$1" = api ]; then
  case "$2" in
    */releases/tags/*) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
    *) echo v1.2.0 ;;
  esac
  exit 0
fi
while [ $# -gt 0 ]; do [ "$1" = --dir ] && dir=$2; shift; done
cp "$STUB_BUNDLE" "$dir/loom-stack.tar.gz"
"""

DOWNGRADES = pytest.mark.parametrize(
    ("own", "allow", "passes"),
    [
        ("clickhouse/clickhouse-server:26.9.17.8", "", False),
        ("clickhouse/clickhouse-server:26.9.17.8", "true", True),
        ("clickhouse/clickhouse-server:26.9.18.11", "", True),
    ],
)


def _run_step(tmp_path: Path, script: str, **env: str):
    """Run a release step's script in a checkout (a git repository with one
    commit and no tags) whose `gh` is STUB_GH."""
    import os
    import shutil
    import subprocess
    import sys

    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    # A copy, not a link: through a link, the script's ../../dev would be the
    # real dev/, not the stub written beside it.
    shutil.copytree(Path(__file__).parents[2] / "scripts" / "release", work / "scripts" / "release")
    git_repo.git(work, "init", "-q")
    git_repo.git(work, "commit", "-q", "--allow-empty", "-m", "release")
    other, _ = _module().build(
        "1.2.0", tmp_path / "other", "clickhouse/clickhouse-server:26.9.18.11"
    )
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(STUB_GH)
    gh.chmod(0o755)
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=work,
        env={
            **os.environ,
            # The step's `python` is setup-python's on the runner.
            "PATH": f"{gh.parent}:{Path(sys.executable).parent}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "o/r",
            "VERSION": "1.1.0",
            "STUB_BUNDLE": str(other),
            **env,
        },
        capture_output=True,
        text=True,
    )


@DOWNGRADES
@pytest.mark.parametrize("dispatched", [False, True])
def test_the_release_refuses_a_downgrade_before_it_builds(
    tmp_path: Path, own: str, allow: str, passes: bool, dispatched: bool
) -> None:
    """A refusal here stops the run before it builds and pushes any image,
    whether the release came from `latest` or a dispatch's clickhouse_image."""
    (resolve,) = [
        s for s in _release_workflow()["jobs"]["clickhouse"]["steps"] if s.get("id") == "resolve"
    ]
    assert resolve["env"]["ALLOW_DOWNGRADE"] == "${{ inputs.allow_clickhouse_downgrade }}"
    latest = tmp_path / "work" / "dev" / "clickhouse_release.sh"
    latest.parent.mkdir(parents=True)
    newest = "clickhouse/clickhouse-server:26.9.19.1"
    latest.write_text(f"echo {newest if dispatched else own}\n")
    output = tmp_path / "github_output"
    output.touch()
    run = _run_step(
        tmp_path,
        resolve["run"],
        CLICKHOUSE_IMAGE=own if dispatched else "",
        ALLOW_DOWNGRADE=allow,
        GITHUB_OUTPUT=str(output),
    )
    assert (run.returncode == 0) is passes, run.stderr
    if passes:
        assert output.read_text() == f"image={own}\n"
    else:
        assert output.read_text() == ""
        assert "older than clickhouse/clickhouse-server:26.9.18.11" in run.stderr


@DOWNGRADES
def test_overlapping_releases_recheck_the_bundle_another_tag_published(
    tmp_path: Path, own: str, allow: str, passes: bool
) -> None:
    """Two tags resolve ClickHouse before either publishes; the publish step,
    one at a time across tags, refuses the one that would now be a downgrade."""
    job = _release_workflow()["jobs"]["github-release"]
    assert job["concurrency"] == {"group": "release-publish", "cancel-in-progress": False}
    (keep,) = [s for s in job["steps"] if "published_bundle.sh" in s.get("run", "")]
    assert keep["env"]["ALLOW_DOWNGRADE"] == "${{ inputs.allow_clickhouse_downgrade }}"
    _module().build("1.1.0", tmp_path / "work" / "release" / "stack", own)
    run = _run_step(tmp_path, keep["run"], ALLOW_DOWNGRADE=allow)
    assert (run.returncode == 0) is passes, run.stderr
    if not passes:
        assert "older than clickhouse/clickhouse-server:26.9.18.11" in run.stderr
