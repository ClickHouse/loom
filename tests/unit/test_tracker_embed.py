"""tracker.similar's vector leg reads embeddings loom computed itself: the
repository's statements on a stub client (FINAL, the tombstone
predicate, the dim guard, the wall, the exclusion, the LIMIT, and
`namespace = {ns}` on every one), the pass on a fake QA upstream and a fake
embedder (rows written with every column, an unchanged digest skipped, an
item that left the window tombstoned, the cursor advanced, EVERY configured
namespace embedded from its own source — a private row never under a
standard namespace), the leg's statuses on the handler (`served` with
rows, `not_computed` on an empty table), the recall-only fusion (the leg
adds, never re-ranks), the query-time catch-up, the post-fusion hydration,
the `relation` the record adds, the registry wiring and the erasure census
entry."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.erasure.store import census as erasure_census
from server.settings import settings
from server.tracker.api.models import TrackerSimilarInput
from server.tracker.app import embed_index as ei
from server.tracker.app import similar as tsim
from server.tracker.app import source as tsrc
from server.tracker.domain import similar as sim
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store import embed as qr
from server.tracker.workers import embed as wk
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, NS, REPO = "clickhouse", "tracker-clickhouse", "ClickHouse/ClickHouse"
PRIV, PREPO = "tracker-priv", "ClickHouse/example-private"
T0 = datetime(2026, 9, 16, 12, 0, 0)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG, raising=False)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json({REPO: NS, PREPO: PRIV}, restricted=PRIV),
        raising=False,
    )
    monkeypatch.setattr(
        settings, "tracker_credentials_json", tracker_credentials_json(), raising=False
    )
    monkeypatch.setattr(settings, "tracker_live_serve", True, raising=False)
    monkeypatch.setattr(settings, "tracker_similar_vector_leg", False, raising=False)
    monkeypatch.setattr(settings, "tracker_embed_body_chars", 600, raising=False)
    monkeypatch.setattr(settings, "tracker_embed_batch", 2, raising=False)
    monkeypatch.setattr(settings, "tracker_embed_closed_days", 365, raising=False)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


# ---------------------------------------------------------------- repository


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append(("all", sql, params or {}))
        return list(self.rows)

    async def query_one(self, sql, params=None):
        self.calls.append(("one", sql, params or {}))
        return self.rows[0] if self.rows else None

    async def insert(self, table, rows, settings=None, **kw):
        self.inserted.append((table, rows))


def _norm(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_nearest_statement_carries_final_tombstone_dim_wall_exclusion_and_limit() -> None:
    ch = _CH(
        [
            {
                "kind": "issue",
                "number": 116490,
                "source_id": f"{REPO}#116490",
                "component": "MergeTree",
                "is_open": 1,
                "created_at": datetime(2026, 8, 1),
                "distance": 0.12,
            }
        ]
    )
    repo = qr.TrackerEmbedRepo(ch)  # type: ignore[arg-type]
    rows = await repo.nearest(
        ORG,
        NS,
        vector=[0.1] * 8,
        dim=8,
        limit=50,
        kinds=["issue"],
        before=T0,
        exclude=("issue", 119601),
    )
    _, sql, params = ch.calls[-1]
    q = _norm(sql)
    assert "FROM loom.qa_item_embeddings FINAL" in q
    assert "tombstoned_at IS NULL" in q and "embedding_dim = {dim:UInt16}" in q
    assert "cosineDistance(embedding, {q:Array(Float32)}) AS distance" in q
    assert "created_at < {before:DateTime}" in q and params["before"] == T0
    assert "NOT (kind = {xk:String} AND number = {xn:UInt64})" in q
    assert params["xk"] == "issue" and params["xn"] == 119601 and params["dim"] == 8
    assert "kind IN {kinds:Array(String)}" in q and params["kinds"] == ["issue"]
    # The bare ORDER BY the codes-first scan needs; the tie-break is in Python
    assert "ORDER BY distance ASC LIMIT {limit:UInt32}" in q and params["limit"] == 50
    assert "is_open = 1" not in q  # a full scan: narrowing measured no faster
    assert (
        rows[0]["number"] == 116490 and rows[0]["is_open"] is True and rows[0]["distance"] == 0.12
    )
    assert "title" not in q and "title" not in rows[0]  # a derived index: keys and vectors only
    # the cap; the guards
    await repo.nearest(ORG, NS, vector=[0.1] * 8, dim=8, limit=999)
    _, sql, params = ch.calls[-1]
    assert params["limit"] == 200 and "created_at <" not in sql and "NOT (kind" not in sql
    n = len(ch.calls)
    assert await repo.nearest(ORG, NS, vector=[], dim=8) == []
    assert await repo.nearest(ORG, NS, vector=[0.1], dim=0) == []
    assert len(ch.calls) == n


@pytest.mark.asyncio
async def test_every_repository_statement_is_scoped_to_the_addressed_namespace() -> None:
    """The leg reads only the addressed namespace's rows: a restricted
    namespace's vectors are reachable only through the restricted-namespace gate on
    THAT namespace (the transport's, before the handler runs), so no
    statement of the repository is without `org = {org} AND namespace =
    {ns}` — the pass's reads and the leg's alike."""
    ch = _CH([])
    repo = qr.TrackerEmbedRepo(ch)  # type: ignore[arg-type]
    await repo.stored(ORG, PRIV)
    await repo.stored_digests(ORG, PRIV, "issue", [1])
    await repo.rows_by_key(ORG, PRIV, "issue", [1])
    await repo.stored_vector(ORG, PRIV, "issue", 1)
    await repo.nearest(ORG, PRIV, vector=[0.1], dim=1)
    await repo.namespace_stats(ORG, PRIV)
    assert len(ch.calls) == 6
    for _, sql, params in ch.calls:
        assert "WHERE org = {org:String} AND namespace = {ns:String}" in _norm(sql), sql
        assert params["org"] == ORG and params["ns"] == PRIV


@pytest.mark.asyncio
async def test_repository_reads_are_keyed_and_the_digest_comes_back_as_text() -> None:
    ch = _CH(
        [
            {
                "kind": "issue",
                "number": 5,
                "text_sha256": b"a" * 64,
                "tombstoned_at": None,
                "updated_at": T0,
                "embedding_dim": 8,
                "embedding_model": "m",
            }
        ]
    )
    repo = qr.TrackerEmbedRepo(ch)  # type: ignore[arg-type]
    stored = await repo.stored(ORG, NS)
    assert stored[("issue", 5)]["text_sha256"] == "a" * 64  # FixedString bytes → str
    assert "FINAL WHERE org = {org:String} AND namespace = {ns:String}" in _norm(ch.calls[-1][1])
    assert "embedding," not in ch.calls[-1][1]  # the scan never reads the vectors
    ch.rows = [{"embedding": [0.5] * 8, "embedding_dim": 8, "embedding_model": "m"}]
    vec = await repo.stored_vector(ORG, NS, "issue", 5)
    assert vec == {"embedding": [0.5] * 8, "embedding_dim": 8, "embedding_model": "m"}
    q = _norm(ch.calls[-1][1])
    assert "kind = {kind:String} AND number = {n:UInt64} AND tombstoned_at IS NULL" in q
    assert "embedding_dim > 0 LIMIT 1" in q
    ch.rows = []
    assert await repo.stored_vector(ORG, NS, "issue", 5) is None
    assert await repo.rows_by_key(ORG, NS, "issue", []) == [] and len(ch.calls) == 3
    await repo.rows_by_key(ORG, NS, "issue", [7, 5, 5])
    assert ch.calls[-1][2]["nums"] == [5, 7] and "number IN {nums:Array(UInt64)}" in ch.calls[-1][1]
    ch.rows = [{"live": 3, "dead": 1, "last_updated": T0, "dims": [8]}]
    assert await repo.namespace_stats(ORG, NS) == {
        "live": 3,
        "dead": 1,
        "last_updated": T0,
        "dims": [8],
    }
    # the builder: every column present, an unknown one refused, the dim from the vector
    row = qr.build_embed_row(org=ORG, namespace=NS, kind="issue", number=1, embedding=[0.1] * 4)
    assert (
        set(row) == set(qr.TRACKER_EMBED_COLS) and row["embedding_dim"] == 4 and row["version"] > 0
    )
    assert row["created_at"] == datetime(1970, 1, 1) and row["text_sha256"] == qr.ZERO_SHA
    with pytest.raises(ValueError, match="unknown columns"):
        qr.build_embed_row(nope=1)
    with pytest.raises(ValueError, match="use build_embed_row"):
        await repo.insert_rows([{"org": ORG}])
    await repo.insert_rows([row])
    assert ch.inserted[-1][0] == qr.TABLE


# ---------------------------------------------------------------- the pass


class _Emb:
    model_name, dim = "fake-ngram-8", 8

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    async def embed_many(self, texts):
        self.batches.append(list(texts))
        return [[float(len(t) % 7) / 7.0] * 8 for t in texts]

    async def embed(self, text):
        return (await self.embed_many([text]))[0]


_MODEL = {"embedding_model": _Emb.model_name}


class _Upstream:
    """A fake TrackerSource: the window's items per kind, keyed by number."""

    populated = False

    def __init__(self, items: dict[str, list[dict[str, Any]]]) -> None:
        self.items = items
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def upstream_embeddings_populated(self, org, ns):
        self.calls.append(("upstream", {}))
        return self.populated

    async def embed_window_keys(self, org, ns, *, kind, closed_days, stamp=""):
        self.calls.append(("keys", {"kind": kind, "closed_days": closed_days}))
        return [
            {"source_id": it["source_id"], "updated_at": it["updated_at"]}
            for it in self.items.get(kind, [])
        ]

    async def embed_items(
        self, org, ns, *, kind, since, closed_days, body_chars, limit, stamp="", since_id=None
    ):
        self.calls.append(
            (
                "items",
                {"kind": kind, "since": since, "since_id": since_id, "body_chars": body_chars},
            )
        )

        def after(it):  # the statement's predicate: strict `>`, or the keyset tuple
            if since is None:
                return True
            if since_id is None:
                return it["updated_at"] > since
            return (it["updated_at"], it["source_id"]) > (since, since_id)

        rows = [it for it in self.items.get(kind, []) if after(it)]
        return sorted(rows, key=lambda r: (r["updated_at"], r["source_id"]))[:limit]

    async def embed_items_by_number(self, org, ns, *, kind, numbers, body_chars, stamp=""):
        self.calls.append(("by_number", {"kind": kind, "numbers": list(numbers)}))
        want = {int(n) for n in numbers}
        return [it for it in self.items.get(kind, []) if int(it["number"]) in want]


class _Repo:
    """The prior rows belong to `ns` (the public namespace by default); any
    other namespace starts empty."""

    def __init__(
        self,
        stored: dict[tuple[str, int], dict[str, Any]] | None = None,
        full: dict[tuple[str, int], dict[str, Any]] | None = None,
        ns: str = NS,
    ) -> None:
        self._stored = stored or {}
        self._full = full or {}
        self.ns = ns
        self.rows: list[dict[str, Any]] = []

    async def stored(self, org, ns):
        return dict(self._stored) if ns == self.ns else {}

    async def rows_by_key(self, org, ns, kind, numbers):
        return [dict(self._full[(kind, n)]) for n in numbers if (kind, n) in self._full]

    async def insert_rows(self, rows):
        for r in rows:
            assert set(r) == set(qr.TRACKER_EMBED_COLS), sorted(set(qr.TRACKER_EMBED_COLS) ^ set(r))
        self.rows.extend(rows)


class _State:
    """The per-namespace, per-kind cursor (`loom.code_index_state` is keyed
    by namespace: one namespace's pass never moves another's)."""

    def __init__(self) -> None:
        self.cursor: dict[tuple[str, str], datetime] = {}
        self.ids: dict[tuple[str, str], str] = {}

    async def get(self, org, ns, kind):
        return self.cursor.get((ns, kind))

    async def get_cursor(self, org, ns, kind):
        return self.cursor.get((ns, kind)), self.ids.get((ns, kind), "")

    async def set(self, org, ns, kind, value, source_id=""):
        self.cursor[(ns, kind)] = value
        self.ids[(ns, kind)] = source_id


def _item(
    kind: str,
    n: int,
    title: str,
    body: str = "body",
    *,
    upd: datetime = T0,
    closed: datetime | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_id": f"{REPO}#{n}",
        "number": n,
        "title": title,
        "body": body,
        "component": "MergeTree",
        "created_at": datetime(2026, 8, 1),
        "updated_at": upd,
        "closed_at": closed,
    }


@pytest.mark.asyncio
async def test_pass_writes_full_rows_skips_unchanged_digests_tombstones_the_departed_and_moves_the_cursor(
    monkeypatch,
) -> None:
    kept_text = wk.embed_text("Kept issue", "same body", body_chars=600)
    stored = {
        ("issue", 1): {"text_sha256": wk.text_sha256(kept_text), "tombstoned_at": None, **_MODEL},
        ("issue", 2): {"text_sha256": "0" * 64, "tombstoned_at": None, **_MODEL},  # text changed
        ("issue", 9): {"text_sha256": "1" * 64, "tombstoned_at": None, **_MODEL},  # left the window
        ("pr", 8): {"text_sha256": "2" * 64, "tombstoned_at": T0},  # already tombstoned: left alone
    }
    full = {
        ("issue", 9): qr.build_embed_row(
            org=ORG,
            namespace=NS,
            kind="issue",
            number=9,
            source_id=f"{REPO}#9",
            component="gone",
            embedding=[0.2] * 8,
            text_sha256="1" * 64,
        )
    }
    up = _Upstream(
        {
            "issue": [
                _item("issue", 1, "Kept issue", "same body"),
                _item("issue", 2, "Changed issue", "new body", upd=T0.replace(hour=13)),
                _item(
                    "issue",
                    3,
                    "New issue",
                    "x" * 900,
                    upd=T0.replace(hour=14),
                    closed=datetime(2026, 9, 1),
                ),
            ],
            "pr": [_item("pr", 7, "A PR", "diff", upd=T0.replace(hour=15))],
        }
    )
    repo, emb, state = _Repo(stored, full), _Emb(), _State()
    result = wk.TrackerEmbedResult()
    empty = _Upstream({})  # the restricted namespace's own source: nothing in its window here
    await wk._run(
        result,
        now=T0,
        repo=repo,
        source_factory=lambda org, ns: up if ns == NS else empty,
        embedder=emb,
        state=state,
    )
    assert result.namespaces == 2 and result.errors == []  # every configured namespace
    assert result.items_seen == 4 and result.rows_skipped == 1 and result.rows_embedded == 3
    assert result.rows_tombstoned == 1 and result.tokens_estimate > 0
    written = {(r["kind"], r["number"]): r for r in repo.rows if r["tombstoned_at"] is None}
    assert set(written) == {("issue", 2), ("issue", 3), ("pr", 7)}
    # the body head is what the digest and the vector are computed over
    three = written[("issue", 3)]
    assert three["text_sha256"] == wk.text_sha256(
        wk.embed_text("New issue", "x" * 900, body_chars=600)
    )
    assert (
        three["is_open"] == 0
        and three["embedding_dim"] == 8
        and three["embedding_model"] == "fake-ngram-8"
    )
    assert three["component"] == "MergeTree" and three["created_at"] == datetime(2026, 8, 1)
    assert written[("pr", 7)]["is_open"] == 1 and written[("issue", 2)]["source_id"] == f"{REPO}#2"
    # batches of `tracker_embed_batch` (2): issues [2, 3] together, the PR alone
    assert [len(b) for b in emb.batches] == [2, 1]
    # the departed row is re-inserted whole with the stamp, its vector carried forward
    tomb = [r for r in repo.rows if r["tombstoned_at"] is not None]
    assert len(tomb) == 1 and tomb[0]["number"] == 9 and tomb[0]["embedding"] == [0.2] * 8
    assert tomb[0]["tombstoned_at"] == T0 and tomb[0]["component"] == "gone"
    assert "title" not in tomb[0] and all("title" not in r for r in repo.rows)
    # the cursor per kind is the newest updated_at embedded
    assert state.cursor == {(NS, "issue"): T0.replace(hour=14), (NS, "pr"): T0.replace(hour=15)}
    assert [c[1]["since"] for c in up.calls if c[0] == "items"][:2] == [None, None]
    assert result.outcome()["items_planned"] == 4 and result.outcome()["rows_written"] == 4
    # a second pass from the cursor reads nothing new and writes nothing
    result2 = wk.TrackerEmbedResult()
    # the table after pass one: the rows it wrote plus the unchanged row it kept (issue 1) —
    # a window item WITHOUT a live row would be read by number and embedded
    repo2 = _Repo(
        {
            ("issue", 1): stored[("issue", 1)],
            **{
                k: {"text_sha256": r["text_sha256"], "tombstoned_at": None, **_MODEL}
                for k, r in written.items()
            },
        }
    )
    await wk._run(
        result2,
        now=T0,
        repo=repo2,
        source_factory=lambda org, ns: up if ns == NS else empty,
        embedder=emb,
        state=state,
    )
    assert result2.items_seen == 0 and repo2.rows == [] and result2.rows_tombstoned == 0
    assert result2.rows_missing == 0
    assert [c[1]["since"] for c in up.calls if c[0] == "items"][-2:] == [
        T0.replace(hour=14),
        T0.replace(hour=15),
    ]


@pytest.mark.asyncio
async def test_a_private_row_lands_only_under_the_restricted_namespace(monkeypatch) -> None:
    """Rows are written from each namespace's OWN source — its repositories
    and its predicate (`is_private = 0` on a standard namespace; a
    restricted one reads its own repositories) — and keyed by that
    namespace: a private item never appears under a standard namespace's
    rows, and a namespace added to the source map is embedded on the next
    pass with no code change."""
    public = _Upstream({"issue": [_item("issue", 1, "public issue")]})
    private = _Upstream(
        {"issue": [{**_item("issue", 501, "private incident"), "source_id": f"{PREPO}#501"}]}
    )
    sources = {NS: public, PRIV: private}
    repo, result = _Repo(ns=""), wk.TrackerEmbedResult()
    await wk._run(
        result,
        now=T0,
        repo=repo,
        source_factory=lambda org, ns: sources[ns],
        embedder=_Emb(),
        state=_State(),
    )
    assert result.namespaces == 2 and result.errors == [] and result.rows_embedded == 2
    by_ns: dict[str, set[int]] = {}
    for r in repo.rows:
        by_ns.setdefault(r["namespace"], set()).add(int(r["number"]))
    assert by_ns == {NS: {1}, PRIV: {501}} and all(r["org"] == ORG for r in repo.rows)
    assert {r["source_id"] for r in repo.rows if r["namespace"] == NS} == {f"{REPO}#1"}

    # the statements: the standard namespace's carry the privacy predicate and its own
    # repositories; the restricted one's name only ITS repositories
    async def _restricted(org, ns):
        return True

    monkeypatch.setattr("server.identity.app.namespace_classification.is_restricted", _restricted)
    up = _Rows([])
    monkeypatch.setattr(qi, "query_rows", up)
    for ns, repos, priv in ((NS, [REPO], True), (PRIV, [PREPO], False)):
        for kind in ("issue", "pr"):
            await src.TrackerSource(ORG, ns).embed_items(
                ORG, ns, kind=kind, since=None, closed_days=365, body_chars=600
            )
            await src.TrackerSource(ORG, ns).embed_window_keys(ORG, ns, kind=kind, closed_days=365)
            for sql, params in up.calls[-2:]:
                assert ("is_private = 0 AND" in sql) is priv, (ns, kind, sql)
                assert params["repos"] == repos
    assert wk.due_scope() == [(ORG, NS), (ORG, PRIV)]


@pytest.mark.asyncio
async def test_a_page_boundary_inside_one_updated_at_second_loses_no_row(monkeypatch) -> None:
    """Three items share one `updated_at` second (a bulk label sweep) and the
    page holds two: the cursor is (updated_at, source_id), so the second page
    reads the third item instead of `updated_at > T0` skipping it. The three
    are stored live with a stale digest — the missing-items read never sees
    them, so the cursor walk is the only path that re-embeds them."""
    monkeypatch.setattr(wk, "PAGE", 2)
    stale = {"text_sha256": "0" * 64, "tombstoned_at": None, **_MODEL}
    stored = {("issue", n): dict(stale) for n in (1, 2, 3)}
    up = _Upstream({"issue": [_item("issue", n, f"edited {n}") for n in (1, 2, 3)]})
    repo, state = _Repo(stored), _State()
    result = wk.TrackerEmbedResult()
    await wk.embed_namespace(up, repo, _Emb(), ORG, NS, result=result, now=T0, state=state)
    assert result.errors == [] and result.rows_missing == 0
    assert sorted(r["number"] for r in repo.rows if r["kind"] == "issue") == [1, 2, 3]
    assert state.cursor[(NS, "issue")] == T0 and state.ids[(NS, "issue")] == f"{REPO}#3"


@pytest.mark.asyncio
async def test_the_cursor_round_trips_both_parts_and_reads_a_timestamp_only_value() -> None:
    """The stored value is `<updated_at>|<source_id>`; a value written before
    the id was stored reads as (ts, '') so the first resumed page re-reads
    that second (a digest skip) rather than skipping its remainder."""

    class _Code:
        def __init__(self) -> None:
            self.v: dict[str, str] = {}

        async def get_state(self, org, ns, key):
            return self.v.get(key, "")

        async def set_state(self, org, ns, key, value, detail=""):
            self.v[key] = value

    code = _Code()
    st = ei.cursor_state(code)
    assert await st.get_cursor(ORG, NS, "issue") == (None, "")
    code.v[f"{ei.CURSOR_KEY}:issue"] = "2026-09-16 12:00:00"  # the legacy value
    assert await st.get_cursor(ORG, NS, "issue") == (T0, "")
    await st.set(ORG, NS, "issue", T0, f"{REPO}#3")
    assert code.v[f"{ei.CURSOR_KEY}:issue"] == f"2026-09-16 12:00:00|{REPO}#3"
    assert await st.get_cursor(ORG, NS, "issue") == (T0, f"{REPO}#3")
    assert await st.get(ORG, NS, "issue") == T0  # the catch-up's timestamp read
    await st.clear(ORG, NS, "issue")
    assert await st.get_cursor(ORG, NS, "issue") == (None, "")


@pytest.mark.asyncio
async def test_pass_notes_a_namespace_outage_and_the_worker_is_registered(
    monkeypatch, caplog
) -> None:
    class _Down:
        async def upstream_embeddings_populated(self, org, ns):
            raise TrackerUnavailable("credential unset")

    result = wk.TrackerEmbedResult()
    await wk._run(
        result,
        now=T0,
        repo=_Repo(),
        source_factory=lambda org, ns: _Down(),
        embedder=_Emb(),
        state=_State(),
    )
    assert result.namespaces == 0 and len(result.errors) == 2  # one per configured namespace
    assert all("tracker_unavailable" in e for e in result.errors)
    # the canonical alternative landed: QA's own column is populated → the pass is a no-op
    # there, said once per process and namespace, never per pass
    up = _Upstream({"issue": [_item("issue", 1, "x")]})
    up.populated = True
    repo, result = _Repo(), wk.TrackerEmbedResult()
    wk._UPSTREAM_LOGGED.clear()
    with caplog.at_level(logging.INFO, logger="loom.workers.tracker_embed"):
        for _ in range(2):
            await wk._run(
                result,
                now=T0,
                repo=repo,
                source_factory=lambda org, ns: up,
                embedder=_Emb(),
                state=_State(),
            )
    assert result.skipped_upstream == 4 and result.namespaces == 0 and repo.rows == []
    assert [c[0] for c in up.calls] == ["upstream"] * 4 and result.outcome()[
        "skipped_upstream"
    ] == 4
    said = [
        r.message for r in caplog.records if "upstream embedding column is populated" in r.message
    ]
    assert len(said) == 2 and sorted(NS in m for m in said) == [False, True]  # once per namespace
    from server.workers.registry import enabled_workers, worker_meta

    monkeypatch.setattr(settings, "tracker_embed_enabled", True, raising=False)
    spec = {w.name: w for w in enabled_workers()}["tracker_embed"]
    assert spec.interval_seconds == settings.tracker_embed_interval_seconds == 1800.0
    assert spec.deadline_seconds() == 900.0 and spec.first_pass_hook() is not None
    assert [t.table for t in spec.owned_tables] == ["loom.qa_item_embeddings"]
    assert spec.owned_tables[0].namespace_column == "namespace"
    assert spec.due_scope() == [(ORG, NS), (ORG, PRIV)]  # every configured namespace, its own rows
    assert worker_meta()["tracker_embed"]["enabled"] is True
    monkeypatch.setattr(settings, "tracker_embed_enabled", False, raising=False)
    assert "tracker_embed" not in {w.name for w in enabled_workers()}
    # the census: reference data, no memory content, namespace-scoped
    assert "namespace-scoped" in erasure_census.NO_MEMORY_CONTENT["loom.qa_item_embeddings"]
    assert erasure_census.classified_tables()["loom.qa_item_embeddings"] == "no_memory_content"


def test_embed_text_is_the_title_and_the_body_head() -> None:
    assert wk.embed_text(" T ", "b" * 10, body_chars=4) == "T\nbbbb"
    assert wk.embed_text("T", "", body_chars=600) == "T\n"
    assert wk.text_sha256("T\n") == wk.text_sha256("T\n") and len(wk.text_sha256("x")) == 64


# ---------------------------------------------------------------- source statements


class _Rows:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def __call__(
        self, sql, params, *, credential=None, max_execution_s=None, query_settings=None
    ):
        self.calls.append((sql, params))
        return list(self.rows)


@pytest.mark.asyncio
async def test_upstream_statements_carry_the_predicate_the_window_and_the_cursor(
    monkeypatch,
) -> None:
    up = _Rows(
        [
            {
                "source_id": f"{REPO}#5",
                "title": "t",
                "body": "b",
                "primary_component": "c",
                "created_at": datetime(2026, 8, 1),
                "updated_at": T0,
                "closed_at": datetime(1970, 1, 1),
            }
        ]
    )
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, NS)
    rows = await s.embed_items(
        ORG, NS, kind="issue", since=T0, closed_days=365, body_chars=600, limit=2000, stamp="p1"
    )
    sql, params = up.calls[-1]
    q = _norm(sql)
    assert (
        "FROM qa_intelligence.github_issues FINAL WHERE is_private = 0 AND splitByChar('#', source_id)[1] IN {repos:Array(String)}"
        in q
    )
    assert (
        "(closed_at <= toDateTime64(0, 3) OR closed_at >= now64(3) - INTERVAL {days:UInt32} DAY)"
        in q
    )
    assert "updated_at > {since:DateTime64(3)}" in q and params["since"] == T0
    assert "substring(body, 1, {chars:UInt32}) AS body" in q and params["chars"] == 600
    assert (
        "ORDER BY updated_at ASC, source_id ASC LIMIT {limit:UInt32}" in q
        and params["limit"] == 2000
    )
    assert rows[0]["number"] == 5 and rows[0]["closed_at"] is None and rows[0]["kind"] == "issue"
    # the pass's keyset: resumes after the last (updated_at, source_id) read
    await s.embed_items(
        ORG, NS, kind="issue", since=T0, since_id=f"{REPO}#5", closed_days=365, body_chars=600
    )
    sql, params = up.calls[-1]
    assert "AND (updated_at, source_id) > ({since:DateTime64(3)}, {since_id:String})" in _norm(sql)
    assert params["since"] == T0 and params["since_id"] == f"{REPO}#5"
    await s.embed_items(ORG, NS, kind="pr", since=None, closed_days=30, body_chars=100, stamp="p1")
    sql, params = up.calls[-1]
    assert "github_prs FINAL" in sql and "updated_at >" not in sql and "since" not in params
    await s.embed_window_keys(ORG, NS, kind="pr", closed_days=365, stamp="p1")
    sql, _ = up.calls[-1]
    assert (
        "SELECT source_id, updated_at FROM qa_intelligence.github_prs FINAL WHERE is_private = 0"
        in _norm(sql)
    )
    assert "body" not in sql
    assert (
        await s.embed_items(ORG, NS, kind="incident", since=None, closed_days=1, body_chars=1) == []
    )
    # the upstream check: NON-ZERO vectors among the 200 newest (never the length — every row
    # carries a 2,560-dim vector), and "populated" is at least half of them, not a straggler
    up.rows = [{"nonzero": 0, "n": 200}]
    assert await s.upstream_embeddings_populated(ORG, NS) is False
    q = _norm(up.calls[-1][0])
    assert "countIf(arraySum(arrayMap(x -> abs(x), embedding)) > 0) AS nonzero, count() AS n" in q
    assert (
        "WHERE is_private = 0 AND splitByChar('#', source_id)[1] IN {repos:Array(String)} ORDER BY created_at DESC LIMIT 200"
        in q
    )
    src.reset_cache_for_tests()
    up.rows = [{"nonzero": 2, "n": 200}]  # two stragglers
    assert await s.upstream_embeddings_populated(ORG, NS) is False
    src.reset_cache_for_tests()
    up.rows = [{"nonzero": 100, "n": 200}]
    assert await s.upstream_embeddings_populated(ORG, NS) is True
    src.reset_cache_for_tests()
    up.rows = [{"nonzero": 0, "n": 0}]
    assert await s.upstream_embeddings_populated(ORG, NS) is False

    # the leg's read passes the gate and returns keys + distance from loom's table, nothing
    # hydrated (the hits that reach the answer are read by key after fusion, `_hydrate`)
    class _R:
        async def nearest(self, org, ns, **kw):
            self.kw = kw
            return [
                {
                    "kind": "pr",
                    "number": 119582,
                    "source_id": f"{REPO}#119582",
                    "component": "",
                    "is_open": False,
                    "created_at": datetime(2026, 9, 1),
                    "distance": 0.3,
                },
                {
                    "kind": "issue",
                    "number": 116490,
                    "source_id": f"{REPO}#116490",
                    "component": "",
                    "is_open": True,
                    "created_at": datetime(2026, 8, 1),
                    "distance": 0.1,
                },
                {
                    "kind": "issue",
                    "number": 4,
                    "source_id": f"{REPO}#4",
                    "component": "",
                    "is_open": True,
                    "created_at": datetime(2026, 8, 1),
                    "distance": 0.2,
                },
            ]

    up.rows = [
        {
            "kind": "pr",
            "source_id": f"{REPO}#119582",
            "url": "u/119582",
            "title": "the fix",
            "created_at": datetime(2026, 9, 1),
            "closed_at": datetime(2026, 9, 2),
            "primary_component": "",
            "state_reason": "",
            "affected_versions": [],
            "error_code": 0,
            "is_merged": 1,
        },
        {
            "kind": "issue",
            "source_id": f"{REPO}#116490",
            "url": "u/116490",
            "title": "columns.txt",
            "created_at": datetime(2026, 8, 1),
            "closed_at": None,
            "primary_component": "MergeTree",
            "state_reason": "",
            "affected_versions": [],
            "error_code": 0,
            "is_merged": 0,
        },
    ]
    r = _R()
    n_calls = len(up.calls)
    out = await s.similar_vector_local(
        ORG,
        NS,
        vector=[0.1] * 8,
        dim=8,
        kinds=["issue", "pr"],
        before=T0,
        exclude=("issue", 119601),
        repo=r,
    )
    assert (
        r.kw["before"] == T0
        and r.kw["exclude"] == ("issue", 119601)
        and r.kw["kinds"] == ["issue", "pr"]
    )
    assert (
        r.kw["limit"] == 50 and len(out) == 3 and "title" not in out[0] and len(up.calls) == n_calls
    )
    # the hydration: one keyed read for the rows without a title, titles and state the
    # upstream's, live; a row the upstream no longer serves drops out; scores and `why` ride along
    fused = [{**o, "score": 0.01 * (3 - i), "why": ["vector"]} for i, o in enumerate(out)]
    fused.insert(
        0,
        {
            "kind": "issue",
            "number": 116481,
            "source_id": f"{REPO}#116481",
            "title": "already hydrated by the lexical leg",
            "score": 0.05,
            "why": ["lexical"],
        },
    )
    hydrated = await tsim._hydrate(
        s, TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601), fused
    )
    sql, params = up.calls[-1]
    assert params["refs"] == [
        f"{REPO}#4",
        f"{REPO}#116490",
        f"{REPO}#119582",
    ]  # the three without a title
    assert "exclude" not in params and "before" not in params and sql.count("UNION ALL") == 1
    assert [(o["kind"], o["number"]) for o in hydrated] == [
        ("issue", 116481),
        ("pr", 119582),
        ("issue", 116490),
    ]
    assert (
        hydrated[1]["title"] == "the fix"
        and hydrated[1]["state"] == "merged"
        and hydrated[1]["url"] == "u/119582"
    )
    assert hydrated[2]["title"] == "columns.txt" and hydrated[2]["state"] == "open"
    assert hydrated[2]["primary_component"] == "MergeTree" and hydrated[2]["distance"] == 0.1
    assert [o["score"] for o in hydrated] == [0.05, 0.03, 0.02] and hydrated[1]["why"] == ["vector"]
    assert hydrated[0]["title"] == "already hydrated by the lexical leg"
    n_calls = len(up.calls)
    assert (
        await tsim._hydrate(
            s, TrackerSimilarInput(org=ORG, namespace=NS, issue_number=1), fused[:1]
        )
        == fused[:1]
    )
    assert len(up.calls) == n_calls  # nothing to hydrate: no read
    # the mentions leg keeps its cap of ten; the vector leg asks for its hits' count
    assert (
        await s.similar_mentions(ORG, NS, numbers=list(range(1, 30)), kinds=["issue"], before=None)
    ) is not None
    assert len(up.calls[-1][1]["refs"]) == 10
    # an unconfigured namespace is refused before any read
    with pytest.raises(TrackerUnavailable):
        await src.TrackerSource(ORG, "tracker-nope").similar_vector_local(
            ORG,
            "tracker-nope",
            vector=[0.1],
            dim=1,
            kinds=["issue"],
            before=None,
            exclude=None,
            repo=r,
        )


