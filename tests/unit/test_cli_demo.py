"""`loom demo`: upload the repo's own memory file and ingest it as
`make ingest` does, ask it a question, print answer + evidence. The HTTP
layer (`_get_json` / `_post_json` / `_post_bytes`) is replaced with a scripted
server."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import server.cli as cli
import server.cli.demo as demo

HITS = [
    {
        "source_uri": "file:///r/AGENTS.md",
        "chunk_idx": 4,
        "content": "Identity tuple on every row: "
        "(org, namespace, agent, user_id, session_id), the last two defaulting to '-'.",
        "section": "Invariants",
        "score": 0.912,
        "similarity": 0.81,
    },
    {
        "source_uri": "file:///r/AGENTS.md",
        "chunk_idx": 9,
        "content": "The server sets actor from the token "
        "and source_class from the op and its source_trace.",
        "section": "",
        "score": 0.4,
    },
    {
        "source_uri": "file:///r/AGENTS.md",
        "chunk_idx": 1,
        "content": "server/ — FastAPI control plane …",
        "section": "Rules by area and layout",
        "score": 0.0,
        "similarity": 0.33,
    },
]


class FakeServer:
    def __init__(
        self,
        *,
        fake_llm: bool,
        synth: dict[str, Any] | None = None,
        fail: dict[str, tuple[int, dict[str, Any]]] | None = None,
        job: dict[str, Any] | None = None,
    ):
        self.fake_llm = fake_llm
        self.synth = synth or {}
        self.fail = fail or {}
        # How the ingest job ends, after one poll that sees it running.
        self.job = job or {"state": "ready", "revision": 3, "chunks": 12, "format": "markdown"}
        self.posts: list[tuple[str, dict[str, Any], str]] = []
        self.gets: list[tuple[str, str]] = []
        self.uploads: list[tuple[dict[str, list[str]], bytes, str]] = []
        self.polls = 0

    def get(self, url: str, token: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
        self.gets.append((url, token))
        if "health" in self.fail:
            return self.fail["health"]
        return 200, {"status": "ok", "fake_llm": self.fake_llm, "llm_model": "gpt-4o-mini"}

    def post_bytes(
        self, url: str, data: bytes, token: str, timeout: float = 90.0
    ) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(url)
        assert parts.path == "/v1/knowledge.upload"
        self.uploads.append((parse_qs(parts.query), data, token))
        if "knowledge.upload" in self.fail:
            return self.fail["knowledge.upload"]
        return 201, {"upload_id": "up-1", "format": "markdown"}

    def post(
        self, base_url: str, op: str, body: dict[str, Any], token: str, timeout: float = 90.0
    ) -> tuple[int, dict[str, Any]]:
        self.posts.append((op, body, token))
        if op in self.fail:
            return self.fail[op]
        if op == "knowledge.ingest":
            return 200, {"ingestion_id": "job-1", "state": "pending"}
        if op == "knowledge.ingest_status":
            self.polls += 1
            running = {"state": "processing"} if self.polls == 1 else self.job
            return 200, {"found": True, **running}
        if op == "knowledge.search":
            return 200, {"results": HITS, "embedding_model": "fake-ngram-1024"}
        if op == "knowledge.synthesize":
            return 200, self.synth
        raise AssertionError(f"unexpected op {op}")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout-shaped directory holding AGENTS.md."""
    (tmp_path / "AGENTS.md").write_text(
        "# Loom\n\n## Invariants\n\n- **Identity tuple on every row:** `(org, namespace, agent, "
        "user_id, session_id)`\n"
    )
    monkeypatch.delenv("LOOM_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch):
    def _install(**kw: Any) -> FakeServer:
        fake = FakeServer(**kw)
        monkeypatch.setattr(demo, "_get_json", fake.get)
        monkeypatch.setattr(demo, "_post_json", fake.post)
        monkeypatch.setattr(demo, "_post_bytes", fake.post_bytes)
        monkeypatch.setattr(demo, "_sleep", lambda s: None)
        return fake

    return _install


def _args(**over: Any) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    demo.register_demo_cli(sub)
    argv = ["demo"]
    for k, v in over.items():
        flag = "--" + k.replace("_", "-")
        argv += [flag] if v is True else [flag, str(v)]
    return parser.parse_args(argv)


def _jwt(claims: dict[str, Any]) -> str:
    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64(claims)}.sig"


