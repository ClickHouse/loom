"""scripts/release/release_clickhouse.sh picks the ClickHouse release a bundle
names: a published bundle's own, a dispatch's, for a final the newest candidate
on its commit with a published bundle, or the newest release. Its sibling
scripts are stubbed; git is real."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit import git_repo

ROOT = Path(__file__).resolve().parents[2]
NEWEST = "clickhouse/clickhouse-server:26.10.1.1"

# A published release is a file $STUB_PUBLISHED/<version> holding its pin;
# "error" in it is a failed lookup.
STUBS = {
    "scripts/release/published_bundle.sh": """
f="$STUB_PUBLISHED/$1"
[ -f "$f" ] || exit 3
[ "$(cat "$f")" != error ] || exit 1
cp "$f" "$2/loom-stack.tar.gz"
""",
    "scripts/release/build_stack_bundle.py": """
import re
import sys
EXACT_CLICKHOUSE = re.compile(r"ch:[a-z]+")
if __name__ == "__main__":
    print(open(sys.argv[2]).read().strip())
""",
    "scripts/release/refuse_downgrade.sh": """
echo "$1" >> "$STUB_PUBLISHED/refuse.log"
[ "$1" != ch:older ] || exit 1
""",
    "dev/clickhouse_release.sh": f"echo {NEWEST}\n",
}


Git = Callable[..., str]
Resolve = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with the script, its stubbed siblings and one commit."""
    repo = tmp_path / "repo"
    script = repo / "scripts" / "release" / "release_clickhouse.sh"
    script.parent.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "release" / "release_clickhouse.sh", script)
    shutil.copy(ROOT / "scripts" / "release" / "prepare.py", script.parent)  # its candidates
    for path, body in STUBS.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(body)
    git_repo.git(repo, "init", "-q")
    git_repo.git(repo, "commit", "-q", "--allow-empty", "-m", "release")
    return repo


@pytest.fixture
def git(repo: Path) -> Git:
    """git in `repo`."""
    return lambda *args: git_repo.git(repo, *args)


@pytest.fixture
def resolve(tmp_path: Path, repo: Path, git: Git) -> Resolve:
    """Tag the commit (a tag's pin, when given, is its published bundle's) and run the script."""
    published = tmp_path / "published"
    published.mkdir()

    def run(
        version: str, tags: dict[str, str], annotated: bool = False, **env: str
    ) -> subprocess.CompletedProcess[str]:
        for tag, pin in tags.items():
            git("tag", *(["-a", "-m", tag] if annotated else []), tag)
            if pin:
                (published / tag[1:]).write_text(pin)
        return subprocess.run(
            ["bash", "scripts/release/release_clickhouse.sh", version],
            cwd=repo,
            env={**os.environ, "STUB_PUBLISHED": str(published), **env},
            capture_output=True,
            text=True,
        )

    return run


def test_a_re_run_keeps_its_published_bundles_release(resolve: Resolve) -> None:
    result = resolve("1.2.0", {"v1.2.0": "ch:a"}, CLICKHOUSE_IMAGE="ch:b")
    assert result.stdout == "ch:a\n"
    assert "::warning::clickhouse_image=ch:b ignored" in result.stderr


def test_a_final_keeps_its_last_candidates_release(resolve: Resolve) -> None:
    result = resolve("1.2.0", {"v1.2.0-rc.9": "ch:old", "v1.2.0-rc.10": "ch:soaked"})
    assert result.stdout == "ch:soaked\n", result.stderr


def test_a_stray_candidate_without_a_bundle_is_passed_over(resolve: Resolve) -> None:
    tags = {"v1.2.0-rc.2": "ch:soaked", "v1.2.0-rc.3": "", "v1.2.0-rc.3-x": "ch:other"}
    result = resolve("1.2.0", tags)
    assert result.stdout == "ch:soaked\n"
    assert "::notice::v1.2.0-rc.3 published no bundle: passed over" in result.stderr


def test_a_final_finds_its_annotated_candidate(resolve: Resolve) -> None:
    """A release tag is annotated (signed); its commit is the one it points at."""
    result = resolve("1.2.0", {"v1.2.0-rc.1": "ch:soaked"}, annotated=True)
    assert result.stdout == "ch:soaked\n", result.stderr