# ---------------------------------------------------------------- the leg on the handler


def _cand(n: int, title: str, **over: Any) -> dict[str, Any]:
    return {
        "kind": "issue",
        "repo": REPO,
        "number": n,
        "source_id": f"{REPO}#{n}",
        "title": title,
        "state": "open",
        "state_reason": "",
        "primary_component": "MergeTree",
        "affected_versions": [],
        "error_code": 0,
        "created_at": datetime(2026, 8, 1),
        "url": f"u/{n}",
        **over,
    }


class _Src:
    def __init__(self, *, item, lexical, vector, edges=(), fixes=(), stored_vec=None):
        self.item, self.lexical, self.vector = item, lexical, vector
        self.edges, self.fixes, self.stored_vec = list(edges), list(fixes), stored_vec
        self.cfg = type("C", (), {"restricted": False})()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def repos(self):
        return [REPO]

    async def similar_item(self, org, ns, number):
        return self.item

    async def token_stats(self, org, ns):
        return {
            "n": 20000,
            "avg_title_len": 10.0,
            "avg_body_len": 540.0,
            "df": {"substreams": 5, "columns": 800, "txt": 300, "missing": 900, "metadata": 400},
        }

    async def similar_lexical(self, org, ns, *, kind, terms, idf, before, exclude, **kw):
        return list(self.lexical) if kind == "issue" else []

    async def similar_frames(self, *a, **k):
        return []

    fresh_items: list[dict[str, Any]] = []
    populated = False

    async def similar_mentions(
        self, org, ns, *, numbers, kinds, before, exclude="", max_numbers=10
    ):
        # the hydration read of the fresh candidates (the table holds no text)
        self.calls.append(("mentions", {"numbers": list(numbers), "before": before}))
        known = {int(it["number"]): it for it in self.fresh_items}
        return [_cand(n, f"fresh #{n}", kind=known[n]["kind"]) for n in numbers if n in known]

    async def upstream_embeddings_populated(self, org, ns):
        return self.populated

    async def similar_vector_local(
        self, org, ns, *, vector, dim, kinds, before, exclude, limit=50, repo=None
    ):
        self.calls.append(
            (
                "vector_local",
                {
                    "dim": dim,
                    "kinds": kinds,
                    "before": before,
                    "exclude": exclude,
                    "vector": vector,
                },
            )
        )
        return list(self.vector)

    async def embed_items(self, org, ns, *, kind, since, closed_days, body_chars, limit, stamp=""):
        self.calls.append(("embed_items", {"kind": kind, "since": since, "limit": limit}))
        return [it for it in self.fresh_items if it["kind"] == kind and it["updated_at"] > since]

    async def duplicates_of(self, org, ns, *, number, limit=20):
        return list(self.edges)

    async def fix_prs_of_issue(self, org, ns, number):
        return [{"pr_number": n} for n in self.fixes]

    async def similar_vector(self, org, ns, *, number, before, limit=50):
        self.calls.append(("vector_upstream", {"number": number, "before": before}))
        return [_cand(777, "from QA's own column", distance=0.05)]


