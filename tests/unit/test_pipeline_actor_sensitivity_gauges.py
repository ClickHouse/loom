"""Pipeline writes carry a principal, the sensitivity sweep
labels what the write path skipped, and three row-level invariants read as
gauges with a repair for the one that hides rows from search.

Most clients are stubs; the sweep's paging runs its real SQL on clickhouse-local."""

from __future__ import annotations

import dataclasses
import json
import time
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

from server.auth.context import set_current_auth
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import extraction_queue as queue
from server.memory.app import sensitivity_sweep as sweep
from server.memory.app.extraction_queue import ENQUEUED_BY_KEY, enqueued_actor, payload_for
from server.memory.app.provenance import writer_actor
from server.memory.domain import provenance as prov
from server.memory.store import MemoryRow
from server.memory.store.sensitivity_sweep_sql import SORT_KEY
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.clock_fake import ManualClock
from tests.unit.memory_rows import memory_head

# ------------------------------------------------------------- (a) principal


def test_writer_actor_falls_back_to_the_pipeline_actor_only_without_a_token() -> None:
    set_current_auth(None)
    assert writer_actor() == ""
    with prov.pipeline_actor("process:b5-memo-bench"):
        assert writer_actor() == "process:b5-memo-bench"
        # a live request's own principal wins over the stage's
        set_current_auth(SimpleNamespace(org="o", subject="alice", is_service_account=False))
        assert writer_actor() == "human:alice"
        set_current_auth(None)
        with prov.pipeline_actor(""):  # empty leaves the chain as it was
            assert writer_actor(fallback="process:x") == "process:x"
    assert writer_actor() == ""
    assert prov.cli_actor("import-clickgap") == "process:cli:import-clickgap"
    assert prov.cli_actor("") == "process:cli:unknown"


class _Heads:
    """The heads `apply` reads by key and the versions it writes over them: each candidate's
    key holds its id at `version`, and a write becomes the head, or, with `replaced`, a
    newer write replaces it at once. ClickHouse holds the heads, or with `published`
    False only the ones read first (a publication pending). The facts a relabel owes are
    recorded in `owed` and their resolution in `resolved`."""

    def __init__(
        self,
        monkeypatch: Any,
        cands: list[Any],
        version: int = 1,
        *,
        tags: Any = (),
        replaced: bool = False,
        on_insert: Any = None,
        published: bool = True,
    ) -> None:
        from server.memory import store as memory_repo

        self.rows: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self._replaced, self._on_insert = replaced, on_insert
        self._heads = {
            (c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key): memory_head(
                org=c.org,
                namespace=c.namespace,
                agent=c.agent,
                user_id=c.user_id,
                session_id=c.session_id,
                memory_key=c.memory_key,
                memory_id=c.memory_id,
                version=version,
                tags=list(tags),
            )
            for c in cands
        }
        monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", self._read)
        monkeypatch.setattr(memory_repo, "insert_versions_over_heads", self._insert)
        self._first = {k: h.version for k, h in self._heads.items()}
        self._published = published
        monkeypatch.setattr(memory_repo, "clickhouse_head_versions", self._clickhouse)
        from server.memory.store import pg_memory

        monkeypatch.setattr(pg_memory, "settled_head_versions", self._authority)
        from server.memory.app import intent_marks

        self.owed: list[dict[str, Any]] = []
        self.resolved: list[str] = []
        # The owed record (`_owe_facts`).
        monkeypatch.setattr(sweep, "record_projection_failure", self._owe)
        monkeypatch.setattr(intent_marks, "resolve_soon", self._resolve)

    async def _read(self, keys: Any, **kw: Any) -> dict[Any, MemoryRow]:
        self.reads.append(kw)
        return {k: self._heads[k] for k in keys if k in self._heads}

    async def _owe(self, **kw: Any) -> str:
        self.owed.append(kw["payload"])
        return f"owed-{len(self.owed)}"

    def _resolve(self, org: str, intents: list[str]) -> None:
        self.resolved.extend(intents)

    async def _authority(self, keys: Any) -> dict[Any, int]:
        return {k: self._heads[k].version for k in keys if k in self._heads}

    async def _clickhouse(self, keys: Any, **_k: Any) -> dict[Any, int]:
        held = {k: h.version for k, h in self._heads.items()} if self._published else self._first
        return {k: held[k] for k in keys if k in held}

    async def _insert(
        self, rows: list[dict[str, Any]], read: Any, *, on_commit: Any = None, **_k: Any
    ) -> int:
        if self._on_insert is not None:
            self._on_insert(rows)
        self.rows.extend(rows)
        if on_commit is not None:
            on_commit()
        for row in rows:
            key = tuple(row[c] for c in SORT_KEY)
            if self._replaced:
                self._heads.pop(key, None)
            else:
                self._heads[key] = self._heads[key].model_copy(update={"version": row["version"]})
        return len(rows)


def _candidate(memory_id: str, org: str, key: str, level: str, shape: str) -> sweep.SweepCandidate:
    """A sweep candidate in namespace `n`, agent `a`, with its sort key as `scan` reads it."""
    ident = (org, "n", "a", "-", "-", key)
    return sweep.SweepCandidate(
        memory_id, *ident, level, (shape,), dict(zip(SORT_KEY, ident, strict=True)), 1
    )


def _input(**kw: Any):
    from server.memory.api.models_bulk import MemorySetFromMessagesInput, _Message

    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="a",
        user_id="u1",
        messages=[_Message(role="user", content="I moved to Lisbon last week")],
        defer=True,
    )
    base.update(kw)
    return MemorySetFromMessagesInput(**base)


def test_the_queue_payload_carries_the_enqueuer_and_the_rebuild_strips_it() -> None:
    set_current_auth(SimpleNamespace(org="o", subject="b5-memo-bench", is_service_account=True))
    try:
        payload, n_msgs, _chars = payload_for(_input(), scope=WHOLE_NAMESPACE, rule_agent="a")
    finally:
        set_current_auth(None)
    data = json.loads(payload)
    assert n_msgs == 1 and data[ENQUEUED_BY_KEY] == "process:b5-memo-bench"
    assert "defer" not in data
    row = {"payload": payload}
    assert enqueued_actor(row) == "process:b5-memo-bench"
    rebuilt = queue.rebuild_input(row)
    assert rebuilt.defer is False and not hasattr(rebuilt, ENQUEUED_BY_KEY)
    # The rule agent the request resolved rides beside it and is stripped the same way.
    assert data[queue.RULE_AGENT_KEY] == "a"
    assert queue.queued_rule_agent(row) == "a"
    assert not hasattr(rebuilt, queue.RULE_AGENT_KEY)
    assert queue.replay_actor(row) == "process:b5-memo-bench"
    # no token at enqueue time → no key → the worker's own principal
    set_current_auth(None)
    payload2, _, _ = payload_for(_input(), scope=WHOLE_NAMESPACE)
    assert ENQUEUED_BY_KEY not in json.loads(payload2)
    assert queue.replay_actor({"payload": payload2}) == "process:extraction-worker"
    assert queue.replay_actor({"payload": "not json"}) == "process:extraction-worker"
    assert (
        payload_for(_input(), scope=WHOLE_NAMESPACE, enqueued_by="human:op")[0].count(
            '"enqueued_by":"human:op"'
        )
        == 1
    )


@pytest.mark.asyncio
async def test_replay_runs_the_handler_under_the_enqueuers_principal(monkeypatch) -> None:
    from server.memory.app.extract import handler as extract_handler_mod

    seen: list[str] = []

    async def _fake_handler(inp, **_kw):
        seen.append(writer_actor())
        return SimpleNamespace(written=[1, 2])

    monkeypatch.setattr(extract_handler_mod, "handle_memory_set_from_messages", _fake_handler)
    set_current_auth(None)
    payload, _, _ = payload_for(
        _input(), scope=WHOLE_NAMESPACE, enqueued_by="process:b5-memo-bench"
    )
    assert await queue.replay_row({"payload": payload}) == 2
    payload_old, _, _ = payload_for(_input(), scope=WHOLE_NAMESPACE, enqueued_by="")
    assert await queue.replay_row({"payload": payload_old}) == 2
    assert seen == ["process:b5-memo-bench", "process:extraction-worker"]
    assert writer_actor() == ""  # the stage's principal does not leak out


def test_the_cli_runs_every_command_as_its_own_principal(monkeypatch) -> None:
    from server import cli
    from server.cli import app, ops

    seen: list[str] = []

    def fake_cmd(ns):
        seen.append(writer_actor())
        return 0

    monkeypatch.setattr(app, "_cmd_integrity_repair", fake_cmd)
    monkeypatch.setattr(ops, "_cmd_sensitivity_sweep", fake_cmd)
    set_current_auth(None)
    assert cli.main(["integrity-repair", "--org", "o"]) == 0
    assert cli.main(["sensitivity-sweep", "--namespace", "n", "--apply"]) == 0
    assert seen == ["process:cli:integrity-repair", "process:cli:sensitivity-sweep"]


# ------------------------------------------------------------- (b) sweep


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.one: dict[str, Any] = {"n": 0}

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return list(self.rows)

    async def query_one(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return dict(self.one)

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))


def _mem(memory_id: str, value: str, ns: str = "demo-full") -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "org": "clickhouse",
        "namespace": ns,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": f"k{memory_id}",
        "version": 1,
        "value": value,
    }


@pytest.mark.asyncio
async def test_scan_labels_exactly_as_the_write_path_would() -> None:
    ch = _CH(
        [
            _mem("1", "OPENAI_API_KEY=" + "sk-" + "proj-" + "Ab3" * 12),
            _mem("2", "reach Bram at bram@example.com"),
            _mem("3", "call me on +1 415 555 0123"),
            _mem("4", "the secret of a good stew is time"),  # coarse hit, detector says ''
        ]
    )
    report = await sweep.scan(
        org="clickhouse",
        namespace="demo-full",
        since_days=2,
        limit=100,
        page_size=100,
        pages=1,
        ch=ch,
    )
    assert report.scanned == 4 and len(report.candidates) == 3 and not report.truncated
    assert report.by_level == {"high": 1, "medium": 2}
    assert report.by_shape == {"credential": 1, "email": 1, "phone": 1}
    assert report.by_namespace()[("clickhouse", "demo-full")] == {"high": 1, "medium": 2}
    narrowed = await sweep.scan(
        only_shapes=["credential", "email"], limit=100, page_size=100, pages=1, ch=ch
    )
    assert narrowed.by_shape == {"credential": 1, "email": 1}  # the phone-only row is skipped
    for sql, params in ch.calls[:2]:  # a page's keys, then their current versions
        assert "sensitivity = ''" in sql and "{shape:String}" in sql
        assert params["shape"] == sweep.coarse_shape_re(set()) and params["days"] == 2
        assert "PREWHERE" not in sql
    assert "FINAL" not in ch.calls[0][0] and "FINAL" in ch.calls[1][0]


