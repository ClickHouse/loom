"""Build pinned public historical inputs without model generation or index writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path

import httpx
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

from examples.software_factory.evals.flask.historical import FrozenSource, git
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.runtime import read, write
from examples.software_factory.harbor.source import (
    check_files,
    command,
    sha,
)
from examples.software_factory.harbor.source_tree import source_files

HERE = Path(__file__).resolve().parent
LOCK = HERE / "suite.lock.json"


def download(url: str, expected: str, target: Path) -> None:
    response = httpx.get(url, follow_redirects=True, timeout=120)
    response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != expected:
        raise PermissionError("Public dataset differs from its pinned digest")
    with target.open("xb") as stream:
        stream.write(response.content)


def select_records(rows: list[dict], pins: list[dict]) -> list[dict]:
    selected = []
    for pin in pins:
        matches = [r for r in rows if r["instance_id"] == pin["task_id"]]
        if len(matches) != 1 or digest(matches[0]) != pin["record_sha256"]:
            raise PermissionError("Public task record differs from the task lock")
        row = matches[0]
        passing, failing = json.loads(row["PASS_TO_PASS"]), json.loads(row["FAIL_TO_PASS"])
        if (
            row["repo"] != "pallets/flask"
            or row["base_commit"] != pin["base_commit"]
            or not passing
            or not failing
            or not pin["visible_selectors"]
            or not set(pin["visible_selectors"]) <= set(test_functions(passing))
        ):
            raise PermissionError("Task source or visible test separation differs from the lock")
        selected.append(row)
    return selected


def test_functions(selectors):
    """Run complete functions, including every published parameterization."""
    return list(dict.fromkeys(node.split("[", 1)[0] for node in selectors))


def hidden_selectors(record):
    return test_functions(json.loads(record["FAIL_TO_PASS"]) + json.loads(record["PASS_TO_PASS"]))


def supplemental_records(lock):
    rows, paths = [], []
    for pin in lock.get("supplemental_records", []):
        path = (HERE / pin["file"]).resolve(strict=True)
        if not path.is_relative_to(HERE) or path.is_symlink() or sha(path) != pin["sha256"]:
            raise PermissionError("Supplemental public task differs from its pinned input")
        rows.append(read(path))
        paths.append(path)
    return rows, paths


def public_task(row: dict) -> dict:
    return {
        "task_id": row["instance_id"],
        "repository": row["repo"],
        "base_commit": row["base_commit"],
        "problem": row["problem_statement"],
    }


def evaluator_task(row: dict) -> dict:
    return {key: row[key] for key in ("base_commit", "test_patch")}


def dependency_recipe(lock: dict, pin: dict) -> tuple[str, str, str]:
    def requirements(names):
        lines = []
        for name in names:
            hashes = lock["package_sha256"][name]
            if not hashes or any(not re.fullmatch("[a-f0-9]{64}", h) for h in hashes):
                raise ValueError("Every dependency needs a published artifact hash")
            lines.append(name + " \\\n" + " \\\n".join("    --hash=sha256:" + h for h in hashes))
        return "\n".join(lines) + "\n"

    dockerfile = (
        "FROM " + lock["python_image"] + "\n"
        "COPY build-requirements.txt runtime-requirements.txt /tmp/\n"
        "COPY packages /packages\n"
        "RUN python -m pip install --no-index --find-links=/packages "
        "--no-cache-dir --no-deps --require-hashes "
        "-r /tmp/build-requirements.txt\n"
        "RUN python -m pip install --no-index --find-links=/packages "
        "--no-cache-dir --no-deps --no-build-isolation "
        "--require-hashes -r /tmp/runtime-requirements.txt\n"
        "RUN python -m pip freeze --all > /runtime-packages.lock\n"
    )
    return dockerfile, requirements(pin["build_requirements"]), requirements(pin["requirements"])


def package_artifact(metadata: dict, hashes: list[str], architecture: str) -> dict:
    """Choose a hash-pinned Python 3.11 artifact for the pinned Linux image."""
    machine = {"arm64": "aarch64", "amd64": "x86_64"}[architecture]
    platforms = [
        "manylinux_2_17_" + machine,
        "manylinux2014_" + machine,
        "manylinux_2_28_" + machine,
    ]
    tags = list(cpython_tags((3, 11), abis=["cp311"], platforms=platforms))
    tags += list(compatible_tags((3, 11), interpreter="cp311", platforms=platforms))
    ranks = {tag: i for i, tag in enumerate(tags)}
    wheels, sources = [], []
    for item in metadata["urls"]:
        if item["digests"]["sha256"] not in hashes:
            continue
        filename = item["filename"]
        if Path(filename).name != filename:
            raise PermissionError("Package artifact filename leaves its cache")
        if item["packagetype"] == "bdist_wheel":
            matching = [ranks[tag] for tag in parse_wheel_filename(filename)[3] if tag in ranks]
            if matching:
                wheels.append((min(matching), filename, item))
        elif item["packagetype"] == "sdist":
            sources.append((filename, item))
    if wheels:
        return min(wheels, key=lambda value: value[:2])[2]
    if sources:
        return min(sources, key=lambda value: value[0])[1]
    raise PermissionError("No hash-pinned package artifact matches the test image")


def cache_packages(lock: dict, pin: dict, folder: Path, architecture: str, cache: Path) -> dict:
    destination = folder / "packages"
    destination.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    files = {}
    for requirement in dict.fromkeys(pin["build_requirements"] + pin["requirements"]):
        name, version = requirement.split("==")
        response = httpx.get(f"https://pypi.org/pypi/{name}/{version}/json", timeout=120)
        response.raise_for_status()
        item = package_artifact(response.json(), lock["package_sha256"][requirement], architecture)
        expected, filename = item["digests"]["sha256"], item["filename"]
        target = cache / filename
        if target.exists():
            if sha(target) != expected:
                raise PermissionError("Cached package artifact differs from its pinned hash")
        else:
            download(item["url"], expected, target)
        copied = destination / filename
        shutil.copyfile(target, copied)
        files[str(copied)] = expected
    return files


def create_assets(output: Path) -> Path:
    import pyarrow.parquet as parquet

    lock = read(LOCK)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    attribution = output / "attribution"
    attribution.mkdir()
    for item in lock["attributions"]:
        download(item["url"], item["sha256"], attribution / item["name"])
    dataset = lock["dataset"]
    dataset_path = output / "tasks.parquet"
    url = (
        "https://huggingface.co/datasets/"
        + dataset["repository"]
        + "/resolve/"
        + dataset["revision"]
        + "/"
        + dataset["file"]
    )
    download(url, dataset["sha256"], dataset_path)
    additional, additional_paths = supplemental_records(lock)
    rows = select_records(parquet.read_table(dataset_path).to_pylist() + additional, lock["tasks"])
    trusted = output / "trusted"
    trusted.mkdir(mode=0o700)
    write(trusted / "tasks.json", rows)
    write(output / "historical.json", {"tasks": [public_task(r) for r in rows]})
    mirror = output / "flask.git"
    command(["git", "init", "--bare", str(mirror)])
    sources = {}
    for pin, row in zip(lock["tasks"], rows, strict=True):
        git(mirror, "fetch", "--depth=1", lock["repository_url"], pin["base_commit"])
        source = FrozenSource(mirror, pin["base_commit"])
        stage_compiler_source(source, output / "compiler" / pin["task_id"])
        files = source.files()
        fixtures = source.fixtures()
        sources[pin["task_id"]] = {
            "commit": pin["base_commit"],
            "tree": git(mirror, "rev-parse", pin["base_commit"] + "^{tree}").decode().strip(),
            "files_sha256": digest(files),
            "fixtures_sha256": digest(fixtures),
            "license_sha256": {
                name: hashlib.sha256(content.encode()).hexdigest()
                for name, content in files.items()
                if name.startswith("LICENSE")
            },
            "test_requirements": files["requirements/tests.in"],
        }
        write(trusted / (pin["task_id"] + ".json"), evaluator_task(row))
        folder = output / "dependencies" / pin["task_id"]
        folder.mkdir(parents=True)
        for name, content in zip(
            ("Dockerfile", "build-requirements.txt", "runtime-requirements.txt"),
            dependency_recipe(lock, pin),
            strict=True,
        ):
            (folder / name).write_text(content)
    write(output / "source-provenance.json", sources)
    inputs = [
        LOCK,
        dataset_path,
        output / "historical.json",
        *trusted.glob("*.json"),
        *additional_paths,
    ]
    inputs.append(output / "source-provenance.json")
    inputs += list(attribution.iterdir())
    inputs += [p for p in (output / "compiler").rglob("*") if p.is_file()]
    inputs += [p for p in (output / "dependencies").rglob("*") if p.is_file()]
    receipt = {
        "kind": "pinned-public-factory-inputs",
        "dataset_url": url,
        "source_git": str(mirror),
        "provider_requests": 0,
        "loom_writes": 0,
        "files": {str(p): sha(p) for p in inputs},
    }
    write(output / "assets.json", receipt)
    return output / "assets.json"


def stage_compiler_source(source: FrozenSource, target: Path) -> dict:
    """SCIP receives the pinned public text roots, without evaluator files or Git objects."""
    roots = read(LOCK)["source_roots"]
    files = source_files(source.mirror, source.commit, roots)
    target.mkdir(parents=True, exist_ok=False)
    tree = target / "source"
    for name, content in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    write(tree / "pyrightconfig.json", {"include": roots, "pythonVersion": "3.11"})
    write(target / "environment.json", [])
    (target / "allowed-paths.txt").write_text("".join(name + "\n" for name in sorted(files)))
    receipt = {
        "base_commit": source.commit,
        "source_sha256": digest(files),
        "paths_sha256": digest(sorted(files)),
        "file_count": len(files),
        "dependency_environment": [],
        "source_root": str(tree),
    }
    write(target / "source.json", receipt)
    return receipt


def build_dependencies(assets: Path) -> Path:
    receipt = read(assets / "assets.json")
    check_files(receipt["files"])
    target = assets / "images.json"
    journal = assets / "dependency-builds.json"
    if target.exists() or journal.exists():
        raise FileExistsError("Inspect recorded builds and use a fresh assets directory")
    lock = read(LOCK)
    command(["docker", "pull", lock["python_image"]])
    base = json.loads(command(["docker", "image", "inspect", lock["python_image"]]))[0]
    images, builds, artifacts = {}, [], {}
    for pin in lock["tasks"]:
        folder = assets / "dependencies" / pin["task_id"]
        tag = "loom-factory-deps:" + pin["task_id"] + "-" + uuid.uuid4().hex[:12]
        builds.append({"task_id": pin["task_id"], "tag": tag, "status": "started"})
        write(journal, builds)
        artifacts.update(
            cache_packages(lock, pin, folder, base["Architecture"], assets / "packages")
        )
        command(
            [
                "docker",
                "build",
                "--network=none",
                "--tag",
                tag,
                "--iidfile",
                str(folder / "image.id"),
                str(folder),
            ]
        )
        image = (folder / "image.id").read_text().strip()
        actual = json.loads(command(["docker", "image", "inspect", tag]))[0]
        if actual["Id"] != image:
            raise PermissionError("Built dependency image differs from its receipt")
        images[pin["task_id"]] = {
            "image": image,
            "tag": tag,
            "architecture": actual["Architecture"],
        }
        builds[-1].update(status="completed", **images[pin["task_id"]])
        write(journal, builds)
    write(
        target,
        {"images": images, "artifact_files": artifacts, "provider_requests": 0, "loom_writes": 0},
    )
    return target


def index_status(endpoint: str, token_file: Path, org: str, namespace: str) -> dict:
    response = httpx.post(
        endpoint.rstrip("/") + "/v1/code.index_status",
        headers={"Authorization": "Bearer " + token_file.read_text().strip()},
        json={"org": org, "namespace": namespace, "brief": False},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("data", body)


def compiler_counts(container: str, org: str, namespace: str, commit: str) -> dict:
    code = """import asyncio,json,sys
