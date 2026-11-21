"""Prepare source-only Python SCIP artifacts with pinned tools and bounded Docker jobs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
import uuid
from pathlib import Path

from .historical import FrozenSource

ROOT = Path(__file__).resolve().parents[4]
LOCK = Path(__file__).with_name("suite.lock.json")
NPM_LOCK = ROOT / "scripts/code-toolchains/package-lock.json"
CONVERTER = ROOT / "scripts/scip_harness/scip_to_delta.py"


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run(command, *, data=None, timeout=300, env=None):
    return subprocess.run(
        command, input=data, capture_output=True, check=True, timeout=timeout, env=env
    ).stdout


def compiler_revisions(assets: Path, materialization: dict) -> list[dict]:
    """Each solver base and any additional chronological history revisions."""
    lock = _read(LOCK)
    source_git = _read(assets / "assets.json")["source_git"]
    images = _read(assets / "images.json")["images"]
    revisions = []
    for pin in lock["tasks"]:
        revisions.append(
            dict(
                key=pin["task_id"] + ":B",
                task_id=pin["task_id"],
                revision="B",
                base_commit=pin["base_commit"],
                source_git=source_git,
                dependency_task_id=pin["task_id"],
                dependency_image=images[pin["task_id"]]["image"],
            )
        )
    dependency = "pallets__flask-4935"
    for task_id in sorted(materialization["tasks"]):
        fixture = materialization["fixtures"][task_id]
        if materialization["tasks"][task_id]["image"] != images[dependency]["image"]:
            raise PermissionError("Authored compiler dependencies differ from their pinned base")
        for label in ("A", "B"):
            revisions.append(
                dict(
                    key=task_id + ":" + label,
                    task_id=task_id,
                    revision=label,
                    base_commit=fixture["revisions"][label]["commit"],
                    source_git=fixture["source_git"],
                    dependency_task_id=dependency,
                    dependency_image=images[dependency]["image"],
                )
            )
    expected = len(lock["tasks"]) + 2 * len(materialization["tasks"])
    if (
        not revisions
        or len(revisions) != expected
        or len({row["base_commit"] for row in revisions}) != expected
    ):
        raise PermissionError("Compiler preparation requires every distinct frozen revision")
    for row in revisions:
        if not re.fullmatch(r"[a-f0-9]{40}", row["base_commit"]):
            raise PermissionError("Compiler revision is not a full Git commit")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", row["dependency_image"]):
            raise PermissionError("Compiler dependency image must have an immutable identity")
    return revisions


def tool_pins(architecture: str) -> dict:
    lock, npm = _read(LOCK), _read(NPM_LOCK)
    if (
        lock["compiler_tools"]["python_language_version"] != "3.11"
        or lock["compiler_tools"]["dependency_environment"] != []
    ):
        raise PermissionError("Compiler requires the pinned Python 3.11 empty type environment")
    dockerfile = (ROOT / "Dockerfile").read_text()
    node = lock["compiler_tools"]["node"]
    if f"ARG NODE_VERSION={node}\n" not in dockerfile:
        raise PermissionError("Suite and container Node pins differ")
    suffix = {"arm64": "ARM64", "amd64": "X64"}[architecture]
    checksum = re.search(r"^ARG NODE_SHA256_" + suffix + r"=([a-f0-9]{64})$", dockerfile, re.M)
    if checksum is None:
        raise PermissionError("Node architecture has no pinned archive hash")
    package = npm["packages"]["node_modules/@sourcegraph/scip-python"]
    if package["version"] != lock["compiler_tools"]["scip_python"]:
        raise PermissionError("Suite and npm SCIP versions differ")
    return dict(
        node=node,
        scip_python=package["version"],
        architecture=architecture,
        node_url=f"https://nodejs.org/dist/v{node}/node-v{node}-linux-"
        + {"amd64": "x64", "arm64": "arm64"}[architecture]
        + ".tar.xz",
        node_sha256=checksum[1],
        npm_lock_sha256=_sha(NPM_LOCK),
        suite_lock_sha256=_sha(LOCK),
        compiler_recipe_sha256=_sha(Path(__file__).with_name("compiler.py")),
        converter_sha256=_sha(CONVERTER),
        uv_lock_sha256=_sha(ROOT / "uv.lock"),
        protobuf_bindings_sha256=_sha(ROOT / "scripts/scip_harness/vendor/scip_pb2.py"),
        wire_schema_sha256=_sha(ROOT / "server/code/app/index/scip_delta.py"),
    )


def _verified_bytes(path: Path, integrity: str) -> bool:
    algorithm, expected = integrity.split("-", 1)
    return path.is_file() and hashlib.new(
        algorithm, path.read_bytes()
    ).digest() == base64.b64decode(expected, validate=True)


def _download(url, integrity, target, candidate=None):
    if target.exists():
        if not _verified_bytes(target, integrity):
            raise PermissionError("Existing compiler download differs from its published integrity")
        return
    if candidate is not None and candidate.exists():
        if not _verified_bytes(candidate, integrity):
            raise PermissionError("Cached compiler download differs from its published integrity")
        shutil.copyfile(candidate, target)
    else:
        with urllib.request.urlopen(url, timeout=60) as response:
            value = response.read(256 * 1024 * 1024 + 1)
        if len(value) > 256 * 1024 * 1024:
            raise ValueError("Compiler archive exceeds its download bound")
        target.write_bytes(value)
        if not _verified_bytes(target, integrity):
            raise PermissionError("Compiler download differs from its published integrity")


def _package_root(archive_path, version):
    with tarfile.open(archive_path) as archive:
        roots = set()
        for member in archive:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise PermissionError("Unsafe compiler package archive member")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise PermissionError("Compiler package requires one archive root")
        root = roots.pop()
        metadata = archive.extractfile(root + "/package.json")
        if metadata is None or json.load(metadata)["version"] != version:
            raise PermissionError("Compiler package version differs from the npm lock")
        return root


def prepare_tool_archives(output: Path, pins: dict, cache: dict) -> dict:
    """Verify/download release archives without executing host Node or package scripts."""
    output.mkdir(parents=True, exist_ok=True)
    node = output / "node.tar.xz"
    sri = "sha256-" + base64.b64encode(bytes.fromhex(pins["node_sha256"])).decode()
    candidate = cache.get("node_archives", {}).get(pins["architecture"])
    _download(pins["node_url"], sri, node, Path(candidate) if candidate else None)
    packages = []
    for path, package in sorted(_read(NPM_LOCK)["packages"].items()):
        if not path or package.get("optional"):
            continue
        if not path.startswith("node_modules/") or ".." in Path(path).parts:
            raise PermissionError("Unsafe compiler package path")
        integrity = package["integrity"]
        algorithm, encoded = integrity.split("-", 1)
        hexadecimal = base64.b64decode(encoded, validate=True).hex()
        archive = output / (hashlib.sha256(integrity.encode()).hexdigest() + ".tgz")
        package_cache = cache.get("package_archives", {}).get(integrity)
        candidate = Path(package_cache) if package_cache else None
        if candidate is None and cache.get("npm_cache"):
            candidate = Path(cache["npm_cache"]) / "_cacache/content-v2" / algorithm
            candidate = candidate / hexadecimal[:2] / hexadecimal[2:4] / hexadecimal[4:]
        _download(package["resolved"], integrity, archive, candidate)
        packages.append(
            dict(
                path=path,
                archive=archive.name,
                integrity=integrity,
                archive_root=_package_root(archive, package["version"]),
            )
        )
    _write(output / "packages.json", packages)
    return dict(
        node_archive=str(node),
        packages=packages,
        files={str(p): _sha(p) for p in output.iterdir() if p.is_file()},
    )


_INSTALL = """import json,tarfile,shutil
from pathlib import Path
root=Path('/opt/compiler-tools')
with tarfile.open('/inputs/node.tar.xz') as archive:
    archive.extractall(root,filter='data')