@pytest.mark.asyncio
async def test_a_pages_current_versions_are_read_by_key_subquery_not_by_key_list() -> None:
    # A page of 20,000 keys bound as one array parameter outgrows the request URL; the
    # FINAL read takes the page's key range as a subquery, the same parameters for any page
    # size.
    ch = _CH([_mem(str(i), "secret") for i in range(2000)])
    await sweep.scan(limit=2000, page_size=2000, pages=2, ch=ch)
    finals = [(sql, params) for sql, params in ch.calls if "FINAL" in sql]
    assert len(finals) == 2
    for sql, params in finals:
        assert "IN (" in sql and "LIMIT" not in sql
        assert not any(isinstance(v, list | tuple) for v in params.values())
        assert params["upto_memory_key"] == "k1999"
    assert "after_memory_key" not in finals[0][1]
    assert finals[1][1]["after_memory_key"] == "k1999"


@pytest.mark.asyncio
async def test_apply_relabels_tombstones_high_and_tags_medium(monkeypatch) -> None:
    from server.memory import store as memory_repo

    ch = _CH()
    tombstoned: list[str] = []

    async def _retire(items, *, ch=None, bare=False):
        tombstoned.extend((key[5], version) for key, version in items)
        return 2

    async def _live(ch, **ident):
        return [
            {
                "memory_key": ident["memory_key"],
                "tags": ["t"],
                "embedding": [0.1],
                "embedding_dim": 1,
            }
        ]

    inserted: list[list[dict[str, Any]]] = []

    async def _ins(rows, *, ch=None):
        inserted.append(rows)

    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(memory_repo, "live_embedding_rows_for_key", _live)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _ins)
    fenced: list[tuple[str, int]] = []

    async def _fence(items, *, model, ch=None):
        fenced.extend((key[5], version) for key, version in items)
        return len(items)

    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)

    ch.rows = [
        {
            "org": "clickhouse",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "memory_key": "khigh",
            "tombstoned_at": None,
        }
    ]
    cands = [
        _candidate("1", "clickhouse", "khigh", "high", "credential"),
        _candidate("2", "clickhouse", "kmed", "medium", "email"),
    ]
    # A newer write replaces the relabel at once: no post-relabel retire.
    heads = _Heads(monkeypatch, cands, tags=["t"], replaced=True)
    res = await sweep.apply(cands, ch=ch)
    written = heads.rows
    assert heads.reads and all(r.get("sequential") for r in heads.reads)
    assert res.as_dict() == {
        "relabelled": 2,
        "high": 1,
        "medium": 1,
        "embeddings_tombstoned": 2,
        "embeddings_tagged": 1,
        "left": 0,
        "errors": 0,
    }
    assert written[0]["sensitivity"] == "high" and written[0]["tags"] == [
        "t",
        "sensitivity:high",
    ]
    assert written[1]["tags"] == ["t", "sensitivity:medium"]
    # One retire of both vector tables at the relabelled memory row's version, so a
    # late metadata copy loses and a newer write's entity vectors stay.
    assert tombstoned == [("khigh", written[0]["version"])]
    # and fences the key there: a plain write still embedding lands below it
    assert fenced == [("khigh", written[0]["version"])]
    assert inserted[0][0]["tags"] == ["t", "sensitivity:medium"] and inserted[0][0]["version"]


class _LocalCH:
    """Runs each query on clickhouse-local after the setup statements, with their params."""

    def __init__(self, setup: list[str], params: dict[str, Any]) -> None:
        self.setup = setup
        self.params = params
        self.returned: list[dict[str, Any]] = []  # every row a query answered

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        rows = run(self.setup, sql, self.params | (params or {}))
        self.returned.extend(rows)
        return rows

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


class _Row(NamedTuple):
    """One loom.memory row version, under session `-`."""

    org: str
    ns: str
    user: str
    memory_id: str
    key: str
    value: Any
    age: int  # seconds since created_at
    sensitivity: str = ""
    version: int = 0
    tombstoned: bool = False
    agent: str = "a"


def _local_memory(rows: list[_Row], *later: list[_Row]) -> _LocalCH:
    """loom.memory on clickhouse-local holding `rows`, then each list in `later` inserted
    as its own part, as a later write would be (no merge runs between them)."""
    # the CLI's closing invariant reads memory_embeddings, whose vector column is quantized
    setup = [
        "SET enable_quantized_codec = 1",
        "SET optimize_on_insert = 0",
        ddl("memory"),
        ddl("memory_embeddings"),
    ]
    params: dict[str, Any] = {}
    for part, part_rows in enumerate([rows, *later]):
        if part_rows:
            insert, part_params = _insert(part_rows, prefix=f"p{part}_")
            setup.append(insert)
            params |= part_params
    return _LocalCH(setup, params)


def _insert(rows: list[_Row], *, prefix: str) -> tuple[str, dict[str, Any]]:
    """One INSERT of `rows` into loom.memory, its parameters named after `prefix`."""
    # Each query reruns the insert, so the timestamps are fixed here rather than `now()`.
    now = int(time.time())
    values, params = [], {}
    for n, row in enumerate(rows):
        i = f"{prefix}{n}"
        values.append(
            f"({{org{i}:String}}, {{ns{i}:String}}, {{agent{i}:String}}, {{user{i}:String}}, '-', "
            f"{{id{i}:UUID}}, {{key{i}:String}}, {{value{i}:String}}, toDateTime({{at{i}:UInt32}}), "
            f"{{sens{i}:String}}, {{ver{i}:UInt64}}, "
            f"if({{tomb{i}:Bool}}, toDateTime({{at{i}:UInt32}}), NULL))"
        )
        params |= {
            f"org{i}": row.org,
            f"ns{i}": row.ns,
            f"agent{i}": row.agent,
            f"user{i}": row.user,
            f"id{i}": row.memory_id,
            f"key{i}": row.key,
            f"value{i}": json.dumps(row.value),
            f"at{i}": now - row.age,
            f"sens{i}": row.sensitivity,
            f"ver{i}": row.version,
            f"tomb{i}": row.tombstoned,
        }
    insert = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, value, created_at, sensitivity, version, tombstoned_at) "
        f"VALUES {', '.join(values)}"
    )
    return insert, params


async def _run_pass_recording(
    monkeypatch, ch: _LocalCH, *, rows_per_pass: int, pages: int, failing: bool = False
) -> tuple[list[Any], dict[str, Any]]:
    """The scheduled pass over `ch`'s rows, with `apply` recording its candidates: the
    candidates it would label, and the pass's outcome. A `failing` apply labels none and
    reports an error for each."""
    from server.memory.workers import sensitivity_sweep as w
    from server.settings import settings

    applied: list[Any] = []

    async def _apply(cands, *, ch=None):
        if failing:
            return sweep.ApplyResult(failed=[(c, f"{c.memory_key}: write failed") for c in cands])
        applied.extend(cands)
        return sweep.ApplyResult(relabelled=len(cands), high=len(cands))

    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(settings, "sensitivity_sweep_window_days", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_max_rows_per_pass", rows_per_pass)
    monkeypatch.setattr(settings, "sensitivity_sweep_pages_per_pass", pages)
    monkeypatch.setattr(settings, "sensitivity_sweep_shapes", "credential,email")
    out = await w.run_pass(ch=ch)  # type: ignore[arg-type]
    return applied, out


def _token_row_in_another_org() -> _Row:
    token = "gh" + "p_" + "Ab3" * 12
    return _Row("zeta", "n", "-", _uuid(99), "deploy_token", f"push with {token}", 60)


def _spin_rows_then_a_pin(spins: int) -> list[_Row]:
    """`spins` selected rows the detector leaves unlabelled, then a row holding a PIN."""
    rows = [
        _Row("o", "n", "-", _uuid(i), f"spin_{i}", {"spin": "4471"}, 600 - i) for i in range(spins)
    ]
    rows.append(_Row("o", "n", "-", _uuid(99), "vault_door", "badge PIN is 4471", 60))
    return rows


def _uuid(i: int) -> str:
    return f"00000000-0000-0000-0000-{i:012d}"


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_pages_past_selected_rows_it_leaves_unlabelled(monkeypatch) -> None:
    # More selected-but-unlabelled rows than one page sit ahead of a PIN row in the same
    # agent. A scan pages on past them and labels the PIN row. The scheduled pass reads
    # one page of that agent, finds nothing and moves past it; with pages left after the
    # last key it comes back to the rest of the agent and labels the PIN row. With one
    # page, the cursor is past the agent.
    from server.workers import pass_context

    ch = _local_memory(_spin_rows_then_a_pin(5))
    one_page = await sweep.scan(since_days=2, limit=2, page_size=2, pages=1, ch=ch)  # type: ignore[arg-type]
    assert one_page.truncated and one_page.candidates == []
    every_page = await sweep.scan(since_days=2, limit=2, page_size=2, pages=10, ch=ch)  # type: ignore[arg-type]
    assert [c.memory_key for c in every_page.candidates] == ["vault_door"]
    assert every_page.candidates[0].level == "high" and every_page.scanned == 6
    for pages, expected, cursor in ((10, ["vault_door"], []), (1, [], {"past": ["o", "n", "a"]})):
        ctx = pass_context.begin("sensitivity_sweep", resume_cursor="")
        try:
            applied, _out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=2, pages=pages)
        finally:
            pass_context.clear()
        assert [c.memory_key for c in applied] == expected
        assert json.loads(ctx.cursor) == cursor