# ---- fake-LLM path (the stock stack) ---------------------------------------------------


def test_demo_fake_llm_ingests_then_prints_hits_and_hint(repo: Path, serve, capsys) -> None:
    fake = serve(fake_llm=True)
    assert demo._cmd_demo(_args()) == 0
    out = capsys.readouterr().out
    ops = [op for op, _, _ in fake.posts]
    # The job is polled until it ends; no synthesize on a fake LLM.
    assert ops == [
        "knowledge.ingest",
        "knowledge.ingest_status",
        "knowledge.ingest_status",
        "knowledge.search",
    ]
    # The file goes as its bytes: the server's extractor parses it, the demo never does.
    ((params, data, token),) = fake.uploads
    assert data == (repo / "AGENTS.md").read_bytes() and token == ""
    assert params == {"org": ["dev"], "namespace": ["local"], "filename": ["AGENTS.md"]}
    ingest = fake.posts[0][1]
    assert (ingest["org"], ingest["namespace"], ingest["upload_id"]) == ("dev", "local", "up-1")
    assert ingest["source_uri"] == f"file://{(repo / 'AGENTS.md').resolve()}"
    assert ingest["title"] == "AGENTS" and ingest["tags"] == ["loom", "project-memory"]
    assert ingest["metadata"] == {"ingested_from": ingest["source_uri"]}  # as `make ingest` writes
    assert fake.posts[1][1]["ingestion_id"] == "job-1"
    search = fake.posts[3][1]
    assert search["query"] == demo.DEFAULT_QUESTION and search["top_k"] == 3
    assert search["uri_prefix"] == [ingest["source_uri"]]
    assert fake.posts[0][2] == ""  # tokenless single-user call
    assert "ingested AGENTS.md → revision=3 chunks=12 format=markdown state=ready" in out
    assert "Top 3 knowledge hits" in out
    assert "0.912" in out and "AGENTS.md#4 (Invariants)" in out
    assert "0.330" in out  # falls back to similarity when score is 0
    assert "AGENTS.md#1 (Rules by area …" in out  # the source column is 28 wide
    assert "synthesis needs a real LLM: set LOOM_LLM_PROVIDER=openai and OPENAI_API_KEY" in out


# ---- real-LLM path -------------------------------------------------------------------------


def test_demo_real_llm_prints_answer_and_evidence(repo: Path, serve, capsys) -> None:
    fake = serve(
        fake_llm=False,
        synth={
            "answer": "Every row carries (org, namespace, agent, user_id, session_id) "
            "[file:///r/AGENTS.md#4].",
            "citations": ["file:///r/AGENTS.md#4"],
            "chunks_read": 12,
            "windows": 1,
            "model": "gpt-4o-mini",
            "abstain": False,
        },
    )
    assert demo._cmd_demo(_args(question="Which columns identify a row?")) == 0
    out = capsys.readouterr().out
    ops = [op for op, _, _ in fake.posts]
    assert ops[-2:] == ["knowledge.search", "knowledge.synthesize"]
    synth_body = fake.posts[-1][1]
    assert synth_body["task"] == "Which columns identify a row?"
    assert synth_body["source_uris"] == [fake.posts[0][1]["source_uri"]]
    assert "Q: Which columns identify a row?" in out
    assert "A: Every row carries (org, namespace, agent, user_id, session_id)" in out
    assert "[12 chunks read, 1 window(s), gpt-4o-mini]" in out
    assert "Evidence (* = cited by the answer):" in out
    table = out.split("Evidence (* = cited by the answer):", 1)[1].splitlines()
    cited_row = next(line for line in table if "AGENTS.md#4 (" in line)
    assert cited_row.startswith("*  1  0.912")
    uncited_row = next(line for line in table if "AGENTS.md#9" in line)
    assert uncited_row.startswith("   2  0.400")
    assert "synthesis needs a real LLM" not in out


