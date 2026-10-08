"""loom as a LangGraph ``BaseStore``.

LangGraph's long-term memory is a key-value store addressed by a
``namespace`` tuple and a ``key``, with ``put`` / ``get`` / ``delete`` /
``search`` (optionally by natural-language ``query``) and
``list_namespaces``. Every graph node reaches it through ``get_store()`` and
every ``BaseStore`` implementation is one class with two abstract methods,
``batch`` and ``abatch``, that execute a list of typed operations. This
adapter is that class over a loom ``Agent``: a LangGraph namespace becomes a
key prefix plus tags, a ``put`` is ``memory.set`` (dedup on write, conflict
detection, TTL, bitemporal validity), a ``search`` with a query is
``memory.search`` (hybrid ranking, calibrated abstention) and without one is
``memory.list``, and the tenant boundary is loom's namespace authorisation —
the graph never sees a partition key.

Usage::

    from langgraph.graph import StateGraph
    from loom import Agent
    from loom.integrations.langgraph import LoomStore

    loom_agent = Agent(org="acme", namespace="support", agent="assistant")
    store = LoomStore(loom_agent)
    graph = builder.compile(store=store)          # nodes: store = get_store()

    # inside a node
    store.put(("users", user_id), "prefs", {"seat": "aisle"})
    store.search(("users", user_id), query="seating preference", limit=3)

    store.close()  # after the sync API: closes the agent and the store's loop

Use one store either through the sync methods or the ``a*`` ones: the sync
methods run the agent on a loop the store owns.

Mapping:

- namespace ``("users", "alice")`` + key ``"prefs"`` → memory key
  ``lg/users/alice/prefs``; tags ``lg:users`` and ``lg:users/alice`` (one per
  prefix), so a prefix search is one tag filter.
- ``value`` (a JSON object) is stored as the memory's value; ``index=False``
  becomes ``memory_type="episodic"`` (kept, not ranked); ``ttl`` (minutes)
  becomes ``ttl_seconds``.
- ``search(query=…)`` ranks with ``memory.search`` (``similarity`` is the
  ``SearchItem.score``) then fetches the values in one ``bulk_get``;
  ``search()`` without a query lists by recency. ``filter`` applies to the
  stored value with LangGraph's operators (``$eq $ne $gt $gte $lt $lte``).
- ``list_namespaces`` derives the tuples from the keys under ``lg/``.

Limits: a ranked search asks for at most ``search_top_k_cap`` (50) rows, so
``offset + limit`` past it returns fewer items. A search without a query and
``list_namespaces`` page through ``memory.list`` until they have what was
asked for or the keys run out; ``filter`` applies to each page read.

The framework is optional: when ``langgraph`` is importable the store
subclasses ``BaseStore`` and reuses its ``Item`` / ``SearchItem`` / op
types; otherwise structural twins are defined here so the module imports,
type-checks and tests without it (the same shape as the Strands adapter).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from loom.agent import Agent

try:  # the framework is optional — only the protocol shapes are needed here
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        PutOp,
        SearchItem,
        SearchOp,
    )

    HAS_LANGGRAPH = True
except Exception:  # noqa: BLE001 — ImportError or an incompatible langgraph
    HAS_LANGGRAPH = False

    class Item:  # type: ignore[no-redef]
        """Structural twin of ``langgraph.store.base.Item``."""

        def __init__(
            self,
            *,
            value: dict[str, Any],
            key: str,
            namespace: tuple[str, ...],
            created_at: datetime,
            updated_at: datetime,
        ) -> None:
            self.value = value
            self.key = key
            self.namespace = namespace
            self.created_at = created_at
            self.updated_at = updated_at

        def __eq__(self, other: object) -> bool:
            return isinstance(other, Item) and (self.namespace, self.key, self.value) == (
                other.namespace,
                other.key,
                other.value,
            )

        def __repr__(self) -> str:
            return f"Item(namespace={self.namespace!r}, key={self.key!r}, value={self.value!r})"

    class SearchItem(Item):  # type: ignore[no-redef]
        def __init__(
            self,
            namespace: tuple[str, ...],
            key: str,
            value: dict[str, Any],
            created_at: datetime,
            updated_at: datetime,
            score: float | None = None,
        ) -> None:
            super().__init__(
                value=value,
                key=key,
                namespace=namespace,
                created_at=created_at,
                updated_at=updated_at,
            )
            self.score = score

    class GetOp(NamedTuple):  # type: ignore[no-redef]
        namespace: tuple[str, ...]
        key: str
        refresh_ttl: bool = True

    class SearchOp(NamedTuple):  # type: ignore[no-redef]
        namespace_prefix: tuple[str, ...]
        filter: dict[str, Any] | None = None
        limit: int = 10
        offset: int = 0
        query: str | None = None
        refresh_ttl: bool = True

    class PutOp(NamedTuple):  # type: ignore[no-redef]
        namespace: tuple[str, ...]
        key: str
        value: dict[str, Any] | None
        index: Any = None
        ttl: float | None = None

    class ListNamespacesOp(NamedTuple):  # type: ignore[no-redef]
        match_conditions: tuple[Any, ...] | None = None
        max_depth: int | None = None
        limit: int = 100
        offset: int = 0

    class BaseStore:  # type: ignore[no-redef]
        """Structural twin: the sync/async convenience methods LangGraph's
        ``BaseStore`` derives from ``batch`` / ``abatch``."""

        def get(
            self, namespace: tuple[str, ...], key: str, *, refresh_ttl: bool | None = None
        ) -> Any:
            return self.batch([GetOp(namespace, key, refresh_ttl is not False)])[0]

        async def aget(
            self, namespace: tuple[str, ...], key: str, *, refresh_ttl: bool | None = None
        ) -> Any:
            return (await self.abatch([GetOp(namespace, key, refresh_ttl is not False)]))[0]

        def put(
            self,
            namespace: tuple[str, ...],
            key: str,
            value: dict[str, Any],
            index: Any = None,
            *,
            ttl: Any = None,
        ) -> None:
            self.batch(
                [
                    PutOp(
                        namespace, key, value, index, ttl if isinstance(ttl, (int, float)) else None
                    )
                ]
            )

        async def aput(
            self,
            namespace: tuple[str, ...],
            key: str,
            value: dict[str, Any],
            index: Any = None,
            *,
            ttl: Any = None,
        ) -> None:
            await self.abatch(
                [
                    PutOp(
                        namespace, key, value, index, ttl if isinstance(ttl, (int, float)) else None
                    )
                ]
            )

        def delete(self, namespace: tuple[str, ...], key: str) -> None:
            self.batch([PutOp(namespace, key, None)])

        async def adelete(self, namespace: tuple[str, ...], key: str) -> None:
            await self.abatch([PutOp(namespace, key, None)])

        def search(
            self,
            namespace_prefix: tuple[str, ...],
            /,
            *,
            query: str | None = None,
            filter: dict[str, Any] | None = None,
            limit: int = 10,
            offset: int = 0,
            refresh_ttl: bool | None = None,
        ) -> Any:
            return self.batch(
                [SearchOp(namespace_prefix, filter, limit, offset, query, refresh_ttl is not False)]
            )[0]

        async def asearch(
            self,
            namespace_prefix: tuple[str, ...],
            /,
            *,
            query: str | None = None,
            filter: dict[str, Any] | None = None,
            limit: int = 10,
            offset: int = 0,
            refresh_ttl: bool | None = None,
        ) -> Any:
            return (
                await self.abatch(
                    [
                        SearchOp(
                            namespace_prefix, filter, limit, offset, query, refresh_ttl is not False
                        )
                    ]
                )
            )[0]

        def list_namespaces(
            self,
            *,
            prefix: tuple[str, ...] | None = None,
            suffix: tuple[str, ...] | None = None,
            max_depth: int | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> Any:
            return self.batch(
                [ListNamespacesOp(_conditions(prefix, suffix), max_depth, limit, offset)]
            )[0]

        async def alist_namespaces(
            self,
            *,
            prefix: tuple[str, ...] | None = None,
            suffix: tuple[str, ...] | None = None,
            max_depth: int | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> Any:
            return (
                await self.abatch(
                    [ListNamespacesOp(_conditions(prefix, suffix), max_depth, limit, offset)]
                )
            )[0]

        def batch(self, ops: Iterable[Any]) -> list[Any]:  # pragma: no cover — abstract
            raise NotImplementedError

        async def abatch(self, ops: Iterable[Any]) -> list[Any]:  # pragma: no cover — abstract
            raise NotImplementedError


@dataclass(frozen=True)
class _Cond:
    match_type: str
    path: tuple[str, ...]


def _conditions(
    prefix: tuple[str, ...] | None, suffix: tuple[str, ...] | None
) -> tuple[Any, ...] | None:
    out = []
    if prefix:
        out.append(_Cond("prefix", tuple(prefix)))
    if suffix:
        out.append(_Cond("suffix", tuple(suffix)))
    return tuple(out) or None


# ---- key + tag mapping --------------------------------------------------------------

KEY_PREFIX = "lg"
TAG_PREFIX = "lg:"
_SEP = "/"
# memory.list's page size cap: the store pages past it with `offset`.
_LIST_PAGE = 1000


def _check_segment(seg: str, what: str) -> None:
    if not isinstance(seg, str) or not seg or _SEP in seg:
        raise ValueError(f"{what} must be a non-empty string without '/': {seg!r}")


def memory_key_for(namespace: tuple[str, ...], key: str) -> str:
    """``("users", "alice")`` + ``"prefs"`` → ``lg/users/alice/prefs``."""
    for seg in namespace:
        _check_segment(seg, "namespace segment")
    _check_segment(key, "key")
    return _SEP.join((KEY_PREFIX, *namespace, key))


def parse_memory_key(memory_key: str) -> tuple[tuple[str, ...], str] | None:
    """Inverse of :func:`memory_key_for`; None for a key this adapter did not write."""
    parts = memory_key.split(_SEP)
    if len(parts) < 3 or parts[0] != KEY_PREFIX or any(not p for p in parts):
        return None
    return tuple(parts[1:-1]), parts[-1]


def tags_for(namespace: tuple[str, ...]) -> list[str]:
    """One tag per prefix, so a prefix search is one tag: ``("users", "alice")``
    → ``["lg:users", "lg:users/alice"]``."""
    return [TAG_PREFIX + _SEP.join(namespace[: i + 1]) for i in range(len(namespace))]


def prefix_tag(namespace_prefix: tuple[str, ...]) -> str | None:
    return TAG_PREFIX + _SEP.join(namespace_prefix) if namespace_prefix else None


# ---- filters (LangGraph's operators, applied to the stored value) ----------------------

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "$eq": lambda a, b: a == b,
    "$ne": lambda a, b: a != b,
    "$gt": lambda a, b: a is not None and a > b,
    "$gte": lambda a, b: a is not None and a >= b,
    "$lt": lambda a, b: a is not None and a < b,
    "$lte": lambda a, b: a is not None and a <= b,
}


def matches_filter(value: Mapping[str, Any], filt: Mapping[str, Any] | None) -> bool:
    """``{"kind": "pref"}`` is equality; ``{"n": {"$gt": 3}}`` is an operator."""
    if not filt:
        return True
    for field, want in filt.items():
        have = value.get(field)
        if isinstance(want, Mapping) and want and all(str(k).startswith("$") for k in want):
            for op, arg in want.items():
                fn = _OPS.get(str(op))
                if fn is None:
                    raise ValueError(f"unsupported filter operator {op!r}")
                try:
                    if not fn(have, arg):
                        return False
                except TypeError:
                    return False
        elif have != want:
            return False
    return True


def _match_namespace(ns: tuple[str, ...], conds: Iterable[Any] | None) -> bool:
    for c in conds or ():
        path = tuple(getattr(c, "path", ()))
        kind = str(getattr(c, "match_type", ""))
        if kind == "prefix" and not _path_matches(ns[: len(path)], path):
            return False
        if kind == "suffix" and not _path_matches(ns[len(ns) - len(path) :] if path else (), path):
            return False
    return True


def _path_matches(have: tuple[str, ...], want: tuple[str, ...]) -> bool:
    return len(have) == len(want) and all(
        w == "*" or w == h for h, w in zip(have, want, strict=True)
    )


def _dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        with contextlib.suppress(ValueError):
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _as_dict(value: Any) -> dict[str, Any]:
    """LangGraph values are JSON objects; a scalar loom value is wrapped."""
    return dict(value) if isinstance(value, Mapping) else {"value": value}


# ---- the store --------------------------------------------------------------------------


class LoomStore(BaseStore):
    """A LangGraph ``BaseStore`` over a loom ``Agent`` (see the module docstring).

    ``agent`` — a :class:`loom.Agent` (identity = org / namespace / agent).
    ``tags`` — extra tags on every write (a project, a tenant label).
    ``search_top_k_cap`` — the most rows one ranked search asks the server for.
    """

    supports_ttl = True

    def __init__(
        self, agent: Agent, *, tags: list[str] | None = None, search_top_k_cap: int = 50
    ) -> None:
        self._agent = agent
        self._tags = list(tags or [])
        self._cap = int(search_top_k_cap)
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- LangGraph's contract --

    def batch(self, ops: Iterable[Any]) -> list[Any]:
        """Synchronous entry: runs ``abatch`` on the store's own loop, reused
        across calls because the agent's connection pool binds to the loop it
        first ran on. Inside a running loop (an async graph) call ``abatch`` /
        the ``a*`` methods instead."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
            return self._loop.run_until_complete(self.abatch(ops))
        raise RuntimeError(
            "LoomStore.batch() called inside a running event loop; "
            "use abatch() / aget / aput / asearch"
        )

    def close(self) -> None:
        """Close the agent and the loop the sync API ran it on. Idempotent; a
        store only ever used through the ``a*`` methods has nothing to close
        (close the agent yourself)."""
        if self._loop is None:
            return
        loop, self._loop = self._loop, None
        try:
            loop.run_until_complete(self._agent.close())
        finally:
            loop.close()

    async def abatch(self, ops: Iterable[Any]) -> list[Any]:
        out: list[Any] = []
        for op in ops:
            if isinstance(op, GetOp):
                out.append(await self._get(op))
            elif isinstance(op, PutOp):
                await self._put(op)
                out.append(None)
            elif isinstance(op, SearchOp):
                out.append(await self._search(op))
            elif isinstance(op, ListNamespacesOp):
                out.append(await self._list_namespaces(op))
            else:
                raise TypeError(f"unsupported store op: {type(op).__name__}")
        return out

    # -- one op each --

    async def _get(self, op: Any) -> Any:
        row = await self._agent.memory.get(memory_key_for(tuple(op.namespace), op.key))
        if not row:
            return None
        return self._item(tuple(op.namespace), op.key, row)

    async def _put(self, op: Any) -> None:
        mk = memory_key_for(tuple(op.namespace), op.key)
        if op.value is None:
            await self._agent.memory.delete(mk)
            return None
        kwargs: dict[str, Any] = {
            "tags": [*self._tags, *tags_for(tuple(op.namespace))],
            # index=False: keep the row but do not rank it (LangGraph's contract)
            "memory_type": "episodic" if op.index is False else "semantic",
        }
        if op.ttl is not None:
            kwargs["ttl_seconds"] = max(1, int(float(op.ttl) * 60))
        await self._agent.memory.set(mk, dict(op.value), **kwargs)
        return None

    async def _search(self, op: Any) -> list[Any]:
        prefix = tuple(op.namespace_prefix)
        tag = prefix_tag(prefix)
        tags = [tag] if tag else None
        want = int(op.offset) + int(op.limit)
        if op.query:
            res = await self._agent.memory.search(
                op.query, tags=tags, top_k=min(want * 2, self._cap), include_tags=False
            )
            ranked = [(h.memory_key, h.similarity) for h in res if parse_memory_key(h.memory_key)]
            return (await self._items(ranked, prefix, op.filter))[int(op.offset) : want]
        items: list[Any] = []
        pages = self._key_pages(_SEP.join((KEY_PREFIX, *prefix)) + _SEP, tags)
        try:
            async for keys in pages:
                items += await self._items([(k, None) for k in keys], prefix, op.filter)
                if len(items) >= want:
                    break
        finally:
            await pages.aclose()  # a page loop left early is closed on this loop
        return items[int(op.offset) : want]

    async def _key_pages(
        self, prefix: str, tags: list[str] | None = None
    ) -> AsyncGenerator[list[str], None]:
        """The store's memory keys under `prefix`, one `memory.list` page at a time."""
        offset = 0
        while True:
            listed = await self._agent.memory.list(
                prefix=prefix, tags=tags, limit=_LIST_PAGE, offset=offset, fields=["memory_key"]
            )
            entries = listed.get("entries", [])
            yield [e["memory_key"] for e in entries if parse_memory_key(e["memory_key"])]
            if not listed.get("truncated") or not entries:
                return
            offset += len(entries)

    async def _items(
        self, ranked: Sequence[tuple[str, float | None]], prefix: tuple[str, ...], filter: Any
    ) -> list[Any]:
        """The `SearchItem`s for `ranked` keys under `prefix` whose value matches `filter`."""
        keys = [k for k, _ in ranked]
        if not keys:
            return []
        fetched = (await self._agent.memory.bulk_get(keys)).get("results", {})
        items: list[Any] = []
        for mk, score in ranked:
            row = fetched.get(mk)
            parsed = parse_memory_key(mk)
            if not row or not row.get("found", True) or parsed is None:
                continue
            ns, key = parsed
            if ns[: len(prefix)] != prefix:
                continue
            value = _as_dict(row.get("value"))
            if not matches_filter(value, filter):
                continue
            items.append(
                SearchItem(
                    ns,
                    key,
                    value,
                    _dt(row.get("created_at")),
                    _dt(row.get("updated_at") or row.get("created_at")),
                    score=score,
                )
            )
        return items

    async def _list_namespaces(self, op: Any) -> list[tuple[str, ...]]:
        seen: dict[tuple[str, ...], None] = {}
        async for keys in self._key_pages(KEY_PREFIX + _SEP):
            for mk in keys:
                parsed = parse_memory_key(mk)
                if parsed is None:
                    continue
                ns = parsed[0]
                if not _match_namespace(ns, op.match_conditions):
                    continue
                if op.max_depth is not None:
                    ns = ns[: int(op.max_depth)]
                seen.setdefault(ns, None)
        ordered = sorted(seen)
        return ordered[int(op.offset) : int(op.offset) + int(op.limit)]

    # -- helpers --

    @staticmethod
    def _item(namespace: tuple[str, ...], key: str, row: Mapping[str, Any]) -> Any:
        return Item(
            value=_as_dict(row.get("value")),
            key=key,
            namespace=namespace,
            created_at=_dt(row.get("created_at")),
            updated_at=_dt(row.get("updated_at") or row.get("created_at")),
        )
