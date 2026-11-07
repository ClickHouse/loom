"""A point lookup that misses on a post-write path re-reads once with
sequential consistency; a hit never pays; `fresh=False` never retries; the
flag turns it off."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo

_ROW: dict[str, Any] = {
    "memory_id": "11111111-1111-1111-1111-111111111111",
    "org": "o",
    "namespace": "ns",
    "agent": "a",
    "user_id": "-",
    "session_id": "-",
    "memory_key": "k",
    "memory_type": "semantic",
    "value": "v",
    "confidence": 1.0,
    "version": 1,
    "pinned": 0,
    "created_at": __import__("datetime").datetime(2026, 9, 11, 12, 0),
}


class _CH:
    def __init__(self, answers: list[dict[str, Any] | None]):
        self.answers = list(answers)
        self.sqls: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        return self.answers.pop(0) if self.answers else None


def _ident() -> memory_repo.Identity:
    return memory_repo.Identity(org="o", namespace="ns", agent="a", user_id="-", session_id="-")


@pytest.mark.asyncio
async def test_miss_re_reads_once_with_sequential_consistency() -> None:
    ch = _CH([None, _ROW])
    row = await memory_repo.get_by_key(_ident(), "k", ch=ch, fresh=True, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    assert row is not None and row.memory_key == "k"
    assert len(ch.sqls) == 2
    assert "select_sequential_consistency" not in ch.sqls[0]
    assert ch.sqls[1].rstrip().endswith("SETTINGS select_sequential_consistency = 1")
    # the retry is the same statement with the setting appended, nothing else
    assert (
        ch.sqls[1].replace("SETTINGS select_sequential_consistency = 1", "").strip()
        == ch.sqls[0].strip()
    )


@pytest.mark.asyncio
async def test_hit_never_pays_and_a_double_miss_is_a_miss() -> None:
    ch = _CH([_ROW])
    assert (
        await memory_repo.get_by_key(_ident(), "k", ch=ch, fresh=True, scope=WHOLE_NAMESPACE)
        is not None
    )  # type: ignore[arg-type]
    assert len(ch.sqls) == 1

    ch = _CH([None, None])
    assert (
        await memory_repo.get_by_key(_ident(), "k", ch=ch, fresh=True, scope=WHOLE_NAMESPACE)
        is None
    )  # type: ignore[arg-type]
    assert len(ch.sqls) == 2  # exactly one retry, never a loop


@pytest.mark.asyncio
async def test_not_fresh_and_flag_off_do_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH([None, _ROW])
    assert await memory_repo.get_by_key(_ident(), "k", ch=ch, scope=WHOLE_NAMESPACE) is None  # type: ignore[arg-type]
    assert len(ch.sqls) == 1

    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes", False)
    ch = _CH([None, _ROW])
    assert (
        await memory_repo.get_by_key(_ident(), "k", ch=ch, fresh=True, scope=WHOLE_NAMESPACE)
        is None
    )  # type: ignore[arg-type]
    assert len(ch.sqls) == 1


@pytest.mark.asyncio
async def test_by_id_lookup_retries_the_same_way() -> None:
    # Two phases — the sort key is discovered without FINAL
    # (retried with sequential consistency on a fresh miss), then the row is
    # read under FINAL by that key. The fake answers: phase-1 miss, phase-1
    # retry hit, phase-2 hit.
    ch = _CH([None, _ROW, _ROW])
    row = await memory_repo.get_by_id(
        _ROW["memory_id"], org="o", namespace="ns", ch=ch, fresh=True, scope=WHOLE_NAMESPACE
    )  # type: ignore[arg-type]
    assert row is not None and len(ch.sqls) == 3
    assert "FROM loom.memory\n" in ch.sqls[0] and "FINAL" not in ch.sqls[0]
    assert "select_sequential_consistency = 1" in ch.sqls[1]
    assert "memory_id = {mid:UUID}" in ch.sqls[1] and "org = {org:String}" in ch.sqls[1]
    assert "FROM loom.memory FINAL" in ch.sqls[2]
    for pred in (
        "org = {org:String}",
        "namespace = {in_ns:String}",
        "created_at = {ca:DateTime}",
        "memory_key = {k:String}",
        "memory_id = {mid:UUID}",
        "tombstoned_at IS NULL",
    ):
        assert pred in ch.sqls[2], pred
    # a key that resolves to no live row (tombstoned, or a lagging replica) is None, no third read
    ch = _CH([_ROW, None])
    assert (
        await memory_repo.get_by_id(
            _ROW["memory_id"], org="o", namespace="ns", ch=ch, scope=WHOLE_NAMESPACE
        )
        is None
    )  # type: ignore[arg-type]
    assert len(ch.sqls) == 2


class _CHMany:
    def __init__(self, answers: list[list[dict[str, Any]]]):
        self.answers = list(answers)
        self.sqls: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        return self.answers.pop(0) if self.answers else []


@pytest.mark.asyncio
async def test_bulk_read_retries_once_when_some_keys_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_b = {**_ROW, "memory_key": "b", "memory_id": "22222222-2222-2222-2222-222222222222"}
    ch = _CHMany([[_ROW], [_ROW, row_b]])
    rows = await memory_repo.get_many_by_keys(_ident(), ["k", "b"], ch=ch, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    assert sorted(r.memory_key for r in rows) == ["b", "k"]
    assert len(ch.sqls) == 2 and "select_sequential_consistency = 1" in ch.sqls[1]

    # every key present first time → one query; nothing asked → no query
    ch = _CHMany([[_ROW, row_b]])
    assert (
        len(await memory_repo.get_many_by_keys(_ident(), ["k", "b"], ch=ch, scope=WHOLE_NAMESPACE))
        == 2
    )  # type: ignore[arg-type]
    assert len(ch.sqls) == 1

    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes", False)
    ch = _CHMany([[_ROW]])
    assert (
        len(await memory_repo.get_many_by_keys(_ident(), ["k", "b"], ch=ch, scope=WHOLE_NAMESPACE))
        == 1
    )  # type: ignore[arg-type]
    assert len(ch.sqls) == 1


def test_every_point_lookup_outside_the_repository_asks_for_fresh_rows() -> None:
    """Every handler/worker call to get_by_key / get_by_id passes fresh=True.
    The one deliberate exception would be a lookup whose miss means 'new key'
    on the hot write path; today there is none — the set precheck runs only
    under if_match_sha256, where the caller has just read the row."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "server"
    calls = 0
    for path in root.rglob("*.py"):
        if "memory_repo" in path.relative_to(root).parts:
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r"memory_repo\.get_by_(?:key|id)\((?:[^()]|\([^()]*\))*\)", text, re.S
        ):
            calls += 1
            assert "fresh=True" in m.group(0), f"{path.relative_to(root)}: {m.group(0)[:80]}"
    assert calls >= 20