def test_demo_real_llm_abstain_is_shown(repo: Path, serve, capsys) -> None:
    serve(
        fake_llm=False,
        synth={
            "answer": "",
            "abstain": True,
            "abstain_reason": "budget_capped",
            "citations": [],
            "chunks_read": 0,
            "windows": 0,
        },
    )
    assert demo._cmd_demo(_args()) == 0
    assert "A: (abstained: budget_capped)" in capsys.readouterr().out


def test_demo_search_only_skips_synthesis_without_the_fake_hint(repo: Path, serve, capsys) -> None:
    fake = serve(fake_llm=False)
    assert demo._cmd_demo(_args(search_only=True)) == 0
    assert [op for op, _, _ in fake.posts][-1] == "knowledge.search"
    assert "knowledge.synthesize" not in [op for op, _, _ in fake.posts]
    assert "synthesis needs a real LLM" not in capsys.readouterr().out


# ---- errors are actionable ---------------------------------------------------------------------


def test_demo_missing_file_is_actionable(repo: Path, serve, capsys) -> None:
    fake = serve(fake_llm=True)
    assert demo._cmd_demo(_args(file="docs/nope.md")) == 2
    err = capsys.readouterr().err
    assert "not a file: docs/nope.md" in err and "--file path/to/doc.md" in err
    assert fake.posts == [] and fake.uploads == []  # nothing was ingested


def test_demo_names_a_relative_file_by_its_absolute_path(repo: Path, serve) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "runbook.md").write_text("# Runbook")
    fake = serve(fake_llm=True)
    assert demo._cmd_demo(_args(file="docs/runbook.md")) == 0
    # The same document `make ingest FILE=docs/runbook.md` names: one file, one source.
    want = f"file://{(repo / 'docs' / 'runbook.md').resolve()}"
    assert fake.posts[0][1]["source_uri"] == want
    assert fake.posts[0][1]["metadata"] == {"ingested_from": want}


def test_demo_no_agents_md_under_root_is_actionable(repo: Path, serve, capsys) -> None:
    (repo / "AGENTS.md").unlink()
    serve(fake_llm=True)
    assert demo._cmd_demo(_args(root=str(repo))) == 2
    assert "not a file: AGENTS.md" in capsys.readouterr().err


def test_demo_failed_job_names_its_code_and_the_extractor(repo: Path, serve, capsys) -> None:
    job = {"state": "failed", "error_code": "format_unavailable", "error_message": "no route"}
    fake = serve(fake_llm=True, job=job)
    assert demo._cmd_demo(_args()) == 1
    err = capsys.readouterr().err
    assert "the job failed (format_unavailable): no route" in err
    assert "`loom up` starts the reference extractor" in err
    assert "knowledge.search" not in [op for op, _, _ in fake.posts]


def test_demo_job_the_server_cannot_find_stops_at_once(repo: Path, serve, capsys) -> None:
    fake = serve(fake_llm=True, job={"found": False, "state": ""})
    assert demo._cmd_demo(_args()) == 1
    assert fake.polls == 2  # no waiting out the deadline for a job that is not there
    assert "no job job-1 in this namespace" in capsys.readouterr().err