class _ClockedCH(_LocalCH):
    """A `_LocalCH` that moves `clock` forward by `seconds` after its first FINAL read,
    so the time runs out after the first page however often the scan reads the clock."""

    def __init__(self, ch: _LocalCH, clock: ManualClock, seconds: float) -> None:
        super().__init__(ch.setup, ch.params)
        self.clock = clock
        self.seconds = seconds
        self.moved = False

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        rows = await super().query_all(sql, params)
        if "FINAL" in sql and not self.moved:
            self.moved = True
            self.clock.advance(self.seconds)
        return rows


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_whose_time_runs_out_applies_what_it_found_and_stores_its_cursor(
    monkeypatch,
) -> None:
    # Two PIN rows in agent `a` with selected-but-unlabelled rows between them, then a PIN
    # row in agent `b`. The pass's time runs out after its first page: it stops paging,
    # applies the PIN row it found, says it was truncated and stores its cursor. Given the
    # time, the same pass labels all three.
    from server.workers import pass_context
    from server.workers.registry import pass_deadline_seconds

    rows = [
        _Row("o", "n", "-", _uuid(1), "a_pin", "badge PIN is 4471", 60),
        _Row("o", "n", "-", _uuid(2), "b_spin", {"spin": "4471"}, 60),
        _Row("o", "n", "-", _uuid(3), "c_spin", {"spin": "4471"}, 60),
        _Row("o", "n", "-", _uuid(4), "d_pin", "door PIN is 5582", 60),
        _Row("o", "n", "-", _uuid(5), "e_pin", "gate PIN is 6693", 60, agent="b"),
    ]
    clock = ManualClock()
    monkeypatch.setattr(sweep, "monotonic", clock.monotonic)
    ch = _ClockedCH(_local_memory(rows), clock, pass_deadline_seconds("sensitivity_sweep"))
    ctx = pass_context.begin("sensitivity_sweep", resume_cursor="")
    try:
        applied, out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=3, pages=10)
    finally:
        pass_context.clear()
    assert [c.memory_key for c in applied] == ["a_pin"]
    assert out["truncated"] and out["relabelled"] == 1
    assert json.loads(ctx.cursor) == ["o", "n", "a"]

    # The clock moved only once, so the second pass has its whole time.
    applied, out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=3, pages=10)
    assert [c.memory_key for c in applied] == ["a_pin", "d_pin", "e_pin"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(("pages", "scanned", "truncated"), [(3, 5, True), (5, 7, False)])
async def test_one_pass_crosses_several_agents_it_finds_nothing_to_label_in(
    monkeypatch, pages: int, scanned: int, truncated: bool
) -> None:
    # Agents `a` and `b` each hold more than a page of selected-but-unlabelled rows, ahead
    # of a PIN row in agent `c`. One pass reads a page of each, moves past both and labels
    # the PIN row.
    spins = _spin_rows_then_a_pin(3)[:-1]
    in_b = [r._replace(agent="b", memory_id=_uuid(10 + i)) for i, r in enumerate(spins)]
    pin_row = _Row("o", "n", "-", _uuid(99), "vault_door", "badge PIN is 4471", 60, agent="c")
    ch = _local_memory([*spins, *in_b, pin_row])
    applied, out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=2, pages=pages)
    assert [c.memory_key for c in applied] == ["vault_door"]
    assert out["scanned"] == scanned and out["truncated"] is truncated


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_that_cannot_reread_every_agent_it_moved_past_says_it_was_truncated(
    monkeypatch,
) -> None:
    # Agents `a` and `b` each hold three selected-but-unlabelled rows, and a pass reads four
    # pages of two. It moves past both, reaches the last key and has one page left: it reads
    # the rest of `a`, a short page, and none of `b`. The outcome says it was truncated.
    spins = _spin_rows_then_a_pin(3)[:-1]
    in_b = [r._replace(agent="b", memory_id=_uuid(10 + i)) for i, r in enumerate(spins)]
    applied, out = await _run_pass_recording(
        monkeypatch, _local_memory([*spins, *in_b]), rows_per_pass=2, pages=4
    )
    assert applied == []
    assert out["scanned"] == 5 and out["truncated"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_reads_at_most_its_page_cap() -> None:
    # A row behind more than the cap's worth of selected rows waits for the next pass's
    # window or the operator's run: the scan stops at the cap and says it was truncated.
    ch = _local_memory(_spin_rows_then_a_pin(5))
    capped = await sweep.scan(since_days=2, limit=2, page_size=2, pages=2, ch=ch)  # type: ignore[arg-type]
    assert capped.scanned == 4 and capped.truncated and capped.candidates == []


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_row_behind_more_than_a_pass_of_keys_in_its_agent_waits_for_the_operator(
    monkeypatch,
) -> None:
    # Seven selected-but-unlabelled rows sit ahead of a PIN row in one agent, more than a
    # pass's three pages of two. Each pass moves past the agent on its first page, finds
    # no key after it, and spends its last page on the agent's third and fourth keys. Its
    # cursor is the first key again, so the next pass reads the same pages and no pass
    # reaches the PIN row. The operator's run does.
    rows = _spin_rows_then_a_pin(7)
    labelled, cursors = await _passes(monkeypatch, rows, count=3, pages=3)
    assert labelled == [[], [], []]
    assert cursors == [[], [], []]
    ch = _local_memory(rows)
    operator = await sweep.scan(since_days=2, limit=2, page_size=2, pages=None, ch=ch)  # type: ignore[arg-type]
    assert [c.memory_key for c in operator.candidates] == ["vault_door"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_each_pass_starts_where_the_last_one_stopped(monkeypatch) -> None:
    # Agent `a` holds two pages of selected-but-unlabelled rows, agent `b` a PIN row, and a
    # pass reads one page. The first pass finds nothing in its page of `a`, so its cursor is
    # past `a`, and the second reaches the PIN row; it reads the last row, and the pass
    # after it starts again at the first. The cursor names the agent only, never a user or
    # a memory key.
    from server.workers import pass_context

    spins = _spin_rows_then_a_pin(4)[:-1]
    pin_row = _Row("o", "n", "-", _uuid(99), "vault_door", "badge PIN is 4471", 60, agent="b")
    ch = _local_memory([*spins, pin_row])
    cursor = ""
    labelled, cursors = [], []
    for _ in range(3):
        ctx = pass_context.begin("sensitivity_sweep", resume_cursor=cursor)
        try:
            applied, _out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=2, pages=1)
        finally:
            pass_context.clear()
        labelled.append([c.memory_key for c in applied])
        cursor = ctx.cursor
        cursors.append(json.loads(cursor))
    assert labelled == [[], ["vault_door"], []]
    assert cursors == [{"past": ["o", "n", "a"]}, [], {"past": ["o", "n", "a"]}]


async def _passes(
    monkeypatch,
    rows: list[_Row],
    *,
    count: int,
    pages: int = 1,
    failing: tuple[int, ...] = (),
) -> tuple[list[list[str]], list[Any]]:
    """`count` scheduled passes of `pages` pages of two over `rows`, each starting at the
    cursor the one before left. A row a pass labels gets a new `high` version in a part of
    its own before the next pass, as `apply` writes it, and its old unlabelled version
    stays: no merge runs. The passes numbered in `failing` fail in `apply`. Returns each
    pass's labelled keys and its cursor."""
    from server.workers import pass_context

    cursor = ""
    labelled, cursors = [], []
    written: list[list[_Row]] = []
    for i in range(count):
        ctx = pass_context.begin("sensitivity_sweep", resume_cursor=cursor)
        try:
            applied, _out = await _run_pass_recording(
                monkeypatch,
                _local_memory(rows, *written),
                rows_per_pass=2,
                pages=pages,
                failing=i in failing,
            )
        finally:
            pass_context.clear()
        keys = [c.memory_key for c in applied]
        written.append(
            [r._replace(sensitivity="high", version=r.version + 1) for r in rows if r.key in keys]
        )
        labelled.append(keys)
        cursor = ctx.cursor
        cursors.append(json.loads(cursor))
    return labelled, cursors


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_that_labels_rows_reads_its_agent_again_and_one_that_fails_moves_on(
    monkeypatch,
) -> None:
    # Six PIN rows in agent `a`, three pages' worth, and the second pass fails in `apply`.
    # A pass that labelled rows leaves the cursor at `a`, so the next reads `a` again. The
    # failed pass leaves it past `a`; the next reaches the last row with pages left, goes
    # back to the first key and labels the rows that failed.
    pin = "badge PIN is 4471"
    rows = [_Row("o", "n", "-", _uuid(i), f"pin_{i}", pin, 600 - i) for i in range(6)]
    labelled, cursors = await _passes(monkeypatch, rows, count=4, pages=3, failing=(1,))
    assert labelled == [["pin_0", "pin_1"], [], ["pin_2", "pin_3"], ["pin_4", "pin_5"]]
    a, past_a = ["o", "n", "a"], {"past": ["o", "n", "a"]}
    assert cursors == [a, past_a, a, a]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_failures_in_an_earlier_agent_leave_the_cursor_where_the_pass_stopped(
    monkeypatch,
) -> None:
    # The first pass finds a PIN row in agent `a`, reads on to agent `d` and fails to apply
    # it. The rest of `d` had no part in the failure, so the cursor stays at `d` and the
    # next pass labels the PIN row there.
    pin, spin = "badge PIN is 4471", {"spin": "4471"}
    rows = [
        _Row("o", "n", "-", _uuid(1), "pin_a", pin, 60),
        _Row("o", "n", "-", _uuid(2), "c0_spin", spin, 60, agent="c"),
        _Row("o", "n", "-", _uuid(3), "c1_spin", spin, 60, agent="c"),
        _Row("o", "n", "-", _uuid(4), "d0_spin", spin, 60, agent="d"),
        _Row("o", "n", "-", _uuid(5), "d1_pin", pin, 60, agent="d"),
    ]
    labelled, cursors = await _passes(monkeypatch, rows, count=2, pages=2, failing=(0,))
    assert labelled == [[], ["d1_pin"]]
    assert cursors == [["o", "n", "d"], []]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_that_reaches_the_last_row_reads_the_rows_before_its_cursor(
    monkeypatch,
) -> None:
    # The cursor is at agent `b` and a PIN row sits in agent `a`. With a page left after
    # the last row, the pass goes back to the first key, labels the row and stops where it
    # started; with no page left, the row waits for the next pass.
    from server.workers import pass_context

    pin_row = _Row("o", "n", "-", _uuid(1), "vault_door", "badge PIN is 4471", 60)
    ch = _local_memory([pin_row])
    for pages, expected in ((2, ["vault_door"]), (1, [])):
        ctx = pass_context.begin("sensitivity_sweep", resume_cursor='["o", "n", "b"]')
        try:
            applied, _out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=2, pages=pages)
        finally:
            pass_context.clear()
        assert [c.memory_key for c in applied] == expected
        assert json.loads(ctx.cursor) == (["o", "n", "b"] if expected else [])


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_page_of_rows_labelled_before_the_parts_merge_never_skips_their_agent(
    monkeypatch,
) -> None:
    # Five PIN rows in agent `a`, more than a page of two. The first pass labels the first
    # page, and their old unlabelled versions stay until a merge, so the next pass's first
    # page holds their keys and its FINAL read gives back no row. That page does not move
    # the scan past `a`: the pass reads on and labels the rows after it.
    pin = "badge PIN is 4471"
    rows = [_Row("o", "n", "-", _uuid(i), f"pin_{i}", pin, 600 - i) for i in range(5)]
    labelled, cursors = await _passes(monkeypatch, rows, count=3, pages=3)
    assert labelled == [["pin_0", "pin_1"], ["pin_2", "pin_3"], ["pin_4"]]
    assert cursors == [["o", "n", "a"], ["o", "n", "a"], []]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_spent_on_versions_waiting_for_a_merge_moves_past_their_agent(
    monkeypatch,
) -> None:
    # Agent `a` holds five PIN rows already labelled `high` whose old unlabelled versions
    # stay until a merge, more keys than two pages of two, and agent `b` a PIN row. The
    # first pass reads both pages inside `a`, finds nothing and leaves the cursor past `a`,
    # so the second labels the PIN row in `b`.
    pin = "badge PIN is 4471"
    unlabelled = [_Row("o", "n", "-", _uuid(i), f"pin_{i}", pin, 600 - i) for i in range(5)]
    labelled_since = [r._replace(sensitivity="high", version=1) for r in unlabelled]
    pin_row = _Row("o", "n", "-", _uuid(99), "vault_door", pin, 60, agent="b")
    rows = [*unlabelled, *labelled_since, pin_row]
    labelled, cursors = await _passes(monkeypatch, rows, count=2, pages=2)
    assert labelled == [[], ["vault_door"]]
    assert cursors[0] == {"past": ["o", "n", "a"]}


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_pass_whose_last_page_crosses_an_agent_keeps_its_cursor_at_that_agent(
    monkeypatch,
) -> None:
    # Agent `a` holds one selected row the detector leaves unlabelled, and agent `b` another
    # followed by a PIN row. The first pass's one page of two keys spans `a` and `b`, gives
    # a row for each key and nothing to label, so the cursor stays at `b` and the second
    # pass labels the PIN row.
    spin = {"spin": "4471"}
    rows = [
        _Row("o", "n", "-", _uuid(1), "spin_a", spin, 600),
        _Row("o", "n", "-", _uuid(2), "spin_b", spin, 500, agent="b"),
        _Row("o", "n", "-", _uuid(99), "vault_door", "badge PIN is 4471", 60, agent="b"),
    ]
    labelled, cursors = await _passes(monkeypatch, rows, count=2, pages=1)
    assert labelled == [[], ["vault_door"]]
    assert cursors[0] == ["o", "n", "b"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_stale_key_in_an_earlier_agent_never_moves_the_cursor_past_the_next_one(
    monkeypatch,
) -> None:
    # Agent `a` holds a PIN row already labelled `high` whose old unlabelled version stays
    # until a merge, and agent `b` a selected row the detector leaves unlabelled followed by
    # a PIN row. The first pass's one page spans `a` and `b`; the stale key is in `a`, so
    # the cursor stays at `b` and the second pass labels the PIN row there.
    pin = "badge PIN is 4471"
    unlabelled = _Row("o", "n", "-", _uuid(1), "pin_a", pin, 600)
    rows = [
        unlabelled,
        unlabelled._replace(sensitivity="high", version=1),
        _Row("o", "n", "-", _uuid(2), "spin_b", {"spin": "4471"}, 500, agent="b"),
        _Row("o", "n", "-", _uuid(99), "vault_door", pin, 60, agent="b"),
    ]
    labelled, cursors = await _passes(monkeypatch, rows, count=2, pages=1)
    assert labelled == [[], ["vault_door"]]
    assert cursors[0] == ["o", "n", "b"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_rows_that_fail_in_apply_on_every_pass_never_hold_the_sweep_in_their_agent(
    monkeypatch,
) -> None:
    # Agent `a` holds more than a page of PIN rows and agent `b` one more, and `apply`
    # fails on every pass. The pass that stops in `a` leaves the cursor past it, so the
    # next reads `b` and the last row, and the sweep comes back round to `a`.
    pin = "badge PIN is 4471"
    rows = [_Row("o", "n", "-", _uuid(i), f"pin_{i}", pin, 600 - i) for i in range(3)]
    rows.append(_Row("o", "n", "-", _uuid(99), "vault_door", pin, 60, agent="b"))
    labelled, cursors = await _passes(monkeypatch, rows, count=3, failing=(0, 1, 2))
    assert labelled == [[], [], []]
    assert cursors == [{"past": ["o", "n", "a"]}, [], {"past": ["o", "n", "a"]}]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_an_agent_name_longer_than_the_cursor_holds_never_stalls_the_sweep(
    monkeypatch,
) -> None:
    # An agent whose name does not fit the cursor holds more selected-but-unlabelled rows
    # than a pass reads. The cursor cannot name the place past it, so the pass that finds
    # nothing there leaves the cursor past its namespace, and the next reaches the PIN row
    # behind it.
    from server.workers.pass_context import MAX_CURSOR

    long_agent = "a" * MAX_CURSOR
    spins = [r._replace(agent=long_agent) for r in _spin_rows_then_a_pin(3)[:-1]]
    pin_row = _Row("o", "p", "-", _uuid(99), "vault_door", "badge PIN is 4471", 60, agent="b")
    labelled, cursors = await _passes(monkeypatch, [*spins, pin_row], count=3)
    assert labelled == [[], ["vault_door"], []]
    assert cursors == [{"past": ["o", "n"]}, [], {"past": ["o", "n"]}]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_candidates_cut_to_the_limit_resume_after_the_last_kept_one() -> None:
    # With a limit of two, the first page gives one candidate and the second two, so the
    # third is cut. The scan resumes after the second kept row's stored key, whose empty
    # user id stays '' (a '-' sorts after every key here), and the next scan labels the third.
    pin = "badge PIN is 4471"
    rows = [
        _Row("o", "n", "", _uuid(1), "k0_spin", {"spin": "4471"}, 60),
        _Row("o", "n", "", _uuid(2), "k1_pin", pin, 60),
        _Row("o", "n", "", _uuid(3), "k2_pin", pin, 60),
        _Row("o", "n", "", _uuid(4), "k3_pin", pin, 60),
    ]
    ch = _local_memory(rows)
    first = await sweep.scan(since_days=2, limit=2, page_size=2, pages=3, ch=ch)  # type: ignore[arg-type]
    assert [c.memory_key for c in first.candidates] == ["k1_pin", "k2_pin"] and first.truncated
    assert {(c.user_id, c.session_id) for c in first.candidates} == {("", "-")}
    assert first.resume_after == {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "",
        "session_id": "-",
        "memory_key": "k2_pin",
    }
    rest = await sweep.scan(
        since_days=2,
        limit=2,
        page_size=2,
        pages=1,
        start_after=first.resume_after,
        ch=ch,  # type: ignore[arg-type]
    )
    assert [c.memory_key for c in rest.candidates] == ["k3_pin"]


def test_the_cursor_keeps_the_agent_or_names_the_place_past_it() -> None:
    from server.memory.store.sensitivity_sweep_sql import past_agent
    from server.memory.workers.sensitivity_sweep import cursor_key, cursor_text
    from server.workers.pass_context import MAX_CURSOR

    key = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    start = {"org": "o", "namespace": "n", "agent": "a", "user_id": "", "session_id": ""}
    assert cursor_text(key | {"memory_key": "k"}) == '["o", "n", "a"]'
    assert cursor_key(cursor_text(key | {"memory_key": "k"})) == start | {"memory_key": ""}
    # past the agent, asked for or as the scan's `past_agent` place
    past = {"org": "o", "namespace": "n", "agent": "a"}
    assert cursor_text(key | {"memory_key": "k"}, past=True) == '{"past": ["o", "n", "a"]}'
    assert cursor_text(past_agent(key | {"memory_key": "k"})) == cursor_text(
        key | {"memory_key": "k"}, past=True
    )
    assert cursor_key('{"past": ["o", "n", "a"]}') == past
    # past the namespace when the agent does not fit
    long_agent = key | {"agent": "a" * MAX_CURSOR, "memory_key": "k"}
    assert cursor_text(long_agent) == '{"past": ["o", "n"]}'
    assert cursor_key(cursor_text(long_agent, past=True)) == {"org": "o", "namespace": "n"}
    # a non-ASCII agent name is measured in the characters the store keeps
    cjk_agent = key | {"agent": "代" * 200, "memory_key": "k"}
    assert cursor_key(cursor_text(cjk_agent)) == start | {"agent": "代" * 200, "memory_key": ""}
    assert cursor_text(None) == "[]" and cursor_key("[]") is None and cursor_key("") is None
    # a cursor that is not one of this worker's forms reads as none
    assert cursor_key(json.dumps(["o", "n", "a", "u", "s", "k"])) is None
    assert cursor_key(json.dumps({"past": ["o", "n", "a", "u"]})) is None
    assert cursor_key(json.dumps({"past": []})) is None


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_read_past_an_agent_reaches_an_agent_named_after_it_with_any_byte() -> None:
    # An identity column takes any string, so `a\x00b` sorts after `a` and before every
    # other name that starts with `a`. A page past agent `a` bounds on the org, namespace
    # and agent alone, so it still reads that agent.
    from server.memory.store.sensitivity_sweep_sql import keys_sql, past_agent

    insert = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
        "memory_key, value, created_at) VALUES "
        "('o', 'n', 'a', '-', '-', generateUUIDv4(), 'k_a', '\"badge PIN is 4471\"', now()), "
        "('o', 'n', concat('a', char(0), 'b'), '-', '-', generateUUIDv4(), 'k_ab', "
        "'\"badge PIN is 4471\"', now())"
    )
    last_in_a = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-"}
    last_in_a |= {"session_id": "-", "memory_key": "k_a"}
    past_a = past_agent(last_in_a)
    params = {"shape": sweep.coarse_shape_re(set()), "lim": 10}
    params |= {f"after_{col}": value for col, value in past_a.items()}
    sql = keys_sql(org=None, namespace=None, since_days=None, after=tuple(past_a))
    keys = run([ddl("memory"), insert], sql, params)
    assert [(k["agent"], k["memory_key"]) for k in keys] == [("a\x00b", "k_ab")]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_pages_cross_namespace_and_user_boundaries_without_a_gap() -> None:
    # Pages of two rows end inside one user, at a user boundary and at a namespace boundary:
    # each selected row comes back exactly once, and the scan reaches the PIN row at the end.
    spin = {"spin": "4471"}
    rows = [
        _Row("o", "n", "-", _uuid(1), "spin_1", spin, 60),
        _Row("o", "n", "-", _uuid(2), "spin_2", spin, 60),
        _Row("o", "n", "-", _uuid(3), "spin_3", spin, 60),
        _Row("o", "n", "u2", _uuid(4), "spin_1", spin, 60),
        _Row("o", "n", "u2", _uuid(5), "spin_2", spin, 60),
        _Row("o", "n2", "-", _uuid(6), "spin_1", spin, 60),
        _Row("o", "n2", "u2", _uuid(7), "spin_1", spin, 60),
        _Row("o", "n2", "u2", _uuid(8), "vault_door", "badge PIN is 4471", 60),
    ]
    ch = _local_memory(rows)
    report = await sweep.scan(since_days=2, limit=2, page_size=2, pages=None, ch=ch)  # type: ignore[arg-type]
    returned = sorted(r["memory_id"] for r in ch.returned if "memory_id" in r)  # FINAL reads
    assert returned == sorted(row.memory_id for row in rows)
    assert [c.memory_key for c in report.candidates] == ["vault_door"]
    assert report.scanned == 8


class _WriteBetweenReads:
    """Answers the first query from `before` and every later one from `after`: a row
    written between a page's key read and its FINAL read."""

    def __init__(self, before: _LocalCH, after: _LocalCH) -> None:
        self.before = before
        self.after = after
        self.calls = 0

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.calls += 1
        return await (self.before if self.calls == 1 else self.after).query_all(sql, params)


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_key_written_inside_a_page_between_its_reads_is_read_once_and_none_skipped() -> (
    None
):
    # The first page's keys are k1 and k3. A row k2, which sorts between them, is written
    # before the page's FINAL read: that read returns k1, k2 and k3, and the next page
    # starts after k3, so every row is read exactly once.
    spin = {"spin": "4471"}
    rows = [
        _Row("o", "n", "-", _uuid(1), "k1", spin, 60),
        _Row("o", "n", "-", _uuid(3), "k3", spin, 60),
        _Row("o", "n", "-", _uuid(5), "k5", spin, 60),
    ]
    written = _Row("o", "n", "-", _uuid(2), "k2", "badge PIN is 4471", 60)
    ch = _WriteBetweenReads(_local_memory(rows), _local_memory([*rows, written]))
    report = await sweep.scan(since_days=2, limit=2, page_size=2, pages=None, ch=ch)  # type: ignore[arg-type]
    returned = [r["memory_id"] for r in ch.after.returned if "memory_id" in r]  # FINAL reads
    assert sorted(returned) == [_uuid(i) for i in (1, 2, 3, 5)]
    assert [c.memory_key for c in report.candidates] == ["k2"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_page_with_a_stale_key_never_skips_its_agent_when_a_new_key_fills_the_count() -> (
    None
):
    # The first page's keys are k1 and k3. k1 was labelled and its old unlabelled version
    # waits for a merge, so the FINAL read gives no row for it; k2, written between the
    # reads, gives one instead. Two rows for two keys, but not one for each key, so the scan
    # does not move past the agent and its second page reaches the PIN row k5.
    spin = {"spin": "4471"}
    pin = "badge PIN is 4471"
    rows = [
        _Row("o", "n", "-", _uuid(1), "k1", pin, 60),
        _Row("o", "n", "-", _uuid(3), "k3", spin, 60),
        _Row("o", "n", "-", _uuid(5), "k5", pin, 60),
    ]
    labelled = [rows[0]._replace(sensitivity="high", version=1)]
    written = _Row("o", "n", "-", _uuid(2), "k2", spin, 60)
    ch = _WriteBetweenReads(
        _local_memory(rows, labelled), _local_memory([*rows, written], labelled)
    )
    report = await sweep.scan(
        since_days=2,
        limit=2,
        page_size=2,
        pages=2,
        skip_noisy_agents=True,
        ch=ch,  # type: ignore[arg-type]
    )
    assert [c.memory_key for c in report.candidates] == ["k5"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_page_holds_distinct_keys_and_yields_only_current_unlabelled_versions() -> None:
    # The key read has no FINAL, so it sees every version: `gone` is tombstoned and
    # `vault_door` labelled `high` (what `apply` writes) in their current versions, and
    # `twice` has two unlabelled versions. A page of three keys holds all three, and the
    # FINAL read yields only `twice`, once.
    pin = "badge PIN is 4471"
    rows = [
        _Row("o", "n", "-", _uuid(1), "gone", pin, 60, version=1),
        _Row("o", "n", "-", _uuid(1), "gone", pin, 60, version=2, tombstoned=True),
        _Row("o", "n", "-", _uuid(2), "twice", pin, 60, version=1),
        _Row("o", "n", "-", _uuid(2), "twice", pin, 60, version=2),
        _Row("o", "n", "-", _uuid(3), "vault_door", pin, 60, version=1),
        _Row("o", "n", "-", _uuid(3), "vault_door", pin, 60, sensitivity="high", version=2),
    ]
    ch = _local_memory(rows)
    report = await sweep.scan(since_days=2, limit=3, page_size=3, pages=1, ch=ch)  # type: ignore[arg-type]
    keys = [r["memory_key"] for r in ch.returned if "memory_id" not in r]
    assert keys == ["gone", "twice", "vault_door"]
    assert [c.memory_key for c in report.candidates] == ["twice"] and report.scanned == 1


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_phone_shaped_rows_in_one_org_never_hide_a_credential_in_another(
    monkeypatch,
) -> None:
    # The first org holds more long digit runs than a pass's pages: epoch seconds and
    # milliseconds, which the detector reads as phone numbers, and 19-digit ids, which it
    # leaves unlabelled. The scheduled pass's filter selects none of them, so the token in
    # the next org is labelled.
    values = [
        *({"ts": 1760000000 + i} for i in range(6)),
        *({"ts": 1760000000000 + i} for i in range(6)),
        *({"id": 1234567890123456789 + i} for i in range(6)),
    ]
    rows = [
        _Row("acme", "n", "-", _uuid(i), f"obs_{i}", value, 60) for i, value in enumerate(values)
    ]
    ch = _local_memory([*rows, _token_row_in_another_org()])
    applied, out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=2, pages=2)
    assert [(c.org, c.memory_key) for c in applied] == [("zeta", "deploy_token")]
    assert out["scanned"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_prose_naming_a_credential_word_never_hides_a_credential_in_another_org(
    monkeypatch,
) -> None:
    # The first org holds more prose rows than a pass's pages, each naming a word the
    # detector labels only as an assignment. The filter selects none of them, so the token in
    # the next org is labelled.
    prose = [
        "I forgot my password",
        "the secret of a good stew is time",
        "cd $(pwd) && make",
        "send the bearer the parcel",
        "rotate the api key every month",
        {"note": "the password: rules are in the wiki"},
    ]
    rows = [
        _Row("acme", "n", "-", _uuid(i), f"note_{i}", value, 60) for i, value in enumerate(prose)
    ]
    ch = _local_memory([*rows, _token_row_in_another_org()])
    applied, out = await _run_pass_recording(monkeypatch, ch, rows_per_pass=2, pages=2)
    assert [(c.org, c.memory_key) for c in applied] == [("zeta", "deploy_token")]
    assert out["scanned"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_cli_sweep_pages_through_its_whole_scope(capsys, monkeypatch) -> None:
    # The operator's run reads past more than a page of selected-but-unlabelled rows and
    # reaches the PIN row behind them, in pages of two keys.
    import argparse

    from server.cli.ops import _sensitivity_sweep_run
    from server.settings import settings

    monkeypatch.setattr(settings, "sensitivity_sweep_max_rows_per_pass", 1)
    ch = _local_memory(_spin_rows_then_a_pin(5))
    page_params: list[dict[str, Any]] = []
    read = ch.query_all

    async def _recording(sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        page_params.append(dict(params or {}))
        return await read(sql, params)

    monkeypatch.setattr(ch, "query_all", _recording)
    args = argparse.Namespace(
        org="", namespace="", since_days=0, limit=2, only_shape=None, show=5, dry_run=True
    )
    rc = await _sensitivity_sweep_run(ch, args)
    out = capsys.readouterr().out
    assert "6 unlabelled row(s) matched" in out and "detector would label 1: high=1" in out
    assert rc == 1  # a dry run with candidates
    # the key reads, two keys to a page; a FINAL read also names the page's last key
    key_reads = [p for p in page_params if p.get("lim") == 2 and "upto_org" not in p]
    assert len(key_reads) > 1


def test_the_cli_sweep_limit_takes_a_positive_integer_only(capsys) -> None:
    import argparse

    from server.cli.ops import _add_sensitivity_sweep_parser

    parser = argparse.ArgumentParser()
    _add_sensitivity_sweep_parser(parser.add_subparsers())
    assert parser.parse_args(["sensitivity-sweep", "--limit", "1"]).limit == 1
    for bad in ("abc", "0", "-1"):
        with pytest.raises(SystemExit):
            parser.parse_args(["sensitivity-sweep", "--limit", bad])
        assert f"{bad!r} is not a positive integer" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_worker_sweeps_the_trailing_window_only(monkeypatch) -> None:
    from server.memory.workers import sensitivity_sweep as w
    from server.settings import settings

    seen: dict[str, Any] = {}

    async def _scan(**kw):
        seen.update(kw)
        return sweep.SweepReport(scanned=3)

    async def _apply(cands, *, ch=None):
        return sweep.ApplyResult()

    monkeypatch.setattr(sweep, "scan", _scan)
    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(settings, "sensitivity_sweep_window_days", 2)
    monkeypatch.setattr(settings, "sensitivity_sweep_max_rows_per_pass", 500)
    monkeypatch.setattr(settings, "sensitivity_sweep_pages_per_pass", 3)
    monkeypatch.setattr(settings, "sensitivity_sweep_shapes", "credential, email")
    monkeypatch.setattr(settings, "sensitivity_sweep_interval_seconds", 86400)
    out = await w.run_pass(ch=_CH())  # type: ignore[arg-type]
    assert seen["seconds"] == 450  # half the 900 s pass deadline
    assert seen["since_days"] == 2 and seen["limit"] == 500 and seen["page_size"] == 500
    assert seen["pages"] == 3
    assert seen["only_shapes"] == ["credential", "email"]  # the phone shape is the operator's
    assert seen["skip_noisy_agents"]
    assert out["scanned"] == 3 and out["relabelled"] == 0


def test_the_worker_is_registered_and_described() -> None:
    from server.workers.registry import worker_meta

    assert worker_meta()["sensitivity_sweep"]["interval_seconds"] == 86400


@pytest.mark.asyncio
async def test_the_missing_embedding_reconcile_skips_high_rows() -> None:
    """The embedding worker's reconcile must not re-embed a `high` row after
    the sweep tombstoned its vector (a high-sensitivity row has no embedding
    row). Both phases exclude the tier."""
    from server.memory import store as memory_repo

    ch = _CH(
        [
            {
                "org": "o",
                "namespace": "n",
                "agent": "a",
                "user_id": "-",
                "session_id": "-",
                "memory_key": "k",
            }
        ]
    )
    await memory_repo.find_memories_without_embedding_rows(batch_size=5, since_seconds=600, ch=ch)
    assert len(ch.calls) == 2
    for sql, _ in ch.calls:
        assert "m.sensitivity != 'high'" in sql and "m.tombstoned_at IS NULL" in sql
    # the stub takes the memory row's version, so a high write after this read fences it
    assert "m.version AS version" in ch.calls[1][0]


# ------------------------------------------------------------- (c) gauges


@pytest.mark.asyncio
async def test_integrity_counts_and_the_dim0_repair(monkeypatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.store import integrity as repo

    ch = _CH()
    ch.one = {"dim0_with_vector": 156, "tombstone_status_drift": 1957, "active_past_valid_to": 23}
    assert await repo.integrity_counts(ch=ch) == {
        "dim0_with_vector": 156,
        "tombstone_status_drift": 1957,
        "active_past_valid_to": 23,
        "high_with_embedding": 0,
    }
    assert "embedding_dim = 0 AND length(embedding) > 0" in ch.calls[0][0]
    inserted: list[list[dict[str, Any]]] = []

    async def _ins(rows, *, ch=None):
        inserted.append(rows)

    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _ins)
    rows = [
        {
            "org": "dev",
            "namespace": "td-1",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "k",
            "embedding": [0.5] * 4,
            "embedding_dim": 0,
            "version": 7,
            "content_excerpt": "x",
            "content_excerpt_lc": "x",
            "created_at": None,
        },
        {
            "org": "dev",
            "namespace": "td-1",
            "memory_key": "empty",
            "embedding": [],
            "embedding_dim": 0,
        },
    ]
    assert await repo.repair_dim0_rows(rows, ch=ch, dry_run=True) == 1 and not inserted
    assert await repo.repair_dim0_rows(rows, ch=ch) == 1
    out = inserted[0][0]
    assert out["embedding_dim"] == 4 and out["embedding"] == [0.5] * 4
    assert out["version"] != 7 and "content_excerpt_lc" not in out  # MATERIALIZED, never inserted


def test_the_probe_reads_the_three_new_keys_from_the_shared_sql() -> None:
    from server.memory.store import integrity as repo
    from server.observability import otel_probes

    captured: list[str] = []

    class _Rows:
        # mutations_pending leads the census row.
        result_rows = [(0, 0, 0, 0, 0, 0, 0, 0, 156, 1957, 23, 1)]

    class _Sync:
        def query(self, sql: str) -> _Rows:
            captured.append(sql)
            return _Rows()

    import unittest.mock as um

    with um.patch.object(otel_probes, "_probe_client", lambda: _Sync()):
        out = otel_probes._probe_invariants()
    assert out["dim0_with_vector"] == 156 and out["tombstone_status_drift"] == 1957
    assert out["active_past_valid_to"] == 23 and out["high_with_embedding"] == 1
    # One pass computes all four; the integration equivalence test holds each
    # equal to its shared statement (`repo.*_SQL`, what `integrity-repair` runs).
    assert repo.GAUGES and all(f"AS {g}" in captured[0] for g in repo.GAUGES)
    assert "sensitivity = 'high'" in captured[0] and "embedding_dim = 0" in captured[0]


@pytest.mark.asyncio
async def test_integrity_repair_cli_dry_run_lists_and_apply_reemits(monkeypatch) -> None:
    from server.cli import ops as cli_ops
    from server.memory.store import integrity as repo

    calls: list[Any] = []
    counts = {
        "dim0_with_vector": 2,
        "tombstone_status_drift": 0,
        "active_past_valid_to": 0,
        "high_with_embedding": 0,
    }

    async def _counts(*, ch=None):
        return dict(counts)

    async def _rows(*, limit, org=None, namespace=None, ch=None):
        calls.append(("rows", org, namespace, limit))
        return [{"org": "dev", "namespace": "td-1"}, {"org": "dev", "namespace": "td-1"}]

    async def _drift(*, limit, org=None, namespace=None, ch=None):
        return []

    async def _repair(rows, *, ch=None, dry_run=False):
        calls.append(("repair", len(rows)))
        counts["dim0_with_vector"] = 0
        return len(rows)

    monkeypatch.setattr(repo, "integrity_counts", _counts)
    monkeypatch.setattr(repo, "dim0_with_vector_rows", _rows)
    monkeypatch.setattr(repo, "drift_sample", _drift)
    monkeypatch.setattr(repo, "repair_dim0_rows", _repair)
    args = SimpleNamespace(org="dev", namespace="td-1", limit=10, show=5, dry_run=True)
    assert await cli_ops._integrity_repair_run(object(), args) == 1  # dry run with rows → non-zero
    assert calls == [("rows", "dev", "td-1", 10)]
    args.dry_run = False
    assert await cli_ops._integrity_repair_run(object(), args) == 0
    assert calls[-1] == ("repair", 2)


@pytest.mark.asyncio
async def test_sensitivity_sweep_cli_gates_on_the_high_sensitivity_invariant(monkeypatch) -> None:
    from server.cli import ops as cli_ops

    state = {"high_live": 1}

    async def _scan(**kw):
        return sweep.SweepReport(
            scanned=1,
            candidates=[_candidate("1", "o", "k", "high", "credential")],
        )

    async def _apply(cands, *, ch=None):
        state["high_live"] = 0
        return sweep.ApplyResult(relabelled=1, high=1, embeddings_tombstoned=1)

    async def _high(*, org=None, namespace=None, ch=None):
        return state["high_live"]

    monkeypatch.setattr(sweep, "scan", _scan)
    monkeypatch.setattr(sweep, "apply", _apply)
    monkeypatch.setattr(sweep, "high_with_embedding_count", _high)
    args = SimpleNamespace(
        org="o", namespace="n", since_days=0, limit=100, show=5, dry_run=True, only_shape=None
    )
    assert (
        await cli_ops._sensitivity_sweep_run(object(), args) == 1
    )  # dry run, candidates, invariant red
    args.dry_run = False
    assert await cli_ops._sensitivity_sweep_run(object(), args) == 0  # applied, invariant 0


@pytest.mark.asyncio
async def test_a_failed_high_retire_leaves_the_row_unlabelled_for_the_next_pass(
    monkeypatch,
) -> None:
    """The vectors and chunks go before the label: the scan reads only unlabelled
    rows, so a relabel written ahead of a failed retire would never be retried."""
    from server.memory import store as memory_repo

    async def _retire(items, *, ch=None, bare=False):
        raise RuntimeError("insert timed out")

    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    cand = _candidate("1", "o", "k", "high", "credential")
    heads = _Heads(monkeypatch, [cand])
    res = await sweep.apply([cand], ch=_CH())
    assert heads.rows == [] and res.relabelled == 0
    assert len(res.errors) == 1 and "insert timed out" in res.errors[0]


@pytest.mark.asyncio
async def test_a_high_relabel_retires_the_keys_chunks_again_after_the_label(
    monkeypatch,
) -> None:
    """A chunk writer re-checks the head after its insert: chunks that landed between
    the fence and the relabel saw an unlabelled head, so the sweep retires the key's
    chunks once more after the relabel is in."""
    from server.memory import store as memory_repo
    from server.memory.app import high_fence

    order: list[str] = []

    async def _retire(items, *, ch=None, bare=False):
        assert bare  # a high row's tombstones hold none of its text
        order.append("retire")
        return 1

    async def _fence(items, *, model, ch=None):
        order.append("fence")
        return 1

    async def _chunks(**_k):
        order.append("chunks")
        return 0

    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)
    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _chunks)
    cand = _candidate("1", "o", "k", "high", "credential")
    _Heads(monkeypatch, [cand], on_insert=lambda _rows: order.append("relabel"))
    res = await sweep.apply([cand], ch=_CH())
    assert not res.errors
    assert res.embeddings_tombstoned == 1  # the second retire is not counted
    assert order == ["retire", "fence", "chunks", "relabel", "chunks", "retire"]


@pytest.mark.asyncio
async def test_the_medium_tag_copy_rechecks_its_head(monkeypatch) -> None:
    """The medium tag re-emit is a vector copy like touch and verify: after its insert
    it runs the post-copy head re-check, which retires it under a head made 'high'."""
    from server.memory import store as memory_repo

    checked: list[list[dict[str, Any]]] = []

    async def _live(ch, **ident):
        return [{**ident, "tags": ["t"], "embedding": [0.1], "embedding_dim": 1, "version": 3}]

    async def _ins(rows, *, ch=None):
        return None

    async def _check(rows, *, ch=None):
        checked.append(rows)
        return 0

    monkeypatch.setattr(memory_repo, "live_embedding_rows_for_key", _live)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", _ins)
    monkeypatch.setattr(memory_repo, "retire_copies_under_unembedded_heads", _check)
    cand = _candidate("2", "o", "k", "medium", "email")
    assert await sweep._tag_embeddings(cand, ch=_CH()) == 1
    assert len(checked) == 1 and checked[0][0]["memory_key"] == "k"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["high", "medium"])
@pytest.mark.parametrize("moved", ["version", "memory_id"])
async def test_a_head_written_since_the_scan_is_left_for_the_next_pass(
    monkeypatch, level, moved
) -> None:
    """The key's head is no longer the one the scan read: a newer version, or another id
    under the key. It holds text the detector did not read, or its writer's own label: the
    sweep neither relabels it nor retires its vectors."""
    from server.memory import store as memory_repo

    touched: list[str] = []

    async def _retire(items, *, ch=None, bare=False):
        touched.append("retire")
        return 1

    async def _fence(items, *, model, ch=None):
        touched.append("fence")
        return len(items)

    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(memory_repo, "fence_vectors", _fence)
    shape = "credential" if level == "high" else "email"
    cand = _candidate("1", "o", "k", level, shape)
    head = dataclasses.replace(cand, memory_id="9") if moved == "memory_id" else cand
    heads = _Heads(monkeypatch, [head], version=2 if moved == "version" else 1)
    res = await sweep.apply([cand], ch=_CH())
    assert res.as_dict()["errors"] == 0 and (res.relabelled, len(res.left)) == (0, 1)
    assert touched == [] and heads.rows == []


@pytest.mark.asyncio
async def test_a_high_candidate_whose_head_is_gone_retires_nothing(monkeypatch) -> None:
    from server.memory import store as memory_repo

    async def _retire(items, *, ch=None, bare=False):
        raise AssertionError("a gone head's vectors are not the sweep's to retire")

    _Heads(monkeypatch, [])
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    res = await sweep.apply([_candidate("1", "o", "k", "high", "credential")], ch=_CH())
    assert (res.relabelled, len(res.left), res.errors) == (0, 1, [])


def test_a_candidate_left_for_a_moved_head_keeps_the_cursor_at_its_agent() -> None:
    # The next pass reads that agent again and the newer head with it. A pass whose
    # candidates in the agent all failed still moves past it.
    from server.memory.workers.sensitivity_sweep import _resume_past_agent

    cands = [_candidate(str(i), "o", f"k{i}", "high", "credential") for i in range(2)]
    report = sweep.SweepReport(candidates=cands, resume_after=cands[-1].sort_key)
    assert not _resume_past_agent(report, sweep.ApplyResult(left=list(cands)))
    mixed = sweep.ApplyResult(failed=[(cands[0], "x")], left=cands[1:])
    assert not _resume_past_agent(report, mixed)
    failed = sweep.ApplyResult(failed=[(c, "x") for c in cands])
    assert _resume_past_agent(report, failed)
    # One left in an earlier agent is read again only when the sweep comes back round to it.
    earlier = dataclasses.replace(cands[0], agent="0", sort_key=cands[0].sort_key | {"agent": "0"})
    assert _resume_past_agent(report, dataclasses.replace(failed, left=[earlier]))


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["high", "medium"])
async def test_a_commit_refused_for_a_moved_head_leaves_the_candidate(monkeypatch, level) -> None:
    # Under Postgres the relabel commits only over the head it read: one that moved after
    # the read refuses it. The candidate is left, not failed, announces nothing, and puts
    # no vector retire or fence above the moved head.
    from server.core import pg
    from server.memory.app import high_fence, webhooks
    from server.memory.store.pg_memory import AdmissionContendedError

    announced: list[Any] = []
    fenced: list[Any] = []

    def _refused(_rows: Any) -> None:
        raise AdmissionContendedError("the head moved")

    async def _fence(items, **_k):
        fenced.append(items)

    async def _announce(events: Any) -> None:
        announced.extend(events)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(high_fence, "fence_committed", _fence)
    monkeypatch.setattr(sweep, "fence_high", _fence)
    monkeypatch.setattr(webhooks, "announce", _announce)
    shape = "credential" if level == "high" else "email"
    cand = _candidate("1", "o", "k", level, shape)
    _Heads(monkeypatch, [cand], on_insert=_refused)
    res = await sweep.apply([cand], ch=_CH())
    assert (res.relabelled, len(res.left), res.errors, announced) == (0, 1, [], [])
    assert (res.embeddings_tombstoned, fenced) == (0, [])


def _lost_answer() -> BaseException:
    from server.core import pg

    return pg.PostgresUnavailableError("lost", outcome_unknown=True)


def _cancelled() -> BaseException:
    import asyncio

    return asyncio.CancelledError()


def _insert_timed_out() -> BaseException:
    return RuntimeError("insert timed out")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("postgres", "published", "error", "closed", "resolved"),
    [
        (False, True, None, True, True),  # in ClickHouse: closed and resolved at once
        (True, True, None, True, True),  # published under Postgres: the same
        (True, False, None, False, False),  # publication pending: left to the repair
        (True, False, _lost_answer, False, False),  # may be in: left to the repair
        (True, False, _cancelled, False, False),
        (False, False, _insert_timed_out, False, False),  # may have landed on ClickHouse too
    ],
)
async def test_a_high_relabel_owes_its_facts_before_its_commit(
    monkeypatch, postgres, published, error, closed, resolved
) -> None:
    """The facts a 'high' relabel's text gave the graph are owed before it commits, so
    whatever fails after, the projection repair closes them; they are closed and the debt
    resolved at once when ClickHouse, which the graph is judged from, holds the relabel."""
    import asyncio

    from server.core import pg
    from server.memory import store as memory_repo
    from server.memory.app import high_fence

    closed_facts: list[Any] = []
    order: list[str] = []

    async def _close(key, memory_id, version, *, ch):
        closed_facts.append((key, memory_id, version))

    async def _quiet(*_a, **_k):
        return 0

    async def _fence_committed(items, **_k):
        return True

    owed_at_commit: list[int] = []

    def _fail(rows: Any) -> None:
        owed_at_commit.append(len(heads.owed))
        order.append("relabel")
        if error is not None:
            raise error()

    async def _fenced(*_a, **_k):
        order.append("fence")

    monkeypatch.setattr(pg, "enabled", lambda: postgres)
    monkeypatch.setattr(sweep, "_close_mined_facts", _close)
    monkeypatch.setattr(sweep, "fence_high", _fenced)
    monkeypatch.setattr(high_fence, "fence_committed", _fence_committed)
    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _quiet)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _quiet)
    cand = _candidate("1", "o", "k", "high", "credential")
    key = tuple(cand.sort_key[c] for c in SORT_KEY)
    heads = _Heads(monkeypatch, [cand], published=published, on_insert=_fail)
    if error is _cancelled:
        with pytest.raises(asyncio.CancelledError):
            await sweep.apply([cand], ch=_CH())
    else:
        await sweep.apply([cand], ch=_CH())
    (owed,) = heads.owed
    assert owed["key"] == list(key) and owed["memory_id"] == cand.memory_id
    assert owed_at_commit == [1]  # owed right before the commit
    # On ClickHouse the fence goes first, so one that fails owes nothing.
    assert order == (["relabel"] if postgres else ["fence", "relabel"])
    assert closed_facts == ([(key, cand.memory_id, owed["version"])] if closed else [])
    assert heads.resolved == (["owed-1"] if resolved else [])


