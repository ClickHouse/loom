"""The release files' keyless signatures: release.yml's sign-files and
attach-signatures jobs, release-check's check of them, and the two scripts they
share. Each step runs under `bash -e` with a stub curl serving a fake release and
a stub cosign."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
CHECK = yaml.safe_load((ROOT / ".github/workflows/release-check.yml").read_text())["jobs"]
IDENTITY = "https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v1.2.0"
ISSUER = "https://token.actions.githubusercontent.com"
API = "https://api.github.com/repos/ClickHouse/loom/releases/tags/v1.2.0"
DOWNLOAD = "https://github.com/ClickHouse/loom/releases/download/v1.2.0"

# Serves API from release.json (or exits 22, as curl -f does, when it is absent)
# and each download URL from assets/<name>.
CURL = f"""#!{sys.executable}
import pathlib, shutil, sys
tmp = pathlib.Path({{tmp!r}})
args = sys.argv[1:]
with open(tmp / "calls", "a") as calls:
    calls.write("curl " + " ".join(args) + "\\n")
url = args[-1]
if url == {API!r}:
    release = tmp / "release.json"
    if not release.exists():
        sys.exit(22)
    sys.stdout.write(release.read_text())
else:
    out = pathlib.Path(args[args.index("--output-dir") + 1])
    name = url.rsplit("/", 1)[1]
    shutil.copy(tmp / "assets" / name, out / name)
"""

# sign-blob writes the bundle --bundle names; verify-blob runs `verify`.
COSIGN = """#!/bin/sh
echo "cosign $*" >> "{tmp}/calls"
if [ "$1" = sign-blob ]; then echo '{{}}' > "$4"; exit 0; fi
{verify}
"""


def _env(tmp_path: Path, assets: dict[str, str] | None, verify: str = "exit 0") -> dict[str, str]:
    """PATH with the stubs, serving a release with `assets` (None: the API fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(CURL.format(tmp=str(tmp_path)))
    (bin_dir / "cosign").write_text(COSIGN.format(tmp=tmp_path, verify=verify))
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    if assets is not None:
        (tmp_path / "assets").mkdir()
        for name, body in assets.items():
            (tmp_path / "assets" / name).write_text(body)
        urls = [{"browser_download_url": f"{DOWNLOAD}/{name}"} for name in assets]
        (tmp_path / "release.json").write_text(json.dumps({"assets": urls}))
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "VERSION": "1.2.0"}


def _step(job: dict, name: str) -> dict:
    (step,) = [s for s in job["steps"] if s.get("name") == name]
    return step


