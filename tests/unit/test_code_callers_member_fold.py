"""Callers of a class seed include callers of its members,
each such edge tagged `via: member`; a function seed never asks for members."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.settings import settings

CLASS = {
    "symbol_id": "c1",
    "qualified_name": "src.users::UsersService",
    "name": "UsersService",
    "kind": "class",
    "path": "src/users.ts",
    "tier": "treesitter",
    "start_line": 3,
    "signature": "",
    "language": "typescript",
}
METHOD = {
    "symbol_id": "m1",
    "qualified_name": "src.users::UsersService::findOne",
    "name": "findOne",
    "kind": "method",
    "path": "src/users.ts",
    "tier": "treesitter",
    "start_line": 8,
    "signature": "",
    "language": "typescript",
}
EDGES = {
    "c1": [
        {
            "edge_kind": "instantiates",
            "from_symbol_id": "f1",
            "to_symbol_id": "c1",
            "tier": "treesitter",
            "path": "src/app.ts",
            "line": 4,
            "confidence": 1.0,
            "from_qualified_name": "src.app::boot",
            "to_qualified_name": CLASS["qualified_name"],
        }
    ],
    "m1": [
        {
            "edge_kind": "calls",
            "from_symbol_id": "f2",
            "to_symbol_id": "m1",
            "tier": "treesitter",
            "path": "src/ctl.ts",
            "line": 9,
            "confidence": 0.9,
            "from_qualified_name": "src.ctl::UsersController::one",
            "to_qualified_name": METHOD["qualified_name"],
        },
        {
            "edge_kind": "calls",
            "from_symbol_id": "m2",
            "to_symbol_id": "m1",
            "tier": "treesitter",
            "path": "src/users.ts",
            "line": 14,
            "confidence": 1.0,
            "from_qualified_name": "src.users::UsersService::update",
            "to_qualified_name": METHOD["qualified_name"],
        },
    ],
}


class _FakeRepo:
    def __init__(self, seed: dict[str, Any]) -> None:
        self.seed = seed
        self.member_calls: list[list[str]] = []

    async def symbols_by_name(self, org, ns, name, limit=10):
        return [self.seed]

    async def symbols_by_ids(self, org, ns, ids):
        return [self.seed]

    async def members_of(self, org, ns, qns, limit=64):
        self.member_calls.append(list(qns))
        return [METHOD] if CLASS["qualified_name"] in qns else []

    async def edges_for_symbols(self, org, ns, ids, **kw):
        return [e for i in ids for e in EDGES.get(i, [])]

    async def all_state(self, org, ns):
        return {}


def _run(seed: dict[str, Any]) -> tuple[Any, _FakeRepo]:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph as h

    fake = _FakeRepo(seed)

    async def no_dispatch(repo, org, ns, seeds):
        return []

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "_dispatch_seeds", new=no_dispatch),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_callers(
                CodeCallersInput(name=seed["name"], include_ambiguous=False)
            )

    return asyncio.run(go()), fake


def _settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def test_a_class_seed_folds_its_members_callers_tagged_via_member(monkeypatch) -> None:
    _settings(monkeypatch)
    out, fake = _run(CLASS)
    assert fake.member_calls == [[CLASS["qualified_name"]]]
    by_from = {e.from_symbol: e for e in out.edges}
    # the sibling call update -> findOne inside the class is not a caller of the class
    assert set(by_from) == {"src.app::boot", "src.ctl::UsersController::one"}
    assert by_from["src.ctl::UsersController::one"].via == "member"
    assert by_from["src.app::boot"].via == ""
    assert "1 member(s)" in out.note
    wire = out.model_dump()
    assert "via" not in wire["edges"][0] or wire["edges"][0]["via"]


def test_a_method_seed_never_asks_for_members(monkeypatch) -> None:
    _settings(monkeypatch)
    out, fake = _run(METHOD)
    assert fake.member_calls == []
    assert [e.from_symbol for e in out.edges] == [
        "src.ctl::UsersController::one",
        "src.users::UsersService::update",
    ]
    assert out.note == ""
