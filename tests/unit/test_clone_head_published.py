"""The clone's tip is published.

Running `git rev-parse` per configured repository on a cold `code.repos`
repeats what the ingest pass computes after every fetch, so the pass
publishes each clone's tip as the `clone_head` state row
(detail = branch); the status reads the row and asks git only when there
is none for its branch.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import index_status


def test_the_row_is_the_answer_for_its_branch_only() -> None:
    sha = "a" * 40
    assert (
        index_status.published_clone_head(
            {"clone_head": {"value": sha, "detail": "master"}}, "master"
        )
        == sha
    )
    assert (
        index_status.published_clone_head(
            {"clone_head": {"value": sha, "detail": "main"}}, "master"
        )
        == ""
    )
    assert (
        index_status.published_clone_head(
            {"clone_head": {"value": "abc", "detail": "master"}}, "master"
        )
        == ""
    )
    assert index_status.published_clone_head({}, "master") == ""


def test_the_status_source_prefers_the_row_to_git() -> None:
    src = open(index_status.__file__, encoding="utf-8").read()
    body = src[src.index("async def _clone_lags(") :]
    body = body[: body.index("\nasync def ", 1)]
    assert "published_clone_head(state, cfg.git_branch) or await gitrepo.head_sha(" in body


def test_the_pass_publishes_the_tip_after_its_fetch() -> None:
    from server.code.app.index import ingest_pass as w

    src = open(w.__file__, encoding="utf-8").read()
    i_fetch = src.index("await fetch_entry(cfg, git_dir, repo, result)")
    i_head = src.index("head = await gitrepo.head_sha(git_dir, cfg.git_branch)", i_fetch)
    i_pub = src.index("await _publish_clone_head(repo, org, ns, head, cfg.git_branch)", i_head)
    assert i_fetch < i_head < i_pub


@pytest.mark.asyncio
async def test_publish_is_soft_and_names_the_branch() -> None:
    from server.code.app.index import pass_fetch as ingest_fetch

    class _Repo:
        def __init__(self) -> None:
            self.rows: list[tuple[str, str, str, str, str]] = []

        async def set_state(
            self, org: str, ns: str, key: str, value: str, detail: str = ""
        ) -> None:
            self.rows.append((org, ns, key, value, detail))

    repo = _Repo()
    await ingest_fetch._publish_clone_head(repo, "o", "code-x", "b" * 40, "master")
    assert repo.rows == [("o", "code-x", "clone_head", "b" * 40, "master")]

    class _Broken:
        async def set_state(self, *a: Any, **kw: Any) -> None:
            raise RuntimeError("no ClickHouse")

    await ingest_fetch._publish_clone_head(_Broken(), "o", "code-x", "b" * 40, "master")  # no raise
