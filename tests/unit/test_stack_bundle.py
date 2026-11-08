from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.release import build_stack_bundle, prepare
from server.settings.catalog import DEFAULT_LOCAL_EMBEDDING_MODEL, LOCAL_EMBEDDING_MODELS
from tests.unit import git_repo

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release" / "build_stack_bundle.py"


def test_release_stack_bundle_is_complete_and_checksummed(tmp_path: Path) -> None:
    archive, checksum = build_stack_bundle.build(
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
            "loom-stack/integrations/librechat/assistant.librechat.yaml",
        } <= names
        assert bundle.extractfile("loom-stack/VERSION").read() == b"1.2.3\n"
        compose = bundle.extractfile("loom-stack/compose.yaml").read().decode()
        assert "${LOOM_CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:26.9.18.11}" in compose
        assert "clickhouse/clickhouse-server:latest" not in compose
        notices = bundle.extractfile("loom-stack/THIRD_PARTY_NOTICES").read().decode()
        assert "CC BY-SA 4.0" in notices


def test_standalone_compose_uses_release_image_and_embeds_no_secret() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    env = (ROOT / "deploy" / "standalone" / "env.example").read_text(encoding="utf-8")
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
        build_stack_bundle.build("1.2.3", tmp_path, image)


def test_a_rerun_reads_the_release_a_published_bundle_names(tmp_path: Path) -> None:
    archive, _ = build_stack_bundle.build(
        "1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11"
    )
    assert build_stack_bundle.pinned_image(archive) == "clickhouse/clickhouse-server:26.9.18.11"


def _edited_bundle(tmp_path: Path, edit: Callable[[str], str]) -> Path:
    """A built bundle whose compose.yaml `edit` rewrote."""
    archive, _ = build_stack_bundle.build(
        "1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11"
    )
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
    assert build_stack_bundle.pinned_image(edited) == "clickhouse/clickhouse-server:26.9.18.11"


def test_a_bundle_from_before_the_override_names_its_release_bare(tmp_path: Path) -> None:
    def bare(compose: str) -> str:
        return compose.replace(
            "${LOOM_CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:26.9.18.11}",
            "clickhouse/clickhouse-server:26.5.1.882",
        )

    edited = _edited_bundle(tmp_path, bare)
    assert build_stack_bundle.pinned_image(edited) == "clickhouse/clickhouse-server:26.5.1.882"


def test_the_pin_pattern_reads_what_build_writes() -> None:
    written = build_stack_bundle._image_default("clickhouse/clickhouse-server:26.9.18.11")
    assert (
        build_stack_bundle._PINNED.fullmatch(written).group(1)
        == "clickhouse/clickhouse-server:26.9.18.11"
    )


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
    published, _ = build_stack_bundle.build(
        "1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11"
    )
    if older:
        with pytest.raises(ValueError, match="older than clickhouse/clickhouse-server:26.9.18.11"):
            build_stack_bundle.refuse_older(image, [published])
    else:
        build_stack_bundle.refuse_older(image, [published])


def test_a_new_bundle_is_checked_against_the_newest_of_several(tmp_path: Path) -> None:
    """A prerelease can name a newer ClickHouse than the latest final release."""
    final, _ = build_stack_bundle.build(
        "1.2.3", tmp_path / "a", "clickhouse/clickhouse-server:26.8.2.1"
    )
    rc, _ = build_stack_bundle.build(
        "1.3.0-rc.1", tmp_path / "b", "clickhouse/clickhouse-server:26.9.18.11"
    )
    with pytest.raises(ValueError, match="older than clickhouse/clickhouse-server:26.9.18.11"):
        build_stack_bundle.refuse_older("clickhouse/clickhouse-server:26.9.1.5", [final, rc])


def _release_workflow() -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())


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
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    # A copy, not a link: through a link, the script's ../../dev would be the
    # real dev/, not the stub written beside it.
    shutil.copytree(ROOT / "scripts" / "release", work / "scripts" / "release")
    git_repo.git(work, "init", "-q")
    git_repo.git(work, "commit", "-q", "--allow-empty", "-m", "release")
    other, _ = build_stack_bundle.build(
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
    build_stack_bundle.build("1.1.0", tmp_path / "work" / "release" / "stack", own)
    run = _run_step(tmp_path, keep["run"], ALLOW_DOWNGRADE=allow)
    assert (run.returncode == 0) is passes, run.stderr
    if not passes:
        assert "older than clickhouse/clickhouse-server:26.9.18.11" in run.stderr


def test_a_releases_install_sh_installs_that_release(tmp_path: Path) -> None:
    """`releases/latest/download/install.sh` must install the release it came from,
    not whatever `latest` names by the time it downloads the bundle."""
    build_stack_bundle.build("1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11")
    stamped = (tmp_path / "install.sh").read_text(encoding="utf-8")
    repository = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "\n  RELEASE_VERSION=latest\n" in repository
    assert stamped == repository.replace(
        "\n  RELEASE_VERSION=latest\n", "\n  RELEASE_VERSION=1.2.3\n"
    )
    assert (tmp_path / "install.sh").stat().st_mode & 0o111


@pytest.mark.parametrize("version", ["dev", "v1.2.3", "1.2", "1.2.3-beta.1", "1.2.3; id"])
def test_a_bundle_names_one_release_version(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError, match="not X.Y.Z or X.Y.Z-rc.N"):
        build_stack_bundle.build(version, tmp_path, "clickhouse/clickhouse-server:26.9.18.11")
    assert not any(tmp_path.iterdir())


def test_a_refused_build_is_one_error_line(tmp_path: Path) -> None:
    run = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "build",
            "--version",
            "dev",
            "--output-dir",
            str(tmp_path),
            "--clickhouse-image",
            "clickhouse/clickhouse-server:26.9.18.11",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1
    assert run.stderr == "::error::not X.Y.Z or X.Y.Z-rc.N: 'dev'\n"


@pytest.mark.parametrize(
    "installer",
    ["#!/bin/sh\nRELEASE_VERSION=${X:-latest}\n", "\n  RELEASE_VERSION=latest\n" * 2],
    ids=["no default", "two defaults"],
)
def test_stamping_refuses_an_installer_without_one_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installer: str
) -> None:
    (tmp_path / "install.sh").write_text(installer)
    monkeypatch.setattr(build_stack_bundle, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="RELEASE_VERSION=latest once"):
        build_stack_bundle.build(
            "1.2.3", tmp_path / "out", "clickhouse/clickhouse-server:26.9.18.11"
        )
    assert not (tmp_path / "out").exists()  # no bundle without its installer


