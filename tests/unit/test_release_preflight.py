"""The release workflow's scripts and wiring: preflight.sh (the owner-granted
prerequisites), published_images.sh (a run on a published tag builds nothing),
make_latest.py (which release becomes Latest), verify_signatures.sh (the images'
keyless signatures), and how release.yml and release-check.yml call them."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
from make_latest import is_latest  # noqa: E402

# A stub curl answering as ghcr.io does: an anonymous pull token only for a
# public package (401 for a private one, 403 for one never pushed), a push
# token for anyone signed in unless NO_TOKEN, and an upload session (202 with a
# Location) only for a writer. Each call is logged.
FAKE_CURL = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
url=${@: -1}
case "$*" in
  *"-X DELETE"*) exit 0 ;;
  *"-X POST"*)
    pkg=${url#*/v2/}; pkg=${pkg%/blobs/uploads/}
    if [[ " $WRITABLE " == *" $pkg "* ]]; then
      printf 'HTTP/2 202\r\nlocation: /v2/%s/blobs/uploads/abc\r\n\r\n' "$pkg"
    else
      printf 'HTTP/2 403\r\n\r\n'
    fi ;;
  *" -u "*) [ -n "${NO_TOKEN:-}" ] && echo '{}' || echo '{"token":"t"}' ;;
  *)
    pkg=${url#*scope=repository:}; pkg=${pkg%%:*}
    if [[ " $PUBLIC " == *" $pkg "* ]]; then echo 200
    elif [[ " ${MISSING:-} " == *" $pkg "* ]]; then echo 403
    else echo 401; fi ;;
esac
"""

IMAGES = "ghcr.io/clickhouse/loom ghcr.io/clickhouse/loom-extractor"
VERIFY = 'bash scripts/release/verify_signatures.sh "$VERSION" $IMAGES'


def _preflight(
    tmp_path: Path,
    mode: str,
    *,
    private: str,
    writable: str = "",
    public: str = "",
    curl: str = FAKE_CURL,
    **extra: str,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(curl)
    (bin_dir / "curl").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "GITHUB_REPOSITORY": "ClickHouse/loom",
        "GITHUB_ACTOR": "release-bot",
        "GH_TOKEN": "token",
        "REPOSITORY_PRIVATE": private,
        "IMAGES": IMAGES,
        "WRITABLE": writable,
        "PUBLIC": public,
        "GHCR": "https://registry.test",
        **extra,
    }
    return subprocess.run(
        ["bash", str(ROOT / "scripts/release/preflight.sh"), mode],
        env=env,
        capture_output=True,
        text=True,
    )


def test_a_public_repository_with_both_packages_ready_passes(tmp_path: Path) -> None:
    both = "clickhouse/loom clickhouse/loom-extractor"
    result = _preflight(tmp_path, "strict", private="false", writable=both, public=both)
    assert result.returncode == 0, result.stdout
    assert "0 release prerequisite(s) missing" in result.stdout
    # The probe's upload session is cancelled, so nothing is left in the registry.
    calls = (tmp_path / "calls").read_text()
    assert calls.count("-X DELETE") == 2
    assert "https://registry.test/v2/clickhouse/loom/blobs/uploads/abc" in calls


def test_each_missing_prerequisite_names_its_fix_and_fails_a_release(tmp_path: Path) -> None:
    """A private repository, an org package this repository cannot write, and
    two private packages."""
    result = _preflight(
        tmp_path, "strict", private="true", writable="clickhouse/loom-extractor", public=""
    )
    assert result.returncode == 1
    errors = [line for line in result.stdout.splitlines() if line.startswith("::error::")]
    assert len(errors) == 5, result.stdout
    assert "ClickHouse/loom is private" in errors[0]
    assert "ghcr.io/clickhouse/loom: this workflow cannot push (HTTP 403)" in errors[1]
    assert "Manage Actions access, add ClickHouse/loom with the Write role" in errors[1]
    assert "https://github.com/orgs/clickhouse/packages/container/loom/settings," in errors[1]
    assert "ghcr.io/clickhouse/loom refuses anonymous pulls (HTTP 401)" in errors[2]
    assert "set the visibility to Public" in errors[2]
    assert "loom-extractor refuses anonymous pulls" in errors[3]
    assert "4 release prerequisite(s) missing" in errors[4]
    assert "can push" in result.stdout  # the extractor


