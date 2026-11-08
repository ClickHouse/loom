"""Code-index errors a viewer reads (`code.scip_status`, `code.repos`, history reads,
`/v1/health?include=mirrors`) name the error's kind; the driver's text is logged.
CH-free."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.core.errors import BackendUnavailableError

_DRIVER_TEXT = "Code: 210. Connection refused (ch-internal-7:9440) INSERT INTO loom.code_symbols"


class _Repo:
    def __init__(self) -> None:
        self.marked: list[dict[str, Any]] = []

    async def pending_scip_batches(self, per_namespace: int) -> list[dict[str, Any]]:
        return [{"org": "o", "namespace": "code-r", "batch_id": "b1", "commit_sha": "c" * 40}]

    async def scip_parts(self, org: str, ns: str, batch_id: str) -> list[dict[str, Any]]:
        raise RuntimeError(_DRIVER_TEXT)

    async def upsert_scip_batch(self, org: str, ns: str, batch_id: str, **kw: Any) -> None:
        self.marked.append(kw)


async def test_a_failed_scip_batch_records_the_kind_only() -> None:
    from server.code.workers import scip_apply

    repo = _Repo()
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        await scip_apply.run_pass()
    assert [m["status"] for m in repo.marked] == ["failed"]
    assert repo.marked[0]["error"] == "RuntimeError"


async def test_a_rejected_scip_batch_tells_the_uploader_why() -> None:
    """Loom's words about the uploader's own batch reach `code.scip_status`."""
    from server.code.workers import scip_apply

    class _BadParts(_Repo):
        async def scip_parts(self, org: str, ns: str, batch_id: str) -> list[dict[str, Any]]:
            return [{"seq": 3, "payload": "{}", "payload_hash": "0" * 64}]

    class _NotADelta(_Repo):
        async def scip_parts(self, org: str, ns: str, batch_id: str) -> list[dict[str, Any]]:
            return [{"seq": 0, "payload": '{"delta_version": "x"}', "payload_hash": ""}]

    for repo, want in (
        (_BadParts(), "VALIDATION_FAILED: part 3 hash mismatch"),
        (_NotADelta(), "VALIDATION_FAILED: the batch is not a SCIP delta"),
    ):
        with patch("server.code.store.index.CodeRepo", return_value=repo):
            await scip_apply.run_pass()
        assert repo.marked[0]["error"].startswith(want)


async def test_a_mirror_job_that_raised_reads_as_its_kind() -> None:
    from server.code.app.index import bootstrap

    async def _job() -> str:
        raise RuntimeError(_DRIVER_TEXT)

    task = asyncio.ensure_future(_job())
    with pytest.raises(RuntimeError):
        await task
    assert bootstrap._job_failure(task) == "RuntimeError"

    async def _loom_job() -> str:
        raise BackendUnavailableError("ch-internal-7:9440 refused")

    task = asyncio.ensure_future(_loom_job())
    with pytest.raises(BackendUnavailableError):
        await task
    # `/v1/health?include=mirrors` has no token: a Loom error's code, not its message.
    assert bootstrap._job_failure(task) == "BACKEND_UNAVAILABLE"


class _Cfg:
    def __init__(self, git_dir: str) -> None:
        self.git_dir = git_dir
        self.git_remote = "https://git.example/acme/app.git"
        self.git_branch = "main"
        self.github_token = ""
        self.classification = "standard"


_GIT_STDERR = b"fatal: unable to access 'https://other-org.example/x.git': /srv/loom/mirrors/x"


async def test_a_failed_clone_or_fetch_names_the_exit_not_gits_stderr(
    monkeypatch, tmp_path
) -> None:
    """The reason reaches `/v1/health?include=mirrors` without a token: git's stderr
    (host paths, remote URLs) is logged, the reason is fixed."""
    from types import SimpleNamespace

    from server.code.app.index import bootstrap, gitrepo, sandbox

    async def _job(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(returncode=128, stderr=_GIT_STDERR)

    monkeypatch.setattr(sandbox, "run_job", _job)
    cfg = _Cfg(str(tmp_path / "app.git"))
    assert await bootstrap.clone_mirror(cfg) == "git clone failed (exit 128)"

    async def _fetch(*_a: Any, **_k: Any) -> None:
        raise gitrepo.GitError(_GIT_STDERR.decode())

    monkeypatch.setattr(gitrepo, "fetch", _fetch)
    assert await bootstrap.fetch_mirror(cfg) == "git fetch failed"


def test_stored_index_errors_scip_errors_and_details_are_cut_on_read() -> None:
    """Rows an older build wrote keep `<Kind>: <text>`: every reader cuts them."""
    from server.code.app import ingest
    from server.code.app.index.ingest_stats import shown_index_error

    assert shown_index_error(f"edges: RuntimeError: {_DRIVER_TEXT}") == "edges: RuntimeError"
    assert shown_index_error("parse: RuntimeError") == "parse: RuntimeError"
    assert shown_index_error("edges: Code: 241. DB::Exception: x") == "edges"
    # A file whose chunks and edges both failed stores both stages, `; `-joined.
    assert shown_index_error("chunk: RuntimeError; edges: ValueError") == (
        "chunk: RuntimeError; edges: ValueError"
    )
    # Only Loom's stages lead a part: driver text holding `; ` keeps nothing after it.
    older = "edges: RuntimeError: x; Received from db-1.internal:9000: y"
    assert shown_index_error(older) == "edges: RuntimeError"
    assert shown_index_error("edges: ValueError: bad; s3cr3t") == "edges: ValueError"
    busy = "edges: OVERLOADED: every connection is busy; retry shortly"
    assert shown_index_error(busy) == busy
    skipped = "skipped: 9000000 bytes > code_ingest_max_file_bytes (2000000)"
    assert shown_index_error(skipped) == skipped  # Loom's own words, kept whole
    assert ingest._shown_batch_error(f"RuntimeError: {_DRIVER_TEXT}") == "RuntimeError"
    assert ingest._shown_batch_error("Code: 241. DB::Exception: x") == (
        "the apply failed; the error is in the server log"
    )
    assert ingest._shown_batch_error("") == ""
    from server.code.domain.index.state import shown_detail as shown

    assert shown("scip_commit", "base unknown: harness exited 3 in /srv/x") == "base unknown"
    assert shown("bootstrap_stage", '{"done": 3, "total": 9}') == '{"done": 3, "total": 9}'
    # A reason is cut on the bootstrap stage only; another key's detail is as stored.
    reason = '{"reason": "fatal: /srv/loom/x.git"}'
    assert shown("bootstrap_stage", reason) == '{"reason": "git clone failed"}'
    assert shown("last_refresh_at", reason) == reason
    assert shown("scip_commit", "b1f0c0de-batch") == "b1f0c0de-batch"  # as stored


async def test_code_index_errors_cuts_a_row_an_older_build_wrote(monkeypatch) -> None:
    from server.code.api.models_status import CodeIndexErrorsInput
    from server.code.app import index_status

    class _Repo:
        async def index_errors(self, org: str, ns: str, *, limit: int) -> Any:
            return [{"path": "a.py", "index_error": f"edges: RuntimeError: {_DRIVER_TEXT}"}], 1

    monkeypatch.setattr(index_status, "require_code_intel", lambda: None)
    monkeypatch.setattr(index_status, "resolve_code_identity", lambda org, ns: ("o", "code-r"))
    monkeypatch.setattr(index_status, "CodeRepo", _Repo)
    out = await index_status.handle_code_index_errors(CodeIndexErrorsInput())
    assert [r.error for r in out.rows] == ["edges: RuntimeError"]


async def test_a_mirror_job_that_raises_is_logged_once(caplog) -> None:
    """`_job_failure` shows a raised job's kind only, so the job itself logs the text."""
    import logging

    from server.code.app.index import bootstrap

    class _C:
        label = "o/code-r"

    async def _run(cfg: Any) -> str:
        raise RuntimeError(_DRIVER_TEXT)

    with caplog.at_level(logging.ERROR, logger="loom.code_index.bootstrap"):
        with pytest.raises(RuntimeError):
            await bootstrap._logged("clone", _run, _C())
    assert [r.message for r in caplog.records] == ["mirror clone of o/code-r raised"]
    exc_info = caplog.records[0].exc_info
    assert exc_info is not None and str(exc_info[1]) == _DRIVER_TEXT
