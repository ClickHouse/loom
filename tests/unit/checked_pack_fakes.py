"""Ordinary typed pack producers for legacy rendering and policy regressions."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from server.knowledge.app import knowledge_search, source_access
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan


def install_ordinary_pack(monkeypatch: Any) -> None:
    async def resolve(input: Any) -> SearchRequest:
        return SearchRequest(input.model_copy(deep=True), (input.namespace,), ())

    async def prepare(org: str, namespaces: list[str]) -> ReadPlan:
        return ReadPlan(org)

    monkeypatch.setattr(knowledge_search, "resolve_search", resolve)
    monkeypatch.setattr(source_access, "prepare", prepare)


@dataclass
class _OrdinaryStack:
    output: Any
    marked: bool = False
    lost: bool = False

    def fresh(self) -> bool:
        return True

    async def current(self) -> Any:
        return self.output

    async def ordinary(self) -> Any:
        return self.output

    def publication_proofs(self) -> tuple[()]:
        return ()


def install_pack_stack(monkeypatch: Any, handler: Any) -> None:
    from server.memory.app import stack

    install_ordinary_pack(monkeypatch)

    async def run(input: Any, plan: ReadPlan) -> _OrdinaryStack:
        assert not plan.marked
        return _OrdinaryStack(await handler(input))

    monkeypatch.setattr(stack, "run_checked_stack", run)


def install_pack_knowledge(monkeypatch: Any, handler: Any) -> None:
    install_ordinary_pack(monkeypatch)

    async def run(request: SearchRequest, plan: ReadPlan) -> CheckedSearch:
        assert not plan.marked
        output = await handler(request.input)
        return CheckedSearch(output, None, SimpleNamespace(marked=False), [], 0)

    monkeypatch.setattr(knowledge_search, "run_checked_search", run)


def install_pack_target(monkeypatch: Any, target: str, handler: Any) -> None:
    if target == "server.memory.app.stack.handle_memory_stack":
        install_pack_stack(monkeypatch, handler)
    elif target == "server.knowledge.app.knowledge_search.handle_knowledge_search":
        install_pack_knowledge(monkeypatch, handler)
    else:
        monkeypatch.setattr(target, handler)