node=next(root.glob('node-*'))
node.rename(root/'node')
for package in json.loads(Path('/inputs/packages.json').read_text()):
    target=root/'toolchains'/package['path']
    target.mkdir(parents=True)
    with tarfile.open('/inputs/'+package['archive']) as archive:
        for item in archive:
            parts=Path(item.name).parts
            if not parts or parts[0]!=package['archive_root'] or '..' in parts:
                raise ValueError('Unexpected npm archive member')
            item.name=str(Path(*parts[1:])) if len(parts)>1 else '.'
            archive.extract(item,target,filter='data')
wrapper=root/'toolchains/bin/scip-python'
wrapper.parent.mkdir(parents=True)
wrapper.write_text('#!/bin/sh\\nexec /opt/compiler-tools/node/bin/node '
    '/opt/compiler-tools/toolchains/node_modules/@sourcegraph/scip-python/index.js "$@"\\n')
wrapper.chmod(0o755)
"""

_RUNNER = """import hashlib,json,subprocess,sys
from pathlib import Path
from compiler_recipe import index_command
root=Path('/job');spec=json.loads((root/'compiler-input.json').read_text())
assert sys.version_info[:3]==(3,11,18)
installed=json.loads(subprocess.check_output([sys.executable,'-m','pip','list','--format=json']))
actual={p['name'].lower().replace('_','-'):p['version'] for p in installed}
assert actual==spec['packages'],(actual,spec['packages'])
cache=Path('/cache');assert not any(cache.iterdir())
node=subprocess.check_output(['node','--version'],text=True).strip()
scip=json.loads(Path('/opt/compiler-tools/toolchains/node_modules/@sourcegraph/scip-python/package.json').read_text())['version']
assert node=='v'+spec['node'] and scip==spec['scip_python']
def hashes():
    return {str(p.relative_to(root/'source')):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root/'source').rglob('*') if p.is_file()}