class _LegRepo:
    def __init__(self, live: int, stored_vec=None) -> None:
        self.live, self.stored_vec = live, stored_vec
        self.digests: dict[tuple[str, int], tuple[str, str]] = {}
        self.inserted: list[dict[str, Any]] = []

    async def stored_vector(self, org, ns, kind, number):
        return self.stored_vec

    async def namespace_stats(self, org, ns):
        return {"live": self.live, "dead": 0, "last_updated": None, "dims": [8]}

    async def stored_digests(self, org, ns, kind, numbers):
        return {k: v for k, v in self.digests.items() if k[0] == kind and k[1] in set(numbers)}

    async def insert_rows(self, rows):
        for r in rows:
            assert set(r) == set(qr.TRACKER_EMBED_COLS)
            self.digests[(r["kind"], r["number"])] = (r["text_sha256"], r["embedding_model"])
        self.inserted.extend(rows)


class _Cursors:
    def __init__(self, cursors: dict[str, datetime] | None = None) -> None:
        self.cursors = cursors or {}

    async def get(self, org, ns, kind):
        return self.cursors.get(kind)


@pytest.fixture
def leg(monkeypatch):
    holder: dict[str, Any] = {"repo": _LegRepo(live=100), "emb": _Emb(), "cursors": _Cursors()}
    monkeypatch.setattr(tsim, "_embed_repo", lambda: holder["repo"])
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: holder["emb"])
    monkeypatch.setattr(
        "server.tracker.app.embed_index.cursor_state", lambda code_repo=None: holder["cursors"]
    )
    return holder