def test_a_dispatch_must_name_one_release(resolve: Resolve) -> None:
    result = resolve("1.2.0", {}, CLICKHOUSE_IMAGE="ch:B4D")
    assert result.returncode == 1
    assert "not one ClickHouse release: 'ch:B4D'" in result.stderr


def test_a_dispatch_names_the_release_over_the_candidates(resolve: Resolve) -> None:
    result = resolve("1.2.0", {"v1.2.0-rc.1": "ch:soaked"}, CLICKHOUSE_IMAGE="ch:named")
    assert result.stdout == "ch:named\n"
    assert "::notice::clickhouse_image=ch:named is used in place of any release" in result.stderr
    assert "(v1.2.0-rc.1)" in result.stderr


@pytest.mark.parametrize(
    ("version", "tags"),
    [
        ("1.2.0", {}),  # a final with no candidate
        ("1.2.0", {"v1.2.0-rc.1": ""}),  # a candidate without a bundle
        ("1.2.0-rc.2", {"v1.2.0-rc.1": "ch:soaked"}),  # a candidate resolves anew
    ],
)
def test_otherwise_the_newest_release_is_named(
    resolve: Resolve, version: str, tags: dict[str, str]
) -> None:
    result = resolve(version, tags)
    assert result.stdout == f"{NEWEST}\n", result.stderr


def test_a_new_pin_is_checked_for_a_downgrade(resolve: Resolve, tmp_path: Path) -> None:
    resolve("1.2.0", {"v1.2.0-rc.1": "ch:soaked"})
    assert (tmp_path / "published" / "refuse.log").read_text() == "ch:soaked\n"


@pytest.mark.parametrize("tags", [{"v1.2.0": "error"}, {"v1.2.0-rc.1": "error"}])
def test_a_failed_lookup_stops_the_job(resolve: Resolve, tags: dict[str, str]) -> None:
    result = resolve("1.2.0", tags)
    assert result.returncode == 1 and result.stdout == ""


def test_only_candidates_on_this_commit_count(resolve: Resolve, git: Git) -> None:
    resolve("1.2.0-rc.1", {"v1.2.0-rc.1": "ch:soaked"})  # tags the first commit
    git("commit", "-q", "--allow-empty", "-m", "a later commit")
    assert resolve("1.2.0", {}).stdout == f"{NEWEST}\n"


def test_a_published_bundles_own_release_skips_the_downgrade_check(
    resolve: Resolve, tmp_path: Path
) -> None:
    resolve("1.2.0", {"v1.2.0": "ch:a"})
    assert not (tmp_path / "published" / "refuse.log").exists()


def test_a_dispatched_release_is_checked_for_a_downgrade(resolve: Resolve, tmp_path: Path) -> None:
    result = resolve("1.2.0", {}, CLICKHOUSE_IMAGE="ch:named")
    assert (tmp_path / "published" / "refuse.log").read_text() == "ch:named\n"
    assert "::notice::clickhouse_image" not in result.stderr  # no candidates: nothing in its place


def test_a_dispatched_candidate_says_nothing_of_other_candidates(resolve: Resolve) -> None:
    result = resolve("1.2.0-rc.2", {"v1.2.0-rc.1": "ch:soaked"}, CLICKHOUSE_IMAGE="ch:named")
    assert result.stdout == "ch:named\n"
    assert "::notice::clickhouse_image" not in result.stderr


def test_a_refused_downgrade_stops_the_job(resolve: Resolve) -> None:
    result = resolve("1.2.0", {}, CLICKHOUSE_IMAGE="ch:older")
    assert result.returncode == 1 and result.stdout == ""


def test_a_candidate_list_git_cannot_read_stops_the_job(resolve: Resolve, tmp_path: Path) -> None:
    result = resolve("1.2.0", {}, GIT_DIR=str(tmp_path / "no-repository"))
    assert result.returncode != 0 and result.stdout == ""
    assert "not a git repository" in result.stderr