from server.core.ch import get_client,set_query_tag
async def main():
    params=json.load(sys.stdin)
    client=get_client();await client.connect()
    set_query_tag('bench.factory.bootstrap.compiler_proof')
    scope=("org={org:String} AND namespace={namespace:String} "
           "AND tier='scip' AND tombstoned_at IS NULL")
    python="indexed_commit={commit:String} AND startsWith(scip_symbol,'scip-python ')"
    try:
        definitions=await client.query_all(
            'SELECT count() AS compiler_definitions,countIf('+python+') AS python_definitions '
            'FROM loom.code_symbols FINAL WHERE '+scope,params)
        references=await client.query_all(
            'SELECT count() AS compiler_references,countIf(indexed_commit={commit:String} '
            'AND from_symbol_id IN (SELECT symbol_id FROM loom.code_symbols FINAL WHERE '
            +scope+' AND '+python+')) AS python_references FROM loom.code_edges FINAL WHERE '
            +scope,params)
        print(json.dumps({k:int(v) for k,v in {**definitions[0],**references[0]}.items()}))
    finally: await client.disconnect()
asyncio.run(main())
"""
    return json.loads(
        command(
            ["docker", "exec", "-i", container, "/opt/venv/bin/python", "-c", code],
            data=json.dumps({"org": org, "namespace": namespace, "commit": commit}).encode(),
        )
    )


def validate_tier(status: dict, counts: dict, tier: str, commit: str) -> None:
    keys = {
        "compiler_definitions",
        "compiler_references",
        "python_definitions",
        "python_references",
    }
    if set(counts) != keys or any(type(value) is not int or value < 0 for value in counts.values()):
        raise PermissionError("Complete nonnegative compiler row counts are required")
    if (
        counts["compiler_definitions"] < counts["python_definitions"]
        or counts["compiler_references"] < counts["python_references"]
    ):
        raise PermissionError("Python compiler rows exceed the complete compiler row count")
    if tier == "treesitter":
        if status.get("scip_commit") or status.get("scip_commit_py") or any(counts.values()):
            raise PermissionError("Tree-sitter scope contains compiler-tier evidence")
    elif tier == "scip":
        if status.get("scip_commit_py") != commit or any(
            counts.get(key, 0) <= 0 for key in ("python_definitions", "python_references")
        ):
            raise PermissionError("Python SCIP watermark or real compiler rows are unverified")
        tiers = status.get("compiler_tiers") or []
        expected = {
            "producer": "python",
            "tool": "scip-python",
            "watermark_key": "scip_commit_py",
            "commit": commit,
        }
        if not any(all(t.get(key) == value for key, value in expected.items()) for t in tiers):
            raise PermissionError("Compiler status does not confirm the pinned Python tier")
    else:
        raise ValueError("Unknown index tier")


def validate_source_scope(tables: dict, files: dict[str, str], commit: str) -> dict:
    expected = {
        name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()
    }
    indexed = tables["code_files"]
    if (
        len(indexed) != len(expected)
        or {row["path"]: row["content_hash"] for row in indexed} != expected
        or any(row["indexed_commit"] != commit for row in indexed)
    ):
        raise PermissionError("Indexed source files differ from the complete exact-base allowlist")
    proof = {}
    for table in ("code_chunks", "code_symbols", "code_edges"):
        rows = tables[table]
        if any(row["path"] not in files or row["commits"] != [commit] for row in rows):
            raise PermissionError(
                "Indexed declarations or references leave the exact-base allowlist"
            )
        proof[table] = {
            "rows": sum(int(row["rows"]) for row in rows),
            "paths_sha256": digest(sorted(row["path"] for row in rows)),
        }
    return {
        "verified": True,
        "base_commit": commit,
        "source_sha256": digest(files),
        "paths_sha256": digest(sorted(files)),
        "file_count": len(files),
        "tables": proof,
    }


def source_scope(container: str, org: str, namespace: str, files: dict, commit: str) -> dict:
    code = """import asyncio,json,sys