ITEM = {
    "kind": "issue",
    "source_id": f"{REPO}#119601",
    "title": "columns_substreams.txt missing after ATTACH",
    "body": "Code: 233\nsee the trace",
    "component": "MergeTree",
    "versions": [],
    "error_code": 233,
    "created_at": datetime(2026, 9, 10, 8, 0, 0),
    "has_embedding": False,
}


@pytest.mark.asyncio
async def test_vector_leg_serves_fuses_with_lexical_and_names_the_recorded_relation(
    leg, monkeypatch
) -> None:
    s = _Src(
        item=ITEM,
        lexical=[
            _cand(116490, "columns.txt missing after ATTACH", bm25=9.0),
            _cand(100000, "unrelated words", bm25=4.0),
        ],
        vector=[
            _cand(116481, "metadata_version.txt missing", distance=0.10),
            _cand(116490, "columns.txt missing after ATTACH", distance=0.12),
        ],
        edges=[
            {"source_id": f"{REPO}#116490", "rel_type": "related_to", "direction": "out"},
            {"source_id": f"{REPO}#116481", "rel_type": "related_to", "direction": "out"},
            {"source_id": f"{REPO}#116481", "rel_type": "duplicate_of", "direction": "in"},
        ],
        fixes=[119582],
    )
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(
            org=ORG, namespace=NS, issue_number=119601, legs=["lexical", "fields", "vector"]
        )
    )
    assert out.legs == {
        "fields": "served",
        "lexical": "served",
        "frames": "skipped",
        "mentions": "skipped",
        "vector": "served",
    }
    assert out.query["record"] == "served"
    vl = next(c for c in s.calls if c[0] == "vector_local")[1]
    # the query vector: no stored row → the item's text embedded once; the wall and the exclusion
    assert vl["dim"] == 8 and vl["before"] == datetime(2026, 9, 10, 8, 0, 0)
    assert vl["exclude"] == ("issue", 119601) and vl["kinds"] == ["issue", "pr"]
    # recall-only fusion: the lexical order stands (116490, 100000); 116490, found by both,
    # keeps its rank, gains `vector` and the agreement boost; 116481, found by the vector leg alone, is APPENDED
    # after every ranked candidate — the leg adds, it never re-ranks or displaces
    assert [i["number"] for i in out.items] == [116490, 100000, 116481]
    top = out.items[0]
    assert (
        top["why"] == ["lexical", "component", "vector", "related_to"]
        and top["relation"] == "related_to"
    )
    assert out.items[1]["relation"] == "none" and out.items[1]["why"] == ["lexical", "component"]
    assert (
        out.items[2]["why"] == ["vector", "duplicate_of"]
        and out.items[2]["relation"] == "duplicate_of"
    )
    assert out.items[0]["score"] > out.items[1]["score"] > out.items[2]["score"] > 0
    # rank 1 of one ranked list, plus the agreement boost: the vector leg found it at rank 2
    assert round(out.items[0]["score"], 5) == round(1 / 61 + 1 / 62, 5)
    fused = sim.fuse(
        {
            "lexical": sim.rank_lexical(s.lexical, sim.Query(component="MergeTree"), fields=True),
            "vector": [{**r, "why": ["vector"]} for r in s.vector],
        }
    )
    assert [f["number"] for f in fused] == [116490, 100000, 116481]
    assert fused[2]["score"] == (1 / 62) / 2  # below the lowest fused score, in the leg's order
    assert out.items[0]["distance"] == 0.12 and out.items[1]["distance"] is None
    assert out.items[2]["distance"] == 0.1
    # top_k 2: the vector-only candidate stays after the ranked ones
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(
            org=ORG,
            namespace=NS,
            issue_number=119601,
            legs=["lexical", "fields", "vector"],
            top_k=2,
        )
    )
    assert [i["number"] for i in out.items] == [116490, 100000]
    # a stored row for the item: the point read replaces the embedding call
    leg["repo"] = _LegRepo(
        live=100,
        stored_vec={"embedding": [0.9] * 8, "embedding_dim": 8, "embedding_model": "fake-ngram-8"},
    )
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert s.calls[-1][1]["vector"] == [0.9] * 8 and out.legs["vector"] == "served"
    assert out.legs["lexical"] == "skipped" and [i["number"] for i in out.items] == [116481, 116490]
    # a text query: embedded, no wall, no exclusion, no record to read relations from
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, text=ITEM["title"], legs=["vector"])
    )
    vl = s.calls[-1][1]
    assert vl["before"] is None and vl["exclude"] is None and out.query["record"] == "skipped"
    assert all(i["relation"] == "none" for i in out.items)


