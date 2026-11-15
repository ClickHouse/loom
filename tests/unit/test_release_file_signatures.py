"""The release files' keyless signatures: release.yml's sign-files and
attach-signatures jobs, release-check's check of them, and the scripts they
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

# Serves API from $STUB_TMP/release.json (or exits 22, as curl -f does, when it
# is absent) and each download URL from $STUB_TMP/assets/<name>.
CURL = f"""#!{sys.executable}
import os, pathlib, shutil, sys
tmp = pathlib.Path(os.environ["STUB_TMP"])
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

# sign-blob writes the blob's sha256 as the Sigstore bundle --bundle names, and
# verify-blob passes only on a bundle of the same bytes. The blob is the one
# argument that is neither a flag nor a flag's value. STUB_COSIGN_FAILS fails
# every call.
COSIGN = f"""#!{sys.executable}
import hashlib, os, pathlib, sys
tmp = pathlib.Path(os.environ["STUB_TMP"])
command, *args = sys.argv[1:]
with open(tmp / "calls", "a") as calls:
    calls.write("cosign " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("STUB_COSIGN_FAILS"):
    sys.exit(1)
valued = {{"--bundle", "--certificate-identity", "--certificate-oidc-issuer"}}
(blob,) = [a for prev, a in zip(["", *args], args) if not a.startswith("--") and prev not in valued]
bundle = pathlib.Path(args[args.index("--bundle") + 1])
digest = hashlib.sha256(pathlib.Path(blob).read_bytes()).hexdigest()
if command == "sign-blob":
    bundle.write_text(digest)
else:
    sys.exit(0 if bundle.exists() and bundle.read_text() == digest else 1)
"""


def _bundle(body: str) -> str:
    """The stub's Sigstore bundle of a file holding `body`."""
    return hashlib.sha256(body.encode()).hexdigest()


def _env(tmp_path: Path, assets: dict[str, str] | None) -> dict[str, str]:
    """PATH with the stubs, serving a release with `assets` (None: the API fails)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "curl").write_text(CURL)
    (bin_dir / "cosign").write_text(COSIGN)
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    if assets is not None:
        (tmp_path / "assets").mkdir()
        for name, body in assets.items():
            (tmp_path / "assets" / name).write_text(body)
        urls = [{"browser_download_url": f"{DOWNLOAD}/{name}"} for name in assets]
        (tmp_path / "release.json").write_text(json.dumps({"assets": urls}))
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    path = f"{bin_dir}:{env['PATH']}"
    return {**env, "PATH": path, "STUB_TMP": str(tmp_path), "VERSION": "1.2.0"}


def _step(job: dict, name: str) -> dict:
    (step,) = [s for s in job["steps"] if s.get("name") == name]
    return step


def _run(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """A workflow step's script, run in cwd with the repository's scripts beside it."""
    cwd.mkdir(parents=True, exist_ok=True)
    (cwd / "scripts").symlink_to(ROOT / "scripts")
    return subprocess.run(
        ["bash", "-e", "-c", script], env=env, cwd=cwd, capture_output=True, text=True
    )


def _script(
    name: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    path = ROOT / "scripts/release" / name
    return subprocess.run(["bash", str(path), *args], env=env, capture_output=True, text=True)


def _calls(tmp_path: Path, tool: str) -> list[str]:
    lines = (tmp_path / "calls").read_text().splitlines()
    return [line.removeprefix(f"{tool} ") for line in lines if line.startswith(f"{tool} ")]


def _sign_blob(file: str) -> str:
    return f"sign-blob --yes --bundle {file}.sigstore.json {file}"


def _verify_blob(file: str) -> str:
    return (
        f"verify-blob {file} --bundle {file}.sigstore.json --certificate-identity {IDENTITY} "
        f"--certificate-oidc-issuer {ISSUER}"
    )


SIGN = _step(RELEASE["sign-files"], "Sign and verify each file")
CHECK_FILES = _step(
    CHECK["signatures"], "Every release file carries the release workflow's keyless signature"
)


def _sign(
    tmp_path: Path, served: dict[str, str], built: dict[str, str], **env: str
) -> subprocess.CompletedProcess[str]:
    """The sign-files step on a release serving `served`, the run having built `built`."""
    work = tmp_path / "work"
    (work / "built/python").mkdir(parents=True)
    for name, body in built.items():
        (work / "built/python" / name).write_text(body)
    return _run(SIGN["run"], {**_env(tmp_path, served), **env}, work)


def _check(tmp_path: Path, assets: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**_env(tmp_path, assets), "RUNNER_TEMP": str(tmp_path / "tmp")}
    return _run(CHECK_FILES["run"], env, tmp_path / "work")


def _signed(tmp_path: Path) -> list[str]:
    return [c for c in _calls(tmp_path, "cosign") if c.startswith("sign-blob")]


def test_sign_files_signs_and_verifies_each_file_this_run_built(tmp_path: Path) -> None:
    """An earlier run's Sigstore bundle is replaced, never signed as a release file."""
    served = {"install.sh": "a", "x.whl": "b", "install.sh.sigstore.json": "old"}
    result = _sign(tmp_path, served, {"install.sh": "a", "x.whl": "b"})
    assert result.returncode == 0, result.stdout + result.stderr
    files = ["files/install.sh", "files/x.whl"]
    assert _calls(tmp_path, "cosign") == [
        *(_sign_blob(f) for f in files),
        *(_verify_blob(f) for f in files),
    ]
    assert (tmp_path / "work/files/install.sh.sigstore.json").read_text() == _bundle("a")