def test_a_dry_run_reports_the_same_prerequisites_as_warnings(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "report", private="true", writable="", public="")
    assert result.returncode == 0
    assert "::error::" not in result.stdout
    assert result.stdout.count("::warning::") == 5
    assert "5 release prerequisite(s) missing" in result.stdout


def test_a_package_never_pushed_warns_without_failing_the_release(tmp_path: Path) -> None:
    """Only a push creates a package, so its visibility cannot be set before the
    first release: the run goes on and the warning names the step after it."""
    both = "clickhouse/loom clickhouse/loom-extractor"
    result = _preflight(
        tmp_path,
        "strict",
        private="false",
        writable=both,
        public="clickhouse/loom",
        MISSING="clickhouse/loom-extractor",
    )
    assert result.returncode == 0, result.stdout
    (warning,) = [line for line in result.stdout.splitlines() if line.startswith("::warning::")]
    assert "loom-extractor does not exist yet" in warning
    assert "re-run the failed jobs" in warning


def test_a_missing_push_token_is_named_apart_from_a_missing_grant(tmp_path: Path) -> None:
    both = "clickhouse/loom clickhouse/loom-extractor"
    result = _preflight(
        tmp_path, "strict", private="false", writable=both, public=both, NO_TOKEN="1"
    )
    assert result.returncode == 1
    assert result.stdout.count("gave this workflow no push token") == 2
    assert "Write role" not in result.stdout
    assert "-X POST" not in (tmp_path / "calls").read_text()


# preflight.sh against a registry that answers 503: an outage is never reported
# as a setting for the owner to change.
OUTAGE_CURL = r"""#!/usr/bin/env bash
case "$*" in
  *" -u "*) echo '{"token":"t"}' ;;
  *"-X POST"*) printf 'HTTP/2 503\r\n\r\n' ;;
  *"-X DELETE"*) ;;
  *) echo 503 ;;
esac
"""


def test_a_registry_outage_is_not_reported_as_a_missing_grant(tmp_path: Path) -> None:
    result = _preflight(tmp_path, "strict", private="false", curl=OUTAGE_CURL)
    assert result.returncode == 1
    assert result.stdout.count("(an outage), so its Write access is unknown") == 2
    assert result.stdout.count("(an outage), so its visibility is unknown") == 2
    assert "Write role" not in result.stdout and "Danger Zone" not in result.stdout


def test_the_workflow_runs_the_preflight_before_any_build() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert jobs["preflight"]["needs"] == "version"
    assert "preflight" in jobs["clickhouse"]["needs"]
    (step,) = [s for s in jobs["preflight"]["steps"] if "preflight.sh" in s.get("run", "")]
    assert step["env"]["MODE"] == "${{ inputs.dry_run && 'report' || 'strict' }}"
    assert step["env"]["REPOSITORY_PRIVATE"] == "${{ github.event.repository.private }}"
    (published,) = [s for s in jobs["preflight"]["steps"] if s.get("id") == "images"]
    assert '"$VERSION" $IMAGES)' in published["run"]


@pytest.mark.parametrize(
    ("version", "published", "latest"),
    [
        ("1.2.0", [], True),
        ("1.2.0", ["v1.0.1", "v1.1.0"], True),
        ("1.2.0", ["v1.2.0"], True),  # a re-run of the latest
        ("1.1.3", ["v1.2.0", "v1.1.2"], False),  # a patch to the older line
        ("1.3.0-rc.1", ["v1.2.0"], False),
        ("1.2.1", ["v1.2.0", "v1.3.0-rc.1"], True),  # prereleases never count
        ("1.10.0", ["v1.9.4"], True),  # numeric, not text, order
    ],
)
def test_only_the_highest_final_version_becomes_latest(
    version: str, published: list[str], latest: bool
) -> None:
    assert is_latest(version, published) is latest