def test_fuse_is_recall_only_for_the_vector_leg() -> None:
    """The fusion rule, on the pure function: a candidate the
    vector leg shares with a ranked leg keeps its fused rank and gains
    `vector` in `why`; one only the vector leg found is appended after
    every ranked candidate in the leg's own order, scored strictly below
    the lowest fused score; the ranked legs' RRF is untouched."""

    def row(n, **k):
        return {"source_id": f"r#{n}", "number": n, **k}

    lists = {
        "lexical": [row(1, why=["lexical", "component"]), row(2, why=["lexical"])],
        "mentions": [row(3, why=["mentions"]), row(1, why=["mentions"])],
        "vector": [
            row(9, why=["vector"]),
            row(2, why=["vector"]),
            row(8, why=["vector"]),
            row(1, why=["vector"]),
            row(9, why=["vector"]),
        ],
    }
    out = sim.fuse(lists)
    # RRF orders the ranked legs (1: two lists; 3: rank 1 once; 2: rank 2 once); 1 and 2
    # keep their rank whatever the vector leg said; 9 and 8 are appended in the leg's order
    assert [r["number"] for r in out] == [1, 3, 2, 9, 8]
    assert [r["number"] for r in sim.rrf({k: v for k, v in lists.items() if k != "vector"})] == [
        1,
        3,
        2,
    ]
    assert out[0]["why"] == ["lexical", "component", "mentions", "vector"]
    assert out[1]["why"] == ["mentions"] and out[2]["why"] == ["lexical", "vector"]
    assert out[3]["why"] == ["vector"] and out[4]["why"] == ["vector"]
    scores = [r["score"] for r in out]
    assert (
        scores == sorted(scores, reverse=True) and scores[3] < scores[2] and scores[4] < scores[3]
    )
    assert out[3]["score"] == out[2]["score"] / 2 and out[4]["score"] == out[2]["score"] / 4
    # only the vector leg: its own order, positive scores
    alone = sim.fuse({"vector": lists["vector"]})
    assert [r["number"] for r in alone] == [9, 2, 8, 1] and alone[0]["score"] > alone[-1][
        "score"
    ] > 0
    assert sim.fuse({}) == [] and sim.RECALL_ONLY_LEGS == ("vector",)