def _run(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    (cwd / "scripts").symlink_to(ROOT / "scripts")
    return subprocess.run(
        ["bash", "-e", "-c", script], env=env, cwd=cwd, capture_output=True, text=True
    )


def _cosign_calls(tmp_path: Path) -> list[str]:
    lines = (tmp_path / "calls").read_text().splitlines()
    return [line.removeprefix("cosign ") for line in lines if line.startswith("cosign ")]


def _verify_blob(file: str) -> str:
    return (
        f"verify-blob {file} --bundle {file}.sigstore.json --certificate-identity {IDENTITY} "
        f"--certificate-oidc-issuer {ISSUER}"
    )


SIGN = _step(RELEASE["sign-files"], "Sign and verify each file")
CHECK_FILES = _step(
    CHECK["signatures"], "Every release file carries the release workflow's keyless signature"
)


def test_sign_files_signs_and_verifies_every_file_the_release_serves(tmp_path: Path) -> None:
    """A Sigstore bundle an earlier run attached is signed again, not signed itself."""
    assets = {"install.sh": "a", "loom-stack.tar.gz": "b", "install.sh.sigstore.json": "{}"}
    (tmp_path / "work").mkdir()
    result = _run(SIGN["run"], _env(tmp_path, assets), tmp_path / "work")
    assert result.returncode == 0, result.stdout + result.stderr
    files = ["files/install.sh", "files/loom-stack.tar.gz"]
    assert _cosign_calls(tmp_path) == [
        *(f"sign-blob --yes --bundle {f}.sigstore.json {f}" for f in files),
        *(_verify_blob(f) for f in files),
    ]
    assert SIGN["env"]["VERSION"] == "${{ needs.version.outputs.version }}"


def test_only_cosign_holds_the_oidc_token_and_only_attach_signatures_writes() -> None:
    sign, attach = RELEASE["sign-files"], RELEASE["attach-signatures"]
    assert sign["permissions"] == {"contents": "read", "id-token": "write"}
    actions = {s["uses"].split("@")[0] for s in sign["steps"] if "uses" in s}
    assert actions == {
        "actions/checkout",
        "sigstore/cosign-installer",
        "actions/upload-artifact",
    }
    assert attach["permissions"] == {"contents": "write"}


def test_attach_signatures_uploads_the_bundles_sign_files_made() -> None:
    (upload,) = [
        s for s in RELEASE["sign-files"]["steps"] if "upload-artifact" in s.get("uses", "")
    ]
    (download,) = [
        s for s in RELEASE["attach-signatures"]["steps"] if "download-artifact" in s.get("uses", "")
    ]
    assert upload["with"]["name"] == download["with"]["name"]
    assert upload["with"]["path"] == "files/*.sigstore.json"
    attach = _step(RELEASE["attach-signatures"], "Upload the bundles")
    assert f"{download['with']['path']}/*.sigstore.json --clobber" in attach["run"]


def test_release_check_waits_for_the_signatures() -> None:
    assert RELEASE["sign-files"]["needs"] == ["version", "github-release"]
    assert RELEASE["attach-signatures"]["needs"] == "sign-files"
    assert RELEASE["release-check"]["needs"] == ["version", "attach-signatures"]


def test_release_check_verifies_each_published_file_signed_in_to_nothing(tmp_path: Path) -> None:
    assets = {"install.sh": "a", "install.sh.sigstore.json": "{}"}
    env = {**_env(tmp_path, assets), "RUNNER_TEMP": str(tmp_path / "tmp")}
    (tmp_path / "work").mkdir()
    result = _run(CHECK_FILES["run"], env, tmp_path / "work")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _cosign_calls(tmp_path) == [_verify_blob(f"{tmp_path}/tmp/files/install.sh")]
    assert CHECK_FILES["env"] == {"VERSION": "${{ inputs.version }}"}


@pytest.mark.parametrize(
    ("assets", "error"),
    [
        ({"install.sh": "a"}, "install.sh has no Sigstore bundle (install.sh.sigstore.json)"),
        ({}, "v1.2.0 has no release files"),
    ],
    ids=["a file without its bundle", "no files"],
)
def test_release_check_fails_closed(tmp_path: Path, assets: dict[str, str], error: str) -> None:
    env = {**_env(tmp_path, assets), "RUNNER_TEMP": str(tmp_path / "tmp")}
    (tmp_path / "work").mkdir()
    result = _run(CHECK_FILES["run"], env, tmp_path / "work")
    assert result.returncode == 1
    assert error in result.stdout + result.stderr
    assert _cosign_calls(tmp_path) == []


def test_a_failed_release_listing_is_not_read_as_an_empty_release(tmp_path: Path) -> None:
    script = ROOT / "scripts/release/download_release_files.sh"
    result = subprocess.run(
        ["bash", str(script), "1.2.0", str(tmp_path / "d")],
        env=_env(tmp_path, None),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 22
    assert "no release files" not in result.stdout + result.stderr


def _verify(tmp_path: Path, verify: str, names: list[str]) -> subprocess.CompletedProcess[str]:
    (tmp_path / "d").mkdir()
    for name in names:
        (tmp_path / "d" / name).write_text(name)
    return subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/release/verify_release_files.sh"),
            "1.2.0",
            str(tmp_path / "d"),
        ],
        env=_env(tmp_path, {}, verify),
        capture_output=True,
        text=True,
    )


def test_a_file_that_does_not_verify_fails_with_the_recovery(tmp_path: Path) -> None:
    result = _verify(tmp_path, "exit 1", ["a", "a.sigstore.json", "b", "b.sigstore.json"])
    assert result.returncode == 1
    assert "could not verify release.yml's signature on a" in result.stdout
    assert "#a-failed-tag-or-signature" in result.stdout
    assert len(_cosign_calls(tmp_path)) == 1  # stops at the first


def test_a_missing_bundle_names_the_recovery(tmp_path: Path) -> None:
    result = _verify(tmp_path, "exit 0", ["a"])
    assert result.returncode == 1
    assert "#a-failed-tag-or-signature" in result.stdout


def test_a_directory_without_files_fails(tmp_path: Path) -> None:
    result = _verify(tmp_path, "exit 0", [])
    assert result.returncode == 1
    assert "no release files in" in result.stdout


@pytest.mark.parametrize("script", ["verify_release_files.sh", "download_release_files.sh"])
@pytest.mark.parametrize("args", [[], ["1.2.0"]], ids=["nothing", "no dir"])
def test_a_missing_version_or_dir_is_a_usage_error(script: str, args: list[str]) -> None:
    path = ROOT / "scripts/release" / script
    result = subprocess.run(["bash", str(path), *args], capture_output=True, text=True)
    assert result.returncode == 2 and "usage:" in result.stderr