def test_the_image_and_the_release_ask_for_latest_when_they_apply_it() -> None:
    """Asked in the job that applies it, so a re-run after a newer tag shipped
    does not take Latest back, and of the tags, which exist from the push on."""
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    for name in ("docker-tag", "github-release"):
        (answer,) = [s for s in jobs[name]["steps"] if s.get("id") == "latest"]
        # An assignment, which fails the step under `bash -e` when the listing fails.
        assert answer["run"].startswith(
            'latest=$(python3 scripts/release/make_latest.py "$VERSION")\n'
        )
        assert answer["env"]["GH_TOKEN"] == "${{ github.token }}", name
    (meta,) = [s for s in jobs["docker"]["steps"] if s.get("id") == "meta"]
    # metadata-action would otherwise add `:latest` to any final version itself.
    assert meta["with"]["flavor"] == "latest=false"
    assert "latest" not in meta["with"]["tags"]
    (tag,) = [s for s in jobs["docker-tag"]["steps"] if s.get("name") == "Tag the scanned images"]
    assert tag["env"]["LATEST"] == "${{ steps.latest.outputs.latest }}"
    (publish,) = [
        s for s in jobs["github-release"]["steps"] if "action-gh-release" in s.get("uses", "")
    ]
    assert publish["with"]["make_latest"] == "${{ steps.latest.outputs.latest }}"
    assert "latest" not in jobs["version"].get("outputs", {})


def test_a_dry_run_publishes_nothing() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    # The registries follow github-release, which a dry run skips (test_release_job_paths.py).
    for name in ("docker-tag", "sign", "github-release"):
        assert "!inputs.dry_run" in jobs[name]["if"], name
    # A release stops at the first failing image; a dry run reports them all.
    assert jobs["docker"]["strategy"]["fail-fast"] == "${{ !inputs.dry_run }}"
    (login,) = [s for s in jobs["docker"]["steps"] if s.get("name") == "Log in to ghcr.io"]
    assert login["if"] == "${{ !inputs.dry_run }}"
    (build,) = [s for s in jobs["docker"]["steps"] if s.get("id") == "build"]
    outputs = build["with"]["outputs"]
    assert outputs.startswith("${{ inputs.dry_run && format('type=docker,")
    # A release pushes by digest, with its attestations, built from today's packages.
    assert "|| format('type=image,name={0},push-by-digest=true,name-canonical=true,push=true'" in (
        outputs
    )
    assert build["with"]["provenance"] == "${{ !inputs.dry_run && 'mode=max' || 'false' }}"
    assert build["with"]["sbom"] == "${{ !inputs.dry_run }}"
    assert build["with"]["no-cache"] is True and "cache-from" not in build["with"]


def test_a_dry_run_smokes_its_own_images_and_a_release_the_published_ones() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    smoke = jobs["docker-smoke"]
    assert smoke["needs"] == [
        "version",
        "preflight",
        "clickhouse",
        "verify",
        "docker",
        "docker-tag",
    ]
    # Which jobs run on each path: tests/unit/test_release_job_paths.py.
    loads = [s for s in smoke["steps"] if s.get("if") == "${{ inputs.dry_run }}"]
    assert loads[0]["with"]["pattern"] == "scanned-*-${{ matrix.arch }}"
    assert "docker load" in loads[1]["run"]
    # The docker job exports under the very names the smoke runs.
    (build,) = [s for s in jobs["docker"]["steps"] if s.get("id") == "build"]
    assert "type=docker,name={0}:{1}" in build["with"]["outputs"]
    assert "matrix.image, needs.version.outputs.version" in build["with"]["outputs"]
    (run,) = [s for s in smoke["steps"] if "docker_quickstart_smoke.py" in s.get("run", "")]
    assert '"ghcr.io/clickhouse/loom:$VERSION"' in run["run"]


