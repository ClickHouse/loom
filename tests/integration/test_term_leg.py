"""memory.search's term leg through the server: a query that names a governed
definition's term is served that definition first, marked governed; a query that names
no term gets no governed hit; an edit of the source ends governance."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

ARR = {
    "term": "ARR",
    "aliases": ["annual recurring revenue"],
    "body": "Yearly subscription revenue.",
}


@pytest.mark.asyncio
async def test_a_named_term_is_served_its_governed_definition_first(
    control_plane_url: str, mint_test_token: Any
) -> None:
    from loom import Agent

    from tests.integration._resources import unique_org

    org, ns = unique_org("term-leg"), f"term-{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"], subject="definer")
    async with Agent(
        org=org, namespace=ns, agent="bot", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("revenue-note", "Revenue grew in the third quarter.", "semantic")
        empty = await a.memory.search("what is ARR", top_k=5)
        assert empty.term_leg == "empty"

        await a.memory.set("definition/arr", ARR, "definition", agent_id="-")
        got = await a.memory.get("definition/arr", agent_id="-")
        await a.memory.promote(
            "definition/arr", agent_id="-", approved_sha256=got["content_sha256"]
        )

        hits = await a.memory.search("what is our ARR this year", top_k=5)
        assert hits.term_leg == "complete"
        assert hits[0].memory_key == "definition/arr" and hits[0].governed
        assert [h.memory_key for h in hits].count("definition/arr") == 1
        assert not hits.abstain

        # a query naming no term adds nothing
        after = await a.memory.search("how did revenue grow", top_k=5)
        assert after.term_leg == "complete"
        assert not any(h.governed for h in after)

        # case per word: `ARR` is matched only as written
        lower = await a.memory.search("what is our arr", top_k=5)
        assert not any(h.governed for h in lower)

        # the ops that run memory.search serve it first too
        found = await a.call("search.query", {"query": "what is our ARR this year"})
        memory = found["memory"]["results"]
        assert memory[0]["memory_key"] == "definition/arr" and memory[0]["governed"]
        pack = await a.call("context.pack", {"query": "what is our ARR this year"})
        assert "Yearly subscription revenue." in pack["sections"]["memories"]
        asked = await a.call(
            "memory.ask", {"query": "what is our ARR this year", "allow_synthesis": False}
        )
        assert asked["results"][0]["memory_key"] == "definition/arr"

        # an edit of the source ends governance
        await a.memory.set("definition/arr", {**ARR, "body": "Edited."}, "definition", agent_id="-")
        edited = await a.memory.search("what is our ARR", top_k=5)
        assert not any(h.governed for h in edited)


@pytest.mark.asyncio
async def test_a_user_bounded_reader_gets_the_governed_definition_and_nothing_else_shared(
    control_plane_url: str, mint_test_token: Any
) -> None:
    """Under a token pinned to one user, the governed definition is served (an admin
    published it for every reader); another shared row and a namespace-wide fact are not."""
    from loom import Agent

    from tests.integration._resources import unique_org

    org, ns = unique_org("term-scope"), f"term-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"], subject="lead")
    reader = mint_test_token(org=org, namespaces=[ns], roles=["writer"], read_user_id="alice")
    async with Agent(
        org=org, namespace=ns, agent="bot", base_url=control_plane_url, api_key=admin
    ) as a:
        await a.memory.set("definition/arr", ARR, "definition", agent_id="-")
        got = await a.memory.get("definition/arr", agent_id="-")
        await a.memory.promote(
            "definition/arr", agent_id="-", approved_sha256=got["content_sha256"]
        )
        await a.memory.set("arr-note", "Our ARR target is ten million.", "semantic", agent_id="-")
        await a.memory.promote("arr-note", agent_id="-")
        # a whole-namespace reader is served the note too
        whole = await a.memory.search("what is our ARR", top_k=5)
        assert "arr-note" in {h.memory_key for h in whole}
    async with Agent(
        org=org, namespace=ns, agent="bot", base_url=control_plane_url, api_key=reader
    ) as r:
        hits = await r.memory.search("what is our ARR", top_k=5, user_id="alice")
        assert [h.memory_key for h in hits if h.governed] == ["definition/arr"]
        assert {h.memory_key for h in hits} == {"definition/arr"}


@pytest.mark.asyncio
async def test_a_definition_in_another_script_is_served_as_written(
    control_plane_url: str, mint_test_token: Any
) -> None:
    from loom import Agent

    from tests.integration._resources import unique_org

    org, ns = unique_org("term-script"), f"term-{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"], subject="lead")
    sales = {"term": "売上", "aliases": [], "body": "月次の売上の合計。"}
    async with Agent(
        org=org, namespace=ns, agent="bot", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("definition/sales", sales, "definition", agent_id="-")
        got = await a.memory.get("definition/sales", agent_id="-")
        await a.memory.promote(
            "definition/sales", agent_id="-", approved_sha256=got["content_sha256"]
        )
        hits = await a.memory.search("今月の売上は", top_k=5)
        assert hits[0].memory_key == "definition/sales" and hits[0].governed
        assert "月次の売上の合計。" in hits[0].content_excerpt

        # a definition past any excerpt is served whole under unit bodies
        long_body = "売上は契約ごとに数える。" * 200
        await a.memory.set(
            "definition/sales", {**sales, "body": long_body}, "definition", agent_id="-"
        )
        got = await a.memory.get("definition/sales", agent_id="-")
        await a.memory.promote(
            "definition/sales", agent_id="-", approved_sha256=got["content_sha256"]
        )
        whole = await a.memory.search("今月の売上は", top_k=5, bodies="unit")
        assert whole[0].governed and long_body in whole[0].content_excerpt
        assert not whole[0].body_truncated


@pytest.mark.parametrize("change", ["edit", "demote"])
@pytest.mark.asyncio
async def test_a_parked_governed_definition_is_withheld_when_its_approval_lapses(
    control_plane_url: str, mint_test_token: Any, change: str
) -> None:
    import asyncio
    import time

    from loom import Agent

    from tests.integration._resources import unique_org

    org, ns = unique_org("term-page"), f"term-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=["*"], roles=["admin", "writer"])
    reader = mint_test_token(org=org, namespaces=[ns], roles=["writer"], read_user_id="alice")
    async with (
        Agent(org=org, namespace=ns, agent="bot", base_url=control_plane_url, api_key=admin) as a,
        Agent(org=org, namespace=ns, agent="bot", base_url=control_plane_url, api_key=reader) as r,
    ):
        for term in ("ARR", "MRR"):
            key = f"definition/{term.lower()}"
            await a.memory.set(
                key,
                {**ARR, "term": term, "aliases": [], "body": "Revenue definition. " * 400},
                "definition",
                agent_id="-",
            )
            source = await a.memory.get(key, agent_id="-")
            await a.memory.promote(key, agent_id="-", approved_sha256=source["content_sha256"])
        first = await r.call(
            "memory.search",
            {"query": "what is ARR and MRR", "top_k": 1, "paging": True, "user_id": "alice"},
        )
        assert first["results"][0]["memory_key"] == "definition/arr" and first["next_page"]
        page_args = {
            "query": "what is ARR and MRR",
            "top_k": 1,
            "page": first["next_page"],
            "bodies": "unit",
            "body_budget_tokens": 200,
        }
        deadline = time.monotonic() + 15
        while True:
            page = await r.call("memory.search", page_args)
            if page.get("abstain_code") != "page_expired" or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        assert (
            page["results"][0]["memory_key"] == "definition/mrr" and page["results"][0]["governed"]
        )
        assert page["results"][0]["body_truncated"] and page["bodies_elided"] == 1
        assert page["tokens_returned"] <= 200
        expanded = await r.call("memory.search", {**page_args, "body_budget_tokens": 4000})
        assert "Revenue definition. " * 400 in expanded["results"][0]["content_excerpt"]
        assert not expanded["results"][0]["body_truncated"]
        if change == "edit":
            await a.memory.set(
                "definition/mrr",
                {**ARR, "term": "MRR", "body": "Edited"},
                "definition",
                agent_id="-",
            )
        else:
            await a.call("memory.demote", {"memory_key": "definition/mrr", "agent": "-"})
        after = await r.call("memory.search", page_args)
        assert after["results"] == []
