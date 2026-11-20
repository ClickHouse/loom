"""Fresh ancestry reads share the complete decision's parameter budget."""

from __future__ import annotations

import json

import pytest

from server.auth import scim, service_accounts
from server.auth.scim_people import MAX_BYTES
from tests.unit.test_scim_people import People
from tests.unit.test_scim_people import people as people


def _parameter_bytes(params: dict) -> int:
    return len(json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("over", [0, 1, 13], ids=["limit", "one-over", "original-counterexample"])
async def test_complete_graph_bounds_the_actual_fresh_reader_parameters(
    people: People, over: int
) -> None:
    # Accepted roots put the reader's fixed parameters exactly at the limit.
    subject = "sa:" + "s" * 32750
    org = "o" * (32737 + over)
    people.services[subject] = {"created_by": "alice@example.test", "lineage": []}
    people.provision("alice@example.test")

    refused = await scim.refuses_subject(subject, org=org)

    sizes = [_parameter_bytes(params) for params in people.reads]
    assert max(sizes, default=0) <= MAX_BYTES
    reader = [
        params for params in people.reads if params.get("sub") == subject and "items" in params
    ]
    assert refused is (over > 0)
    if over:
        assert reader == []
    else:
        assert len(reader) == 1
        assert _parameter_bytes(reader[0]) == MAX_BYTES


async def test_existing_inheritance_reader_keeps_its_query_contract(
    people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, org = "sa:" + "s" * 32750, "o" * 32750
    people.services[subject] = {"created_by": "alice@example.test", "lineage": []}
    monkeypatch.setattr(service_accounts, "_creators", {})

    creator = await service_accounts.sa_creator(org, subject)

    assert creator is not None and creator.subject == "alice@example.test"
    assert len(people.reads) == 1
    assert _parameter_bytes(people.reads[0]) == MAX_BYTES + 13