def test_fuse_agreement_boost_lifts_a_candidate_both_legs_found() -> None:
    """Under `agreement` a candidate a ranked leg AND the vector
    leg found gains the vector leg's RRF contribution (once) and the ranked
    set is re-sorted; vector-only candidates stay appended; `agreement=False`
    is byte-for-byte the recall-only rule."""

    def row(n, **k):
        return {"source_id": f"r#{n}", "number": n, **k}

    # the 120416 → 117420 shape: lexical ranks the target 4th, vector 1st
    lexical = [row(1), row(2), row(3), row(4, why=["lexical", "related_to"])]
    vector = [
        row(4, distance=0.21),
        row(1, distance=0.30),
        row(9, distance=0.5),
        row(4, distance=0.21),
    ]
    off = sim.fuse({"lexical": lexical, "vector": vector})
    assert [r["number"] for r in off] == [1, 2, 3, 4, 9]
    on = sim.fuse({"lexical": lexical, "vector": vector}, agreement=True)
    # 1 is in both legs too (ranks 1 and 2) and keeps the top; 4 rises from 4th to 2nd
    assert [r["number"] for r in on] == [1, 4, 2, 3, 9]
    k = sim.RRF_K
    assert on[0]["score"] == 1 / (k + 1) + 1 / (k + 2)
    assert on[1]["score"] == 1 / (k + 4) + 1 / (
        k + 1
    )  # boosted once, not twice for the duplicate row
    assert on[2]["score"] == 1 / (k + 2) and on[3]["score"] == 1 / (k + 3)
    assert on[1]["why"] == ["lexical", "related_to", "vector"] and on[1]["distance"] == 0.21
    # 9 is vector-only: still appended below every ranked candidate, never boosted into the set
    assert on[4]["why"] == ["vector"] and on[4]["score"] < on[3]["score"]
    # scores stay monotone
    scores = [r["score"] for r in on]
    assert scores == sorted(scores, reverse=True)
    # the dials: half weight halves the lift; a rank cap leaves deep vector ranks unboosted
    half = sim.fuse({"lexical": lexical, "vector": vector}, agreement=True, agreement_weight=0.5)
    assert half[1]["number"] == 4 and half[1]["score"] == 1 / (k + 4) + 0.5 / (k + 1)
    capped = sim.fuse({"lexical": lexical, "vector": vector}, agreement=True, agreement_max_rank=1)
    # 4 (vector rank 1) is boosted, 1 (vector rank 2) is not — so 4 now leads
    assert [r["number"] for r in capped] == [4, 1, 2, 3, 9]
    assert capped[0]["score"] == 1 / (k + 4) + 1 / (k + 1) and capped[1]["score"] == 1 / (k + 1)
    # no shared candidate → the flag changes nothing
    assert sim.fuse({"lexical": lexical[:2], "vector": [row(9)]}, agreement=True) == sim.fuse(
        {"lexical": lexical[:2], "vector": [row(9)]}
    )