from server.core.ch import get_client,set_query_tag
async def main():
    params=json.load(sys.stdin);client=get_client();await client.connect()
    set_query_tag('bench.factory.bootstrap.source_scope')
    scope='org={org:String} AND namespace={namespace:String} AND tombstoned_at IS NULL'
    try:
        tables={'code_files':await client.query_all(
            'SELECT path,content_hash,indexed_commit FROM loom.code_files FINAL WHERE '
            +scope,params)}
        for table in ('code_chunks','code_symbols','code_edges'):
            tables[table]=await client.query_all(
                'SELECT path,groupUniqArray(indexed_commit) AS commits,count() AS rows FROM loom.'
                +table+' FINAL WHERE '+scope+' GROUP BY path',params)
        print(json.dumps(tables))
    finally: await client.disconnect()
asyncio.run(main())
"""
    tables = json.loads(
        command(
            ["docker", "exec", "-i", container, "/opt/venv/bin/python", "-c", code],
            data=json.dumps({"org": org, "namespace": namespace}).encode(),
        )
    )
    return validate_source_scope(tables, files, commit)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("assets", "build"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assets", type=Path)
    args = parser.parse_args()
    if args.command == "assets" and args.output:
        result = create_assets(args.output.resolve())
    elif args.command == "build" and args.assets:
        result = build_dependencies(args.assets.resolve())
    else:
        parser.error("assets requires --output; build requires --assets")
    print(result)


if __name__ == "__main__":
    main()