@pytest.mark.asyncio
async def test_a_relabel_refused_resolves_what_it_owed_and_one_unowed_is_not_written(
    monkeypatch,
) -> None:
    """A commit refused for a moved head resolves the facts it owed, as nothing landed; a
    relabel whose debt cannot be recorded fails unlabelled, so the next pass retries it."""
    from server.core import pg
    from server.memory.store.pg_memory import AdmissionContendedError

    def _refused(_rows: Any) -> None:
        raise AdmissionContendedError("the head moved")

    monkeypatch.setattr(pg, "enabled", lambda: True)
    cand = _candidate("1", "o", "k", "high", "credential")
    heads = _Heads(monkeypatch, [cand], on_insert=_refused)
    res = await sweep.apply([cand], ch=_CH())
    assert (len(res.left), heads.resolved) == (1, ["owed-1"])

    async def _unrecorded(**_kw: Any) -> str:
        raise RuntimeError("insert refused")

    heads = _Heads(monkeypatch, [cand])
    monkeypatch.setattr(sweep, "record_projection_failure", _unrecorded)
    res = await sweep.apply([cand], ch=_CH())
    assert (res.relabelled, len(res.errors), heads.rows) == (0, 1, [])


@pytest.mark.asyncio
async def test_a_relabel_in_that_then_fails_is_announced_and_its_facts_close(
    monkeypatch,
) -> None:
    """Its facts close first once the relabel is in, so a step after it that fails, here
    the chunk tombstone, leaves none open; the relabel is announced as it landed."""
    from server.memory import store as memory_repo
    from server.memory.app import high_fence, webhooks

    closed: list[Any] = []
    announced: list[Any] = []

    async def _close(key, memory_id, version, *, ch):
        closed.append(key)

    async def _chunks(**_k):
        if heads.rows:  # the tombstone after the relabel is in
            raise RuntimeError("chunk read timed out")
        return 0

    async def _quiet(*_a, **_k):
        return 0

    async def _announce(events: Any) -> None:
        announced.extend(events)

    monkeypatch.setattr(sweep, "_close_mined_facts", _close)
    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _chunks)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _quiet)
    monkeypatch.setattr(memory_repo, "fence_vectors", _quiet)
    monkeypatch.setattr(webhooks, "announce", _announce)
    cand = _candidate("1", "o", "k", "high", "credential")
    heads = _Heads(monkeypatch, [cand])
    res = await sweep.apply([cand], ch=_CH())
    assert len(res.errors) == 1 and "chunk read timed out" in res.errors[0]
    assert (len(closed), heads.resolved, len(announced)) == (1, ["owed-1"], 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("postgres", "authority", "clickhouse", "outcome"),
    [
        (True, 5, 5, "closed"),  # in ClickHouse: closed
        (True, 7, 7, "closed"),  # a later write or delete: closed once ClickHouse holds it
        (True, 5, 1, "pending"),  # in the authority only: the repair waits
        (True, 5, None, "pending"),  # ClickHouse holds no row of the key yet
        (True, 1, 1, "nothing"),  # settled below the relabel: it did not land
        (True, None, None, "nothing"),  # the key holds no row (erased)
        (True, "timeout", 1, "raised"),  # a COMMIT still holds the head: left active
        (False, 5, 5, "closed"),  # ClickHouse is the authority
        (False, 1, 1, "nothing"),
    ],
)
async def test_owed_facts_close_once_clickhouse_holds_the_relabel(
    monkeypatch, postgres, authority, clickhouse, outcome
) -> None:
    """The repair's close of owed facts: nothing owed when the authority's head settled
    below the relabel's version or the key holds no row, a wait while ClickHouse lacks
    it, a close once it holds it. Under Postgres the head is the settled one, so a commit
    still waiting on the standby is waited for, and a wait past the lock timeout raises."""
    from server.core import pg
    from server.memory import store as memory_repo
    from server.memory.store import pg_memory

    key = ("o", "n", "a", "-", "-", "k")
    closed: list[Any] = []

    async def _settled(keys):
        assert postgres  # on ClickHouse its own head is the authority's
        if authority == "timeout":
            raise pg.PostgresUnavailableError("lock wait timed out")
        return {} if authority is None else {key: authority}

    async def _clickhouse(keys, **_k):
        return {} if clickhouse is None else {key: clickhouse}

    async def _closed(*a, **_k):
        closed.append(a)

    monkeypatch.setattr(pg, "enabled", lambda: postgres)
    monkeypatch.setattr(pg_memory, "settled_head_versions", _settled)
    monkeypatch.setattr(memory_repo, "clickhouse_head_versions", _clickhouse)
    monkeypatch.setattr(sweep, "_close_mined_facts", _closed)
    if outcome == "raised":
        with pytest.raises(pg.PostgresUnavailableError):
            await sweep.close_owed_facts(key, "mid", 5, ch=_CH())
    else:
        done = await sweep.close_owed_facts(key, "mid", 5, ch=_CH())
        assert done == (outcome != "pending")
    assert closed == ([(key, "mid", 5)] if outcome == "closed" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edges", "triples", "raised"),
    [
        ("fails", "closes", "edges unreachable"),
        ("drops", "fails", "triples unreachable"),
        ("fails", "fails", "triples unreachable"),  # both: the triples' error is raised
        ("drops", "restate", "repair timed out"),
        ("fails", "restate", "repair timed out"),
    ],
)
async def test_a_failed_close_of_owed_facts_raises_so_the_debt_stays(
    monkeypatch, caplog, edges, triples, raised
) -> None:
    """The owed close calls the raising forms, so a failure leaves the owed record active.
    The triples close whatever the edge drop did, and a fallback restate whose interval
    repair failed is owed on its own with the closes it made, as a reconcile owes it."""
    from datetime import datetime

    from server.memory import store as memory_repo
    from server.memory.app import edge_links, projection_failures, triple_projections
    from server.memory.app.projection_errors import (
        TRIPLES_INSERTED_AT,
        TRIPLES_RESTORE_ID,
        TriplesRepairFailed,
    )

    key = ("o", "n", "a", "u1", "-", "k")  # the user the record is erased with
    closed: list[str] = []
    recorded: list[dict[str, Any]] = []

    async def _versions(keys, **_k):
        return {key: 5}

    async def _drop(*_a, **_k):
        if edges == "fails":
            raise RuntimeError("edges unreachable")

    async def _close(*_a, **_k):
        closed.append("triples")
        if triples == "fails":
            raise RuntimeError("triples unreachable")
        if triples == "restate":
            raise TriplesRepairFailed(
                datetime(2026, 1, 1), RuntimeError("repair timed out"), restore_id="r1"
            )

    async def _record(**kw):
        recorded.append(kw)
        return "f2"

    monkeypatch.setattr(memory_repo, "clickhouse_head_versions", _versions)
    monkeypatch.setattr(edge_links, "drop_undeclared_links", _drop)
    monkeypatch.setattr(triple_projections, "close_dropped_facts", _close)
    monkeypatch.setattr(projection_failures, "record_projection_failure", _record)
    expected = TriplesRepairFailed if triples == "restate" else RuntimeError
    with caplog.at_level("WARNING"), pytest.raises(expected, match=raised):
        await sweep.close_owed_facts(key, "mid", 5, ch=_CH())
    assert closed == ["triples"]  # closed whatever the edge drop did
    # The edge drop's error is logged only when the triples' is raised in its place.
    lost = edges == "fails" and triples != "closes"
    assert ("edge drop of mid failed: RuntimeError" in caplog.text) == lost
    assert "edges unreachable" not in caplog.text  # the summary names the type, not the text
    if triples == "restate":
        (owed,) = recorded
        assert owed["projection"] == projection_failures.TRIPLES_RECONCILE
        assert (owed["payload"]["memory_ids"], owed["payload"][TRIPLES_RESTORE_ID]) == (
            ["mid"],
            "r1",
        )
        assert owed["payload"][TRIPLES_INSERTED_AT] == "2026-01-01T00:00:00"
        assert owed["user_id"] == "u1"
    else:
        assert recorded == []


