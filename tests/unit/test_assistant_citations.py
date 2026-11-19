"""Citation parsing and persisted evidence queries keep their source and caller scopes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.assistant.domain.citations import parse
from server.memory.domain.set_chunk_session import set_chunk_session_id
from server.observability.consumer import ASSISTANT_CONSUMER
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


def test_a_key_with_an_at_sign_and_a_section_with_a_comma_are_parsed_whole() -> None:
    memory, doc = parse("[memory:user/pref/a@b.com@v3] [doc:spec.pdf § Billing, refunds]")
    assert memory.key == "user/pref/a@b.com"
    assert doc.section == ("Billing, refunds",)


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
        ddl("agent_events"),
        ddl("agent_events_private"),
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
    colon = LocalCH(
        ddl("memory_exposures"),
        ddl("agent_events"),
        ddl("agent_events_private"),
        row([chunk(f"{key}:v2")]),
    )
    assert asyncio.run(ledger.returned_keys("acme", "ns", "r1", "alice", [key], ch=colon)) == set()  # type: ignore[arg-type]
    both = asyncio.run(
        ledger.returned_keys("acme", "ns", "r1", "alice", [key, f"{key}:v2"], ch=colon)  # type: ignore[arg-type]
    )
    assert both == {f"{key}:v2"}
    # Neither a longer key nor its chunk stands for the cited one.
    longer = LocalCH(
        ddl("memory_exposures"),
        ddl("agent_events"),
        ddl("agent_events_private"),
        row([f"{key}x", chunk(f"{key}x")]),
    )
    alone = asyncio.run(ledger.returned_keys("acme", "ns", "r1", "alice", [key], ch=longer))  # type: ignore[arg-type]
    assert alone == set()


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
        ddl("agent_events_private"),
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
