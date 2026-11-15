"""The release files' keyless signatures: release.yml's sign-files and
attach-signatures jobs, release-check's check of them, and the two scripts they
share. Each step runs under `bash -e` with a stub curl serving a fake release and
a stub cosign."""

from __future__ import annotations

import hashlib
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

# sign-blob writes the file's sha256 as its bundle; verify-blob passes only on a
# bundle of the same bytes, unless `verify` overrides it.
COSIGN = """#!/bin/sh
echo "cosign $*" >> "{tmp}/calls"
if [ "$1" = sign-blob ]; then sha256sum < "$5" > "$4"; exit 0; fi
{verify}
[ "$(sha256sum < "$2")" = "$(cat "$4")" ]
"""


def _bundle(body: str) -> str:
    """The stub's Sigstore bundle of a file holding `body`."""
    return hashlib.sha256(body.encode()).hexdigest() + "  -\n"


def _env(tmp_path: Path, assets: dict[str, str] | None, verify: str = "") -> dict[str, str]:
    """PATH with the stubs, serving a release with `assets` (None: the API fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
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
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    return {**env, "PATH": f"{bin_dir}:{env['PATH']}", "VERSION": "1.2.0"}


def _step(job: dict, name: str) -> dict:
    (step,) = [s for s in job["steps"] if s.get("name") == name]
    return step


def _run(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "scripts").symlink_to(ROOT / "scripts")
    return subprocess.run(
        ["bash", "-e", "-c", script], env=env, cwd=cwd, capture_output=True, text=True
    )


def _calls(tmp_path: Path, tool: str) -> list[str]:
    lines = (tmp_path / "calls").read_text().splitlines()
    return [line.removeprefix(f"{tool} ") for line in lines if line.startswith(f"{tool} ")]


def _verify_blob(file: str) -> str:
    return (
        f"verify-blob {file} --bundle {file}.sigstore.json --certificate-identity {IDENTITY} "
        f"--certificate-oidc-issuer {ISSUER}"
    )


SIGN = _step(RELEASE["sign-files"], "Sign and verify each file")
CHECK_FILES = _step(
    CHECK["signatures"], "Every release file carries the release workflow's keyless signature"
)


def _check(tmp_path: Path, assets: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**_env(tmp_path, assets), "RUNNER_TEMP": str(tmp_path / "tmp")}
    return _run(CHECK_FILES["run"], env, tmp_path / "work")


def test_sign_files_signs_and_verifies_every_file_the_release_serves(tmp_path: Path) -> None:
    """A Sigstore bundle an earlier run attached is signed again, not signed itself."""
    assets = {"install.sh": "a", "loom-stack.tar.gz": "b", "install.sh.sigstore.json": "{}"}
    result = _run(SIGN["run"], _env(tmp_path, assets), tmp_path / "work")
    assert result.returncode == 0, result.stdout + result.stderr
    files = ["files/install.sh", "files/loom-stack.tar.gz"]
    assert _calls(tmp_path, "cosign") == [
        *(f"sign-blob --yes --bundle {f}.sigstore.json {f}" for f in files),
        *(_verify_blob(f) for f in files),
    ]


def test_a_failed_signature_names_the_recovery(tmp_path: Path) -> None:
    env = _env(tmp_path, {"install.sh": "a"})
    (tmp_path / "bin/cosign").write_text("#!/bin/sh\nexit 1\n")
    result = _run(SIGN["run"], env, tmp_path / "work")
    assert result.returncode == 1
    assert "could not sign files/install.sh" in result.stdout
    assert "#a-failed-tag-or-signature" in result.stdout


def test_both_steps_read_the_runs_version_and_list_with_the_runs_token() -> None:
    token = "${{ github.token }}"
    assert SIGN["env"] == {"VERSION": "${{ needs.version.outputs.version }}", "GH_TOKEN": token}
    assert CHECK_FILES["env"] == {"VERSION": "${{ inputs.version }}", "GH_TOKEN": token}


def test_no_step_that_writes_to_the_release_can_request_an_oidc_token() -> None:
    sign, attach = RELEASE["sign-files"], RELEASE["attach-signatures"]
    assert sign["permissions"] == {"contents": "read", "id-token": "write"}
    actions = {s["uses"].split("@")[0] for s in sign["steps"] if "uses" in s}
    assert actions == {"actions/checkout", "sigstore/cosign-installer", "actions/upload-artifact"}
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
    assert upload["with"]["overwrite"] is True  # a re-run of sign-files uploads it again
    attach = _step(RELEASE["attach-signatures"], "Upload the Sigstore bundles")
    assert f"{download['with']['path']}/*.sigstore.json --clobber" in attach["run"]


def test_release_check_verifies_each_published_file(tmp_path: Path) -> None:
    result = _check(tmp_path, {"install.sh": "a", "install.sh.sigstore.json": _bundle("a")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(tmp_path, "cosign") == [_verify_blob(f"{tmp_path}/tmp/files/install.sh")]


def test_a_file_replaced_after_signing_fails_until_it_is_signed_again(tmp_path: Path) -> None:
    """A dispatch replaces the files; the tag run's Sigstore bundles no longer verify them."""
    assets = {"install.sh": "rebuilt", "install.sh.sigstore.json": _bundle("tag run")}
    result = _check(tmp_path / "before", assets)
    assert result.returncode == 1
    assert "could not verify release.yml's signature on install.sh" in result.stdout
    signed = _run(SIGN["run"], _env(tmp_path / "sign", assets), tmp_path / "sign/work")
    assert signed.returncode == 0, signed.stdout + signed.stderr
    bundle = (tmp_path / "sign/work/files/install.sh.sigstore.json").read_text()
    assets["install.sh.sigstore.json"] = bundle  # what attach-signatures uploads
    assert _check(tmp_path / "after", assets).returncode == 0


@pytest.mark.parametrize(
    ("assets", "error"),
    [
        ({"install.sh": "a"}, "install.sh has no Sigstore bundle (install.sh.sigstore.json)"),
        ({}, "v1.2.0 has no release files"),
    ],
    ids=["a file without its bundle", "no files"],
)
def test_release_check_fails_closed(tmp_path: Path, assets: dict[str, str], error: str) -> None:
    result = _check(tmp_path, assets)
    assert result.returncode == 1
    assert error in result.stdout + result.stderr
    assert "#a-failed-tag-or-signature" in result.stdout
    assert _calls(tmp_path, "cosign") == []


def test_the_token_reaches_the_listing_and_no_download(tmp_path: Path) -> None:
    env = {**_env(tmp_path, {"install.sh": "a"}), "GH_TOKEN": "t0ken"}
    script = ROOT / "scripts/release/download_release_files.sh"
    subprocess.run(["bash", str(script), "1.2.0", str(tmp_path / "d")], env=env, check=True)
    listing, download = _calls(tmp_path, "curl")
    assert listing == f"-fsSL -H Authorization: Bearer t0ken {API}"
    assert download == f"-fsSL --output-dir {tmp_path}/d -O {DOWNLOAD}/install.sh"


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
    assert len(_calls(tmp_path, "cosign")) == 1  # stops at the first


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
