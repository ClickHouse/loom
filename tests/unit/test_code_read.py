from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from server.code.app.read import _parse_range
from server.core.errors import ValidationError


@pytest.mark.parametrize("value", ["../file.py:2", "/file.py:1", "file.py:0", "file.py:9-2"])
def test_read_refuses_invalid_ranges(value: str) -> None:
    with pytest.raises(ValidationError):
        _parse_range(value)


@pytest.mark.parametrize("source_changed", [False, True])
@pytest.mark.asyncio
async def test_read_keeps_file_commit_while_reconciliation_checks_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_changed: bool
) -> None:
    from server.code.api.models_read import CodeReadInput
    from server.code.app import read
    from server.code.app.index import gitrepo
    from server.code.app.index.reconcile import find_stale_paths

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }

    def git(*args: str, input: str | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env=env,
            input=input,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    # commit-tree creates fixture history without invoking commit hooks.
    git("init", "--quiet")
    (tmp_path / "src").mkdir()
    raw = b"fn function() {}\n"
    (tmp_path / "src/lib.rs").write_bytes(raw)
    (tmp_path / "README.md").write_text("first\n")
    git("add", ".")
    old = git("commit-tree", git("write-tree"), input="initial fixture\n")
    (tmp_path / "README.md").write_text("unrelated change\n")
    if source_changed:
        (tmp_path / "src/lib.rs").write_text("fn function() { changed(); }\n")
    git("add", ".")
    head = git("commit-tree", git("write-tree"), "-p", old, input="next fixture\n")
    blob = gitrepo.blob_sha(raw)

    class Repo:
        async def file_identities(self, org, namespace):
            return [("src/lib.rs", old, blob)]

        async def file_rows_for_paths(self, org, namespace, paths):
            assert paths == ["src/lib.rs"]
            return {
                "src/lib.rs": {
                    "indexed_commit": old,
                    "blob_sha": blob,
                    "chunk_count": 1,
                    "lang": "rust",
                }
            }

    async def as_of(*_):
        return {}

    async def present(_):
        return str(tmp_path / ".git")

    traces: list[dict] = []
    monkeypatch.setattr(read, "CodeRepo", Repo)
    monkeypatch.setattr(read, "require_code_intel", lambda: None)
    monkeypatch.setattr(read, "resolve_code_identity", lambda org, ns: (org, ns))
    monkeypatch.setattr(read, "repo_for_namespace", lambda *_: None)
    monkeypatch.setattr(read, "index_as_of", as_of)
    monkeypatch.setattr(read, "present_dir", present)
    monkeypatch.setattr(read, "schedule_code_trace", lambda **row: traces.append(row))
    state = await find_stale_paths(Repo(), str(tmp_path / ".git"), "org", "source", head)
    result = await read.handle_code_read(
        CodeReadInput(org="org", namespace="source", path="src/lib.rs", start_line=1, end_line=1)
    )
    assert old != head
    assert state.stale == (["src/lib.rs"] if source_changed else [])
    assert state.commits_failed == 0
    assert state.by_tree == 1
    source = result.results[0]
    assert source.status == "complete"
    assert source.indexed_commit == old
    assert source.windows[0].text == raw.decode()
    (trace,) = traces
    assert result.trace_run_id and trace["trace_run_id"] == result.trace_run_id
    assert trace["org"] == "org" and trace["ns"] == "source"
    assert trace["op"] == "code.read" and trace["returned_paths"] == ["src/lib.rs"]