@pytest.mark.asyncio
async def test_a_restate_whose_record_fails_logs_and_raises_the_restate(
    monkeypatch, caplog
) -> None:
    """A fallback restate's repair the sweep cannot record is lost, as a reconcile's is:
    the record's failure is logged by its type, never its text (the payload may hold the
    row's facts), and the restate's error is raised."""
    from datetime import datetime

    from server.memory import store as memory_repo
    from server.memory.app import edge_links, projection_failures, triple_projections
    from server.memory.app.projection_errors import TriplesRepairFailed

    key = ("o", "n", "a", "-", "-", "k")

    async def _versions(keys, **_k):
        return {key: 5}

    async def _quiet(*_a, **_k):
        return None

    async def _close(*_a, **_k):
        raise TriplesRepairFailed(
            datetime(2026, 1, 1), RuntimeError("repair timed out"), restore_id="r1"
        )

    async def _record(**_kw):
        raise RuntimeError("record refused")

    monkeypatch.setattr(memory_repo, "clickhouse_head_versions", _versions)
    monkeypatch.setattr(edge_links, "drop_undeclared_links", _quiet)
    monkeypatch.setattr(triple_projections, "close_dropped_facts", _close)
    monkeypatch.setattr(projection_failures, "record_projection_failure", _record)
    with caplog.at_level("WARNING"), pytest.raises(TriplesRepairFailed, match="repair timed out"):
        await sweep.close_owed_facts(key, "mid", 5, ch=_CH())
    assert "projection failure log failed for triples_reconcile" in caplog.text
    assert "record refused" not in caplog.text


