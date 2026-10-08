"""A code.* input that names a commit takes only a commit sha.

These values reach `git` as revision arguments: `code.bisect_candidates`'
`bad_sha` at once (`git log -1 --format=%ct <bad_sha>`), `code.scip_ingest`'s
`commit_sha` / `base_sha` and `code.reindex`'s `git_head` later, from the
index-state watermark they become. A value git reads as an option —
`--output=<path>` makes `git log` write a file — must never get that far.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server.code.api.models_history import CodeBisectCandidatesInput
from server.code.api.models_ingest import CodeReindexInput, CodeScipIngestInput

_OPTIONS = ["--output=/tmp/loom-pwned", "-p", "HEAD", "main", "abc", "a" * 65, "12345zz"]


def _scip(**kw: Any) -> CodeScipIngestInput:
    return CodeScipIngestInput(batch_id="b" * 32, seq=0, total_parts=1, payload="{}", **kw)


BUILDERS = {
    "code.bisect_candidates.bad_sha": lambda v: CodeBisectCandidatesInput(bad_sha=v),
    "code.scip_ingest.commit_sha": lambda v: _scip(commit_sha=v),
    "code.scip_ingest.base_sha": lambda v: _scip(base_sha=v),
    "code.reindex.git_head": lambda v: CodeReindexInput(git_head=v),
}


@pytest.mark.parametrize("field", sorted(BUILDERS))
@pytest.mark.parametrize("value", _OPTIONS)
def test_anything_but_a_sha_is_refused(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as e:
        BUILDERS[field](value)
    assert [err["loc"] for err in e.value.errors()] == [(field.rsplit(".", 1)[1],)]


@pytest.mark.parametrize("field", sorted(BUILDERS))
def test_a_sha_passes_lower_cased_and_empty_stays_empty(field: str) -> None:
    attr = field.rsplit(".", 1)[1]
    for given, stored in (
        ("e" * 40, "e" * 40),
        (" ABCDEF1 ", "abcdef1"),
        ("0" * 64, "0" * 64),  # a SHA-256 repository
    ):
        assert getattr(BUILDERS[field](given), attr) == stored
    if attr != "git_head":  # the others default to "" and mean "not given"
        assert getattr(BUILDERS[field](""), attr) == ""


def test_the_bisect_option_never_reaches_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over HTTP the refusal is the named 422 and no git process starts."""
    from server.code.app.index import gitrepo
    from server.settings import settings
    from server.transport.http import build_router

    calls: list[tuple[str, ...]] = []

    async def _git(git_dir: str, *args: str, **_k: Any) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(gitrepo, "_git", _git)
    monkeypatch.setattr(settings, "auth_required", False)
    app = FastAPI()
    app.include_router(build_router())
    r = TestClient(app).post(
        "/v1/code.bisect_candidates",
        json={"symbols": ["DB::f"], "bad_sha": "--output=/tmp/loom-pwned"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["errors"][0]["loc"][-1] == "bad_sha"
    assert calls == []
