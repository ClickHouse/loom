"""Build isolated, deterministic Git fixtures and separate private grading inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from examples.software_factory.evals.flask.historical import FrozenSource, git
from examples.software_factory.factory.model import digest

from .evaluation import evaluator_spec
from .fixtures import load_manifest, prepare_case

LOCK = Path(__file__).parents[1] / "suite.lock.json"
FAMILY = "authored-simulated-memory-history"
FIXTURE_DATE = "2000-01-01T00:00:00+00:00"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _git(repo: Path, *args: str, data: bytes | None = None, index: Path | None = None) -> bytes:
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Flask fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Flask fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": FIXTURE_DATE,
        "GIT_COMMITTER_DATE": FIXTURE_DATE,
    }
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "gc.auto=0",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        input=data,
        capture_output=True,
        check=True,
        timeout=120,
        env=env,
    ).stdout


def _revision(repo, parent, files, message, index):
    _git(repo, "read-tree", parent, index=index)
    for path, body in sorted(files.items()):
        metadata = _git(repo, "ls-tree", parent, "--", path).split(b"\t", 1)[0].split()
        if len(metadata) != 3 or metadata[0] not in {b"100644", b"100755"}:
            raise PermissionError("Fixture source path is not an existing regular file")
        oid = _git(repo, "hash-object", "-w", "--stdin", data=body.encode()).decode().strip()
        _git(repo, "update-index", "--cacheinfo", metadata[0].decode(), oid, path, index=index)
    tree = _git(repo, "write-tree", index=index).decode().strip()
    commit = _git(repo, "commit-tree", tree, "-p", parent, data=(message + "\n").encode())
    return commit.decode().strip(), tree


def _trees(repo, commit):
    entries = {}
    for record in _git(repo, "ls-tree", "-rz", commit).split(b"\0"):
        if record:
            meta, path = record.split(b"\t", 1)
            entries[path] = meta
    return entries


def materialize_cases(assets_path: Path, output_path: Path) -> dict:
    """Return three task configs and combined six-task manifests without inference or ingestion."""
    assets, output = Path(assets_path).resolve(strict=True), Path(output_path).resolve()
    asset_receipt = json.loads((assets / "assets.json").read_text())
    original_repo = Path(asset_receipt["source_git"]).resolve(strict=True)
    if not original_repo.is_dir() or output.exists():
        raise FileExistsError(
            "Use an existing local source repository and a fresh output directory"
        )
    manifest = load_manifest()
    original = FrozenSource(original_repo, manifest["base_commit"])
    original_files = original.files("src/flask")
    images = json.loads((assets / "images.json").read_text())["images"]
    image = images[manifest["dependency_task_id"]]["image"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise PermissionError("A qualified dependency image identity is required")
    pin = next(
        row
        for row in json.loads(LOCK.read_text())["tasks"]
        if row["task_id"] == manifest["dependency_task_id"]
    )
    public = json.loads((assets / "historical.json").read_text())["tasks"]
    controls = json.loads((assets / "trusted/tasks.json").read_text())
    public = [{**row, "scenario_family": "historical-issue"} for row in public]
    source_refs = git(original_repo, "for-each-ref", "--format=%(refname) %(objectname)")
    output.mkdir(parents=True)
    (output / "private").mkdir(mode=0o700)
    receipt = dict(
        schema_version=1,
        kind="offline-flask-memory-materialization",
        status="preparing",
        origin_base_commit=original.commit,
        source_git=str(original_repo),
        origin_refs_sha256=hashlib.sha256(source_refs).hexdigest(),
        fixture_git_dates="Fixed synthetic metadata for deterministic IDs; not elapsed history",
        provider_requests=0,
        loom_requests=0,
        index_writes=0,
        tasks={},
        fixtures={},
        files={},
    )
    _write(output / "materialization.json", receipt)
    for case in manifest["cases"]:
        case_id = case["case_id"]
        if not re.fullmatch(r"[a-z0-9-]+", case_id):
            raise PermissionError("Unsafe fixture task identifier")
        prepared = prepare_case(case_id, original_files)
        repo = output / "repos" / (case_id + ".git")
        repo.mkdir(parents=True)
        _git(repo, "init", "--bare", "--object-format=sha1", "--initial-branch=fixture-b")
        _git(
            repo,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--depth=1",
            str(original_repo),
            original.commit + ":refs/heads/origin-base",
        )
        base_tree = _trees(repo, original.commit)
        revisions, parent = {}, original.commit
        index = output / "private" / (case_id + ".index")
        try:
            for label in ("A", "B"):
                commit, tree = _revision(
                    repo,
                    parent,
                    prepared["source_revisions"][label],
                    f"Authored policy fixture {label}: {case_id}",
                    index,
                )
                _git(repo, "update-ref", "refs/heads/fixture-" + label.lower(), commit)
                actual = FrozenSource(repo, commit).files("src/flask")
                if digest(actual) != prepared["source_sha256"][label]:
                    raise PermissionError("Materialized source differs from the public overlay")
                entries = _trees(repo, commit)
                if entries.keys() != base_tree.keys() or any(
                    meta != entries[path]
                    for path, meta in base_tree.items()
                    if path.decode() not in original_files
                ):
                    raise PermissionError("A fixture changed nonimplementation base files")
                revisions[label] = dict(commit=commit, tree=tree, source_sha256=digest(actual))
                parent = commit
        finally:
            index.unlink(missing_ok=True)
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/fixture-b")
        _git(repo, "fsck", "--strict", "--no-dangling")
        base = revisions["B"]["commit"]
        private = evaluator_spec(case_id)
        reference_patch = _git(
            repo, "diff", "--binary", base, original.commit, "--", "src/flask"
        ).decode()
        evaluation_file = output / "private" / (case_id + ".json")
        _write(
            evaluation_file,
            dict(
                base_commit=base,
                test_patch=private["test_patch"],
                evaluator_sha256=private["evaluator_sha256"],
                scenario_family=FAMILY,
            ),
        )
        control = dict(
            instance_id=case_id,
            repo="pallets/flask",
            base_commit=base,
            problem_statement=prepared["problem"],
            scenario_family=FAMILY,
            patch=reference_patch,
            test_patch=private["test_patch"],
            FAIL_TO_PASS=json.dumps(private["hidden_selectors"]),
            PASS_TO_PASS=json.dumps(pin["visible_selectors"]),
        )
        controls.append(control)
        row = dict(
            task_id=case_id,
            repository="pallets/flask",
            base_commit=base,
            problem=prepared["problem"],
            scenario_family=FAMILY,
            origin_base_commit=original.commit,
        )
        public.append(row)
        fixture_file = output / "public" / "fixtures" / (case_id + ".json")
        fixture = {key: value for key, value in prepared.items() if key != "source_revisions"}
        fixture.update(source_git=str(repo), revisions=revisions)
        _write(fixture_file, fixture)
        receipt["fixtures"][case_id] = dict(
            fixture_file=str(fixture_file),
            source_git=str(repo),
            revisions=revisions,
            history_counts=prepared["history_counts"],
        )
        receipt["tasks"][case_id] = dict(
            source_git=str(repo),
            symbol=case["function"],
            image=image,
            evaluation_file=str(evaluation_file),
            visible_selectors=pin["visible_selectors"],
            hidden_selectors=private["hidden_selectors"] + pin["visible_selectors"],
            scenario_family=FAMILY,
            public_fixture_file=str(fixture_file),
        )
    if len({row["task_id"] for row in public}) != len(public):
        raise PermissionError("Duplicate public task identities")
    if git(original_repo, "for-each-ref", "--format=%(refname) %(objectname)") != source_refs:
        raise PermissionError("The original source repository references changed")
    public_path, private_path = output / "public/tasks.json", output / "private/tasks.json"
    _write(public_path, dict(tasks=public))
    _write(private_path, controls)
    receipt.update(
        status="complete",
        historical_manifest=str(public_path),
        reference_controls_file=str(private_path),
        source_refs_unchanged=True,
    )
    receipt["files"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.rglob("*"))
        if path.is_file() and path != output / "materialization.json"
    }
    _write(output / "materialization.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = materialize_cases(args.assets, args.output)
    print(json.dumps(dict(status=receipt["status"], tasks=list(receipt["tasks"]))))


if __name__ == "__main__":
    main()
