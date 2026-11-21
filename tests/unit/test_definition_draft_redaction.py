"""Redaction blanks private copies of one value and candidates named by its tags."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.hashing import served_content_sha256
from server.memory.app import definition_review
from server.memory.store import learnings
from tests.unit.definition_queue_fake import install

VALUE = {"term": "ARR", "body": "Revenue."}


@pytest.mark.parametrize("live_same", [False, True])
async def test_redaction_reaches_exact_text_and_tag_but_not_other_drafts(
    monkeypatch: pytest.MonkeyPatch, live_same: bool
) -> None:
    install(monkeypatch)
    pages, scrubbed = [], []
    target = SimpleNamespace(
        agent="-",
        user_id="-",
        session_id="-",
        memory_key="definition/draft",
        namespace="n",
        content_sha256=served_content_sha256(VALUE),
        tags=("candidate-id:b",),
    )

    async def page(org: str, ns: str, after: str) -> Any:
        pages.append((org, ns, after))
        return (
            []
            if after
            else [
                {"candidate_id": "a", "proposed_value": json.dumps(VALUE)},
                {
                    "candidate_id": "b",
                    "proposed_value": json.dumps({**VALUE, "body": "A former version."}),
                },
                {
                    "candidate_id": "c",
                    "proposed_value": json.dumps({**VALUE, "body": "Another draft."}),
                },
            ]
        )

    async def scrub(org: str, ns: str, cid: str) -> None:
        scrubbed.append((org, ns, cid))

    @asynccontextmanager
    async def claim(*a: Any, **kw: Any) -> Any:
        yield True

    from server.core import leases

    monkeypatch.setattr(leases, "holding", claim)
    monkeypatch.setattr(learnings, "private_candidates_after", page)
    monkeypatch.setattr(learnings, "scrub_private_candidate", scrub)
    await definition_review.redact_candidates("o", target, target if live_same else None)
    assert scrubbed == ([] if live_same else [("o", "n", "a"), ("o", "n", "b")])
    assert pages == ([] if live_same else [("o", "n", ""), ("o", "n", "c")])
