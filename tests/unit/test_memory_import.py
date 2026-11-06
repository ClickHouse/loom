"""memory.import: items another memory system distilled, written through the bulk
write path under keys derived from their text.

The key is a digest of the text, so a re-run writes nothing new and the item is embedded
and lexically indexed by its value alone; every check the bulk write makes answers per
entry; a dry run makes the checks and writes nothing; a registered category is linked
through the statements memory.assign_category uses, and an unregistered one is noted,
not refused.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value
from server.memory.api.models_bulk import (
    REFUSED,
    MemoryBulkSetInput,
    _BulkSetEntry,
)
from server.memory.api.models_import import MAX_IMPORT_ENTRIES, MemoryImportInput
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import memory_import as _import_mod
from server.memory.app.memory_import import handle_memory_import
from server.memory.domain.conflicts import ConflictPeer, ConflictResult
from server.memory.domain.embed_text import embedding_texts
from server.memory.domain.excerpt import EXCERPT_LIMIT, build_excerpt
from server.memory.domain.import_keys import import_key, is_import_key
from server.memory.domain.temporal_sql import LIVE_STATUSES
from server.operations.base import Role
from tests.unit.bulk_stubs import BatchEmbedder, StubCH, no_delete_in_flight, wire_stubs
from tests.unit.memory_rows import projection_row

_IDENT = {
    "org": "test",
    "namespace": "personal",
    "agent": "cursor",
    "user_id": "u",
    "session_id": "s",
}


def _row(**cols: Any) -> dict[str, Any]:
    """A live loom.memory row of the caller's identity unless `cols` say otherwise."""
    ident = {k: cols.pop(k, v) for k, v in _IDENT.items()}
    return projection_row(**ident, **cols)


def _identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row[k]) for k in (*_IDENT, "memory_key"))


def _tombstone(row: dict[str, Any]) -> dict[str, Any]:
    """The version a delete writes over `row`: the same row, newer, tombstoned."""
    return {**row, "version": int(row["version"]) + 1, "tombstoned_at": naive_utc_now()}


def _newest(rows: list[dict[str, Any]], key: Any) -> list[dict[str, Any]]:
    """The newest version (highest `version`) of each group of `rows` that `key` names."""
    out: dict[Any, dict[str, Any]] = {}
    for row in rows:
        group = key(row)
        if group not in out or int(row["version"]) > int(out[group]["version"]):
            out[group] = row
    return list(out.values())


class _StoreCH(StubCH):
    """A recording client that answers the reads of an import as the store would, from
    the stubbed rows plus every loom.memory row it was sent (so a second import meets the
    first one's rows), versions merged as ReplacingMergeTree merges them:

    - the live-row lookup: each asked identity key's newest version, unless a tombstone;
    - identity dedup: each identity key's newest version, kept when it holds an asked
      hash and is live;
    - the import's own read (`key_heads`): each asked key's newest version across
      sessions, a tombstone or a superseded row included.

    The category read answers the namespace's `registry` (name → id), and the link read
    the newest link row it was sent per (key, category)."""

    def __init__(
        self, rows: list[dict[str, Any]] | None = None, *, registry: dict[str, str] | None = None
    ) -> None:
        super().__init__()
        self.rows = list(rows or [])
        self.registry = dict(registry or {})

    def _stored(self, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        sent = [{**r, "value_text": r["value"]} for r in self.rows_for("loom.memory")]
        rows = [*self.rows, *sent]
        if params is None:
            return rows
        ident = (params["org"], params["ns"], params["agent"], params["uid"])
        return [r for r in rows if (r["org"], r["namespace"], r["agent"], r["user_id"]) == ident]

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        if "FROM loom.memory_categories" in sql:
            sent = self.rows_for("loom.memory_categories")
            asked = [r for r in sent if r["memory_key"] in params["keys"]]
            heads: dict[tuple[str, str], dict[str, Any]] = {}
            for r in asked:  # ReplacingMergeTree(created_at): the latest row of a link wins
                heads[(r["memory_key"], r["category_id"])] = r
            return [
                {"memory_key": k, "category_id": c, "live": r["tombstoned_at"] is None}
                for (k, c), r in heads.items()
            ]
        if "FROM loom.categories" in sql:
            return [
                {"name": n, "category_id": self.registry[n]}
                for n in params["names"]
                if n in self.registry
            ]
        if "IN {keys:Array(Tuple(" in sql:
            wanted = {tuple(k) for k in params["keys"]}
            asked = [r for r in self._stored() if _identity(r) in wanted]
            return [r for r in _newest(asked, _identity) if r["tombstoned_at"] is None]
        if "content_sha256 IN" in sql:
            heads = _newest(self._stored(params), _identity)
            return [
                {
                    "head_sha": r["content_sha256"],
                    "memory_key": r["memory_key"],
                    "memory_id": r["memory_id"],
                }
                for r in heads
                if r["content_sha256"] in params["shas"]
                and r["tombstoned_at"] is None
                and r["status"] in LIVE_STATUSES
            ]
        return []

    async def key_heads(
        self, org: str, ns: str, agent: str, uid: str, keys: list[str], **_: Any
    ) -> list[dict[str, Any]]:
        """`memory_repo.key_heads_across_sessions`: each key's newest version."""
        params = {"org": org, "ns": ns, "agent": agent, "uid": uid}
        asked = [r for r in self._stored(params) if r["memory_key"] in keys]
        return [
            {
                "memory_key": r["memory_key"],
                "deleted": r["tombstoned_at"] is not None,
                "head_status": r["status"],
                "head_sha256": r["content_sha256"],
                "head_sensitivity": r.get("sensitivity", ""),
            }
            for r in _newest(asked, lambda r: r["memory_key"])
        ]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """Wire the bulk write to a `_StoreCH` with nothing reaching a server, and record
    what it leaves: embedding rows, usage events and audit rows. `verdicts` scripts the
    conflict check per value (clear otherwise); `blacklist` names a refused substring."""
    import server.core.ch as ch_mod
    from server.settings import settings

    no_delete_in_flight(monkeypatch)
    # One conflict statement per fact: the scripted check stands in for it.
    monkeypatch.setattr(settings, "memory_write_candidate_batch", 0)
    real_client = ch_mod.get_client
    stubs: list[Any] = []

    def make(ch: _StoreCH, *, blacklist: str = "") -> SimpleNamespace:
        rec = SimpleNamespace(
            embedder=BatchEmbedder(), embedded=[], events=[], audits=[], verdicts={}
        )
        wire_stubs(monkeypatch, ch, rec.embedder, rec.embedded)
        stubs.append(lambda: ch)
        monkeypatch.setattr(ch_mod, "get_client", stubs[-1])
        monkeypatch.setattr(memory_repo, "key_heads_across_sessions", ch.key_heads)

        async def _find_match(*, org: str, namespace: str, value: str) -> Any:
            if blacklist and blacklist in value:
                return SimpleNamespace(pattern_id="p-1", scope="namespace", reason="test")
            return None

        async def _check(**kw: Any) -> ConflictResult:
            return rec.verdicts.get(
                json.loads(kw["new_value"]), ConflictResult(verdict="clear", peers=[])
            )

        async def _usage(**kw: Any) -> None:
            rec.events.append(kw)

        async def _audit(event_type: str, **kw: Any) -> str:
            rec.audits.append((event_type, kw))
            return ""

        async def _announce(_changes: Any) -> bool:
            return False

        monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _find_match)
        monkeypatch.setattr("server.memory.app.conflicts.check_on_commit", _check)
        monkeypatch.setattr("server.memory.store.usage.record_usage_event", _usage)
        monkeypatch.setattr("server.observability.audit.emit", _audit)
        monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
        return rec

    yield make
    # A module first imported during the test bound the stub with `from server.core.ch
    # import get_client`; give it the real client back, or later tests write into `ch`.
    for mod in list(sys.modules.values()):
        if any(getattr(mod, "get_client", None) is stub for stub in stubs):
            mod.get_client = real_client