@pytest.mark.asyncio
async def test_vector_leg_catches_up_on_items_changed_since_the_cursor(leg, monkeypatch) -> None:
    """Three items changed upstream after the pass's cursor —
    the leg embeds them in one batched call, ranks them beside the table's
    hits, writes their rows; the second call finds their digests stored and
    embeds nothing. A kind without a cursor is left to the pass."""
    s = _Src(item=ITEM, lexical=[], vector=[_cand(116490, "columns.txt", distance=0.30)])
    s.fresh_items = [
        _item(
            "issue",
            120001,
            "columns_substreams.txt missing after ATTACH (fresh)",
            upd=T0.replace(hour=13),
        ),
        _item("issue", 120002, "unrelated fresh issue", upd=T0.replace(hour=14)),
        _item("issue", 119601, "the query item itself, just edited", upd=T0.replace(hour=15)),
        _item("pr", 120003, "a fresh PR", upd=T0.replace(hour=13)),
        _item("issue", 100, "changed long ago", upd=T0.replace(hour=1)),  # before the cursor
    ]
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    leg["cursors"] = _Cursors({"issue": T0.replace(hour=12)})  # the PR pass never ran: no cursor
    emb = leg["emb"]
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.legs["vector"] == "served" and out.query["fresh_items"] == 3
    assert out.query["fresh_left_to_pass"] == 0
    # one upstream read (issues since the cursor; PRs have no cursor), one batched embed of the three
    assert [c[1] for c in s.calls if c[0] == "embed_items"] == [
        {"kind": "issue", "since": T0.replace(hour=12), "limit": 200}
    ]
    assert sorted(len(b) for b in emb.batches) == [1, 3]  # the query text; the three in ONE call
    # the rows are written whole; the query item itself was embedded (its text changed) but is not a candidate
    assert sorted((r["kind"], r["number"]) for r in leg["repo"].inserted) == [
        ("issue", 119601),
        ("issue", 120001),
        ("issue", 120002),
    ]
    # the fresh candidates are ranked beside the table's hit and hydrated by key after fusion
    assert {i["number"] for i in out.items} == {116490, 120001, 120002}
    assert all(i["why"] == ["vector"] for i in out.items)
    fresh = {i["number"]: i for i in out.items if i["number"] != 116490}
    assert fresh[120001]["title"] == "fresh #120001" and fresh[120002]["title"] == "fresh #120002"
    hyd = [c for c in s.calls if c[0] == "mentions"]
    assert (
        len(hyd) == 1
        and sorted(hyd[0][1]["numbers"]) == [120001, 120002]
        and hyd[0][1]["before"] is None
    )
    # the second call: the digests are stored → nothing embedded, nothing written, the same candidates
    n_ins, n_batches = len(leg["repo"].inserted), len(emb.batches)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.query["fresh_items"] == 0 and len(leg["repo"].inserted) == n_ins
    assert len(emb.batches) == n_batches + 1  # the query text only (no stored vector for the item)
    assert {i["number"] for i in out.items} == {
        116490
    }  # the fresh rows now live in the table (faked away here)
    # more than the cap changed: the newest 50 are embedded now, the rest is said to be left to the pass
    s.fresh_items = [
        _item("issue", 200000 + i, f"fresh {i}", upd=T0.replace(hour=13, minute=i % 60))
        for i in range(70)
    ]
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.query["fresh_items"] == 50 and out.query["fresh_left_to_pass"] == 20
    # no cursor at all → no catch-up read
    leg["cursors"] = _Cursors()
    s.calls.clear()
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.query["fresh_items"] == 0 and not [c for c in s.calls if c[0] == "embed_items"]

    # an upstream outage on the catch-up alone: the table's answer stands
    class _Down(_Src):
        async def embed_items(self, *a, **k):
            raise TrackerUnavailable("breaker OPEN")

    d = _Down(item=ITEM, lexical=[], vector=[_cand(116490, "columns.txt", distance=0.30)])
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: d)
    leg["cursors"] = _Cursors({"issue": T0})
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert (
        out.legs["vector"] == "served"
        and out.query["fresh_items"] == 0
        and [i["number"] for i in out.items] == [116490]
    )


@pytest.mark.asyncio
async def test_a_catch_up_past_its_budget_answers_from_the_table_and_lands_in_the_background(
    leg, monkeypatch
) -> None:
    """The catch-up runs beside the scan under
    `tracker_similar_catch_up_budget_ms`; past it the call answers from the
    table (`fresh_pending`), the embedding finishes in the background and
    the next call finds the rows stored — the interval never bounds what a
    query can find, only what one call waits for."""
    import asyncio

    class _Slow(_Src):
        async def embed_items(self, *a, **k):
            await asyncio.sleep(0.05)
            return await super().embed_items(*a, **k)

    s = _Slow(item=ITEM, lexical=[], vector=[_cand(116490, "columns.txt", distance=0.30)])
    s.fresh_items = [_item("issue", 120001, "fresh", upd=T0.replace(hour=13))]
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    monkeypatch.setattr(settings, "tracker_similar_catch_up_budget_ms", 0, raising=False)
    leg["cursors"] = _Cursors({"issue": T0})
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.legs["vector"] == "served" and [i["number"] for i in out.items] == [116490]
    assert out.query["fresh_pending"] is True and out.query["fresh_items"] == 0
    assert len(tsim._BACKGROUND) == 1
    await asyncio.gather(*tsim._BACKGROUND)
    assert not tsim._BACKGROUND and [r["number"] for r in leg["repo"].inserted] == [120001]
    # with the budget: waited for, the fresh row is in the answer
    monkeypatch.setattr(settings, "tracker_similar_catch_up_budget_ms", 5000, raising=False)
    leg["repo"].digests.clear()
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.query["fresh_items"] == 1 and "fresh_pending" not in out.query
    assert {i["number"] for i in out.items} == {116490, 120001}


def test_catch_up_helpers_rank_under_the_wall_and_the_dim_guard() -> None:
    assert ei.cosine_distance([1.0, 0.0], [1.0, 0.0]) == 0.0
    assert round(ei.cosine_distance([1.0, 0.0], [0.0, 1.0]), 6) == 1.0
    assert ei.cosine_distance([1.0], [1.0, 2.0]) == 1.0 and ei.cosine_distance([0.0], [0.0]) == 1.0
    hits = [
        {
            "kind": "issue",
            "number": 1,
            "source_id": "r#1",
            "created_at": T0,
            "embedding": [1.0, 0.0],
        },
        {
            "kind": "issue",
            "number": 2,
            "source_id": "r#2",
            "created_at": T0.replace(hour=1),
            "embedding": [0.0, 1.0],
        },
        {
            "kind": "issue",
            "number": 3,
            "source_id": "r#3",
            "created_at": T0.replace(hour=1),
            "embedding": [1.0],
        },
        {
            "kind": "issue",
            "number": 4,
            "source_id": "r#4",
            "created_at": T0.replace(hour=1),
            "embedding": [1.0, 1.0],
        },
    ]
    out = ei.rank_fresh(hits, [1.0, 0.0], before=T0.replace(hour=6), exclude=("issue", 4))
    # #1 is at the wall (created >= before), #3 has another dim, #4 is the query item
    assert [(o["number"], round(o["distance"], 3)) for o in out] == [
        (2, 1.0)
    ] and "embedding" not in out[0]


@pytest.mark.asyncio
async def test_vector_leg_statuses_and_the_default_set(leg, monkeypatch) -> None:
    s = _Src(item=ITEM, lexical=[_cand(116490, "columns.txt", bm25=9.0)], vector=[])
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    # the default set: the flag says whether the leg is in it
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601)
    )
    assert out.legs["vector"] == "skipped" and s.calls == []
    monkeypatch.setattr(settings, "tracker_similar_vector_leg", True, raising=False)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601)
    )
    assert out.legs["vector"] == "empty" and len(s.calls) == 1  # rows exist, none qualified
    leg["repo"] = _LegRepo(live=0)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.legs["vector"] == "not_computed" and out.items == [] and not out.tracker_unavailable
    # a kinds list without issues / PRs never reads the table
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(
            org=ORG, namespace=NS, issue_number=119601, legs=["vector"], kinds=["incident"]
        )
    )
    assert out.legs["vector"] == "skipped"
    # the canonical alternative: the NAMESPACE's upstream column is populated and the item
    # carries a vector → QA's own column, the upstream reader, loom's table never read
    s.item = {**ITEM, "has_embedding": True}
    s.populated = True
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert out.legs["vector"] == "served" and [i["number"] for i in out.items] == [777]
    assert s.calls[-1] == ("vector_upstream", {"number": 119601, "before": ITEM["created_at"]})
    # a straggler: the item carries a vector but the column is not being filled → loom's table
    s.populated = False
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    )
    assert s.calls[-1][0] == "vector_local" and out.legs["vector"] == "not_computed"  # live=0 above
    s.item = ITEM

    # the record read failing is named, the legs still serve
    class _Down(_Src):
        async def duplicates_of(self, *a, **k):
            raise TrackerUnavailable("breaker OPEN")

    d = _Down(item=ITEM, lexical=[_cand(116490, "columns.txt", bm25=9.0)], vector=[])
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: d)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["lexical"])
    )
    assert (
        out.query["record"] == "tracker_unavailable" and out.items and not out.tracker_unavailable
    )
    assert out.items[0]["relation"] == "none"


@pytest.mark.asyncio
async def test_a_vector_leg_whose_own_store_fails_degrades_the_answer(leg, monkeypatch) -> None:
    """The vector leg reads loom's own ClickHouse and the embedder: their failure marks
    the leg unavailable (the answer degraded) instead of failing tracker.similar."""
    s = _Src(item=ITEM, lexical=[_cand(116490, "columns.txt", bm25=9.0)], vector=[])
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)

    async def _boom(*a, **k):
        raise RuntimeError("local store down")

    monkeypatch.setattr(tsim, "_similar_vector_leg", _boom)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["lexical", "vector"])
    )
    assert out.legs["vector"] == "tracker_unavailable"
    assert [i["number"] for i in out.items] == [116490]