def _truncated_bundle(tmp_path: Path) -> Path:
    whole, _ = build_stack_bundle.build(
        "1.2.3", tmp_path / "whole", "clickhouse/clickhouse-server:26.9.18.11"
    )
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(whole.read_bytes()[:100])
    return cut


def _corrupted_bundle(tmp_path: Path) -> Path:
    whole, _ = build_stack_bundle.build(
        "1.2.3", tmp_path / "whole", "clickhouse/clickhouse-server:26.9.18.11"
    )
    data = bytearray(whole.read_bytes())
    for i in range(20, 28):  # the compressed stream, past gzip's 10-byte header
        data[i] ^= 0xFF
    corrupted = tmp_path / "corrupted.tar.gz"
    corrupted.write_bytes(bytes(data))
    return corrupted


def _empty_bundle(tmp_path: Path) -> Path:
    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    return empty


@pytest.mark.parametrize("command", ["pinned-in", "not-older"])
@pytest.mark.parametrize(
    ("bundle", "error"),
    [
        (lambda tmp: tmp / "missing.tar.gz", "is not a readable release bundle"),
        (_truncated_bundle, "is not a readable release bundle"),
        (_corrupted_bundle, "is not a readable release bundle"),
        (_empty_bundle, "has no loom-stack/compose.yaml"),
    ],
    ids=["missing", "cut short", "corrupted", "no compose.yaml"],
)
def test_a_bundle_that_cannot_be_read_is_one_error_line(
    tmp_path: Path, command: str, bundle: Callable[[Path], Path], error: str
) -> None:
    archive = str(bundle(tmp_path))
    args = (
        [archive]
        if command == "pinned-in"
        else ["clickhouse/clickhouse-server:26.9.18.11", archive]
    )
    run = subprocess.run(
        [sys.executable, str(SCRIPT), command, *args], capture_output=True, text=True, check=False
    )
    assert run.returncode == 1
    assert run.stderr.startswith("::error::") and error in run.stderr, run.stderr
    assert "Traceback" not in run.stderr


@pytest.mark.parametrize(
    "version",
    ["1.2.0", "1.2.0-rc.1", "1.2.0-rc.10", "1.2.0-beta.1", "1.2", "1.2.0-rc", "1.2.0-rc.1.2"],
)
def test_a_bundle_takes_the_versions_the_release_takes(tmp_path: Path, version: str) -> None:
    """release.yml's tag parse, prepare.py and build() accept the same versions,
    so a tag the release runs never fails late, at the bundle."""
    (parse,) = [
        s for s in _release_workflow()["jobs"]["version"]["steps"] if s.get("id") == "parse"
    ]
    output = tmp_path / "output"
    env = {
        "PATH": "/usr/bin:/bin",
        "REF": f"refs/tags/v{version}",
        "DRY_RUN": "false",
        "GITHUB_OUTPUT": str(output),
    }
    run = subprocess.run(
        ["bash", "-e", "-c", parse["run"]],
        env=env,
        capture_output=True,
        text=True,
    )
    taken = build_stack_bundle.VERSION_PATTERN.fullmatch(version) is not None
    assert taken is bool(prepare.FINAL.fullmatch(version) or prepare.CANDIDATE.fullmatch(version))
    if taken:
        assert run.returncode == 0, run.stdout + run.stderr
        assert f"version={version}" in output.read_text().splitlines()
    else:
        assert run.returncode == 1 and "is not X.Y.Z or X.Y.Z-rc.N" in run.stdout, run.stdout


def test_the_installer_env_names_the_default_local_model_at_its_width() -> None:
    env = (ROOT / "deploy" / "standalone" / "env.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1) for line in env.splitlines() if line.startswith("LOOM_EMBEDDING_")
    )
    assert values["LOOM_EMBEDDING_MODEL"] == "local"
    assert (
        int(values["LOOM_EMBEDDING_DIM"])
        == LOCAL_EMBEDDING_MODELS[DEFAULT_LOCAL_EMBEDDING_MODEL][1]
    )


def test_every_host_path_compose_mounts_is_in_the_bundle(tmp_path: Path) -> None:
    # A bind mount whose path the bundle lacks becomes an empty directory in the container.
    import yaml

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    mounted = {
        volume.split(":", 1)[0].removeprefix("./")
        for service in compose["services"].values()
        for volume in service.get("volumes", [])
        if isinstance(volume, str) and volume.startswith("./")
    }
    archive, _ = build_stack_bundle.build(
        "1.2.3", tmp_path, "clickhouse/clickhouse-server:26.9.18.11"
    )
    with tarfile.open(archive) as bundle:
        names = {n.removeprefix("loom-stack/") for n in bundle.getnames()}
    missing = {
        p for p in mounted if p not in names and not any(n.startswith(p + "/") for n in names)
    }
    assert not missing, missing