def test_a_kept_stack_bundle_keeps_its_sigstore_bundle(tmp_path: Path) -> None:
    """The release kept an earlier run's stack bundle, which this run did not build."""
    served = {
        "loom-stack.tar.gz": "tag run",
        "loom-stack.tar.gz.sigstore.json": _bundle("tag run"),
    }
    result = _sign(tmp_path, served, {"loom-stack.tar.gz": "rebuilt"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "loom-stack.tar.gz: kept, its Sigstore bundle verifies" in result.stdout
    assert _calls(tmp_path, "cosign") == [_verify_blob("files/loom-stack.tar.gz")] * 2


def test_a_dispatch_signs_what_it_rebuilt_and_keeps_the_signed_stack_pair(tmp_path: Path) -> None:
    """What every final's dispatch meets: rebuilt files beside the tag run's stack pair."""
    served = {
        "install.sh": "a",
        "x.whl": "rebuilt",
        "x.whl.sigstore.json": _bundle("tag run"),
        "loom-stack.tar.gz": "tag run",
        "loom-stack.tar.gz.sigstore.json": _bundle("tag run"),
        "loom-stack.tar.gz.sha256": "tag run sum",
        "loom-stack.tar.gz.sha256.sigstore.json": _bundle("tag run sum"),
    }
    built = {
        "install.sh": "a",
        "x.whl": "rebuilt",
        "loom-stack.tar.gz": "rebuilt",
        "loom-stack.tar.gz.sha256": "rebuilt sum",
    }
    result = _sign(tmp_path, served, built)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _signed(tmp_path) == [_sign_blob("files/install.sh"), _sign_blob("files/x.whl")]
    files = sorted((tmp_path / "work/files").glob("*.sigstore.json"))
    assert [f.name for f in files] == [
        "install.sh.sigstore.json",
        "loom-stack.tar.gz.sha256.sigstore.json",
        "loom-stack.tar.gz.sigstore.json",
        "x.whl.sigstore.json",
    ]
    assert (tmp_path / "work/files/x.whl.sigstore.json").read_text() == _bundle("rebuilt")
    kept = tmp_path / "work/files/loom-stack.tar.gz.sha256.sigstore.json"
    assert kept.read_text() == _bundle("tag run sum")


NO_BUNDLE = "has no Sigstore bundle: never signed, attached by hand, or changed"
INCIDENT = "its Sigstore bundle does not verify (cosign's error above): unless Sigstore is down"


@pytest.mark.parametrize(
    ("served", "error"),
    [
        ({"install.sh": "swapped"}, NO_BUNDLE),
        ({"install.sh": "swapped", "install.sh.sigstore.json": _bundle("a")}, INCIDENT),
        ({"install.sh": "a", "zz.txt": "uploaded by hand"}, NO_BUNDLE),
        ({"loom-stack.tar.gz": "tag run, never signed"}, NO_BUNDLE),
    ],
    ids=["replaced", "replaced beside its old bundle", "not built by the run", "kept unsigned"],
)
def test_a_file_that_is_not_this_runs_build_fails_before_anything_is_signed(
    tmp_path: Path, served: dict[str, str], error: str
) -> None:
    result = _sign(tmp_path, served, {"install.sh": "a", "loom-stack.tar.gz": "rebuilt"})
    assert result.returncode == 1
    assert error in result.stdout
    assert "#a-failed-tag-or-signature" in result.stdout
    assert _signed(tmp_path) == []


def test_a_failed_signature_names_the_recovery(tmp_path: Path) -> None:
    result = _sign(tmp_path, {"install.sh": "a"}, {"install.sh": "a"}, STUB_COSIGN_FAILS="1")
    assert result.returncode == 1
    assert "could not sign install.sh" in result.stdout
    assert "#a-failed-tag-or-signature" in result.stdout


def test_both_steps_read_the_runs_version_and_list_with_the_runs_token() -> None:
    token = "${{ github.token }}"
    assert SIGN["env"] == {"VERSION": "${{ needs.version.outputs.version }}", "GH_TOKEN": token}
    assert CHECK_FILES["env"] == {"VERSION": "${{ inputs.version }}", "GH_TOKEN": token}


def test_no_step_that_writes_to_the_release_can_request_an_oidc_token() -> None:
    sign, attach = RELEASE["sign-files"], RELEASE["attach-signatures"]
    assert sign["permissions"] == {"contents": "read", "id-token": "write"}
    actions = {s["uses"].split("@")[0] for s in sign["steps"] if "uses" in s}
    assert actions == {
        "actions/checkout",
        "sigstore/cosign-installer",
        "actions/download-artifact",
        "actions/upload-artifact",
    }
    assert attach["permissions"] == {"contents": "write"}


def test_sign_files_compares_with_the_files_the_build_job_made() -> None:
    steps = RELEASE["sign-files"]["steps"]
    (built,) = [s for s in steps if "download-artifact" in s.get("uses", "")]
    (made,) = [s for s in RELEASE["build"]["steps"] if "upload-artifact" in s.get("uses", "")]
    assert built["with"] == {"name": made["with"]["name"], "path": "built"}
    assert 'sign_release_files.sh "$VERSION" files built' in SIGN["run"]


def test_attach_signatures_uploads_the_bundles_sign_files_made() -> None:
    steps = RELEASE["sign-files"]["steps"]
    (upload,) = [s for s in steps if "upload-artifact" in s.get("uses", "")]
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


def test_a_file_replaced_by_a_dispatch_fails_until_it_is_signed_again(tmp_path: Path) -> None:
    """A dispatch replaces the files; the tag run's Sigstore bundles no longer verify them."""
    assets = {"install.sh": "rebuilt", "install.sh.sigstore.json": _bundle("tag run")}
    result = _check(tmp_path / "before", assets)
    assert result.returncode == 1
    assert "could not verify release.yml's signature on install.sh" in result.stdout
    signed = _sign(tmp_path / "sign", assets, {"install.sh": "rebuilt"})
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
    result = _script("download_release_files.sh", "1.2.0", str(tmp_path / "d"), env=env)
    assert result.returncode == 0, result.stderr
    listing, download = _calls(tmp_path, "curl")
    assert listing == f"-fsSL -H Authorization: Bearer t0ken {API}"
    assert download == f"-fsSL --output-dir {tmp_path}/d -O {DOWNLOAD}/install.sh"


def test_a_failed_release_listing_is_not_read_as_an_empty_release(tmp_path: Path) -> None:
    env = _env(tmp_path, None)
    result = _script("download_release_files.sh", "1.2.0", str(tmp_path / "d"), env=env)
    assert result.returncode == 22
    assert "no release files" not in result.stdout + result.stderr


def _verify(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    (tmp_path / "d").mkdir()
    for name, body in files.items():
        (tmp_path / "d" / name).write_text(body)
    env = _env(tmp_path, {})
    return _script("verify_release_files.sh", "1.2.0", str(tmp_path / "d"), env=env)


def test_a_file_that_does_not_verify_fails_with_the_recovery(tmp_path: Path) -> None:
    files = {"a": "a", "a.sigstore.json": _bundle("x"), "b": "b", "b.sigstore.json": _bundle("b")}
    result = _verify(tmp_path, files)
    assert result.returncode == 1
    assert "could not verify release.yml's signature on a" in result.stdout
    assert "#a-failed-tag-or-signature" in result.stdout
    assert len(_calls(tmp_path, "cosign")) == 1  # stops at the first


def test_a_directory_without_files_fails(tmp_path: Path) -> None:
    result = _verify(tmp_path, {})
    assert result.returncode == 1
    assert "no release files in" in result.stdout
    assert "#a-failed-tag-or-signature" in result.stdout


@pytest.mark.parametrize(
    ("script", "args"),
    [
        ("verify_release_files.sh", ["1.2.0"]),
        ("download_release_files.sh", ["1.2.0"]),
        ("sign_release_files.sh", ["1.2.0", "files"]),
    ],
)
def test_a_missing_argument_is_a_usage_error(script: str, args: list[str]) -> None:
    result = _script(script, *args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