def _input(*entries: dict[str, Any], dry_run: bool = False) -> MemoryImportInput:
    return MemoryImportInput(**_IDENT, entries=list(entries), dry_run=dry_run)


def _outcomes(out: Any) -> list[tuple[str, str]]:
    return [(r.outcome, r.reason) for r in out.results]


# ---- the key ---------------------------------------------------------------


def test_the_key_is_import_and_24_hex_of_the_sha256_of_the_text() -> None:
    value = "Prefers dark roast coffee."
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert import_key(value) == "import:" + digest[:24]


def test_the_key_hashes_the_text_as_given() -> None:
    assert import_key("Prefers dark roast.") != import_key("Prefers dark roast. ")
    assert import_key("Prefers dark roast.") != import_key("prefers dark roast.")


# ---- the embedded text -----------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [import_key("Prefers dark roast."), "import:" + "0" * 24, "import:0123456789abcdef01234567"],
)
def test_a_key_of_the_derived_shape_is_an_import_key(key: str) -> None:
    assert is_import_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "import:" + "0" * 23,
        "import:" + "0" * 25,
        "import:" + "A" * 24,  # import_key derives lower case only
        "import:" + "g" * 24,
        "Import:" + "0" * 24,
        "imported:" + "0" * 24,
        "recall_memories_" + "0" * 24,
        " import:" + "0" * 24,
        "import:" + "0" * 24 + "\n",
        "import:" + "0" * 24 + "_notes",
        "import:dark_roast",
        "import:",
        "",
    ],
)
def test_any_other_key_is_not_an_import_key(key: str) -> None:
    assert not is_import_key(key)


def test_an_import_key_adds_nothing_to_the_text_and_a_readable_key_keeps_its_suffix() -> None:
    value = json.dumps("Prefers dark roast coffee.")
    digest = import_key("Prefers dark roast coffee.")
    assert embedding_texts("semantic", value, value, digest) == (value, value, "")
    framed = f"{value} (#coffee_order)"
    assert embedding_texts("semantic", value, value, "coffee_order") == (
        framed,
        framed,
        "coffee order",
    )
    # A key that only resembles a digest keeps its words.
    near = "import:" + "0" * 23
    assert build_excerpt(value, memory_key=near) == f"{value} (#{near})"
    # With no suffix to make room for, a long value keeps the whole clip.
    long = "x" * (EXCERPT_LIMIT + 50)
    assert build_excerpt(long, memory_key=digest) == "x" * EXCERPT_LIMIT


@pytest.mark.asyncio
async def test_an_imported_item_is_embedded_and_indexed_by_its_value_alone(harness) -> None:
    """The key is a digest with no words in it: the text embedded and lexically indexed
    is the value as stored, with no `(#key)` suffix, and no key-axis vector is built."""
    ch = _StoreCH()
    rec = harness(ch)
    await handle_memory_import(_input({"value": "Prefers dark roast coffee."}))
    (row,) = ch.rows_for("loom.memory")
    assert row["value"] == json.dumps("Prefers dark roast coffee.")
    assert {text for batch in rec.embedder.batches for text in batch} == {row["value"]}
    (embedding,) = rec.embedded
    assert embedding["content_excerpt"] == row["value"]
    assert embedding["memory_key_embedding_dim"] == 0


@pytest.mark.asyncio
async def test_a_readable_key_still_frames_its_value(harness) -> None:
    ch = _StoreCH()
    rec = harness(ch)
    bulk = MemoryBulkSetInput(
        **_IDENT, entries=[_BulkSetEntry(memory_key="coffee_order", value="Dark roast.")]
    )
    await _bulk_set_mod.handle_memory_bulk_set(bulk)
    framed = json.dumps("Dark roast.") + " (#coffee_order)"
    assert {text for batch in rec.embedder.batches for text in batch} == {framed, "coffee order"}
    (embedding,) = rec.embedded
    assert embedding["content_excerpt"] == framed
    assert embedding["memory_key_embedding_dim"] == rec.embedder.dim


@pytest.mark.asyncio
async def test_a_restatement_of_an_imported_key_is_embedded_as_the_import_was(harness) -> None:
    """memory.set (whose texts `embedding_texts` builds) and memory.bulk_set write a digest
    key's item under the text the import wrote, so the two vectors stay comparable."""
    value = "Prefers dark roast coffee."
    stored = json.dumps(value)
    assert embedding_texts("semantic", value, stored, import_key(value)) == (stored, stored, "")
    ch = _StoreCH()
    rec = harness(ch)
    bulk = MemoryBulkSetInput(
        **_IDENT, entries=[_BulkSetEntry(memory_key=import_key(value), value=value)]
    )
    await _bulk_set_mod.handle_memory_bulk_set(bulk)
    assert {text for batch in rec.embedder.batches for text in batch} == {stored}
    (embedding,) = rec.embedded
    assert (embedding["content_excerpt"], embedding["memory_key_embedding_dim"]) == (stored, 0)


