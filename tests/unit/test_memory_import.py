"""memory.import: items another memory system distilled, written through the bulk
write path under keys their text derives.

The key is a digest of the text, so a re-run writes nothing new; every check the
bulk write makes answers per entry; a dry run makes the checks and writes nothing; a
registered category is linked through the statements memory.assign_category uses, and
an unregistered one is noted, not refused.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pydantic
import pytest

from server.cli.import_recall import _recall_memory_key
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.api.models_import import MAX_IMPORT_ENTRIES, MemoryImportInput
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app.memory_import import handle_memory_import
from server.memory.domain.conflicts import ConflictResult
from server.memory.domain.memory_import import import_key
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


class _StoreCH(StubCH):
    """A recording client that answers the bulk write's two reads like the store would:
    the live rows under the identity keys it asks for, and the rows of the identity
    (any session, any key) holding the hashes it asks for, from the stubbed rows plus
    every loom.memory row it was sent, so a second import meets the first one's rows.
    The category read answers the namespace's `registry` (name → id)."""

    def __init__(
        self, rows: list[dict[str, Any]] | None = None, *, registry: dict[str, str] | None = None
    ) -> None:
        super().__init__()
        self.rows = list(rows or [])
        self.registry = dict(registry or {})

    def _stored(self) -> list[dict[str, Any]]:
        sent = [{**r, "value_text": r["value"]} for r in self.rows_for("loom.memory")]
        return [*self.rows, *sent]

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        if "FROM loom.categories" in sql:
            return [
                {"name": n, "category_id": self.registry[n]}
                for n in params["names"]
                if n in self.registry
            ]
        if "IN {keys:Array(Tuple(" in sql:
            wanted = {tuple(k) for k in params["keys"]}
            return [r for r in self._stored() if _identity(r) in wanted]
        if "content_sha256 IN" in sql:
            ident = (params["org"], params["ns"], params["agent"], params["uid"])
            return [
                r
                for r in self._stored()
                if r["content_sha256"] in params["shas"]
                and (r["org"], r["namespace"], r["agent"], r["user_id"]) == ident
            ]
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> None:
        return None

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """Wire the bulk write to a `_StoreCH` with nothing reaching a server, and record
    what it leaves: embedding rows, usage events and audit rows. `verdicts` scripts the
    conflict check per value (clear otherwise); `blacklist` names a refused substring."""
    from server.settings import settings

    no_delete_in_flight(monkeypatch)
    # One conflict statement per fact: the scripted check stands in for it.
    monkeypatch.setattr(settings, "memory_write_candidate_batch", 0)

    def make(ch: _StoreCH, *, blacklist: str = "") -> SimpleNamespace:
        rec = SimpleNamespace(
            embedder=BatchEmbedder(), embedded=[], events=[], audits=[], verdicts={}
        )
        wire_stubs(monkeypatch, ch, rec.embedder, rec.embedded)
        monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

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

    return make


def _input(*entries: dict[str, Any], dry_run: bool = False) -> MemoryImportInput:
    return MemoryImportInput(**_IDENT, entries=list(entries), dry_run=dry_run)


def _outcomes(out: Any) -> list[tuple[str, str]]:
    return [(r.outcome, r.reason) for r in out.results]


# ---- the key ---------------------------------------------------------------


def test_the_key_is_import_and_24_hex_of_the_sha256_of_the_text() -> None:
    value = "Prefers dark roast coffee."
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert import_key(value) == "import:" + digest[:24]


@pytest.mark.parametrize("value", ["Prefers dark roast.", "  café au lait  ", "one\ntwo"])
def test_the_key_hashes_the_text_as_the_recall_importer_does(value: str) -> None:
    """The text is hashed as given, whitespace and case kept: the recall importer's
    content-hash key carries the same digest."""
    digest = import_key(value).removeprefix("import:")
    assert _recall_memory_key("memories", None, value) == f"recall_memories_{digest}"
    assert import_key(value) != import_key(value + " ")


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
    assert out.totals.model_dump() == {"written": 2, "reinforced": 0, "skipped": 0, "refused": 0}
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
async def test_a_rerun_writes_nothing_new(harness) -> None:
    ch = _StoreCH()
    rec = harness(ch)
    items = ({"value": "Dog is Biscuit."}, {"value": "Deploys happen on Tuesday."})
    first = await handle_memory_import(_input(*items))
    assert first.totals.written == 2
    rows, vectors = len(ch.rows_for("loom.memory")), len(rec.embedded)
    again = await handle_memory_import(_input(*items))
    assert _outcomes(again) == [("skipped", "unchanged"), ("skipped", "unchanged")]
    assert again.totals.model_dump() == {"written": 0, "reinforced": 0, "skipped": 2, "refused": 0}
    assert (len(ch.rows_for("loom.memory")), len(rec.embedded)) == (rows, vectors) == (2, 2)


