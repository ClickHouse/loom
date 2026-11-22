"""Build pinned public historical inputs without model generation or index writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import parse_wheel_filename

from examples.software_factory.evals.flask.historical import FrozenSource, git
from examples.software_factory.evals.flask.public_metadata import public_reproduction
from examples.software_factory.factory.model import Budget, digest
from examples.software_factory.harbor.index import snapshot
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.pricing import cost_basis
from examples.software_factory.harbor.runtime import read, write
from examples.software_factory.harbor.source import (
    check_files,
    command,
    controller_identity,
    server_identity,
    sha,
)

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
    """SCIP receives the exact public implementation, without evaluator files or Git objects."""
    files = source.files("src/flask")
    target.mkdir(parents=True, exist_ok=False)
    tree = target / "source"
    for name, content in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    write(tree / "pyrightconfig.json", {"include": ["src/flask"], "pythonVersion": "3.11"})
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


def create_protocol(config_path: Path, output: Path) -> Path:
    config, lock = read(config_path), read(LOCK)
    endpoint = urlsplit(config["loom_url"])
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
    ):
        raise ValueError("Use an explicit Loom HTTP(S) endpoint without embedded credentials")
    if not re.fullmatch("[a-f0-9]{40}", config["server_commit"]):
        raise ValueError("Pin the full serving build commit")
    assets = Path(config["assets"]).resolve(strict=True)
    asset_receipt = read(assets / "assets.json")
    check_files(asset_receipt["files"])
    images_path = assets / "images.json"
    image_receipt = read(images_path)
    images = image_receipt["images"]
    check_files(image_receipt["artifact_files"])
    # Resolve credential paths without copying their contents into study artifacts.
    credentials = Path(config["credentials_file"]).resolve(strict=True)
    identity = server_identity(
        Path(config["server_source_root"]).resolve(strict=True),
        config["server_container"],
        config["container_source_root"],
    )
    if identity["commit"] != config["server_commit"]:
        raise PermissionError("Serving source checkout and declared build commits differ")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    tasks, statuses = {}, {}
    for pin in lock["tasks"]:
        task_id = pin["task_id"]
        source = FrozenSource(Path(asset_receipt["source_git"]), pin["base_commit"])
        source_files = source.files("src/flask")
        indexes = {}
        for tier in ("treesitter", "scip"):
            scope = config["tasks"][task_id]["indexes"][tier]
            token = Path(scope["reader_token_file"]).resolve(strict=True)
            status = index_status(config["loom_url"], token, config["org"], scope["namespace"])
            expected = {
                "org": config["org"],
                "namespace": scope["namespace"],
                "repo": "pallets/flask",
                "git_head": pin["base_commit"],
                "build_commit": config["server_commit"],
            }
            if any(status.get(k) != v for k, v in expected.items()) or any(
                status.get(k) for k in ("restricted", "abstained", "ignored_args")
            ):
                raise PermissionError("Loom reader scope, source watermark or build differs")
            if status.get("path_prefixes") != ["src/flask"] or not status.get("counts", {}).get(
                "code_files"
            ):
                raise PermissionError("Source-only src/flask index coverage is required")
            counts = compiler_counts(
                config["server_container"], config["org"], scope["namespace"], pin["base_commit"]
            )
            validate_tier(status, counts, tier, pin["base_commit"])
            coverage = source_scope(
                config["server_container"],
                config["org"],
                scope["namespace"],
                source_files,
                pin["base_commit"],
            )
            indexes[tier] = {
                "namespace": scope["namespace"],
                "reader_token_file": str(token),
                "compiler_rows": counts,
                "source_scope": coverage,
            }
            statuses[task_id + ":" + tier] = {
                **expected,
                "status_sha256": digest(status),
                "compiler_rows": counts,
                "source_scope": coverage,
            }
        if indexes["treesitter"]["namespace"] == indexes["scip"]["namespace"]:
            raise PermissionError("The two index tiers require separate frozen namespaces")
        image = images[task_id]["image"]
        actual = json.loads(command(["docker", "image", "inspect", image]))[0]
        if actual["Id"] != image or images[task_id]["tag"] not in actual.get("RepoTags", []):
            raise PermissionError("Dependency image no longer has its recorded build reference")
        record = next(r for r in read(assets / "trusted/tasks.json") if r["instance_id"] == task_id)
        tasks[task_id] = {
            "source_git": asset_receipt["source_git"],
            "indexes": indexes,
            "symbol": pin["symbol"],
            "image": image,
            "evaluation_file": str(assets / "trusted" / (task_id + ".json")),
            "visible_selectors": pin["visible_selectors"],
            "hidden_selectors": hidden_selectors(record),
        }
    if (
        config.get("source_writers_stopped") is not True
        or config.get("backend_generation_disabled") is not True
    ):
        raise PermissionError("Stop source writers and disable backend generation before freezing")
    content = snapshot(config["server_container"], config["org"])
    write(output / "index-content.json", content)
    helper = output / "index-snapshot.py"
    shutil.copyfile(HERE.parents[1] / "harbor/index.py", helper)
    certificate = output / "index-certificate.json"
    write(
        certificate,
        {
            "kind": "operational-source-freeze",
            "scopes": statuses,
            "index_content_sha256": digest(content),
            "server_source_identity": identity,
            "source_writers_stopped": True,
            "backend_generation_disabled": True,
            "assertion_basis": "Operator attestation; content audited before and after every trial",
            "database_write_denial": False,
        },
    )
    rows = schedule(sorted(tasks), seed=lock["schedule_seed"])
    budget = {name: None for name in asdict(Budget())}
    budget.update(output_tokens=128000)
    public_inputs = {
        **asset_receipt["files"],
        **image_receipt["artifact_files"],
        str(assets / "assets.json"): sha(assets / "assets.json"),
        str(images_path): sha(images_path),
        str(helper): sha(helper),
        str(certificate): sha(certificate),
        str(output / "index-content.json"): sha(output / "index-content.json"),
    }
    journal = assets / "dependency-builds.json"
    if journal.exists():
        public_inputs[str(journal)] = sha(journal)
    plan = {
        "schema_version": 1,
        "controller": "harbor",
        "harbor_version": "0.27.0",
        "model_id": lock["model_id"],
        "provider": "Anthropic direct Messages API",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "api_version": "2023-06-01",
        "credentials_file": str(credentials),
        "index_tier": "per-arm",
        "suite": {
            key: lock[key]
            for key in ("id", "title", "repository", "languages", "source_roots", "adapter")
        },
        "org": config["org"],
        "loom_url": config["loom_url"],
        "server_container": config["server_container"],
        "server_commit": config["server_commit"],
        "server_source_identity": identity,
        "controller_source_identity": controller_identity(),
        "historical_manifest": str(assets / "historical.json"),
        "tasks": tasks,
        "reference_controls_file": str(assets / "trusted/tasks.json"),
        "schedule": rows,
        "schedule_sha256": digest(rows),
        "schedule_seed": lock["schedule_seed"],
        "attempt_budget": budget,
        "visible_test_call_limit": None,
        "max_physical_loom_requests": None,
        "index_snapshot_script": str(helper),
        "index_certificate_file": str(certificate),
        "index_certificate_sha256": sha(certificate),
        "public_inputs": public_inputs,
        "cost_basis": cost_basis(lock["model_id"]),
        "success_target": {
            arm: {
                "successes": 9,
                "generation_cost_ratio_max": 0.8,
                "end_to_end_ratio_max": 0.8,
            }
            for arm in ("loom-treesitter", "loom-scip")
        },
        "reporting": {"publish_charts": config.get("publish_charts", True)},
        "judge": config.get(
            "judge",
            {
                "enabled": True,
                "model_id": lock["model_id"],
                "seed": 741,
                "output_tokens": 4096,
            },
        ),
        "files": {},
    }
    plan["public_reproduction"] = public_reproduction(lock, plan)
    write(output / "bootstrap-protocol.json", plan)
    return output / "bootstrap-protocol.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("assets", "build", "protocol"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if args.command == "assets" and args.output:
        result = create_assets(args.output.resolve())
    elif args.command == "build" and args.assets:
        result = build_dependencies(args.assets.resolve())
    elif args.command == "protocol" and args.output and args.config:
        result = create_protocol(args.config.resolve(), args.output.resolve())
    else:
        parser.error(
            "assets requires --output; build requires --assets; "
            "protocol requires --config and --output"
        )
    print(result)


if __name__ == "__main__":
    main()
