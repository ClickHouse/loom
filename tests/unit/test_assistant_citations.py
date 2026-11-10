"""The assistant answer's citations: what is parsed, and each checked against the answer's
own ledgered calls or, for a document, through Loom's own routes as the answer's token (an
app of its own here). The two ledger statements run on clickhouse-local, against the
production tables; the rest needs no ClickHouse."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, Request

from server.assistant.app import citations
from server.assistant.domain.citations import parse
from server.memory.domain.set_chunk_session import set_chunk_session_id
from server.observability.consumer import ASSISTANT_CONSUMER
from server.observability.event_args import text_digest
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

SQL = "SELECT agent, count() FROM memory GROUP BY agent"
ANSWER = f"""handbook-bot wrote 7 [memory:policy/deploy/freeze@v12], see
[doc:handbook/deploy-runbook.md § Deploy runbook > Canary, revision 3] and
[doc:contracts/hosting.txt] and [code:server/main.py:40], and
[memory:policy/deploy/freeze@v12] again.

```sql
{SQL}
```
"""


def test_every_citation_form_is_parsed_once() -> None:
    cited = parse(ANSWER)
    assert [c.kind for c in cited] == ["memory", "doc", "doc", "code", "sql"]
    memory, runbook, contract, code, sql = cited
    assert memory.key == "policy/deploy/freeze"
    assert runbook.source_uri == "handbook/deploy-runbook.md"
    assert runbook.section == ("Deploy runbook", "Canary")
    assert contract.section == () and code.path == "server/main.py"
    assert sql.sql == SQL


# The memory key the answer's own tool calls were returned (its run's exposure rows).
MEMORY_KEY = "policy/deploy/freeze"


@pytest.fixture
def loom(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Loom's document route, answering for one document with a Canary section, and the
    answer's ledgered rows (one statement ran, one memory key returned), recording the
    token each read was sent with and the run each ledger read named."""
    seen: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/v1/knowledge.outline")
    async def outline(request: Request) -> dict[str, Any]:
        body = await request.json()
        seen.append(
            {
                "auth": request.headers["authorization"],
                "run": request.headers.get("x-loom-run-id"),
                **body,
            }
        )
        if body["source_uri"] != "handbook/deploy-runbook.md":
            return {"found": False}
        return {"found": True, "sections": [{"path": ["Deploy runbook", "Canary"]}]}

    async def digests(org: str, ns: str, run: str, subject: str) -> set[str]:
        seen.append({"ledger": "digests", "run": run, "subject": subject})
        return {text_digest(SQL)}

    async def keys(org: str, ns: str, run: str, subject: str, cited: list[str]) -> set[str]:
        seen.append({"ledger": "keys", "run": run, "subject": subject, "cited": cited})
        return {MEMORY_KEY} & set(cited)

    monkeypatch.setattr(citations, "_app", app)
    monkeypatch.setattr(citations.ledger, "query_digests", digests)
    monkeypatch.setattr(citations.ledger, "returned_keys", keys)
    monkeypatch.setattr(citations, "_LEDGER_WAIT_S", 0.0)
    return seen


def _verify(text: str, run_id: str = "r1") -> list[dict[str, Any]]:
    checked = asyncio.run(
        citations.verify(
            text, org="acme", namespace="ns", token="answer-token", run_id=run_id, subject="alice"
        )
    )
    return [c.citation.model_dump() for c in checked]


def test_each_citation_is_checked_against_what_the_answer_read(
    loom: list[dict[str, Any]],
) -> None:
    checked = {c["text"].split("]")[0]: c for c in _verify(ANSWER)}
    # A key one of the answer's own calls was returned: under whichever user or session.
    assert checked["[memory:policy/deploy/freeze@v12"]["verified"] is True
    assert checked["[doc:handbook/deploy-runbook.md § Deploy runbook > Canary, revision 3"][
        "verified"
    ]
    contract = checked["[doc:contracts/hosting.txt"]
    assert contract["verified"] is False and "no live document" in contract["reason"]
    assert checked["[code:server/main.py:40"]["verified"] is False
    assert checked["```sql\n" + SQL + "\n```"]["verified"] is True
    refs = {c["kind"]: c["ref"] for c in checked.values()}
    assert refs == {
        "memory": "policy/deploy/freeze",
        "doc": "contracts/hosting.txt",
        "code": "server/main.py",
        "sql": "",
    }
    reads = [s for s in loom if "auth" in s]
    # Documents read as the answer's token, under no run of the answer's.
    assert {s["auth"] for s in reads} == {"Bearer answer-token"}
    assert {s["run"] for s in reads} == {None}
    # The ledger read by the answer's run and the caller's subject.
    assert {(s["run"], s["subject"]) for s in loom if "ledger" in s} == {("r1", "alice")}