def test_make_latest_fails_when_the_tags_cannot_be_listed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
    (bin_dir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/release/make_latest.py"), "1.2.0"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and result.stdout == ""
    assert "HTTP 502" in result.stderr  # the cause reaches the log


def test_make_latest_reads_the_repositorys_tags(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        '#!/bin/sh\n[ "$2" = repos/o/r/git/matching-refs/tags/v ] || exit 3\n'
        "printf 'v1.2.0\\nv1.1.0\\nv1.3.0-rc.1\\n'\n"
    )
    (bin_dir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r"}

    def latest(version: str) -> str:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/release/make_latest.py"), version],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert latest("1.2.1") == "true"
    assert latest("1.1.1") == "false"


# A stub registry for published_images.sh: PUBLISHED lists "package:tag" pairs
# that exist (an index of one platform plus an attestation, its manifest, and a
# config labelled with BUILT_FROM), NEVER packages never pushed, DOWN makes
# every request a 503.
FAKE_REGISTRY = r"""#!/usr/bin/env bash
echo "$*" >> "$CALLS"
url=${@: -1} out=/dev/null
for ((i = 1; i < $#; i++)); do [ "${!i}" = -o ] && { j=$((i + 1)); out=${!j}; }; done
[ -n "${DOWN:-}" ] && { echo 503; exit 0; }
case $url in
  *"/token?"*)
    pkg=${url#*scope=repository:}; pkg=${pkg%%:*}
    if [[ " ${NEVER:-} " == *" $pkg "* ]]; then echo 403; exit 0; fi
    echo '{"token":"anon"}' > "$out"; echo 200 ;;
  *"/manifests/sha256:platform")
    echo '{"config":{"digest":"sha256:config"}}' > "$out"; echo 200 ;;
  *"/manifests/"*)
    pkg=${url#*/v2/}; tag=${pkg##*/manifests/}; pkg=${pkg%/manifests/*}
    if [[ " $PUBLISHED " == *" $pkg:$tag "* ]]; then
      echo '{"manifests":[{"digest":"sha256:att","platform":{"os":"unknown"}},
        {"digest":"sha256:platform","platform":{"os":"linux","architecture":"amd64"}}]}' > "$out"
      echo 200
    else echo 404; fi ;;
  *"/blobs/sha256:config")
    printf '{"config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}' "$BUILT_FROM" > "$out"
    echo 200 ;;
esac
"""


def _published(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(FAKE_REGISTRY)
    (bin_dir / "curl").chmod(0o755)
    return subprocess.run(
        ["bash", str(ROOT / "scripts/release/published_images.sh"), "1.2.0", *IMAGES.split()],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALLS": str(tmp_path / "calls"),
            "GHCR": "https://registry.test",
            "PUBLISHED": "",
            "REVISION": "c0ffee",
            "BUILT_FROM": "c0ffee",
            **env,
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("published", "never", "answer"),
    [
        ("clickhouse/loom:1.2.0 clickhouse/loom-extractor:1.2.0", "", "true"),
        ("clickhouse/loom:1.1.0 clickhouse/loom-extractor:1.1.0", "", "false"),
        ("", "clickhouse/loom-extractor", "false"),  # a package never pushed
    ],
)
def test_published_images_says_whether_every_image_carries_the_version(
    tmp_path: Path, published: str, never: str, answer: str
) -> None:
    result = _published(tmp_path, PUBLISHED=published, NEVER=never)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == answer
    assert " -u " not in (tmp_path / "calls").read_text()  # signed in to nothing


def test_published_images_refuses_images_built_from_another_commit(tmp_path: Path) -> None:
    """A tag moved after its images were pushed: those images would ship beside
    the new commit's files."""
    both = "clickhouse/loom:1.2.0 clickhouse/loom-extractor:1.2.0"
    result = _published(tmp_path, PUBLISHED=both, BUILT_FROM="0ldc0de")
    assert result.returncode == 1 and result.stdout == ""
    assert "was built from 0ldc0de, not c0ffee" in result.stderr
    assert "needs a new version, never a moved tag" in result.stderr
    # The platform's own manifest was read, not the attestation's.
    calls = (tmp_path / "calls").read_text()
    assert "manifests/sha256:platform" in calls and "sha256:att" not in calls


def test_one_image_out_without_the_other_fails_with_the_recovery(tmp_path: Path) -> None:
    """A docker-tag that stopped halfway: the error points at the recovery section."""
    result = _published(tmp_path, PUBLISHED="clickhouse/loom:1.2.0")
    assert result.returncode == 1 and result.stdout == ""
    assert "only ghcr.io/clickhouse/loom carries 1.2.0" in result.stderr
    assert "docs/development/release-process.md#a-failed-tag-or-signature" in result.stderr
    page = (ROOT / "docs/development/release-process.md").read_text()
    assert "### A failed tag or signature" in page


def test_published_images_fails_on_an_outage_instead_of_rebuilding(tmp_path: Path) -> None:
    result = _published(tmp_path, DOWN="1")
    assert result.returncode == 1 and result.stdout == ""
    assert "the registry answered HTTP 503" in result.stderr


def test_a_run_on_a_published_tag_builds_and_tags_no_image() -> None:
    """The registry dispatch on a released tag: an uncached rebuild would move
    the version's tags onto other bytes."""
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert (
        jobs["preflight"]["outputs"]["images_published"] == "${{ steps.images.outputs.published }}"
    )
    (step,) = [s for s in jobs["preflight"]["steps"] if s.get("id") == "images"]
    assert step["if"] == "${{ !inputs.dry_run }}"
    assert "published_images.sh" in step["run"]
    assert step["env"]["REVISION"] == "${{ github.sha }}"
    assert jobs["docker"]["if"] == "${{ needs.preflight.outputs.images_published != 'true' }}"
    assert "needs.preflight.outputs.images_published != 'true'" in jobs["docker-tag"]["if"]


@pytest.mark.parametrize("name", ["pypi", "npm", "mcp-registry"])
def test_the_registries_publish_after_a_release_that_skipped_its_image_jobs(name: str) -> None:
    """An implicit success() reads every job upstream: on a dispatch on a
    published tag the skipped docker jobs would skip every registry publish."""
    job = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"][name]
    condition = job["if"]
    assert condition.startswith("${{ !cancelled() && needs.github-release.result == 'success' && ")
    assert "github.event_name == 'workflow_dispatch'" in condition


def test_the_workflow_images_are_the_docker_matrix_images() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    matrix = workflow["jobs"]["docker"]["strategy"]["matrix"]["include"]
    assert workflow["env"]["IMAGES"].split() == [i["image"] for i in matrix if "image" in i]


def test_only_sign_and_the_registries_get_an_oidc_token() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert "id-token" not in workflow["permissions"]
    jobs = workflow["jobs"]
    oidc = [name for name, job in jobs.items() if "id-token" in job.get("permissions", {})]
    assert set(oidc) == {"sign", "pypi", "npm", "mcp-registry"}


def test_the_tag_run_signs_the_digests_it_made_and_every_run_verifies_them() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    tag = jobs["docker-tag"]
    assert tag["outputs"]["digests"] == "${{ steps.tag.outputs.digests }}"
    # What the step writes there: test_docker_quickstart_smoke.py runs it.
    steps = {s["id"]: s for s in jobs["sign"]["steps"] if "id" in s}
    assert steps["sign"]["if"] == "${{ needs.docker-tag.result == 'success' }}"
    assert steps["login"]["if"] == steps["sign"]["if"]  # the push of a signature needs it
    assert steps["sign"]["env"]["DIGESTS"] == "${{ needs.docker-tag.outputs.digests }}"
    assert "if" not in steps["verify"]  # runs whether or not this run signed
    assert VERIFY in steps["verify"]["run"]


def _stub_cosign(tmp_path: Path, body: str) -> dict[str, str]:
    """An environment whose cosign logs its arguments to tmp_path/calls, then runs `body`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "cosign").write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/calls"\n{body}\n')
    (bin_dir / "cosign").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


@pytest.mark.parametrize(
    ("cosign", "code", "signed"),
    [("exit 0", 0, ["a@sha256:1", "b@sha256:2"]), ("exit 1", 1, ["a@sha256:1"])],
    ids=["both", "stops at a failure"],
)
def test_the_sign_step_signs_each_digest(
    tmp_path: Path, cosign: str, code: int, signed: list[str]
) -> None:
    """The sign job's signing step, run under `bash -e` with a stub cosign."""
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    (step,) = [s for s in jobs["sign"]["steps"] if s.get("id") == "sign"]
    env = {**_stub_cosign(tmp_path, cosign), "DIGESTS": "a@sha256:1 b@sha256:2"}
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True)
    assert result.returncode == code
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == [f"sign --yes {ref}" for ref in signed]


def test_release_check_verifies_every_image_the_release_signs_with_the_same_script() -> None:
    check = yaml.safe_load((ROOT / ".github/workflows/release-check.yml").read_text())
    (step,) = [s for s in check["jobs"]["signatures"]["steps"] if "run" in s]
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert VERIFY in step["run"]
    assert step["env"]["VERSION"] == "${{ inputs.version }}"
    assert step["env"]["IMAGES"] == release["env"]["IMAGES"]


def _verify(tmp_path: Path, cosign: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts/release/verify_signatures.sh"), "1.2.0", *IMAGES.split()],
        env=_stub_cosign(tmp_path, cosign),
        capture_output=True,
        text=True,
    )


def test_each_image_is_verified_against_the_release_workflows_identity(tmp_path: Path) -> None:
    result = _verify(tmp_path, "exit 0")
    assert result.returncode == 0, result.stderr
    identity = "https://github.com/ClickHouse/loom/.github/workflows/release.yml@refs/tags/v1.2.0"
    issuer = "https://token.actions.githubusercontent.com"
    assert (tmp_path / "calls").read_text().splitlines() == [
        f"verify {image}:1.2.0 --certificate-identity {identity} --certificate-oidc-issuer {issuer}"
        for image in IMAGES.split()
    ]


def test_an_image_that_does_not_verify_fails_with_the_recovery(tmp_path: Path) -> None:
    result = _verify(tmp_path, "exit 1")
    assert result.returncode == 1
    assert (
        "could not verify release.yml's signature on ghcr.io/clickhouse/loom:1.2.0" in result.stdout
    )
    assert "#a-failed-tag-or-signature" in result.stdout
    assert len((tmp_path / "calls").read_text().splitlines()) == 1  # stops at the first


@pytest.mark.parametrize("args", [[], ["1.2.0"]], ids=["nothing", "no image"])
def test_a_missing_version_or_image_is_a_usage_error(args: list[str]) -> None:
    script = ROOT / "scripts/release/verify_signatures.sh"
    result = subprocess.run(["bash", str(script), *args], capture_output=True, text=True)
    assert result.returncode == 2 and "usage:" in result.stderr


@pytest.mark.parametrize(
    ("version", "prerelease", "latest", "tag"),
    [
        ("1.3.0", "false", "true", "latest"),
        ("1.2.4", "false", "false", "release-1.2"),  # a patch to an older line
        ("1.3.0-rc.1", "true", "false", "next"),
    ],
)
def test_npm_latest_follows_the_github_releases_latest(
    tmp_path: Path, version: str, prerelease: str, latest: str, tag: str
) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    assert jobs["github-release"]["outputs"]["latest"] == "${{ steps.latest.outputs.latest }}"
    assert jobs["npm"]["env"]["LATEST"] == "${{ needs.github-release.outputs.latest }}"
    (step,) = [s for s in jobs["npm"]["steps"] if s.get("id") == "publish"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # npm view finds nothing published; npm publish logs its arguments.
    (bin_dir / "npm").write_text(
        f'#!/bin/sh\n[ "$1" = view ] && exit 1\necho "$*" >> "{tmp_path}/calls"\n'
    )
    (bin_dir / "npm").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VERSION": version,
        "PRERELEASE": prerelease,
        "LATEST": latest,
    }
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    (call,) = (tmp_path / "calls").read_text().splitlines()
    assert call.endswith(f"--tag {tag}")
