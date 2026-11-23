"""A sibling read and its parsed views belong to the pass's Git revision."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import ingest_resolve as ir


@pytest.fixture
def revisions(tmp_path: Path) -> tuple[str, list[str], set[str]]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    heads = []
    paths: set[str] = set()
    for name in ("first", "second"):
        texts = {
            "body.py": f"{name} body\n",
            "pyproject.toml": '[project.scripts]\nrepo-cli = "cli:main"\n'
            + f'[tool.setuptools]\npackage-dir = {{"" = "{name}"}}\n',
            "first/cli.py": "def main():\n    return 1\n",
            "second/cli.py": "def main():\n    return 2\n",
            "package.json": '{"workspaces": ["packages/*"]}',
            "packages/pkg/package.json": f'{{"name": "@repo/{name}"}}',
            "go.mod": f"module example.com/{name}\n",
            "tsconfig.json": '{"compilerOptions": {"paths": {"~/*": ["./' + name + '/*"]}}}',
            "src/index.ts": f"export * from './{name}';\n",
            "src/first.ts": "export const first = 1;\n",
            "src/second.ts": "export const second = 2;\n",
        }
        for path, text in texts.items():
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        paths.update(texts)
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                name,
            ],
            check=True,
        )
        heads.append(
            subprocess.check_output(
                ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                text=True,
            ).strip()
        )
    return str(tmp_path / ".git"), heads, paths


class Repo:
    def __init__(self, paths: set[str]) -> None:
        self.paths = paths
        self.reads: list[str] = []

    async def live_text_for_path(self, org: str, ns: str, path: str) -> str:
        self.reads.append(path)
        return "indexed text"

    async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
        return self.paths.intersection(paths)

    async def live_paths_named(self, org: str, ns: str, name: str) -> list[str]:
        return [p for p in self.paths if p.rsplit("/", 1)[-1] == name]


@pytest.mark.parametrize("kind", ["text", "python", "workspace", "go", "tsconfig", "barrel"])
def test_cached_source_views_follow_the_revision(revisions, monkeypatch, kind: str) -> None:
    git_dir, heads, paths = revisions
    repo: Any = Repo(paths)
    monkeypatch.setitem(ir._LIVE_PATHS_CACHE, ("org", "ns"), (time.monotonic(), paths))

    async def read(name: str) -> None:
        if kind == "text":
            assert await ir._package_json_text(repo, "org", "ns", "body.py") == f"{name} body\n"
        elif kind == "python":
            layout = await ir._py_layout(repo, "org", "ns", paths)
            assert name in layout.roots_for("consumer.py")
            assert ({"first", "second"} - {name}).isdisjoint(layout.roots_for("consumer.py"))
        elif kind == "workspace":
            assert await ir._workspace_registry(repo, "org", "ns", paths) == {
                f"@repo/{name}": "packages/pkg",
            }
        elif kind == "go":
            assert await ir._go_module_index(repo, "org", "ns") == [(f"example.com/{name}", "")]
        elif kind == "tsconfig":
            pf: Any = SimpleNamespace(path="src/run.ts", includes=["~/unit"])
            candidates = await ir._tsconfig_candidates(repo, "org", "ns", pf, paths)
            assert f"{name}/unit.ts" in candidates["~/unit"]
            other = "second" if name == "first" else "first"
            assert all(not p.startswith(other + "/") for p in candidates["~/unit"])
        else:
            assert await ir._barrel_targets_of(repo, "org", "ns", "src/index.ts") == (
                f"src/{name}.ts",
            )

    for head, name in [(heads[0], "first"), (heads[1], "second"), (heads[0], "first")]:
        with ir.git_text_source(git_dir, head):
            asyncio.run(read(name))
    assert repo.reads == []


def test_concurrent_revisions_do_not_share_an_inflight_read(revisions) -> None:
    git_dir, heads, paths = revisions
    repo: Any = Repo(paths)

    async def read(head: str) -> str:
        with ir.git_text_source(git_dir, head):
            return await ir._package_json_text(repo, "org", "ns", "body.py")

    async def run() -> list[str]:
        return await asyncio.gather(*(read(head) for head in heads))

    assert asyncio.run(run()) == ["first body\n", "second body\n"]
    assert repo.reads == []


def test_indexed_text_does_not_mask_the_git_source(revisions) -> None:
    git_dir, heads, paths = revisions
    repo: Any = Repo(paths)
    assert asyncio.run(ir._package_json_text(repo, "org", "ns", "body.py")) == "indexed text"
    with ir.git_text_source(git_dir, heads[0]):
        assert asyncio.run(ir._package_json_text(repo, "org", "ns", "body.py")) == "first body\n"
    assert asyncio.run(ir._package_json_text(repo, "org", "ns", "body.py")) == "indexed text"
    assert repo.reads == ["body.py"]


@pytest.mark.parametrize("revision", [0, 1])
def test_first_python_file_keeps_its_revision_entrypoint(revisions, monkeypatch, revision) -> None:
    from server.code.app.index.ingest import ingest_file
    from server.code.store.index import CodeRepo
    from tests.unit.ch_fake import RecordingCH

    git_dir, heads, paths = revisions
    name = ("first", "second")[revision]
    path = f"{name}/cli.py"
    source = f"def main():\n    return {revision + 1}\n".encode()
    monkeypatch.setitem(ir._LIVE_PATHS_CACHE, ("org", "ns"), (time.monotonic(), paths))
    ch = RecordingCH()
    for table in [
        "code_chunks",
        "code_symbols",
        "code_symbol_names",
        "code_edges",
        "code_call_sites_unresolved",
        "code_call_guards",
        "code_test_facets",
    ]:
        ch.on(f"FROM loom.{table}", [])
    repo: Any = CodeRepo(ch)
    with ir.git_text_source(git_dir, heads[revision]):
        stats = asyncio.run(
            ingest_file(
                repo,
                "org",
                "ns",
                path=path,
                source=source,
                commit_sha=heads[revision],
                embedding_model="m",
            )
        )
    assert not stats.error
    facets = [
        row
        for row in ch.inserted("code_test_facets")
        if row["facet_kind"] == "entry_point" and row["tombstoned_at"] is None
    ]
    assert [(row["name"], row["path"], row["line"]) for row in facets] == [
        ("repo-cli", path, 1),
    ]