before=hashes()
command=index_command(root,Path('/opt/compiler-tools/toolchains/bin/scip-python'),base_commit=spec['base_commit'])
(root/'compiler-command.json').write_text(json.dumps(command))
status=subprocess.run(command,cwd=root/'source').returncode
after=hashes()
(root/'compiler-environment.json').write_text(json.dumps(dict(python=sys.version,node=node,scip_python=scip,packages=actual,cache_initially_empty=True,network='none',source_before=before,source_after=after)))
assert before==after
raise SystemExit(status)
"""


def compiler_dockerfile(dependency_image: str, tools_image: str | None = None) -> str:
    if dependency_image.startswith("sha256:"):
        raise ValueError("Dockerfile requires a local tag verified against the frozen image ID")
    prefix = "FROM " + dependency_image + "\n"
    if tools_image is None:
        prefix += "COPY archives /inputs\nCOPY install.py /install.py\nRUN python /install.py\n"
    else:
        prefix += f"COPY --from={tools_image} /opt/compiler-tools /opt/compiler-tools\n"
    return prefix + (
        "COPY compiler_recipe.py runner.py /opt/compiler-runner/\n"
        "ENV PATH=/opt/compiler-tools/node/bin:/opt/compiler-tools/toolchains/bin:$PATH\n"
        'CMD ["python","/opt/compiler-runner/runner.py"]\n'
    )


def compiler_container_command(image: str, project: Path, name: str, *, owner: str) -> list[str]:
    return [
        "docker",
        "create",
        "--name",
        name,
        "--label=loom.factory.compiler.owner=" + owner,
        "--network=none",
        "--read-only",
        "--cpus=2",
        "--memory=2g",
        "--pids-limit=128",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs=/tmp:rw,nosuid,size=256m",
        "--mount",
        f"type=bind,src={project},dst=/job",
        "--mount",
        f"type=bind,src={project / 'source'},dst=/job/source,readonly",
        "--mount",
        f"type=bind,src={project / 'worker-cache'},dst=/cache",
        "--env=HOME=/cache/home",
        "--env=XDG_CACHE_HOME=/cache/xdg",
        "--env=NODE_OPTIONS=--max-old-space-size=1536",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        image,
    ]


def _build_images(output, archives, images, receipt):
    built, first = {}, None
    for dependency, dependency_ref in sorted(images.items()):
        actual = _run(["docker", "image", "inspect", "--format", "{{.Id}}", dependency_ref])
        if actual.decode().strip() != dependency:
            raise PermissionError("Qualified compiler dependency tag changed image identity")
        folder = output / ("image-" + dependency.removeprefix("sha256:")[:12])
        folder.mkdir()
        if first is None:
            shutil.copytree(archives, folder / "archives")
            (folder / "install.py").write_text(_INSTALL)
        shutil.copyfile(Path(__file__).with_name("compiler.py"), folder / "compiler_recipe.py")
        (folder / "runner.py").write_text(_RUNNER)
        (folder / "Dockerfile").write_text(compiler_dockerfile(dependency_ref, first))
        tag = "loom-factory-compiler:" + uuid.uuid4().hex
        owned = dict(
            tag=tag,
            id=None,
            dependency_image=dependency,
            dependency_ref=dependency_ref,
            status="building",
            owner=receipt["owner"],
            label="loom.factory.compiler.owner",
        )
        receipt["owned_images"].append(owned)
        _write(output / "compiler-artifacts.json", receipt)
        _run(
            [
                "docker",
                "build",
                "--network=none",
                "--pull=false",
                "--label",
                "loom.factory.compiler.owner=" + receipt["owner"],
                "--tag",
                tag,
                "--iidfile",
                str(folder / "image.id"),
                str(folder),
            ],
            timeout=600,
        )
        image = (folder / "image.id").read_text().strip()
        built[dependency] = image
        owned.update(id=image, status="complete")
        _write(output / "compiler-artifacts.json", receipt)
        first = first or tag
    return built


_VALIDATE = """import json,sys,importlib.metadata
from pathlib import Path
from scripts.scip_harness.scip_to_delta import _ensure_scip_pb2
from server.code.app.index.scip_delta import ScipDelta
p=json.load(sys.stdin);root=Path(p['project']);allowed=set((root/'allowed-paths.txt').read_text().splitlines())
delta=json.loads((root/'delta.json').read_text());ScipDelta.model_validate(delta)
assert delta['commit']==p['base_commit'] and delta['files'] and not delta.get('deleted_files')
for f in delta['files']:
    assert f['path'] in allowed
    for item in f.get('symbols',[])+f.get('edges',[]):
        assert not item.get('path') or item['path'] in allowed