# ---- writes ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_item_is_written_under_its_key_as_imported(harness) -> None:
    ch = _StoreCH()
    rec = harness(ch)
    known = datetime(2024, 5, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    out = await handle_memory_import(
        _input(
            {
                "value": "Dog is Biscuit.",
                "tags": ["pets"],
                "valid_at": known,
                "source_trace": "n/7",
            },
            {"value": "Deploys happen on Tuesday."},
        )
    )
    keys = [import_key("Dog is Biscuit."), import_key("Deploys happen on Tuesday.")]
    assert [(r.index, r.key) for r in out.results] == [(0, keys[0]), (1, keys[1])]
    assert _outcomes(out) == [("written", ""), ("written", "")]
    assert out.totals.model_dump() == {"written": 2, "held": 0, "skipped": 0, "refused": 0}
    rows = {r["memory_key"]: r for r in ch.rows_for("loom.memory")}
    dog, deploys = rows[keys[0]], rows[keys[1]]
    assert dog["value"] == json.dumps("Dog is Biscuit.") and "pets" in dog["tags"]
    assert {dog["source_class"], deploys["source_class"]} == {"imported"}
    assert {r["source_class"] for r in rec.embedded} == {"imported"}
    # The source's time is the validity time, as naive UTC; creation is the server's.
    assert dog["valid_at"] == datetime(2024, 5, 1, 10, 0)
    assert abs(naive_utc_now() - dog["created_at"]) < timedelta(minutes=5)
    assert deploys["valid_at"] is None
    # An item that names no source is traced by its key.
    assert (dog["source_trace"], deploys["source_trace"]) == ("n/7", keys[1])


@pytest.mark.asyncio
async def test_reruns_write_nothing_new_and_reinforce_nothing(harness) -> None:
    """Three re-runs of two items add no row, no vector and no usage event."""
    ch = _StoreCH()
    rec = harness(ch)
    items = ({"value": "Dog is Biscuit."}, {"value": "Deploys happen on Tuesday."})
    first = await handle_memory_import(_input(*items))
    assert first.totals.written == 2
    rows, vectors = len(ch.rows_for("loom.memory")), len(rec.embedded)
    for _ in range(3):
        again = await handle_memory_import(_input(*items))
        assert _outcomes(again) == [("skipped", "unchanged"), ("skipped", "unchanged")]
        assert again.totals.model_dump() == {
            "written": 0,
            "held": 0,
            "skipped": 2,
            "refused": 0,
        }
    assert (len(ch.rows_for("loom.memory")), len(rec.embedded)) == (rows, vectors) == (2, 2)
    assert rec.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dedup_scope", ["identity", "session"])
async def test_the_item_imported_in_another_session_is_skipped(
    harness, monkeypatch, dedup_scope: str
) -> None:
    """The heads read spans sessions, so the key holding the text in another session is
    the item already imported, whatever scope the bulk write's own dedup has."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_bulk_dedup_scope", dedup_scope)
    value = "Dog is Biscuit."
    ch = _StoreCH(
        [
            _row(
                memory_key=import_key(value),
                session_id="earlier",
                memory_id="mid-1",
                content_sha256=sha256_value(value),
            )
        ]
    )
    rec = harness(ch)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", "unchanged")]
    assert ch.rows_for("loom.memory") == [] and rec.events == []


@pytest.mark.asyncio
async def test_a_rerun_keeps_the_stored_labels(harness) -> None:
    value = "Dog is Biscuit."
    ch = _StoreCH()
    harness(ch)
    await handle_memory_import(_input({"value": value, "tags": ["pets"]}))
    again = await handle_memory_import(_input({"value": value, "tags": ["animals"]}))
    assert _outcomes(again) == [("skipped", "unchanged")]
    (row,) = ch.rows_for("loom.memory")
    assert "pets" in row["tags"] and "animals" not in row["tags"]


@pytest.mark.asyncio
async def test_a_rerun_naming_another_sensitivity_says_the_stored_level_stands(harness) -> None:
    """The stored row keeps its level, and the entry says so rather than `unchanged`."""
    value = "Dog is Biscuit."
    ch = _StoreCH(registry={"pets": "cat-1"})
    harness(ch)
    await handle_memory_import(_input({"value": value, "sensitivity": "low"}))
    again = await handle_memory_import(
        _input({"value": value, "sensitivity": "high", "category": "pets"})
    )
    assert [(r.outcome, r.reason, r.note) for r in again.results] == [
        ("skipped", "sensitivity_kept", "")
    ]
    (row,) = ch.rows_for("loom.memory")
    assert row["sensitivity"] == "low"
    same = await handle_memory_import(_input({"value": value, "sensitivity": "low"}))
    assert _outcomes(same) == [("skipped", "unchanged")]


@pytest.mark.asyncio
async def test_the_same_text_under_another_key_is_held_and_never_reinforced(harness) -> None:
    value = "Dog is Biscuit."
    ch = _StoreCH(
        [
            _row(
                memory_key="user_dog_name",
                memory_id="mid-keeper",
                content_sha256=sha256_value(value),
            )
        ]
    )
    rec = harness(ch)
    for _ in range(2):
        out = await handle_memory_import(_input({"value": value}))
        assert _outcomes(out) == [("held", "identity_duplicate")]
    assert out.results[0].held_by == "user_dog_name"
    assert out.totals.held == 1 and ch.rows_for("loom.memory") == []
    assert rec.events == []


@pytest.mark.asyncio
async def test_a_text_repeated_in_one_request_is_written_once(harness) -> None:
    ch = _StoreCH(registry={"pets": "cat-1"})
    harness(ch)
    item = {"value": "Dog is Biscuit.", "category": "pets"}
    out = await handle_memory_import(_input(item, item))
    assert _outcomes(out) == [("written", ""), ("skipped", "repeated")]
    assert out.results[0].key == out.results[1].key
    assert len(ch.rows_for("loom.memory")) == 1
    # The repeat's key holds the item, so its category is linked.
    links = ch.rows_for("loom.memory_categories")
    assert [(r["memory_key"], r["category_id"]) for r in links] == [(out.results[0].key, "cat-1")]


def test_a_repeat_with_other_labels_is_refused() -> None:
    """A repeat is not written again, so labels it adds would be dropped."""
    with pytest.raises(pydantic.ValidationError, match="entries 0 and 1"):
        _input({"value": "Dog is Biscuit."}, {"value": "Dog is Biscuit.", "sensitivity": "high"})


@pytest.mark.asyncio
async def test_a_repeat_of_a_refused_entry_is_refused_as_it_was(harness) -> None:
    secret = "The launch code is zq-1."
    ch = _StoreCH()
    harness(ch, blacklist="launch code")
    out = await handle_memory_import(_input({"value": secret}, {"value": secret}))
    assert _outcomes(out) == [("refused", "blacklist"), ("refused", "blacklist")]
    assert out.totals.refused == 2 and ch.rows_for("loom.memory") == []


# ---- deleted and superseded items ------------------------------------------


@pytest.mark.asyncio
async def test_a_rerun_leaves_an_item_its_owner_deleted_deleted(harness) -> None:
    """No live row holds a deleted key, so a bulk write alone would bring it back; the
    import reads the key's newest version, a tombstone, and skips it."""
    ch = _StoreCH(registry={"pets": "cat-1"})
    rec = harness(ch)
    item = {"value": "Dog is Biscuit.", "category": "pets"}
    await handle_memory_import(_input(item))
    (row,) = ch.rows_for("loom.memory")
    ch.rows.append(_tombstone(row))
    again = await handle_memory_import(_input(item, item))
    assert _outcomes(again) == [("skipped", "deleted"), ("skipped", "deleted")]
    assert len(ch.rows_for("loom.memory")) == 1 and rec.events == []
    assert len(ch.rows_for("loom.memory_categories")) == 1  # the first import's link only
    bulk = MemoryBulkSetInput(
        **_IDENT, entries=[_BulkSetEntry(memory_key=row["memory_key"], value="Dog is Biscuit.")]
    )
    assert (await _bulk_set_mod.handle_memory_bulk_set(bulk, dry_run=True)).written[0].version


@pytest.mark.asyncio
async def test_an_item_superseded_in_another_session_is_not_recreated(harness) -> None:
    """Identity dedup counts no superseded row, so another session would write the fact
    again as a new active row; the import skips the key its newest version retired."""
    value = "Deploys happen on Tuesday."
    retired = _row(
        memory_key=import_key(value),
        session_id="earlier",
        content_sha256=sha256_value(value),
        status="superseded",
        version=5,
    )
    ch = _StoreCH([retired])
    harness(ch)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", "superseded")]
    assert ch.rows_for("loom.memory") == []


@pytest.mark.asyncio
async def test_a_key_its_owner_corrected_is_left_corrected(harness) -> None:
    value = "Dog is Biscuit."
    corrected = _row(memory_key=import_key(value), value="Dog is Rex.", content_sha256="f" * 64)
    ch = _StoreCH([corrected])
    harness(ch)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", "revised")]
    assert ch.rows_for("loom.memory") == []


@pytest.mark.asyncio
async def test_a_key_restored_after_its_delete_is_unchanged(harness) -> None:
    ch = _StoreCH()
    harness(ch)
    item = {"value": "Dog is Biscuit."}
    await handle_memory_import(_input(item))
    (row,) = ch.rows_for("loom.memory")
    tombstone = _tombstone(row)
    ch.rows += [tombstone, {**row, "version": int(tombstone["version"]) + 1}]
    again = await handle_memory_import(_input(item))
    assert _outcomes(again) == [("skipped", "unchanged")]


# ---- outcome mapping -------------------------------------------------------


def test_every_verdict_a_bulk_write_refuses_names_its_check() -> None:
    assert set(_import_mod._CHECK_OF) == REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["s", "earlier"])
