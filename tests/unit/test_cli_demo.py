"""`loom demo`: ingest the repo's own memory file through the
`make ingest` code path, ask it a question, print answer + evidence. The HTTP
layer (`_get_json` / `_post_json`) is replaced with a scripted server."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

import pytest

import server.cli as cli
import server.cli.demo as demo

ROOT = Path(__file__).resolve().parents[2]

HITS = [
    {
        "source_uri": "file:///r/AGENTS.md",
        "chunk_idx": 4,
        "content": "Identity tuple on every row: "
        "(org, namespace, agent, user_id, session_id) — first three required, rest default to '-'.",
        "section": "Conventions that aren't obvious",
        "score": 0.912,
        "similarity": 0.81,
    },
    {
        "source_uri": "file:///r/AGENTS.md",
        "chunk_idx": 9,
        "content": "Two more columns are stamped "
        "by the server, never read from the body: actor and source_class.",
        "section": "",
        "score": 0.4,
    },
    {
        "source_uri": "file:///r/AGENTS.md",
        "chunk_idx": 1,
        "content": "Repo orientation …",
        "section": "Repo orientation",
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
    ):
        self.fake_llm = fake_llm
        self.synth = synth or {}
        self.fail = fail or {}
        self.posts: list[tuple[str, dict[str, Any], str]] = []
        self.gets: list[tuple[str, str]] = []

    def get(self, url: str, token: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
        self.gets.append((url, token))
        if "health" in self.fail:
            return self.fail["health"]
        return 200, {"status": "ok", "fake_llm": self.fake_llm, "llm_model": "gpt-4o-mini"}

    def post(
        self, base_url: str, op: str, body: dict[str, Any], token: str, timeout: float = 90.0
    ) -> tuple[int, dict[str, Any]]:
        self.posts.append((op, body, token))
        if op in self.fail:
            return self.fail[op]
        if op == "knowledge.upsert":
            return 200, {"doc_id": "doc-1", "chunks_created": 12, "chunks_superseded": 12}
        if op == "knowledge.search":
            return 200, {"results": HITS, "embedding_model": "fake-ngram-1024"}
        if op == "knowledge.synthesize":
            return 200, self.synth
        raise AssertionError(f"unexpected op {op}")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout-shaped directory: AGENTS.md plus the real dev/loom_ingest.py."""
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "loom_ingest.py").write_text((ROOT / "dev" / "loom_ingest.py").read_text())
    (tmp_path / "AGENTS.md").write_text(
        "# Loom\n\n## Conventions\n\n- **Identity tuple on every row:** `(org, namespace, agent, "
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
    assert ops == ["knowledge.upsert", "knowledge.search"]  # no synthesize on a fake LLM
    upsert = fake.posts[0][1]
    assert upsert["org"] == "dev" and upsert["namespace"] == "local"
    assert upsert["source_uri"] == f"file://{(repo / 'AGENTS.md').resolve()}"
    assert upsert["title"] == "AGENTS" and upsert["tags"] == ["loom", "project-memory"]
    assert "Identity tuple on every row" in upsert["content"]
    assert upsert["metadata"]["ingested_format"] == ".md"
    search = fake.posts[1][1]
    assert search["query"] == demo.DEFAULT_QUESTION and search["top_k"] == 3
    assert search["uri_prefix"] == [upsert["source_uri"]]
    assert fake.posts[0][2] == ""  # tokenless single-user call
    assert "ingested AGENTS.md → doc_id=doc-1 chunks=12" in out
    assert "Top 3 knowledge hits" in out
    assert "0.912" in out and "AGENTS.md#4 (Conventions th…" in out  # source column is 28 wide
    assert "0.330" in out  # falls back to similarity when score is 0
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
    assert ops == ["knowledge.upsert", "knowledge.search", "knowledge.synthesize"]
    synth_body = fake.posts[2][1]
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
    assert [op for op, _, _ in fake.posts] == ["knowledge.upsert", "knowledge.search"]
    assert "synthesis needs a real LLM" not in capsys.readouterr().out


# ---- errors are actionable ---------------------------------------------------------------------


def test_demo_missing_file_is_actionable(repo: Path, serve, capsys) -> None:
    fake = serve(fake_llm=True)
    assert demo._cmd_demo(_args(file="docs/nope.md")) == 2
    err = capsys.readouterr().err
    assert "not a file: docs/nope.md" in err and "--file path/to/doc.md" in err
    assert fake.posts == []  # nothing was ingested


def test_demo_no_agents_md_in_cwd_is_actionable(repo: Path, serve, capsys, monkeypatch) -> None:
    (repo / "AGENTS.md").unlink()
    monkeypatch.setattr(demo, "load_loom_ingest", lambda root=None: None)
    serve(fake_llm=True)
    assert demo._cmd_demo(_args()) == 2
    assert "dev/loom_ingest.py not found" in capsys.readouterr().err


def test_demo_unreachable_server_points_at_loom_up(repo: Path, serve, capsys) -> None:
    serve(fake_llm=True, fail={"health": (0, {"detail": "URLError: refused"})})
    assert demo._cmd_demo(_args()) == 1
    err = capsys.readouterr().err
    assert "cannot reach http://localhost:7777" in err and "`loom up` boots it" in err


def test_demo_401_says_where_the_token_goes(repo: Path, serve, capsys) -> None:
    serve(fake_llm=True, fail={"knowledge.upsert": (401, {"detail": "missing bearer"})})
    assert demo._cmd_demo(_args()) == 1
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
    assert fake.posts[0][2] == tok
    assert demo.resolve_identity(argparse.Namespace(org=None, namespace="x"), "") == ("dev", "x")
    assert demo.jwt_claims("not-a-jwt") == {}


# ---- wiring + the shared ingest body ----------------------------------------------------------


def test_loom_demo_is_registered_on_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(demo, "_cmd_demo", lambda args: seen.append(args) or 0)
    assert cli.main(["demo", "--question", "why?", "--base-url", "http://h:1/"]) == 0
    assert seen[0].question == "why?" and seen[0].base_url == "http://h:1/"
    assert seen[0].file is None and seen[0].tags == demo.DEFAULT_TAGS


def test_knowledge_body_is_the_make_ingest_shape(repo: Path) -> None:
    mod = demo.load_loom_ingest(str(repo))
    assert mod is not None
    path = repo / "AGENTS.md"
    body = mod.knowledge_body(
        local_path=path,
        source_uri="file://x",
        content="c",
        org="o",
        namespace="n",
        title="t",
        tags=["a"],
    )
    assert body == {
        "org": "o",
        "namespace": "n",
        "content": "c",
        "source_uri": "file://x",
        "title": "t",
        "tags": ["a"],
        "metadata": {
            "ingested_from": "file://x",
            "ingested_format": ".md",
            "ingested_bytes": path.stat().st_size,
        },
    }


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
    ):
        assert status == want[0]
        if isinstance(want[1], dict):
            assert body == want[1]
        else:
            assert want[1] in body["detail"]