index=_ensure_scip_pb2().Index();index.ParseFromString((root/'index.scip').read_bytes())
assert index.metadata.tool_info.name=='scip-python'
assert index.metadata.tool_info.version==p['scip_python']
paths=[d.relative_path for d in index.documents]
assert paths and len(paths)==len(set(paths)) and set(paths)<=allowed
prefix='scip-python python flask '+p['base_commit']+' '
assert any(o.symbol.startswith(prefix) for d in index.documents for o in d.occurrences)
symbols=sum(len(f.get('symbols',[])) for f in delta['files'])
references=sum(len(f.get('edges',[])) for f in delta['files'])
assert symbols>0 and references>0
print(json.dumps(dict(schema_validated=True,symbols=symbols,references=references,
    raw_documents=len(paths),
    raw_definitions=sum(bool(o.symbol_roles&1) for d in index.documents for o in d.occurrences),
    raw_references=sum(bool(o.symbol) and not(o.symbol_roles&1)
        for d in index.documents for o in d.occurrences),
    unindexed_noncode_paths=sorted(allowed-set(paths)),
    normalizer_python=sys.version,protobuf_version=importlib.metadata.version('protobuf'))))
"""


def _expected_packages(task_id):
    pin = next(row for row in _read(LOCK)["tasks"] if row["task_id"] == task_id)
    return {
        name.lower().replace("_", "-"): version
        for name, version in (
            item.split("==", 1) for item in pin["requirements"] + pin["build_requirements"]
        )
    }


def _normalizer_python(explicit):
    executable = explicit or os.environ.get("FACTORY_LOOM_PYTHON")
    if not executable or not Path(executable).is_file():
        raise ValueError(
            "The controller must provide FACTORY_LOOM_PYTHON from its locked Loom venv"
        )
    return str(Path(executable).absolute())


def _normalize(project, revision, pins, python):
    env = dict(
        PATH=os.defpath,
        PYTHONPATH=str(ROOT) + os.pathsep + str(ROOT / "sdk/python"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    command = [
        python,
        str(CONVERTER),
        "--index",
        str(project / "index.scip"),
        "--commit",
        revision["base_commit"],
        "--source-root",
        str(project / "source"),
        "--changed-files",
        str(project / "allowed-paths.txt"),
        "--producer",
        "scip-python",
        "--full",
        "--dry-run",
        str(project / "delta.json"),
    ]
    (project / "converter.stdout").write_bytes(_run(command, env=env))
    (project / "converter-command.json").write_text(json.dumps(command))
    proof = json.loads(
        _run(
            [python, "-c", _VALIDATE],
            data=json.dumps(
                dict(
                    project=str(project),
                    base_commit=revision["base_commit"],
                    scip_python=pins["scip_python"],
                )
            ).encode(),
            env=env,
        )
    )
    _write(project / "validation.json", proof)
    return proof


def _cached_raw(cache, revision, source_sha, pins, project):
    for entry in cache.get("revisions", {}).values():
        identity = {**revision, "source_sha256": source_sha}
        fields = ("base_commit", "source_sha256", "dependency_image")
        if not all(entry.get(key) == identity[key] for key in fields):
            continue
        if any(
            entry.get(key) != pins[key]
            for key in (
                "node",
                "scip_python",
                "node_sha256",
                "npm_lock_sha256",
                "compiler_recipe_sha256",
            )
        ):
            raise PermissionError("Cached compiler provenance differs from the pinned recipe")
        if (
            not {"index.scip", "compiler-environment.json", "compiler-command.json", "source.json"}
            <= entry["files"].keys()
        ):
            raise PermissionError("Cached compiler artifact has incomplete provenance")
        for name, record in entry["files"].items():
            path = Path(record["path"])
            if _sha(path) != record["sha256"]:
                raise PermissionError("Cached compiler artifact changed")
            if name in {"index.scip", "compiler-environment.json", "compiler-command.json"}:
                shutil.copyfile(path, project / name)
        environment = _read(project / "compiler-environment.json")
        source = _read(entry["files"]["source.json"]["path"])
        expected = {
            str(path.relative_to(project / "source")): _sha(path)
            for path in (project / "source").rglob("*")
            if path.is_file()
        }
        if (
            environment["node"] != "v" + pins["node"]
            or environment["scip_python"] != pins["scip_python"]
            or not environment["python"].startswith("3.11.18")
            or environment["network"] != "none"
            or environment["cache_initially_empty"] is not True
            or environment["packages"] != _expected_packages(revision["dependency_task_id"])
            or environment["source_before"] != expected
            or environment["source_after"] != expected
            or source["source_sha256"] != source_sha
            or source["base_commit"] != revision["base_commit"]
        ):
            raise PermissionError("Cached compiler source or execution evidence differs")
        return True
    return False


def prepare_compiler_artifacts(
    assets: Path,
    materialization: dict,
    output: Path,
    *,
    loom_python=None,
    cache_file: Path | None = None,
    owner: str | None = None,
) -> dict:
    """Compile nine frozen source trees; no model calls, server writes, or repository tests."""
    from .bootstrap import stage_compiler_source

    assets, output = Path(assets).resolve(strict=True), Path(output).resolve()
    python = _normalizer_python(loom_python)
    owner = owner or os.environ.get("FACTORY_OWNER") or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", owner):
        raise ValueError("Compiler resource owner requires 32 lowercase hex characters")
    revisions = compiler_revisions(assets, materialization)
    image_rows = _read(assets / "images.json")["images"]
    architectures = {image_rows[row["dependency_task_id"]]["architecture"] for row in revisions}
    if len(architectures) != 1:
        raise PermissionError("Compiler dependencies require one frozen Docker architecture")
    pins = tool_pins(architectures.pop())
    cache = _read(cache_file) if cache_file is not None else {}
    output.mkdir(parents=True, exist_ok=False)
    receipt = dict(
        schema_version=1,
        language="python",
        producer="scip-python",
        producer_count_fields={
            "definitions": "python_definitions",
            "references": "python_references",
        },
        status="preparing",
        owner=owner,
        pins=pins,
        revisions={},
        owned_images=[],
        containers=[],
        provider_requests=0,
        loom_writes=0,
    )
    _write(output / "compiler-artifacts.json", receipt)
    pending = []
    for revision in revisions:
        source = FrozenSource(Path(revision["source_git"]), revision["base_commit"])
        project = output / (revision["task_id"] + "-" + revision["revision"])
        staged = stage_compiler_source(source, project)
        reused = _cached_raw(cache, revision, staged["source_sha256"], pins, project)
        if reused:
            proof = _normalize(project, revision, pins, python)
            receipt["revisions"][revision["key"]] = {
                **revision,
                **staged,
                **proof,
                "reused_raw": True,
                "project": str(project),
            }
        else:
            pending.append((revision, project, staged))
    if pending:
        archives = output / "tool-archives"
        receipt["tool_archives"] = prepare_tool_archives(archives, pins, cache)
        receipt["node_archives"] = {pins["architecture"]: str(archives / "node.tar.xz")}
        receipt["package_archives"] = {
            item["integrity"]: str(archives / item["archive"])
            for item in receipt["tool_archives"]["packages"]
        }
        built = _build_images(
            output,
            archives,
            {
                row[0]["dependency_image"]: image_rows[row[0]["dependency_task_id"]]["tag"]
                for row in pending
            },
            receipt,
        )
        for revision, project, staged in pending:
            packages = _expected_packages(revision["dependency_task_id"])
            _write(project / "compiler-input.json", {**pins, **revision, "packages": packages})
            (project / "worker-cache").mkdir()
            command = compiler_container_command(
                built[revision["dependency_image"]],
                project,
                "loom-factory-compiler-" + uuid.uuid4().hex,
                owner=owner,
            )
            owned = dict(
                id=None,
                name=command[3],
                command=command,
                removed=False,
                owner=owner,
                label="loom.factory.compiler.owner",
            )
            receipt["containers"].append(owned)
            _write(output / "compiler-artifacts.json", receipt)
            container = _run(command).decode().strip()
            owned["id"] = container
            _write(output / "compiler-artifacts.json", receipt)
            try:
                try:
                    log = _run(["docker", "start", "-a", container])
                except subprocess.CalledProcessError as error:
                    (project / "compiler.stdout").write_bytes(error.stdout or b"")
                    (project / "compiler.stderr").write_bytes(error.stderr or b"")
                    raise
                (project / "compiler.stdout").write_bytes(log)
                exit_code = _run(
                    ["docker", "inspect", "--format", "{{.State.ExitCode}}", container]
                )
                if int(exit_code) != 0:
                    raise RuntimeError("Compiler container failed; inspect retained output")
            finally:
                _run(["docker", "rm", "-f", container], timeout=30)
                owned["removed"] = True
                _write(output / "compiler-artifacts.json", receipt)
            proof = _normalize(project, revision, pins, python)
            receipt["revisions"][revision["key"]] = {
                **revision,
                **staged,
                **proof,
                "reused_raw": False,
                "project": str(project),
            }
    for row in receipt["revisions"].values():
        row["producer"] = "scip-python"
        project = Path(row["project"])
        row["files"] = {
            name: dict(path=str(project / name), sha256=_sha(project / name))
            for name in (
                "index.scip",
                "delta.json",
                "source.json",
                "allowed-paths.txt",
                "environment.json",
                "compiler-environment.json",
                "compiler-command.json",
                "validation.json",
            )
        }
        row.update(
            {
                key: pins[key]
                for key in (
                    "node",
                    "scip_python",
                    "node_sha256",
                    "npm_lock_sha256",
                    "compiler_recipe_sha256",
                )
            }
        )
    receipt["status"] = "complete"
    _write(output / "compiler-artifacts.json", receipt)
    return receipt