def test_an_unread_key_a_missing_section_and_an_unrun_statement_are_unverified(
    loom: list[dict[str, Any]],
) -> None:
    text = (
        "[memory:policy/other@v11] [doc:handbook/deploy-runbook.md § Rollback]\n"
        "```sql\nSELECT 1\n```\n"
    )
    reasons = [(c["kind"], c["verified"], c["reason"]) for c in _verify(text)]
    assert reasons == [
        ("memory", False, citations._UNREAD),
        ("doc", False, "the document has no such section"),
        ("sql", False, "no insights.query in this answer ran it"),
    ]


def test_an_answer_with_no_citation_checks_nothing(loom: list[dict[str, Any]]) -> None:
    assert _verify("No memory holds that.") == [] and loom == []


def test_a_check_loom_refuses_or_cannot_run_leaves_the_citation_unchecked(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.post("/v1/knowledge.outline")
    async def outline() -> Any:
        return JSONResponse({"detail": {"code": "RATE_LIMITED"}}, status_code=429)

    async def ledger_down(*_: Any) -> set[str]:
        raise ConnectionError("ClickHouse is down")

    monkeypatch.setattr(citations, "_app", app)
    monkeypatch.setattr(citations.ledger, "returned_keys", ledger_down)
    text = f"[memory:{MEMORY_KEY}@v12] [doc:handbook/deploy-runbook.md]\n"
    reasons = [(c["kind"], c["verified"], c["reason"]) for c in _verify(text)]
    # Neither a read Loom refused nor a ledger read that failed says the source is
    # missing: each says the check could not run.
    assert reasons == [
        ("memory", False, "the check could not run"),
        ("doc", False, "the check could not run"),
    ]


def test_with_no_run_id_no_memory_or_statement_can_be_checked(
    loom: list[dict[str, Any]],
) -> None:
    # With no run id any of the caller's rows with none would match.
    text = f"[memory:{MEMORY_KEY}@v12]\n```sql\n{SQL}\n```\n"
    reasons = {(c["kind"], c["reason"]) for c in _verify(text, run_id="")}
    assert reasons == {("memory", "the check could not run"), ("sql", "the check could not run")}


def test_with_memory_reads_not_recorded_only_memory_citations_go_unchecked(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(citations.exposure_ledger, "enabled", lambda: False)
    text = f"[memory:{MEMORY_KEY}@v12]\n```sql\n{SQL}\n```\n"
    out = {c["kind"]: (c["verified"], c["reason"]) for c in _verify(text)}
    # The statements' ledger does not depend on the exposure ledger.
    assert out == {
        "memory": (False, "this deployment does not record memory reads"),
        "sql": (True, ""),
    }


def test_a_document_cited_at_two_sections_is_read_once(loom: list[dict[str, Any]]) -> None:
    text = (
        "[doc:handbook/deploy-runbook.md § Deploy runbook > Canary] "
        "[doc:handbook/deploy-runbook.md § Rollback] [doc:handbook/deploy-runbook.md]"
    )
    out = [(c["verified"], c["reason"]) for c in _verify(text)]
    assert out == [(True, ""), (False, "the document has no such section"), (True, "")]
    assert len([s for s in loom if s.get("source_uri")]) == 1


def test_the_checks_are_bounded_as_a_whole(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(*_: Any) -> set[str]:
        await asyncio.sleep(30)
        return set()

    monkeypatch.setattr(citations.ledger, "returned_keys", slow)
    monkeypatch.setattr(citations, "_CHECKS_S", 0.2)
    text = f"[memory:{MEMORY_KEY}@v12] [doc:handbook/deploy-runbook.md]\n"
    out = {c["kind"]: (c["verified"], c["reason"]) for c in _verify(text)}
    # The check that ended keeps its result; the one cut by the bound could not run.
    assert out == {"memory": (False, "the check could not run"), "doc": (True, "")}


def test_a_key_with_an_at_sign_and_a_section_with_a_comma_are_parsed_whole() -> None:
    memory, doc = parse("[memory:user/pref/a@b.com@v3] [doc:spec.pdf § Billing, refunds]")
    assert memory.key == "user/pref/a@b.com"
    assert doc.section == ("Billing, refunds",)


def test_a_check_leaves_the_answers_own_request_state_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An in-process request runs Loom's request setup in the caller's task unless the
    # checks run in one of their own: the answer's run (and auth, and cost) would change.
    from server.observability.run_context import current_ledger_run, set_ledger_run

    app = FastAPI()

    entered: list[str] = []

    @app.middleware("http")
    async def setup(request: Request, call_next: Any) -> Any:
        # The run a refusal ledgered before the transport binds its own would name.
        entered.append(current_ledger_run())
        set_ledger_run("the-check's-own-run")
        return await call_next(request)

    @app.post("/v1/knowledge.outline")
    async def outline() -> dict[str, Any]:
        return {"found": True, "sections": []}

    monkeypatch.setattr(citations, "_app", app)

    async def run() -> str:
        set_ledger_run("chatcmpl-1")
        await citations.verify(
            "[doc:handbook/deploy-runbook.md]",
            org="acme",
            namespace="ns",
            token="answer-token",
            run_id="chatcmpl-1",
            subject="alice",
        )
        return current_ledger_run()

    assert asyncio.run(run()) == "chatcmpl-1"
    # The check's request starts under no run: none of its rows land among the answer's.
    assert entered == [""]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_key_statement_finds_a_cited_key_and_its_chunks_and_nothing_else() -> None:
    from server.assistant.store import ledger

    key = "policy/deploy/freeze"

    def chunk(of: str) -> str:
        # A search hit's id on one of a key's chunks, as the chunk writer forms it.
        return f"chunk:{set_chunk_session_id(of)}:0"

    def row(returned: list[str], *, run: str = "r1", actor: str = "alice") -> str:
        keys = ", ".join(f"'{k}'" for k in returned)
        return (
            "INSERT INTO loom.memory_exposures (org, namespace, actor, run_id, operation, "
            f"returned_keys) VALUES ('acme', 'ns', '{actor}', '{run}', 'memory.search', [{keys}])"
        )

    ch = LocalCH(
        ddl("memory_exposures"),
        # A chunk of the cited key, then keys that only look like it.
        row([chunk(key), f"{key}x", chunk(f"{key}x")]),
        # The cited key itself, but under another run, and under another caller.
        row([key], run="r2"),
        row([key], actor="bob"),
        row(["a/b"]),
    )
    found = asyncio.run(ledger.returned_keys("acme", "ns", "r1", "alice", [key, "a/b"], ch=ch))  # type: ignore[arg-type]
    assert found == {key, "a/b"}
    # A key may hold `:`: a chunk of `<key>:v2` is that key's, not the cited one's.
    colon = LocalCH(ddl("memory_exposures"), row([chunk(f"{key}:v2")]))
    assert asyncio.run(ledger.returned_keys("acme", "ns", "r1", "alice", [key], ch=colon)) == set()  # type: ignore[arg-type]
    both = asyncio.run(
        ledger.returned_keys("acme", "ns", "r1", "alice", [key, f"{key}:v2"], ch=colon)  # type: ignore[arg-type]
    )
    assert both == {f"{key}:v2"}
    # Neither a longer key nor its chunk stands for the cited one.
    longer = LocalCH(ddl("memory_exposures"), row([f"{key}x", chunk(f"{key}x")]))
    alone = asyncio.run(ledger.returned_keys("acme", "ns", "r1", "alice", [key], ch=longer))  # type: ignore[arg-type]
    assert alone == set()


def test_an_exception_the_app_raises_leaves_the_citation_unchecked(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Raised outside a handler (a middleware, a gate): the app's 500, not the answer's.
    app = FastAPI()

    @app.middleware("http")
    async def broken(request: Request, call_next: Any) -> Any:
        raise RuntimeError("a gate failed")

    monkeypatch.setattr(citations, "_app", app)
    (doc,) = _verify("[doc:handbook/deploy-runbook.md]")
    assert (doc["verified"], doc["reason"]) == (False, "the check could not run")


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_statement_read_finds_only_the_answers_own_successful_queries() -> None:
    from server.assistant.store import ledger

    def call(digest: str, **kw: Any) -> str:
        cols = {
            "org": "acme",
            "namespace": "ns",
            "run_id": "r1",
            "subject": "alice",
            "consumer": ASSISTANT_CONSUMER,
            "operation": "insights.query",
            "success": 1,
            **kw,
        }
        names = ", ".join([*cols, "args"])
        values = ", ".join(
            [
                *(f"'{v}'" if isinstance(v, str) else str(v) for v in cols.values()),
                f"map('out_sql_digest', '{digest}')",
            ]
        )
        return f"INSERT INTO loom.agent_events ({names}) VALUES ({values})"

    ch = LocalCH(
        ddl("agent_events"),
        call("d-ran"),
        # Not the answer's: another run, another caller, the caller's own client, a call
        # that failed, another op.
        call("d-other-run", run_id="r2"),
        call("d-other-caller", subject="bob"),
        call("d-own-client", consumer="my-script"),
        call("d-failed", success=0),
        call("d-other-op", operation="memory.search"),
    )
    found = asyncio.run(ledger.query_digests("acme", "ns", "r1", "alice", ch=ch))  # type: ignore[arg-type]
    assert found == {"d-ran"}


def test_a_row_that_lands_after_the_first_read_is_waited_for(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rows an answer's calls leave land in the background: the check reads again
    # until they are in, and does not re-read a kind it found whole.
    reads = {"digests": 0, "keys": 0}

    async def digests(*_: Any) -> set[str]:
        reads["digests"] += 1
        return {text_digest(SQL)}

    async def keys(org: str, ns: str, run: str, subject: str, cited: list[str]) -> set[str]:
        reads["keys"] += 1
        return set(cited) if reads["keys"] >= 3 else set()

    monkeypatch.setattr(citations.ledger, "query_digests", digests)
    monkeypatch.setattr(citations.ledger, "returned_keys", keys)
    monkeypatch.setattr(citations, "_LEDGER_WAIT_S", 5.0)
    monkeypatch.setattr(citations, "_LEDGER_POLL_S", 0.01)
    text = f"[memory:{MEMORY_KEY}@v12]\n```sql\n{SQL}\n```\n"
    out = {c["kind"]: c["verified"] for c in _verify(text)}
    assert out == {"memory": True, "sql": True}
    assert reads == {"digests": 1, "keys": 3}


@pytest.mark.parametrize("status", [403, 404])
def test_a_document_the_token_may_not_read_is_not_readable(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # Refused or absent for the answer's token: the source is not readable, which is not
    # the same as a check that could not run.
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.post("/v1/knowledge.outline")
    async def outline() -> Any:
        return JSONResponse({"detail": {"code": "AUTH_INSUFFICIENT"}}, status_code=status)

    monkeypatch.setattr(citations, "_app", app)
    (doc,) = _verify("[doc:handbook/deploy-runbook.md]")
    assert (doc["verified"], doc["reason"]) == (
        False,
        "no live document of this source is readable",
    )


def test_a_citation_found_on_the_first_read_waits_for_nothing(
    loom: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(citations, "_LEDGER_WAIT_S", 5.0)
    monkeypatch.setattr(citations.asyncio, "sleep", sleep)
    text = f"[memory:{MEMORY_KEY}@v12]\n```sql\n{SQL}\n```\n"
    assert {c["kind"]: c["verified"] for c in _verify(text)} == {"memory": True, "sql": True}
    assert slept == []