def test_demo_job_that_never_ends_names_the_worker(
    repo: Path, serve, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(fake_llm=True, job={"state": "pending"})
    clock = iter(range(0, 10_000, 60))
    monkeypatch.setattr(demo.time, "monotonic", lambda: next(clock))
    assert demo._cmd_demo(_args()) == 1
    err = capsys.readouterr().err
    assert "still pending after 120s" in err and "LOOM_BACKGROUND_WORKERS" in err
    assert "knowledge.ingest_status job-1" in err


def test_demo_unreachable_server_points_at_loom_up(repo: Path, serve, capsys) -> None:
    serve(fake_llm=True, fail={"health": (0, {"detail": "URLError: refused"})})
    assert demo._cmd_demo(_args()) == 1
    err = capsys.readouterr().err
    assert "cannot reach http://localhost:7777" in err and "`loom up` boots it" in err


def test_demo_401_says_where_the_token_goes(repo: Path, serve, capsys) -> None:
    fake = serve(fake_llm=True, fail={"knowledge.upload": (401, {"detail": "missing bearer"})})
    assert demo._cmd_demo(_args()) == 1
    assert fake.posts == []  # nothing is ingested past a refused upload
    err = capsys.readouterr().err
    assert "HTTP 401" in err and "LOOM_API_KEY" in err and "~/.loom/token" in err


def test_demo_synthesize_failure_still_shows_hits(repo: Path, serve, capsys) -> None:
    serve(fake_llm=False, fail={"knowledge.synthesize": (500, {"detail": "boom"})})
    assert demo._cmd_demo(_args()) == 1
    captured = capsys.readouterr()
    assert "knowledge.synthesize: HTTP 500 boom" in captured.err
    assert "Top 3 knowledge hits" in captured.out


# ---- token + identity resolution ----------------------------------------------------------------


def test_token_resolution_order(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert demo.resolve_token(None) == ("", "none")
    cfg = Path(repo / "nohome" / ".loom")
    cfg.mkdir(parents=True)
    (cfg / "token").write_text("FILE.TOK\n")
    assert demo.resolve_token(None) == ("FILE.TOK", str(cfg / "token"))
    monkeypatch.setenv("LOOM_API_KEY", "ENV.TOK")
    assert demo.resolve_token(None) == ("ENV.TOK", "LOOM_API_KEY")
    assert demo.resolve_token("FLAG.TOK") == ("FLAG.TOK", "--token")


def test_identity_from_token_claims_then_defaults(repo: Path, serve) -> None:
    tok = _jwt({"org": "acme", "namespaces": ["team", "other"]})
    fake = serve(fake_llm=True)
    assert demo._cmd_demo(_args(token=tok)) == 0
    body = fake.posts[0][1]
    assert (body["org"], body["namespace"]) == ("acme", "team")
    assert fake.posts[0][2] == tok and fake.uploads[0][2] == tok
    assert demo.resolve_identity(argparse.Namespace(org=None, namespace="x"), "") == ("dev", "x")
    assert demo.jwt_claims("not-a-jwt") == {}


# ---- wiring + the HTTP seams -------------------------------------------------------------------


def test_loom_demo_is_registered_on_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(demo, "_cmd_demo", lambda args: seen.append(args) or 0)
    assert cli.main(["demo", "--question", "why?", "--base-url", "http://h:1/"]) == 0
    assert seen[0].question == "why?" and seen[0].base_url == "http://h:1/"
    assert seen[0].file is None and seen[0].tags == demo.DEFAULT_TAGS


class _Resp:
    def __init__(self, raw: bytes, status: int = 200) -> None:
        self.raw, self.status = raw, status

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.raw


@pytest.mark.parametrize(
    ("answer", "want"),
    [
        (_Resp(b'{"status": "ok"}'), (200, {"status": "ok"})),
        (_Resp(b"[1, 2]"), (200, {})),
        # A login page on a 200 is no Loom answer: status 0, never a success.
        (_Resp(b"<html>sign in</html>"), (0, "not a JSON answer")),
        (TimeoutError("The read operation timed out"), (0, "TimeoutError")),
    ],
)
def test_the_http_seams_turn_every_answer_into_status_and_object(
    monkeypatch: pytest.MonkeyPatch, answer: Any, want: tuple[int, Any]
) -> None:
    import urllib.request

    def _urlopen(req: Any, timeout: float = 0) -> _Resp:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    for status, body in (
        demo._get_json("http://x/v1/health", ""),
        demo._post_json("http://x", "knowledge.search", {}, "t"),
        demo._post_bytes("http://x/v1/knowledge.upload", b"bytes", "t"),
    ):
        assert status == want[0]
        if isinstance(want[1], dict):
            assert body == want[1]
        else:
            assert want[1] in body["detail"]