async def test_a_rejected_key_is_left_rejected_in_every_session(harness, session: str) -> None:
    """memory.reject flips the status in place, with no tombstone: the live-row lookup
    still sees the row, and identity dedup does not. The key stays rejected, whichever
    session imports it, and nothing is written or linked."""
    value = "Dog is Biscuit."
    rejected = _row(
        memory_key=import_key(value),
        session_id=session,
        content_sha256=sha256_value(value),
        status="rejected",
    )
    ch = _StoreCH([rejected], registry={"pets": "cat-1"})
    rec = harness(ch)
    out = await handle_memory_import(
        _input({"value": value, "category": "pets"}, {"value": "Cat is Miso."})
    )
    assert _outcomes(out) == [("skipped", "rejected"), ("written", "")]
    assert [r["memory_key"] for r in ch.rows_for("loom.memory")] == [import_key("Cat is Miso.")]
    assert ch.rows_for("loom.memory_categories") == [] and rec.events == []


@pytest.mark.asyncio
async def test_a_proposed_key_is_left_on_trial(harness) -> None:
    """A proposal moves only through memory.proposal_decide: importing its text does not
    write a new row or promote it."""
    value = "Deploys happen on Tuesday."
    proposal = _row(
        memory_key=import_key(value), content_sha256=sha256_value(value), status="proposed"
    )
    ch = _StoreCH([proposal])
    harness(ch)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", "proposed")]
    assert ch.rows_for("loom.memory") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["s", "earlier"])
async def test_a_concurrent_import_landing_after_the_read_is_unchanged(
    harness, monkeypatch, session: str
) -> None:
    """Another import of the same text lands between this import's read of the key and
    the bulk write's lookup: the skip gate (same session) or identity dedup (another
    session) finds the row, and the entry is skipped as unchanged, not crashed on."""
    value = "Dog is Biscuit."
    ch = _StoreCH()
    rec = harness(ch)
    read = memory_repo.key_heads_across_sessions

    async def _read_then_race(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        heads = await read(*args, **kwargs)
        ch.rows.append(
            _row(
                memory_key=import_key(value), session_id=session, content_sha256=sha256_value(value)
            )
        )
        return heads

    monkeypatch.setattr(memory_repo, "key_heads_across_sessions", _read_then_race)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", "unchanged")]
    assert ch.rows_for("loom.memory") == [] and rec.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("tombstoned", "deleted"),
        # A status a newer schema adds, which this build does not know.
        ("archived", "unknown"),
    ],
)
async def test_a_head_status_that_is_not_live_leaves_the_key_as_it_is(
    harness, caplog, status: str, reason: str
) -> None:
    value = "Dog is Biscuit."
    head = _row(memory_key=import_key(value), content_sha256=sha256_value(value), status=status)
    ch = _StoreCH([head])
    harness(ch)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", reason)]
    assert ch.rows_for("loom.memory") == []
    assert ("unknown head status" in caplog.text) == (reason == "unknown")