def test_the_embedding_window_is_per_kind(monkeypatch) -> None:
    """Issues stay in the window for tracker_embed_issue_closed_days
    (every closed issue the record holds — a duplicate's target is an older,
    often years-closed issue), PRs for tracker_embed_closed_days; the catch-up
    resolves a per-kind map or a single number."""
    monkeypatch.setattr(settings, "tracker_embed_closed_days", 365, raising=False)
    monkeypatch.setattr(settings, "tracker_embed_issue_closed_days", 3650, raising=False)
    assert ei.window_days("issue") == 3650 and ei.window_days("pr") == 365
    assert ei.window_days_by_kind() == {"issue": 3650, "pr": 365}
    assert ei._days_for({"issue": 3650, "pr": 365}, "issue") == 3650
    assert ei._days_for({"issue": 3650, "pr": 365}, "pr") == 365
    assert ei._days_for({"pr": 365}, "incident") == 365 and ei._days_for(90, "issue") == 90
    assert type(settings).model_fields["tracker_embed_issue_closed_days"].default == 3650


@pytest.mark.asyncio
async def test_a_widened_window_embeds_the_older_issues_in_the_same_pass(monkeypatch) -> None:
    """The cursor walk reads `updated_at > cursor`; an issue
    closed years ago that a wider window admits was last touched before the
    cursor and is never a page's row. The pass reads the window's items with
    no live row by number and embeds them; a second pass reads none."""
    monkeypatch.setattr(settings, "tracker_embed_issue_closed_days", 3650, raising=False)
    old = _item(
        "issue",
        40,
        "Old duplicate target",
        "closed years ago",
        upd=datetime(2021, 3, 1),
        closed=datetime(2021, 3, 2),
    )
    older = _item(
        "issue",
        41,
        "Another old one",
        "also closed",
        upd=datetime(2020, 1, 1),
        closed=datetime(2020, 1, 2),
    )
    recent_text = wk.embed_text("Recent issue", "in the 365-d window", body_chars=600)
    recent = _item("issue", 50, "Recent issue", "in the 365-d window", upd=T0.replace(hour=10))
    stored = {
        ("issue", 50): {"text_sha256": wk.text_sha256(recent_text), "tombstoned_at": None, **_MODEL}
    }
    up = _Upstream({"issue": [old, older, recent]})
    repo, emb, state = _Repo(stored), _Emb(), _State()
    await state.set(ORG, NS, "issue", T0.replace(hour=10))  # the cursor sits at the newest change
    result = wk.TrackerEmbedResult()
    empty = _Upstream({})
    await wk._run(
        result,
        now=T0,
        repo=repo,
        source_factory=lambda org, ns: up if ns == NS else empty,
        embedder=emb,
        state=state,
    )
    # the cursor walk read nothing newer; the missing read fetched the two old issues by number
    by_number = [c[1] for c in up.calls if c[0] == "by_number"]
    assert by_number == [{"kind": "issue", "numbers": [40, 41]}]
    assert result.rows_missing == 2 and result.rows_embedded == 2 and result.rows_tombstoned == 0
    written = {(r["kind"], r["number"]) for r in repo.rows if r["tombstoned_at"] is None}
    assert written == {("issue", 40), ("issue", 41)}
    assert state.cursor[(NS, "issue")] == T0.replace(hour=10)  # the cursor is not moved backwards
    # a second pass: every window item has a live row → no by-number read, nothing written
    repo2 = _Repo(
        {
            **stored,
            ("issue", 40): {
                "text_sha256": next(r["text_sha256"] for r in repo.rows if r["number"] == 40),
                "tombstoned_at": None,
                **_MODEL,
            },
            ("issue", 41): {
                "text_sha256": next(r["text_sha256"] for r in repo.rows if r["number"] == 41),
                "tombstoned_at": None,
                **_MODEL,
            },
        }
    )
    result2 = wk.TrackerEmbedResult()
    up.calls.clear()
    await wk._run(
        result2,
        now=T0,
        repo=repo2,
        source_factory=lambda org, ns: up if ns == NS else empty,
        embedder=emb,
        state=state,
    )
    assert (
        not [c for c in up.calls if c[0] == "by_number"]
        and result2.rows_missing == 0
        and repo2.rows == []
    )


@pytest.mark.asyncio
async def test_an_embedding_model_change_re_embeds_unchanged_texts() -> None:
    """A digest match is not a skip when another model wrote the row:
    nearest() filters on the served width, so the old rows would drop out of
    tracker.similar for good (or compare two spaces at the same width)."""
    text = wk.embed_text("Kept issue", "same body", body_chars=600)
    stored = {
        ("issue", 1): {
            "text_sha256": wk.text_sha256(text),
            "tombstoned_at": None,
            "embedding_model": "text-embedding-3-small",
        }
    }
    up = _Upstream({"issue": [_item("issue", 1, "Kept issue", "same body")]})
    repo, emb, state = _Repo(stored), _Emb(), _State()
    result = wk.TrackerEmbedResult()
    await wk._run(
        result,
        now=T0,
        repo=repo,
        source_factory=lambda org, ns: up if ns == NS else _Upstream({}),
        embedder=emb,
        state=state,
    )
    assert result.rows_skipped == 0 and result.rows_embedded == 1
    (row,) = [r for r in repo.rows if r["tombstoned_at"] is None]
    assert row["embedding_model"] == emb.model_name


@pytest.mark.asyncio
async def test_vector_leg_catch_up_re_embeds_a_row_another_model_wrote(leg, monkeypatch) -> None:
    """After an embedder switch the stored digest still matches the text, but
    the row's vector is in the old model's space: the catch-up re-embeds it
    rather than leave the changed item out of the vector leg until the pass."""
    s = _Src(item=ITEM, lexical=[], vector=[])
    s.fresh_items = [_item("issue", 120001, "a fresh issue", upd=T0.replace(hour=13))]
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    leg["cursors"] = _Cursors({"issue": T0.replace(hour=12)})
    inp = TrackerSimilarInput(org=ORG, namespace=NS, issue_number=119601, legs=["vector"])
    await tsim.handle_tracker_similar(inp)
    (row,) = [r for r in leg["repo"].inserted if r["number"] == 120001]
    # the same text, but the row was written by the previous embedder
    leg["repo"].digests[("issue", 120001)] = (row["text_sha256"], "old-model")
    n_ins = len(leg["repo"].inserted)
    out = await tsim.handle_tracker_similar(inp)
    assert out.query["fresh_items"] == 1
    (new,) = leg["repo"].inserted[n_ins:]
    assert new["number"] == 120001 and new["embedding_model"] == _Emb.model_name


@pytest.mark.asyncio
async def test_a_window_read_at_its_cap_tombstones_nothing(monkeypatch) -> None:
    """The window-keys read is capped; one that returns the cap may have
    stopped short, and an item past it would read as departed. The pass
    keeps every stored row and says so."""

    monkeypatch.setattr(settings, "tracker_embed_window_keys_limit", 1)
    stored = {
        ("issue", 1): {"text_sha256": "0" * 64, "tombstoned_at": None, **_MODEL},
        ("issue", 9): {"text_sha256": "1" * 64, "tombstoned_at": None, **_MODEL},
    }
    up = _Upstream({"issue": [_item("issue", 1, "Kept issue", "same body")]})
    repo, emb, state = _Repo(stored, {}), _Emb(), _State()
    result = wk.TrackerEmbedResult()
    await wk._run(
        result,
        now=T0,
        repo=repo,
        source_factory=lambda org, ns: up if ns == NS else _Upstream({}),
        embedder=emb,
        state=state,
    )
    assert result.rows_tombstoned == 0
    assert all(r["tombstoned_at"] is None for r in repo.rows)
    assert f"{NS}:issue" in result.tombstones_skipped
    assert result.outcome()["tombstones_skipped"] >= 1


@pytest.mark.asyncio
async def test_the_window_keys_read_is_ordered_and_capped(monkeypatch) -> None:

    up = _Rows([])
    monkeypatch.setattr(qi, "query_rows", up)
    await src.TrackerSource(ORG, NS).embed_window_keys(ORG, NS, kind="issue", closed_days=365)
    sql, params = up.calls[-1]
    assert "ORDER BY source_id" in sql and "LIMIT {lim:UInt32}" in sql
    assert params["lim"] == settings.tracker_embed_window_keys_limit