@pytest.mark.asyncio
async def test_strong_reads_with_sequential_consistency_first() -> None:
    """A stale HIT (the previous version from a lagging replica) is not a
    miss, so the retry never fires; a read that feeds a write asks for
    sequential consistency up front — one query, the setting on it."""
    ch = _CH([_ROW])
    row = await memory_repo.get_by_key(
        _ident(), "k", ch=ch, fresh=True, strong=True, scope=WHOLE_NAMESPACE
    )  # type: ignore[arg-type]
    assert row is not None and len(ch.sqls) == 1
    assert ch.sqls[0].rstrip().endswith("SETTINGS select_sequential_consistency = 1")
    ch = _CH([_ROW, _ROW])
    await memory_repo.get_by_id(
        _ROW["memory_id"], org="o", namespace="ns", ch=ch, strong=True, scope=WHOLE_NAMESPACE
    )  # type: ignore[arg-type]
    # both phases of the by-id read carry the setting
    assert len(ch.sqls) == 2 and all("select_sequential_consistency = 1" in q for q in ch.sqls)


@pytest.mark.asyncio
async def test_strong_respects_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes", False)
    ch = _CH([_ROW])
    await memory_repo.get_by_key(_ident(), "k", ch=ch, strong=True, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    assert len(ch.sqls) == 1 and "select_sequential_consistency" not in ch.sqls[0]


@pytest.mark.asyncio
async def test_procedure_read_modify_write_reads_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    """procedure.evolve's read-before-write marks the memory.get strong, so the
    fold never runs against a previous version (a double fold)."""
    from types import SimpleNamespace as NS

    from server.memory.app.procedure import _load_procedure_value

    seen: list[Any] = []

    async def _get(inp: Any) -> Any:
        seen.append(inp)
        return NS(found=True, value={"steps": ["a"], "revision": 1}, content_sha256="abc")

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    value, sha = await _load_procedure_value("o", "ns", "a", "-", "-", "proc")
    assert sha == "abc" and value["steps"] == ["a"]
    assert getattr(seen[0], "_strong_read", False) is True


@pytest.mark.asyncio
async def test_memory_get_passes_strong_through_to_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryGetInput
    from server.memory.app import handlers as h

    seen: dict[str, Any] = {}

    async def _get_by_key(identity, key, **kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(memory_repo, "get_by_key", _get_by_key)
    inp = MemoryGetInput(org="o", namespace="ns", agent="a", memory_key="k")
    out = await h._handle_memory_get(inp)
    assert out.found is False and seen.get("strong") is False
    inp2 = MemoryGetInput(org="o", namespace="ns", agent="a", memory_key="k")
    inp2._strong_read = True
    await h._handle_memory_get(inp2)
    assert seen.get("strong") is True


@pytest.mark.asyncio
async def test_bulk_read_session_wildcard_drops_the_session_axis() -> None:
    """Session_id="*" reads the keys across every session of
    the identity, the way agent="*" already fans out across agents."""
    ch = _CHMany([[_ROW]])
    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="u", session_id="*")
    rows = await memory_repo.get_many_by_keys(ident, ["k"], ch=ch, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    assert [r.memory_key for r in rows] == ["k"]
    assert "session_id = {sid:String}" not in ch.sqls[0]
    ch = _CHMany([[_ROW]])
    await memory_repo.get_many_by_keys(_ident(), ["k"], ch=ch, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    assert "session_id = {sid:String}" in ch.sqls[0]


@pytest.mark.asyncio
async def test_bulk_read_user_wildcard_drops_the_user_axis() -> None:
    ch = _CHMany([[_ROW]])
    ident = memory_repo.Identity(org="o", namespace="n", agent="*", user_id="*", session_id="*")
    await memory_repo.get_many_by_keys(ident, ["k"], ch=ch, scope=WHOLE_NAMESPACE)  # type: ignore[arg-type]
    sql = ch.sqls[0]
    assert "user_id = {uid:String}" not in sql and "session_id = {sid:String}" not in sql
    assert "agent = {agent:String}" not in sql