@pytest.mark.asyncio
async def test_the_item_imported_in_another_session_is_skipped(harness) -> None:
    """Identity dedup spans sessions: the key holding the text in another session of the
    identity is the item already imported."""
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
    harness(ch)
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("skipped", "unchanged")]
    assert ch.rows_for("loom.memory") == []


@pytest.mark.asyncio
async def test_the_same_text_under_another_key_reinforces_that_memory(harness) -> None:
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
    out = await handle_memory_import(_input({"value": value}))
    assert _outcomes(out) == [("reinforced", "identity_duplicate")]
    assert out.totals.reinforced == 1 and ch.rows_for("loom.memory") == []
    assert [(e["memory_id"], e["event_type"]) for e in rec.events] == [("mid-keeper", "evidence")]


@pytest.mark.asyncio
async def test_a_text_repeated_in_one_request_is_written_once(harness) -> None:
    ch = _StoreCH(registry={"pets": "cat-1"})
    harness(ch)
    out = await handle_memory_import(
        _input({"value": "Dog is Biscuit."}, {"value": "Dog is Biscuit.", "category": "pets"})
    )
    assert _outcomes(out) == [("written", ""), ("skipped", "repeated")]
    assert out.results[0].key == out.results[1].key
    assert len(ch.rows_for("loom.memory")) == 1
    # The repeat's key holds the item, so its category is linked.
    links = ch.rows_for("loom.memory_categories")
    assert [(r["memory_key"], r["category_id"]) for r in links] == [(out.results[0].key, "cat-1")]


# ---- the inherited checks, per entry ---------------------------------------


@pytest.mark.asyncio
async def test_each_check_that_refuses_an_entry_is_named_on_it(harness) -> None:
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
        ("refused", "pinned"),
        ("refused", "locked"),
        ("refused", "conflict"),
    ]
    assert out.totals.model_dump() == {"written": 1, "reinforced": 0, "skipped": 0, "refused": 4}
    assert [r["memory_key"] for r in ch.rows_for("loom.memory")] == [import_key(plain)]
    # A refused entry's category is not linked.
    assert ch.rows_for("loom.memory_categories") == []


@pytest.mark.asyncio
async def test_the_conflict_verdicts_that_write_or_reinforce_are_named(harness) -> None:
    same, near = "Prod deploys are Tuesdays.", "Prod deploys happen Tuesdays."
    ch = _StoreCH()
    rec = harness(ch)
    rec.verdicts[same] = ConflictResult(verdict="merge", peers=[], existing_memory_id="m-keep")
    rec.verdicts[near] = ConflictResult(verdict="duplicate", peers=[], duplicate_of_id="m-peer")
    out = await handle_memory_import(_input({"value": same}, {"value": near}))
    assert _outcomes(out) == [("reinforced", "merge"), ("written", "duplicate")]
    (row,) = ch.rows_for("loom.memory")
    assert (row["memory_key"], row["status"], row["duplicate_of"]) == (
        import_key(near),
        "duplicate",
        "m-peer",
    )
    assert [e["memory_id"] for e in rec.events] == ["m-keep"]


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
async def test_a_bulk_dry_run_persists_no_entity_mapping(harness, monkeypatch) -> None:
    """Entity resolution writes the mappings it decides, and no check reads them: the
    dry run leaves it out."""
    resolved: list[Any] = []

    async def _resolve(*_a: Any, **kw: Any) -> dict[str, str]:
        resolved.append(kw)
        return {}

    harness(_StoreCH())
    monkeypatch.setattr("server.memory.app.entity_resolution.resolve_entities_for_write", _resolve)
    bulk = MemoryBulkSetInput(
        **_IDENT, entries=[_BulkSetEntry(memory_key="k", value="Ada met Bob.", entities=["Ada"])]
    )
    dry = await _bulk_set_mod.handle_memory_bulk_set(bulk, dry_run=True)
    assert dry.written[0].version > 0 and resolved == []
    await _bulk_set_mod.handle_memory_bulk_set(bulk)
    assert len(resolved) == 1


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


# ---- bounds and declaration ------------------------------------------------


def test_501_entries_are_refused_and_500_accepted() -> None:
    entries = [{"value": f"fact {i}"} for i in range(MAX_IMPORT_ENTRIES + 1)]
    with pytest.raises(pydantic.ValidationError) as caught:
        MemoryImportInput(**_IDENT, entries=entries)
    assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [(("entries",), "too_long")]
    assert len(MemoryImportInput(**_IDENT, entries=entries[:-1]).entries) == 500
    with pytest.raises(pydantic.ValidationError):
        MemoryImportInput(**_IDENT, entries=[])


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