@pytest.mark.asyncio
async def test_a_pin_or_lock_landing_after_the_read_refuses_the_entry(harness, monkeypatch) -> None:
    """The heads read saw nothing; then the keys were pinned and locked with other text.
    The bulk write's own guards refuse the entries."""
    pinned, locked = "Dog is Biscuit.", "Cat is Miso."
    ch = _StoreCH()
    harness(ch)
    read = memory_repo.key_heads_across_sessions

    async def _read_then_race(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        heads = await read(*args, **kwargs)
        ch.rows += [
            _row(memory_key=import_key(pinned), content_sha256="0" * 64, pinned=1),
            _row(memory_key=import_key(locked), content_sha256="1" * 64, lock_mode="read_only"),
        ]
        return heads

    monkeypatch.setattr(memory_repo, "key_heads_across_sessions", _read_then_race)
    out = await handle_memory_import(_input({"value": pinned}, {"value": locked}))
    assert _outcomes(out) == [("refused", "pinned"), ("refused", "locked")]
    assert ch.rows_for("loom.memory") == []


# ---- the inherited checks, per entry ---------------------------------------


@pytest.mark.asyncio
async def test_the_refusals_and_corrected_keys_are_named(harness) -> None:
    """A pinned or locked key holding other text was corrected by its owner: it is left as
    `revised` before the bulk write's own pin and lock guards are reached."""
    plain, secret = "Deploys happen on Tuesday.", "The launch code is zq-1."
    pinned, locked, close = "Dog is Biscuit.", "Cat is Miso.", "Deploys happen on Friday."
    ch = _StoreCH(
        [
            _row(memory_key=import_key(pinned), content_sha256="0" * 64, pinned=1),
            _row(memory_key=import_key(locked), content_sha256="1" * 64, lock_mode="read_only"),
        ],
        registry={"pets": "cat-1"},
    )
    rec = harness(ch, blacklist="launch code")
    rec.verdicts[close] = ConflictResult(verdict="rejected", peers=[], pinned_blocker_id="m-pin")
    out = await handle_memory_import(
        _input(
            {"value": plain},
            {"value": secret},
            {"value": pinned, "category": "pets"},
            {"value": locked},
            {"value": close},
        )
    )
    assert _outcomes(out) == [
        ("written", ""),
        ("refused", "blacklist"),
        ("skipped", "revised"),
        ("skipped", "revised"),
        ("refused", "pinned_peer"),
    ]
    assert out.totals.model_dump() == {"written": 1, "held": 0, "skipped": 2, "refused": 2}
    assert [r["memory_key"] for r in ch.rows_for("loom.memory")] == [import_key(plain)]
    # A refused or skipped entry's category is not linked.
    assert ch.rows_for("loom.memory_categories") == []


@pytest.mark.asyncio
async def test_the_conflict_verdicts_that_write_or_hold_are_named(harness, monkeypatch) -> None:
    same, near = "Prod deploys are Tuesdays.", "Prod deploys happen Tuesdays."
    other = "Prod deploys are Fridays."
    ch = _StoreCH()
    rec = harness(ch)

    marks: list[tuple[str, list[str]]] = []

    async def _mark(*, org: str, new_memory_id: str, peer_ids: list[str]) -> str:
        marks.append((new_memory_id, peer_ids))  # the mark lands and flags the new row
        return "conflict"

    monkeypatch.setattr("server.memory.app.conflicts.mark_peers_conflicted", _mark)
    rec.verdicts[same] = ConflictResult(verdict="merge", peers=[], existing_memory_id="m-keep")
    rec.verdicts[near] = ConflictResult(verdict="duplicate", peers=[], duplicate_of_id="m-peer")
    rec.verdicts[other] = ConflictResult(
        verdict="conflict",
        peers=[ConflictPeer("m-rival", "deploy_day", 0.9, False, "", agent="cursor", user_id="u")],
    )
    out = await handle_memory_import(_input({"value": same}, {"value": near}, {"value": other}))
    assert _outcomes(out) == [
        ("held", "merge"),
        ("written", "duplicate"),
        ("written", "conflicted"),
    ]
    rows = {r["memory_key"]: r for r in ch.rows_for("loom.memory")}
    assert (rows[import_key(near)]["status"], rows[import_key(near)]["duplicate_of"]) == (
        "duplicate",
        "m-peer",
    )
    assert marks == [(rows[import_key(other)]["memory_id"], ["m-rival"])]
    assert rec.events == []  # the merge target is not reinforced


@pytest.mark.asyncio
async def test_an_item_that_retires_a_live_memory_says_so(harness, monkeypatch) -> None:
    """Under newest_wins the conflict check can answer `refines`: the item is written and
    retires the live memories it updates, and the entry names that."""
    value = "Prod deploys are Fridays."
    peer = ConflictPeer("m-old", "deploy_day", 0.9, False, "", agent="cursor", user_id="u")
    ch = _StoreCH()
    rec = harness(ch)
    rec.verdicts[value] = ConflictResult(verdict="refines", peers=[peer], refined_peers=[peer])
    retired: list[tuple[str, list[str]]] = []

    async def _refine(*, new_memory_key: str, refined_peers: list[Any], **_: Any) -> int:
        retired.append((new_memory_key, [p.memory_id for p in refined_peers]))
        return len(refined_peers)

    monkeypatch.setattr("server.memory.app.conflicts.apply_refines", _refine)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("written", "refines")]
    assert retired == [(import_key(value), ["m-old"])]


