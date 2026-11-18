"""Checked synthesis chunks and current metadata proofs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import InitVar, dataclass, field
from typing import Any

from server.core.ch import sequential_reads, set_query_tag
from server.core.datetimes import to_naive_utc_second
from server.core.diagnostics import private_work
from server.knowledge.app.source_access import AccessEvidence, Admitted, ReadPlan
from server.knowledge.domain.chunk_metadata import read_structure
from server.memory.domain.corpus_synthesis import CorpusChunk
from server.observability.genai import current_stage, llm_stage


class LostCorpus(Exception):
    def __init__(self) -> None:
        super().__init__("document access changed")


def _key(item: Admitted) -> tuple[str, str]:
    return item.candidate["namespace"], item.candidate["source_uri"]


def _identity(item: Admitted) -> tuple[Any, ...]:
    return (
        item.namespace,
        item.document["version"],
        item.document["revision"],
        to_naive_utc_second(item.document["ingested_at"]),
        item.serving,
    )


@dataclass
class Proofs:
    plan: ReadPlan
    items: InitVar[tuple[Admitted, ...]]
    invalid: bool = field(default=False, init=False)
    _expected: dict[tuple[str, str], tuple[Any, ...]] = field(default_factory=dict, init=False)
    _evidence: tuple[AccessEvidence, ...] = field(default=(), init=False, repr=False)
    _checking: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self, items: tuple[Admitted, ...]) -> None:
        for item in items:
            key, identity = _key(item), _identity(item)
            if key in self._expected and self._expected[key] != identity:
                self.invalid = True
            self._expected[key] = identity

    @property
    def mirrored(self) -> bool:
        return bool(self._expected)

    def fresh(self) -> bool:
        """Check all retained deadlines after the composed request's last await."""
        if self.invalid:
            return False
        if not self._expected:
            return True
        self.invalid = (
            self.plan.failed
            or self.plan.unread
            or len(self._evidence) != len(self._expected)
            or not all(self.plan.current_evidence(item) for item in self._evidence)
        )
        return not self.invalid

    async def check(self) -> None:
        """Renew every retained copy; an invalid corpus cannot be admitted again."""
        async with self._checking:
            if self.invalid:
                raise LostCorpus()
            if not self._expected:
                return
            set_query_tag("knowledge.mirror.synthesis_proof")
            try:
                with private_work(), sequential_reads():
                    await self._check()
                    if self.invalid:
                        raise LostCorpus()
            except LostCorpus:
                self.invalid = True
                raise
            except Exception:  # noqa: BLE001 — conditional proof failures disclose no backend details
                self.invalid = self.plan.failed = True
                raise LostCorpus() from None

    async def _check(self) -> None:
        if self.plan.failed or self.plan.unread:
            raise LostCorpus()
        keys, refreshed = list(self._expected), []
        for start in range(0, len(keys), 64):
            batch = keys[start : start + 64]
            current = await self.plan.check(
                [{"namespace": ns, "source_uri": uri} for ns, uri in batch]
            )
            by_key = {_key(item): item for item in current}
            if (
                self.plan.failed
                or self.plan.unread
                or len(current) != len(batch)
                or set(by_key) != set(batch)
            ):
                raise LostCorpus()
            for key in batch:
                item = by_key[key]
                if _identity(item) != self._expected[key]:
                    raise LostCorpus()
                refreshed.append(item)
        if not all(self.plan.current(item) for item in refreshed):
            raise LostCorpus()
        self._evidence = tuple(AccessEvidence.from_admitted(item) for item in refreshed)


@dataclass(frozen=True)
class Corpus:
    chunks: list[CorpusChunk]
    documents: int
    proofs: Proofs


@dataclass(frozen=True)
class CheckedLLM:
    client: Any
    proofs: Proofs

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        await self.proofs.check()
        # Keep the caller's map, plan or reduce stage through provider forwarding.
        with llm_stage(current_stage()):
            result = await self.client.complete(*args, **kwargs)
        await self.proofs.check()
        return result


@dataclass(frozen=True)
class _Document:
    chunks: list[CorpusChunk]
    mirrored: Admitted | None = None


async def _mirror(plan: ReadPlan, namespace: str, uri: str, model: str) -> _Document:
    from server.knowledge.app import source_documents

    checked = await source_documents.get(plan, namespace, uri)
    if checked is None:
        return _Document([])
    chunks = await source_documents.chunks(plan, checked, model=model)
    if chunks is None:
        raise LostCorpus()
    if not chunks:
        return _Document([])
    out = []
    for chunk in chunks:
        if (
            checked.row.org != plan.org
            or _key(chunk.admitted) != (checked.row.namespace, checked.row.source_uri)
            or _identity(chunk.admitted) != _identity(checked.admitted)
        ):
            raise LostCorpus()
        row = chunk.row
        meta = read_structure(row.get("chunk_metadata"))
        out.append(
            CorpusChunk(
                source_uri=uri,
                idx=int(row["chunk_idx"]),
                text=str(row.get("content") or ""),
                section_path=meta.section_path,
                revision=meta.revision,
                locator=meta.locator,
            )
        )
    return _Document(out, checked.admitted)


async def load(
    plan: ReadPlan,
    namespace: str,
    source_uris: list[str],
    *,
    ordinary_chunks: Callable[[str, str, str], Awaitable[list[CorpusChunk]]],
    model: str,
) -> Corpus:
    """Assemble whole copies for synthesis while keeping generic readers ordinary-only."""
    from server.knowledge.store import documents

    set_query_tag("knowledge.mirror.synthesis_corpus")
    selected = []
    by_uri: dict[str, _Document] = {}
    with private_work(), sequential_reads():
        for uri in source_uris:
            if uri in by_uri:
                selected.append(by_uri[uri])
                continue
            ordinary = await ordinary_chunks(plan.org, namespace, uri)
            if ordinary:
                document = _Document(ordinary)
            # The existing resolver arbitrates heads, older builds and pending publications.
            elif await documents.get_one(plan.org, namespace, uri) is not None:
                document = _Document([])
            else:
                try:
                    document = await _mirror(plan, namespace, uri, model)
                except Exception:  # noqa: BLE001 — a conditional mirror failure cannot publish a partial corpus
                    plan.failed = True
                    document = _Document([])
            by_uri[uri] = document
            selected.append(document)
        proofs = Proofs(
            plan, tuple(item.mirrored for item in selected if item.mirrored is not None)
        )
        try:
            await proofs.check()
        except LostCorpus:
            selected = [item for item in selected if item.mirrored is None]
            proofs = Proofs(plan, ())
        return Corpus(
            [chunk for item in selected for chunk in item.chunks],
            sum(bool(item.chunks) for item in selected),
            proofs,
        )