@pytest.mark.asyncio
async def test_a_committed_relabel_whose_close_fails_keeps_its_debt(monkeypatch) -> None:
    """In the sweep, a relabel that is in but whose facts close fails is counted and
    announced, and leaves its owed record for the repair."""
    from server.memory import store as memory_repo
    from server.memory.app import webhooks

    announced: list[Any] = []

    async def _close(*_a, **_k):
        raise RuntimeError("triples unreachable")

    async def _quiet(*_a, **_k):
        return 0

    async def _announce(events: Any) -> None:
        announced.extend(events)

    monkeypatch.setattr(sweep, "_close_mined_facts", _close)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _quiet)
    monkeypatch.setattr(memory_repo, "fence_vectors", _quiet)
    monkeypatch.setattr(webhooks, "announce", _announce)
    cand = _candidate("1", "o", "k", "high", "credential")
    heads = _Heads(monkeypatch, [cand])
    res = await sweep.apply([cand], ch=_CH())
    assert (res.relabelled, res.high, res.errors) == (1, 1, [])
    assert (len(heads.owed), heads.resolved, len(announced)) == (1, [], 1)


@pytest.mark.asyncio
async def test_under_postgres_a_high_relabel_is_fenced_once_committed(monkeypatch) -> None:
    """Under Postgres the fence follows the relabel's commit, at its version (the commit's
    delivery retires the head's vectors: tests/integration/test_pg_authority.py)."""
    from server.core import pg
    from server.memory import store as memory_repo
    from server.memory.app import high_fence

    order: list[str] = []
    fenced: list[Any] = []

    async def _retire(items, *, ch=None, bare=False):
        order.append("retire")
        return 1

    async def _fence_committed(items, **_k):
        order.append("fence")
        fenced.extend(items)
        return True

    async def _chunks(**_k):
        order.append("chunks")
        return 0

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(high_fence, "fence_committed", _fence_committed)
    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _chunks)
    cand = _candidate("1", "o", "k", "high", "credential")
    heads = _Heads(monkeypatch, [cand], on_insert=lambda _rows: order.append("relabel"))
    res = await sweep.apply([cand], ch=_CH())
    assert not res.errors and (res.high, res.embeddings_tombstoned) == (1, 0)
    # The chunks and vectors go again after the label: the read is the authority's, so
    # this holds while the publish is pending.
    assert order == ["relabel", "fence", "chunks", "retire"]
    assert fenced == [(tuple(cand.sort_key[c] for c in SORT_KEY), heads.rows[0]["version"])]