_CAPITALS = [
    "The capital of Soviet Union is Moscow",
    "The capital of Soviet Union is Russellville",
    "The capital of Soviet Union is Paris",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid_at", "survivor"),
    [
        (["2024-06-01T00:00:00Z", "2021-01-01T00:00:00"], 0),  # newest first; naive is UTC
        (["2021-01-01T00:00:00", "2024-06-01T00:00:00Z"], 1),
        ([None, None], 1),  # undated: the later in the request
        (["2021-01-01T00:00:00Z", None], 1),  # an undated item counts as said now
        ([None, "2021-01-01T00:00:00Z"], 0),
        (["2024-06-01T00:00:00Z", None, "2021-01-01T00:00:00Z"], 1),
    ],
)
async def test_an_import_keeps_the_newest_statement_of_an_attribute(
    harness, monkeypatch, valid_at: list[str | None], survivor: int
) -> None:
    """Under newest_wins, entries stating one attribute differently: whatever order the
    source exported them in, only the newest stays active, and each result keeps its own
    entry's key."""

    async def _policy(*_: Any, **__: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", _policy)
    ch = _StoreCH()
    harness(ch)
    entries = [
        {"value": value, "valid_at": at}
        for value, at in zip(_CAPITALS[: len(valid_at)], valid_at, strict=True)
    ]
    out = await handle_memory_import(_input(*entries))
    assert [r.key for r in out.results] == [import_key(e["value"]) for e in entries]
    assert {r.outcome for r in out.results} == {"written"}
    status = {json.loads(r["value"]): r["status"] for r in ch.rows_for("loom.memory")}
    assert [status[e["value"]] for e in entries] == [
        "active" if i == survivor else "superseded" for i in range(len(entries))
    ]


@pytest.mark.asyncio
async def test_an_import_never_runs_the_value_update_linker(harness, monkeypatch) -> None:
    """The extraction path's linker may retire a live memory one value token away; an
    import leaves updates to its conflict verdict, whatever the setting says."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_value_updates", True)
    linked: list[Any] = []

    async def _link(**kw: Any) -> Any:
        linked.append(kw)
        return SimpleNamespace(links=[])

    monkeypatch.setattr("server.memory.app.value_updates.link_value_updates", _link)
    ch = _StoreCH()
    harness(ch)
    out = await handle_memory_import(_input({"value": "Dog is Biscuit."}))
    assert _outcomes(out) == [("written", "")] and linked == []


@pytest.mark.asyncio
async def test_the_sensitivity_rule_applies_to_an_imported_item(harness) -> None:
    ch = _StoreCH()
    rec = harness(ch)
    await handle_memory_import(
        _input({"value": "Badge number is 4242.", "sensitivity": "high"}, {"value": "Likes tea."})
    )
    by_key = {r["memory_key"]: r for r in ch.rows_for("loom.memory")}
    assert by_key[import_key("Badge number is 4242.")]["sensitivity"] == "high"
    # A high row is never embedded.
    assert [r["memory_key"] for r in rec.embedded] == [import_key("Likes tea.")]


# ---- dry run ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dry_run_makes_every_check_and_writes_nothing(harness) -> None:
    ch = _StoreCH(registry={"pets": "cat-1"})
    rec = harness(ch, blacklist="launch code")
    out = await handle_memory_import(
        _input(
            {"value": "Dog is Biscuit.", "category": "pets"},
            {"value": "The launch code is zq-1."},
            {"value": "Cat is Miso.", "category": "nope"},
            dry_run=True,
        )
    )
    assert out.dry_run is True
    assert [(r.outcome, r.reason, r.note) for r in out.results] == [
        ("written", "", ""),
        ("refused", "blacklist", ""),
        ("written", "", "category_unregistered"),
    ]
    assert ch.insert_calls == [] and rec.embedded == []
    assert rec.events == [] and rec.audits == []
    # The checks ran: the values were embedded for the conflict check.
    assert rec.embedder.batches


@pytest.mark.asyncio
async def test_a_dry_run_answers_every_outcome_the_real_run_does(harness) -> None:
    """One request reaching every outcome and reason answers the same dry and for real,
    and the dry run writes no row, vector, event or audit row."""
    texts = {
        "plain": "Deploys happen on Tuesday.",
        "secret": "The launch code is zq-1.",
        "pinned": "Dog is Biscuit.",
        "locked": "Cat is Miso.",
        "close": "Deploys happen on Friday.",
        "same": "Prod deploys are Tuesdays.",
        "near": "Prod deploys happen Tuesdays.",
        "elsewhere": "The wiki lives at docs.example.test.",
        "held": "Lunch is at noon.",
        "deleted": "Standup is at nine.",
        "retired": "Retro is on Friday.",
        "rejected": "Lunch moved to one.",
        "proposed": "Demos are on Thursday.",
    }
    deleted = _row(
        memory_key=import_key(texts["deleted"]),
        content_sha256=sha256_value(texts["deleted"]),
        version=1,
    )
    ch = _StoreCH(
        [
            _row(memory_key=import_key(texts["pinned"]), content_sha256="0" * 64, pinned=1),
            _row(
                memory_key=import_key(texts["locked"]),
                content_sha256="1" * 64,
                lock_mode="read_only",
            ),
            _row(memory_key="wiki_location", content_sha256=sha256_value(texts["elsewhere"])),
            _row(memory_key=import_key(texts["held"]), content_sha256=sha256_value(texts["held"])),
            deleted,
            _tombstone(deleted),
            _row(
                memory_key=import_key(texts["retired"]),
                session_id="earlier",
                content_sha256=sha256_value(texts["retired"]),
                status="superseded",
            ),
            _row(
                memory_key=import_key(texts["rejected"]),
                content_sha256=sha256_value(texts["rejected"]),
                status="rejected",
            ),
            _row(
                memory_key=import_key(texts["proposed"]),
                session_id="earlier",
                content_sha256=sha256_value(texts["proposed"]),
                status="proposed",
            ),
        ]
    )
    rec = harness(ch, blacklist="launch code")
    rec.verdicts[texts["close"]] = ConflictResult(
        verdict="rejected", peers=[], pinned_blocker_id="m-pin"
    )
    rec.verdicts[texts["same"]] = ConflictResult(
        verdict="merge", peers=[], existing_memory_id="m-keep"
    )
    rec.verdicts[texts["near"]] = ConflictResult(
        verdict="duplicate", peers=[], duplicate_of_id="m-peer"
    )
    entries = [{"value": text} for text in texts.values()]
    entries += [{"value": texts["plain"]}, {"value": texts["secret"]}]
    expected = [
        ("written", ""),
        ("refused", "blacklist"),
        ("skipped", "revised"),
        ("skipped", "revised"),
        ("refused", "pinned_peer"),
        ("held", "merge"),
        ("written", "duplicate"),
        ("held", "identity_duplicate"),
        ("skipped", "unchanged"),
        ("skipped", "deleted"),
        ("skipped", "superseded"),
        ("skipped", "rejected"),
        ("skipped", "proposed"),
        ("skipped", "repeated"),
        ("refused", "blacklist"),
    ]
    dry = await handle_memory_import(_input(*entries, dry_run=True))
    assert _outcomes(dry) == expected and dry.dry_run is True
    assert ch.insert_calls == [] and rec.embedded == []
    assert rec.events == [] and rec.audits == []
    real = await handle_memory_import(_input(*entries))
    assert _outcomes(real) == expected and real.dry_run is False
    assert sorted(r["memory_key"] for r in ch.rows_for("loom.memory")) == sorted(
        [import_key(texts["plain"]), import_key(texts["near"])]
    )


# ---- categories ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_registered_category_is_linked_and_an_unregistered_one_noted(harness) -> None:
    ch = _StoreCH(registry={"pets": "cat-1"})
    rec = harness(ch)
    out = await handle_memory_import(
        _input(
            {"value": "Dog is Biscuit.", "category": "pets"},
            {"value": "Cat is Miso.", "category": "nope"},
        )
    )
    assert [(r.outcome, r.note) for r in out.results] == [
        ("written", ""),
        ("written", "category_unregistered"),
    ]
    links = ch.rows_for("loom.memory_categories")
    assert [(r["memory_key"], r["category_id"], r["tombstoned_at"]) for r in links] == [
        (import_key("Dog is Biscuit."), "cat-1", None)
    ]
    audited = dict(rec.audits)
    assert audited["memory.import"]["subject"]["category_links"] == [
        {"memory_key": import_key("Dog is Biscuit."), "category": "pets"}
    ]
    # The audit rows hold keys and hashes, never the text.
    assert "content_sha256" in audited["memory.bulk_set"]["subject"]
    assert "Biscuit" not in json.dumps(rec.audits, default=str)


@pytest.mark.asyncio
async def test_a_rerun_links_a_category_registered_since(harness) -> None:
    ch = _StoreCH()
    harness(ch)
    item = {"value": "Dog is Biscuit.", "category": "pets"}
    first = await handle_memory_import(_input(item))
    assert first.results[0].note == "category_unregistered"
    assert ch.rows_for("loom.memory_categories") == []
    ch.registry["pets"] = "cat-1"
    again = await handle_memory_import(_input(item))
    assert (again.results[0].outcome, again.results[0].note) == ("skipped", "")
    links = ch.rows_for("loom.memory_categories")
    assert [r["memory_key"] for r in links] == [import_key("Dog is Biscuit.")]


@pytest.mark.asyncio
async def test_a_rerun_leaves_a_rescinded_link_rescinded_and_audits_nothing(harness) -> None:
    from server.memory.store import categories

    ch = _StoreCH(registry={"pets": "cat-1"})
    rec = harness(ch)
    item = {"value": "Dog is Biscuit.", "category": "pets"}
    await handle_memory_import(_input(item))
    key = import_key("Dog is Biscuit.")
    await categories.link("test", "personal", [(key, "cat-1")], tombstone=True, ch=ch)
    rec.audits.clear()
    again = await handle_memory_import(_input(item))
    assert (again.results[0].outcome, again.results[0].note) == ("skipped", "category_rescinded")
    links = ch.rows_for("loom.memory_categories")
    assert len(links) == 2 and links[-1]["tombstoned_at"] is not None
    assert "memory.import" not in dict(rec.audits)


@pytest.mark.asyncio
async def test_a_rerun_adds_no_link_already_stored(harness) -> None:
    ch = _StoreCH(registry={"pets": "cat-1"})
    rec = harness(ch)
    item = {"value": "Dog is Biscuit.", "category": "pets"}
    await handle_memory_import(_input(item))
    rec.audits.clear()
    again = await handle_memory_import(_input(item))
    assert (again.results[0].outcome, again.results[0].note) == ("skipped", "")
    assert len(ch.rows_for("loom.memory_categories")) == 1
    assert "memory.import" not in dict(rec.audits)


@pytest.mark.parametrize(
    ("outcome", "reason", "linkable", "registered", "stored_live", "note"),
    [
        ("written", "", False, True, None, "category_refused"),
        ("written", "", True, False, None, "category_unregistered"),
        ("skipped", "revised", True, True, True, ""),  # a live link stays and says nothing
        ("written", "", True, True, False, "category_rescinded"),
        ("refused", "blacklist", True, True, None, "category_not_linked"),
        ("held", "merge", True, True, None, "category_not_linked"),
        ("written", "", True, True, None, ""),  # no note: the key is linked
        ("skipped", "unchanged", True, True, None, ""),
        ("skipped", "repeated", True, True, None, ""),
    ],
)
def test_the_category_note_of_each_case(
    outcome: str,
    reason: str,
    linkable: bool,
    registered: bool,
    stored_live: bool | None,
    note: str,
) -> None:
    from server.memory.domain.memory_import import category_note

    got = category_note(
        outcome,
        reason,
        linkable=linkable,
        registered=registered,
        stored_live=stored_live,
    )
    assert got == note


@pytest.mark.asyncio
async def test_a_corrected_key_keeps_its_link_and_answers_no_note(harness) -> None:
    """The key was imported and linked, then its owner corrected it: the re-run answers
    `revised` and says nothing about a category the key is still linked to."""
    value = "Dog is Biscuit."
    ch = _StoreCH(registry={"pets": "cat-1"})
    harness(ch)
    item = {"value": value, "category": "pets"}
    await handle_memory_import(_input(item))
    ch.rows.append(_row(memory_key=import_key(value), version=2**62, content_sha256="f" * 64))
    again = await handle_memory_import(_input(item))
    assert [(r.outcome, r.reason, r.note) for r in again.results] == [("skipped", "revised", "")]
    assert len(ch.rows_for("loom.memory_categories")) == 1


@pytest.mark.asyncio
async def test_a_category_on_an_entry_its_key_does_not_hold_is_not_linked(harness) -> None:
    ch = _StoreCH(registry={"pets": "cat-1"})
    harness(ch, blacklist="launch code")
    out = await handle_memory_import(_input({"value": "The launch code is 1.", "category": "pets"}))
    assert [(r.outcome, r.note) for r in out.results] == [("refused", "category_not_linked")]
    assert ch.rows_for("loom.memory_categories") == []


@pytest.mark.asyncio
async def test_a_token_bounded_to_one_user_links_no_category(harness, monkeypatch) -> None:
    """A category link has no user axis, so a user-pinned token links none (it is refused
    memory.assign_category too); the item is still written."""
    from server.identity.domain.read_scope import ReadFilters

    ch = _StoreCH(registry={"pets": "cat-1"})
    harness(ch)
    pinned = ReadFilters(scope="own_user", user_id="u", session_id=None)
    monkeypatch.setattr(_import_mod, "request_scope", lambda: pinned)
    out = await handle_memory_import(
        _input({"value": "Dog is Biscuit.", "category": "pets"}, {"value": "Cat is Miso."})
    )
    assert [(r.outcome, r.note) for r in out.results] == [
        ("written", "category_refused"),
        ("written", ""),
    ]
    assert ch.rows_for("loom.memory_categories") == []
    assert len(ch.rows_for("loom.memory")) == 2


# ---- bounds and declaration ------------------------------------------------


def test_201_entries_are_refused_and_200_accepted() -> None:
    entries = [{"value": f"fact {i}"} for i in range(MAX_IMPORT_ENTRIES + 1)]
    with pytest.raises(pydantic.ValidationError) as caught:
        MemoryImportInput(**_IDENT, entries=entries)
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [(("entries",), "too_long")]
    assert len(MemoryImportInput(**_IDENT, entries=entries[:-1]).entries) == 200
    with pytest.raises(pydantic.ValidationError):
        MemoryImportInput(**_IDENT, entries=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk_cap", [0, 50, 200])
async def test_every_entry_is_checked_whatever_the_bulk_cap_says(
    harness, monkeypatch, bulk_cap: int
) -> None:
    """Whatever the bulk write's cap (0, 50 or the default 200), a full import checks
    every entry: the 151st, too close to a pinned memory, is refused, not written unchecked."""
    from server.settings import settings

    monkeypatch.setattr(settings, "bulk_conflict_check_max_entries", bulk_cap)
    rec = harness(_StoreCH())
    entries = [{"value": f"Fact number {i}."} for i in range(MAX_IMPORT_ENTRIES)]
    rec.verdicts["Fact number 150."] = ConflictResult(
        verdict="rejected", peers=[], pinned_blocker_id="m-pin"
    )
    checked: list[str] = []

    async def _check(**kw: Any) -> ConflictResult:
        value = json.loads(kw["new_value"])
        checked.append(value)
        return rec.verdicts.get(value, ConflictResult(verdict="clear", peers=[]))

    monkeypatch.setattr("server.memory.app.conflicts.check_on_commit", _check)
    out = await handle_memory_import(_input(*entries))
    assert sorted(checked) == sorted(e["value"] for e in entries)
    assert (out.results[150].outcome, out.results[150].reason) == ("refused", "pinned_peer")
    assert out.totals.model_dump() == {
        "written": MAX_IMPORT_ENTRIES - 1,
        "held": 0,
        "skipped": 0,
        "refused": 1,
    }


@pytest.mark.asyncio
async def test_a_bulk_write_checks_up_to_its_setting_unless_its_caller_names_a_cap(
    harness, monkeypatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "bulk_conflict_check_max_entries", 1)
    harness(_StoreCH())
    bulk = MemoryBulkSetInput(
        **_IDENT,
        entries=[_BulkSetEntry(memory_key=f"k{i}", value=f"Fact {i}.") for i in range(3)],
    )
    by_setting = await _bulk_set_mod.handle_memory_bulk_set(bulk, dry_run=True)
    named = await _bulk_set_mod.handle_memory_bulk_set(bulk, dry_run=True, conflict_check_cap=3)
    assert (by_setting.unchecked, named.unchecked) == (2, 0)


@pytest.mark.parametrize(
    ("entry", "loc"),
    [
        ({"value": ""}, ("entries", 0, "value")),
        ({"value": " \n\t"}, ("entries", 0, "value")),
        ({"value": "x" * 30_001}, ("entries", 0, "value")),
        ({"value": "v", "tags": ["t"] * 65}, ("entries", 0, "tags")),
        ({"value": "v", "tags": ["t" * 257]}, ("entries", 0, "tags", 0)),
        ({"value": "v", "source_trace": "s" * 513}, ("entries", 0, "source_trace")),
        ({"value": "v", "source_trace": "memory.delete_version:x"}, ("entries", 0, "source_trace")),
        ({"value": "v", "category": "c" * 257}, ("entries", 0, "category")),
        ({"value": "v", "sensitivity": "secret"}, ("entries", 0, "sensitivity")),
    ],
)
def test_an_entry_out_of_bounds_is_refused_by_its_own_field(
    entry: dict[str, Any], loc: tuple[Any, ...]
) -> None:
    """Every bound is a field constraint: the refusal names the field, never the model."""
    with pytest.raises(pydantic.ValidationError) as caught:
        MemoryImportInput(**_IDENT, entries=[entry])
    assert [e["loc"] for e in caught.value.errors()] == [loc]


def test_the_op_is_an_experimental_writer_op_called_by_name_over_mcp() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import _MCP_HIDDEN_OPS, op_toolsets

    op = OPERATIONS_BY_NAME["memory.import"]
    assert (op.required_role, op.idempotent, op.audit, op.maturity) == (
        Role.WRITER,
        True,
        False,
        "experimental",
    )
    assert op.mcp_tool_name == "loom_memory_import" and not op.read_only
    assert op.name not in _MCP_HIDDEN_OPS and op_toolsets(op.name) == []


def test_the_op_draws_from_its_own_rate_class() -> None:
    from server.core.ratelimit import op_class_for

    assert op_class_for("memory.import") == "memory.import"


def test_repeats_with_one_time_in_two_zone_spellings_are_the_same_entry() -> None:
    """A naive time is UTC: `...Z` and the same time without a zone are one entry."""
    entries = [
        {"value": "Dog is Biscuit.", "valid_at": "2024-01-01T00:00:00Z"},
        {"value": "Dog is Biscuit.", "valid_at": "2024-01-01T00:00:00"},
    ]
    parsed = MemoryImportInput(**_IDENT, entries=entries)
    assert parsed.entries[0].valid_at == parsed.entries[1].valid_at == datetime(2024, 1, 1)


def test_an_entry_the_blacklist_could_not_check_names_that_reason() -> None:
    """Told apart from a match: sent in a smaller import it is checked."""
    from server.memory.api.models_bulk import UNCHECKED

    assert _import_mod._CHECK_OF[UNCHECKED] == "blacklist_unchecked"
