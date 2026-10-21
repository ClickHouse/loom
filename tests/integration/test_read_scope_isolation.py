"""Read scope inside one namespace: a token pinned to user U never sees user V.

Users U and V write into the SAME namespace: facts (one key both hold, the
rest V's alone, across two sessions), raw turns through set_from_messages
(the same session id for both), an entity both rows name, a profile section,
a session brief, a working-memory block, a procedure, a saved chat message
(one conversation id for both) and a blacklist pattern. V's rows carry a
nonce (each fact row its own, all starting with V's). A token minted with
`read_user_id=U` then drives every op the registry holds:

* an op in `SCOPED_OPS` answers, and no answer carries V's nonce;
* the same op asked for `user_id="*"` or `user_id=V` is refused (403);
* every other op that does not return memory-free data is refused (403), so
  the entity graph, conversations and exports never answer such a token;
* every write a bounded token may call, aimed at V's keys, ids and brief by a
  token pinned to a third user W, answers without V's nonce and leaves each
  of V's rows (value, lifecycle, lock, version) and its usage counters as
  they were;
* W re-saving V's saved message is refused as another user's turn, W
  unsaving it finds nothing, and V's message stays saved.

An op the walks cannot build a body for fails the test unless `_UNBUILDABLE`
names it with the reason.

A positive control proves U's token finds U's own canary and a
namespace-scoped token finds V's, so an empty answer cannot pass by accident.
The cache case asks the same question as V, then as U: neither the precedent
store nor the LLM response cache may hand V's evidence to U.

Skips without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import time
import typing
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from server.settings import settings
from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

U, V, W = "user-u", "user-v", "user-w"
_OK = {200, 404}

# Ops no walk can build a body for, with the reason. Any other unbuildable op fails.
_UNBUILDABLE = {
    "memory.apply": "its target is a nested selector over many rows",
    "memory.record.write": "needs a registered record schema",
    "namespace.restore": "its payload is a whole namespace snapshot",
    "namespace.rollback": "would rewind the seeded namespace if its refusal regressed",
    "procedure.revise": "its graph is a nested procedure graph",
}


# Required fields whose name other ops use for another shape: filled only when the
# required field has this container type (a schema's fields, a policy's rule).
_SHAPED_HINTS: dict[str, tuple[type, Any]] = {
    "fields": (list, [{"name": "canary"}]),
    "actions": (list, ["memory.set"]),
    "condition": (dict, {"op": "formerly", "within": "1h", "operation": "memory.set"}),
}


@dataclass
class _Seed:
    org: str
    ns: str
    nonce_u: str
    nonce_v: str
    entity: str
    keys_v: list[str] = field(default_factory=list)
    memory_ids_v: list[str] = field(default_factory=list)
    brief_key: str = ""
    # One conversation both users saved a message in; V's message id carries V's nonce.
    conversation_id: str = ""
    # A blacklist pattern added by a namespace-wide token (its reason carries V's nonce).
    pattern_v: str = ""
    pattern_id_v: str = ""


def _saved_message_id(nonce: str) -> str:
    """The id of the chat message a seeded user saves; it carries the user's nonce."""
    return f"msg-{nonce}"


def _post(base: str, token: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=60.0) as c:
        return c.post(f"/v1/{op}", json=payload, headers={"Authorization": f"Bearer {token}"})


def _mint(org: str, ns: str, **claims: Any) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"sa:read-scope-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=["admin", "writer"],
        ttl_seconds=900,
        is_service_account=True,
        **claims,
    )


def _write(base: str, tok: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    r = _post(base, tok, op, body)
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text}"
    return r.json()


def _seed_user(base: str, tok: str, s: _Seed, user: str, nonce: str) -> list[str]:
    """One user's rows; returns the memory ids it wrote."""
    ids: list[str] = []
    for i, session in enumerate(("sess-1", "sess-2")):
        ident = {"org": s.org, "namespace": s.ns, "agent": "a", "user_id": user}
        own = _write(
            base,
            tok,
            "memory.set",
            {
                **ident,
                "session_id": session,
                "memory_key": f"canary/{user}/{i}",
                "value": f"{s.entity} keeps the vault code {nonce}r{i} in drawer {i}",
                "tags": ["canary"],
                "entities": [s.entity],
            },
        )
        ids.append(str(own.get("memory_id") or ""))
    # The key both users hold: a join on the key alone would mix them.
    shared = _write(
        base,
        tok,
        "memory.set",
        {
            "org": s.org,
            "namespace": s.ns,
            "agent": "a",
            "user_id": user,
            "memory_key": "canary/shared",
            "value": f"the shared key holds {nonce}rs",
            "entities": [s.entity],
            "tags": ["canary"],
        },
    )
    ids.append(str(shared.get("memory_id") or ""))
    ident = {"org": s.org, "namespace": s.ns, "agent": "a", "user_id": user, "session_id": "sess-1"}
    # Raw turns in loom.memory_evidence, behind memory.ask's literal and
    # deep-evidence legs; the same session id for both users.
    _write(
        base,
        tok,
        "memory.set_from_messages",
        {
            **ident,
            "messages": [{"role": "user", "content": f"{s.entity} said the vault code is {nonce}"}],
        },
    )
    _write(base, tok, "profile.set", {**ident, "label": "canary", "value": f"likes {nonce}"})
    _write(base, tok, "working_memory.set", {**ident, "block_key": "scratchpad", "content": nonce})
    _write(
        base,
        tok,
        "procedure.add",
        {
            **ident,
            "name": f"open the vault {user}",
            "situation": f"the vault is locked and {s.entity} is away",
            "steps": [f"find code {nonce}", "turn the dial"],
        },
    )
    _write(
        base,
        tok,
        "session.brief_save",
        {
            **ident,
            "brief_key": f"{s.brief_key}-{user}",
            "trigger": "model",
            "sections": {"goal": f"rotate the {nonce} code"},
        },
    )
    saved = _write(
        base,
        tok,
        "conversation.save_message",
        {
            "org": s.org,
            "namespace": s.ns,
            "agent": "a",
            "user_id": user,
            "conversation_id": s.conversation_id,
            "message_id": _saved_message_id(nonce),
            "role": "user",
            "content": f"{s.entity} saved the vault code {nonce}",
        },
    )
    assert saved["indexed"], saved
    return ids


@pytest.fixture(scope="module")
def seed(control_plane_url: str) -> _Seed:
    org = unique_org("read-scope")
    s = _Seed(
        org=org,
        ns=f"rs-{uuid.uuid4().hex[:8]}",
        nonce_u=f"NONCEU{uuid.uuid4().hex[:10]}",
        nonce_v=f"NONCEV{uuid.uuid4().hex[:10]}",
        entity=f"Zorblax{uuid.uuid4().hex[:6]}",
        brief_key=f"brief-{uuid.uuid4().hex[:6]}",
        conversation_id=f"conv-{uuid.uuid4().hex[:8]}",
    )
    writer = _mint(org, s.ns)
    _seed_user(control_plane_url, writer, s, U, s.nonce_u)
    s.memory_ids_v = _seed_user(control_plane_url, writer, s, V, s.nonce_v)
    s.keys_v = [f"canary/{V}/0", f"canary/{V}/1", "canary/shared"]
    s.pattern_v = f"BLKV{uuid.uuid4().hex[:10]}"
    s.pattern_id_v = _write(
        control_plane_url,
        writer,
        "blacklist.add",
        {"org": org, "namespace": s.ns, "pattern": s.pattern_v, "reason": f"my {s.nonce_v}"},
    )["pattern_id"]

    def _searchable() -> bool:
        r = _post(
            control_plane_url,
            writer,
            "memory.search",
            {"org": org, "namespace": s.ns, "agent": "*", "query": s.nonce_v, "top_k": 10},
        )
        return r.status_code == 200 and s.nonce_v in r.text

    wait_until(_searchable, max_wait=30.0, description="V's canary to be searchable")
    return s


@pytest.fixture(scope="module")
def tokens(seed: _Seed) -> dict[str, str]:
    return {
        "namespace": _mint(seed.org, seed.ns),
        "pinned_u": _mint(seed.org, seed.ns, read_scope="own_user", read_user_id=U),
        "pinned_v": _mint(seed.org, seed.ns, read_scope="own_user", read_user_id=V),
        "pinned_w": _mint(seed.org, seed.ns, read_scope="own_user", read_user_id=W),
    }


def _payload(model: type, seed: _Seed, user_id: str) -> dict[str, Any] | None:
    """A body for an input model aimed at V's rows. None when a required field
    cannot be synthesised."""
    hints: dict[str, Any] = {
        "org": seed.org,
        "namespace": seed.ns,
        "agent": "*",
        "user_id": user_id,
        "session_id": "-",
        "json_schema": {"type": "object", "properties": {"value": {"type": "string"}}},
        "memory_key": seed.keys_v[0],
        "memory_keys": seed.keys_v,
        "memory_id": seed.memory_ids_v[0],
        "memory_ids": seed.memory_ids_v,
        # memory.delete_version names the version it removes; the gate refuses first.
        "version": "1",
        "query": f"vault code {seed.entity}",
        "question": f"what is the vault code {seed.entity} keeps?",
        "situation": f"the vault is locked and {seed.entity} is away",
        "entity": seed.entity,
        "seed_entity": seed.entity,
        "tags": ["canary"],
        "prefix": "canary",
        "memory_key_prefix": "canary",
        "block_key": "scratchpad",
        "brief_key": f"{seed.brief_key}-{V}",
        "project": "",
        "label": "canary",
        "thread": "",
        "namespace_scope": "this",
        "value": "canary",
        "steps": ["canary", "turn the dial"],
        "ttl_seconds": 3600,
        "from_date": "2026-01-01",
        "to_date": "2099-12-31",
        "messages": [{"role": "user", "content": "canary"}],
        "entries": [{"memory_key": seed.keys_v[0], "value": "canary"}],
        "failure_ids": [str(uuid.uuid4())],
        "conversation_id": seed.conversation_id,
        "message_ids": [_saved_message_id(seed.nonce_v)],
    }
    out: dict[str, Any] = {}
    fields = model.model_fields
    for name, f in fields.items():
        if name == "memory_keys" and "memory_key" in fields:
            continue  # an op taking either names one key
        if name in hints:
            out[name] = hints[name]
        elif f.is_required() and name in _SHAPED_HINTS:
            shape, value = _SHAPED_HINTS[name]
            if typing.get_origin(f.annotation) is not shape:
                return None
            out[name] = value
        elif f.is_required():
            ann = f.annotation
            if ann is str:
                out[name] = "canary"
            elif ann is int:
                out[name] = 1
            elif ann is bool:
                out[name] = False
            elif typing.get_origin(ann) is typing.Literal:
                out[name] = typing.get_args(ann)[0]
            else:
                return None
    return out


def _writes(op: Any) -> bool:
    from server.operations.base import Role

    return op.required_role >= Role.WRITER or op.audit or op.writes


def _scoped_ops() -> list[Any]:
    """The reads of SCOPED_OPS; its writes are walked by the write walk."""
    from server.identity.domain.read_scope import SCOPED_OPS
    from server.operations.registry import OPERATIONS_BY_NAME

    return [OPERATIONS_BY_NAME[n] for n in sorted(SCOPED_OPS) if not _writes(OPERATIONS_BY_NAME[n])]


def _bounded_writes() -> list[Any]:
    """The writes a token bounded to one user may call: those in SCOPED_OPS and those
    NARROW_SAFE_OPS names as a write. A group key (`code.`) is not walked: its writes
    reach no memory row."""
    from server.identity.domain.read_scope import NARROW_SAFE_OPS, SCOPED_OPS
    from server.operations.registry import OPERATIONS_BY_NAME

    names = {n for n, why in NARROW_SAFE_OPS.items() if why.startswith("a write")}
    names |= {n for n in SCOPED_OPS if _writes(OPERATIONS_BY_NAME[n])}
    return [OPERATIONS_BY_NAME[n] for n in sorted(names)]


def _refused_ops() -> list[Any]:
    from server.identity.domain.read_scope import honours_read_scope
    from server.operations.registry import ALL_OPERATIONS

    return [op for op in ALL_OPERATIONS if not honours_read_scope(op.name)]


def test_positive_controls(control_plane_url: str, seed: _Seed, tokens: dict[str, str]) -> None:
    body = {"org": seed.org, "namespace": seed.ns, "agent": "*", "memory_key": "canary/shared"}
    r = _post(control_plane_url, tokens["pinned_u"], "memory.get", body)
    assert r.status_code == 200 and seed.nonce_u in r.text, r.text
    r = _post(
        control_plane_url,
        tokens["namespace"],
        "memory.list",
        {"org": seed.org, "namespace": seed.ns, "agent": "*", "user_id": "*", "session_id": "*"},
    )
    assert r.status_code == 200 and seed.nonce_v in r.text, "V's canary is unreachable at all"


def test_every_scoped_op_keeps_v_out_of_us_answers(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    leaks: dict[str, str] = {}
    bad: dict[str, int] = {}
    unbuildable: list[str] = []
    for op in _scoped_ops():
        body = _payload(op.input_model, seed, "-")
        if body is None:
            unbuildable.append(op.name)
            continue
        r = _post(control_plane_url, tokens["pinned_u"], op.name, body)
        if r.status_code == 422:
            unbuildable.append(op.name)
            continue
        if r.status_code not in _OK:
            bad[op.name] = r.status_code
        if seed.nonce_v in r.text:
            leaks[op.name] = r.text[:300]
    assert not leaks, f"V's canary reached U's token: {leaks}"
    assert not bad, f"unexpected statuses: {bad}"
    assert set(unbuildable) <= set(_UNBUILDABLE), unbuildable


def test_the_walk_reaches_vs_rows_without_the_bound(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    """The same bodies, with `user_id="*"` under a namespace-scoped token,
    return V's canary: the walk above is aimed at V's rows, so its empty
    answers are the bound working, not a probe that missed."""
    reached: set[str] = set()
    for op in _scoped_ops():
        body = _payload(op.input_model, seed, "*")
        if body is None:
            continue
        r = _post(control_plane_url, tokens["namespace"], op.name, body)
        if seed.nonce_v in r.text:
            reached.add(op.name)
    must = {
        "memory.list",
        "memory.bulk_get",
        "memory.get_by_id",
        "memory.history",
        "memory.timeline",
        "context.pack",
        "search.query",
        "profile.render",
        "session.brief_list",
        "session.brief_get",
    }
    assert must <= reached, sorted(must - reached)


def test_the_walk_reaches_vs_saved_messages(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    """These ops read `user_id="*"` as a literal user, so the same bodies are asked
    for V under a namespace-scoped token, and they return V's saved message."""
    from server.operations.registry import OPERATIONS_BY_NAME

    for name in (
        "conversation.saved_status",
        "conversation.list_saved_messages",
        "conversation.search_saved_messages",
    ):
        body = _payload(OPERATIONS_BY_NAME[name].input_model, seed, V)
        assert body is not None, name
        r = _post(control_plane_url, tokens["namespace"], name, body)
        assert r.status_code == 200 and seed.nonce_v in r.text, f"{name}: {r.text[:300]}"


def test_a_pinned_token_cannot_save_or_unsave_vs_saved_message(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str], ch_client: Any
) -> None:
    """W re-saves and unsaves V's saved message as itself: the save is refused as
    another user's turn, the unsave finds nothing, and V's message stays saved
    as V's one row. Both answers echo the message id W sent, so they are checked
    here and not by the write walk's nonce check."""
    conv = {
        "org": seed.org,
        "namespace": seed.ns,
        "agent": "a",
        "conversation_id": seed.conversation_id,
    }
    msg = {**conv, "message_id": _saved_message_id(seed.nonce_v)}
    saved = _write(
        control_plane_url,
        tokens["pinned_w"],
        "conversation.save_message",
        {**msg, "user_id": W, "role": "user", "content": "canary"},
    )
    assert saved["reason"] == "owner_mismatch", saved
    unsaved = _write(
        control_plane_url, tokens["pinned_w"], "conversation.unsave_message", {**msg, "user_id": W}
    )
    assert unsaved["found"] is False, unsaved
    status = _write(
        control_plane_url,
        tokens["namespace"],
        "conversation.saved_status",
        {**conv, "user_id": V, "message_ids": [msg["message_id"]]},
    )
    assert status["saved"] == [msg["message_id"]], status
    status_w = _write(
        control_plane_url,
        tokens["pinned_w"],
        "conversation.saved_status",
        {**conv, "user_id": W, "message_ids": [msg["message_id"]]},
    )
    assert status_w["saved"] == [], status_w
    rows = ch_client.query(
        """
        SELECT user_id, content, is_saved, tombstoned_at IS NULL
        FROM loom.conversations
        WHERE org = {org:String} AND namespace = {ns:String}
          AND thread_id = {tid:String} AND msg_id = {mid:String}
        """,
        parameters={
            "org": seed.org,
            "ns": seed.ns,
            "tid": seed.conversation_id,
            "mid": msg["message_id"],
        },
    ).result_rows
    assert [tuple(r) for r in rows] == [
        (V, f"{seed.entity} saved the vault code {seed.nonce_v}", True, True)
    ], rows


@pytest.mark.parametrize("other_user", ["*", V])
def test_a_wildcard_or_foreign_user_is_refused(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str], other_user: str
) -> None:
    answered: dict[str, int] = {}
    for op in _scoped_ops():
        if "user_id" not in op.input_model.model_fields:
            continue
        body = _payload(op.input_model, seed, other_user)
        if body is None:
            continue
        r = _post(control_plane_url, tokens["pinned_u"], op.name, body)
        if r.status_code == 422:
            continue
        assert seed.nonce_v not in r.text, op.name
        if r.status_code != 403:
            answered[op.name] = r.status_code
    assert not answered, f"user_id={other_user!r} was served under a pinned token: {answered}"


def test_ops_that_cannot_bound_their_answer_are_refused(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    served: dict[str, int] = {}
    unbuildable: list[str] = []
    for op in _refused_ops():
        body = _payload(op.input_model, seed, "-")
        if body is None:
            unbuildable.append(op.name)
            continue
        r = _post(control_plane_url, tokens["pinned_u"], op.name, body)
        assert seed.nonce_v not in r.text, op.name
        # Only the gate's 403 proves the refusal: a 422 is a body the walk got wrong.
        if r.status_code != 403:
            served[op.name] = r.status_code
    assert not served, f"not refused for a token bounded to one user: {served}"
    assert set(unbuildable) <= set(_UNBUILDABLE), unbuildable


def _v_rows(ch: Any, seed: _Seed) -> dict[str, tuple[Any, ...]]:
    """Each of V's memory rows by id: value, lifecycle, lock and version."""
    rows = ch.query(
        """
        SELECT toString(memory_id), memory_key, session_id, value, toString(tombstoned_at),
               status, lock_mode, lock_reason, toString(lock_until), locked_by, version
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND user_id = {uid:String}
        """,
        parameters={"org": seed.org, "ns": seed.ns, "uid": V},
    ).result_rows
    return {str(r[0]): tuple(r[1:]) for r in rows}


def _v_usage(ch: Any, seed: _Seed, ids: list[str]) -> dict[tuple[str, str], int]:
    """The usage counters an outcome moves, per (memory_id, event type)."""
    rows = ch.query(
        """
        SELECT toString(memory_id), event_type, sum(event_count)
        FROM loom.memory_usage_events
        WHERE org = {org:String} AND namespace = {ns:String}
          AND toString(memory_id) IN {ids:Array(String)}
          AND event_type IN ('evidence', 'used', 'harm', 'rejection')
        GROUP BY 1, 2
        """,
        parameters={"org": seed.org, "ns": seed.ns, "ids": ids},
    ).result_rows
    return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}


def _attack_moves(seed: _Seed) -> list[tuple[str, dict[str, Any]]]:
    """Writes aimed at each of V's facts by key (with the `*` agent) and by id."""
    w = {"org": seed.org, "namespace": seed.ns, "agent": "*", "user_id": "-"}
    moves: list[tuple[str, dict[str, Any]]] = []
    for key, mid in zip(seed.keys_v, seed.memory_ids_v, strict=True):
        moves += [
            ("memory.delete", {**w, "memory_key": key}),
            ("memory.lock", {**w, "memory_key": key, "mode": "read_only", "reason": "probe"}),
            ("memory.touch", {**w, "memory_key": key, "ttl_seconds": 60}),
            ("memory.outcome", {**w, "memory_key": key, "outcome": "negative", "weight": 100}),
            ("memory.outcome", {**w, "memory_key": key, "outcome": "positive", "weight": 100}),
            ("memory.outcome", {**w, "memory_id": mid, "outcome": "negative", "weight": 100}),
            ("memory.outcome", {**w, "memory_id": mid, "outcome": "positive", "weight": 100}),
        ]
    return moves


def test_writes_aimed_at_vs_rows_never_answer_or_change_them(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str], ch_client: Any
) -> None:
    """A token pinned to W (who holds nothing) sends every write a bounded token may
    call at V's keys, ids and brief, plus a delete, lock, touch and outcome (by key and
    by id) and an if_match probe at each of V's facts. No answer carries V's nonce or
    hash, a refusal is a 409 or a 423 (W's own lock) at most, and each of V's rows and
    its usage counters read back unchanged afterwards."""
    ns_tok, w_tok = tokens["namespace"], tokens["pinned_w"]
    shas: list[str] = []
    for key, session in zip(seed.keys_v, ("sess-1", "sess-2", "-"), strict=True):
        got = _write(
            control_plane_url,
            ns_tok,
            "memory.get",
            {
                "org": seed.org,
                "namespace": seed.ns,
                "agent": "a",
                "user_id": V,
                "session_id": session,
                "memory_key": key,
            },
        )
        assert got.get("content_sha256"), got
        shas.append(got["content_sha256"])
    ids = list(seed.memory_ids_v)

    # Settle first: a write's own follow-ups (entity re-emits) may still bump a version.
    def _stable() -> tuple[dict[str, tuple[Any, ...]], dict[tuple[str, str], int]] | None:
        first = (_v_rows(ch_client, seed), _v_usage(ch_client, seed, ids))
        return (
            first if first == (_v_rows(ch_client, seed), _v_usage(ch_client, seed, ids)) else None
        )

    rows_before, usage_before = wait_until(_stable, max_wait=30.0, description="V's rows to settle")
    assert set(ids) <= set(rows_before), "V's facts are missing before the walk"
    assert len({rows_before[i][2] for i in ids}) == len(ids), "each fact needs its own nonce"

    leaks: dict[str, str] = {}
    bad: dict[str, int] = {}
    for op, body in _attack_moves(seed):
        r = _post(control_plane_url, w_tok, op, body)
        if r.status_code not in {200, 404}:
            bad[f"{op} {body}"] = r.status_code
        if seed.nonce_v in r.text:
            leaks[op] = r.text[:300]
        if op == "memory.outcome" and r.json().get("recorded") is not False:
            bad[f"{op} {body} recorded"] = r.status_code
    for key in seed.keys_v:
        probe = {
            "org": seed.org,
            "namespace": seed.ns,
            "agent": "*",
            "user_id": "-",
            "memory_key": key,
            "value": "probe",
            "if_match_sha256": "0" * 64,
        }
        r = _post(control_plane_url, w_tok, "memory.set", probe)
        if r.status_code != 409:
            bad[f"if_match on {key}"] = r.status_code
        if any(sha in r.text for sha in shas):
            leaks[f"if_match on {key}"] = r.text[:300]

    unbuildable: list[str] = []
    walked: set[str] = set()
    for op in _bounded_writes():
        body = _payload(op.input_model, seed, "-")
        if body is None:
            unbuildable.append(op.name)
            continue
        r = _post(control_plane_url, w_tok, op.name, body)
        if r.status_code == 422:
            unbuildable.append(op.name)
            continue
        walked.add(op.name)
        if r.status_code not in {200, 404, 409, 423}:  # 409/423: a refused write
            bad[op.name] = r.status_code
        if seed.nonce_v in r.text:
            leaks[op.name] = r.text[:300]

    rows_after = _v_rows(ch_client, seed)
    changed = {
        i: (rows_before.get(i), rows_after.get(i))
        for i in rows_before.keys() | rows_after.keys()
        if rows_before.get(i) != rows_after.get(i)
    }
    assert not changed, f"V's rows changed: {changed}"
    usage_after = _v_usage(ch_client, seed, ids)
    assert usage_after == usage_before, f"V's usage moved: {usage_before} -> {usage_after}"
    assert not leaks, f"V's canary or hash reached W's token: {leaks}"
    assert not bad, f"unexpected answers: {bad}"
    assert set(unbuildable) <= set(_UNBUILDABLE), unbuildable
    assert {"session.brief_save", "session.brief_delete", "memory.set"} <= walked
    r = _post(
        control_plane_url,
        ns_tok,
        "session.brief_get",
        {
            "org": seed.org,
            "namespace": seed.ns,
            "agent": "*",
            "user_id": "*",
            "brief_key": f"{seed.brief_key}-{V}",
        },
    )
    assert r.status_code == 200 and seed.nonce_v in r.text, f"brief: {r.text[:300]}"


def test_a_narrowed_token_cannot_reach_any_blacklist_op(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    """A pattern has no user axis, so W's add, list, remove and sweep are refused (an
    add would be a namespace-wide Refuse W could never manage, and its sweep would
    reach V's rows). The namespace-wide pattern still refuses W's write, and the 422
    names neither its id nor V's reason."""
    w = {"org": seed.org, "namespace": seed.ns}
    by_id = {"pattern_id": seed.pattern_id_v}
    for op, body in (
        ("blacklist.add", {**w, "pattern": seed.nonce_v[:10], "reason": "probe", "sweep": True}),
        ("blacklist.list", w),
        ("blacklist.remove", {"org": seed.org, **by_id}),
        ("blacklist.sweep", {**w, **by_id}),
    ):
        r = _post(control_plane_url, tokens["pinned_w"], op, body)
        assert r.status_code == 403, f"{op}: {r.status_code} {r.text[:300]}"
        for secret in (seed.pattern_v, seed.nonce_v, seed.pattern_id_v):
            assert secret not in r.text, f"{op}: {r.text[:300]}"
    # A namespace-wide token still sees it.
    r = _post(control_plane_url, tokens["namespace"], "blacklist.list", w)
    assert r.status_code == 200 and seed.pattern_id_v in r.text, r.text[:300]

    # Another web process takes the pattern up within its version-check interval,
    # read here from this process's settings: the server is assumed to share them.
    time.sleep(settings.blacklist_version_check_interval_seconds + 0.5)
    r = _post(
        control_plane_url,
        tokens["pinned_w"],
        "memory.set",
        {
            **w,
            "agent": "a",
            "user_id": W,
            "memory_key": "probe/blacklisted",
            "value": f"a note holding {seed.pattern_v}",
        },
    )
    body = r.text[:300]
    assert r.status_code == 422 and "BLACKLISTED" in r.text, f"{r.status_code} {body}"
    assert seed.nonce_v not in r.text and seed.pattern_id_v not in r.text, body


def test_pinned_users_keep_their_own_brief_under_one_key(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    key = f"{seed.brief_key}-both"
    tok = {U: tokens["pinned_u"], V: tokens["pinned_v"]}
    for user, nonce in ((U, seed.nonce_u), (V, seed.nonce_v)):
        ident = {"org": seed.org, "namespace": seed.ns, "agent": "a", "user_id": user}
        body = {**ident, "brief_key": key, "trigger": "model", "sections": {"goal": nonce}}
        saved = _write(control_plane_url, tok[user], "session.brief_save", body)
        assert saved["brief_key"] == key and saved["revision"] == 1, saved
    for user, mine, theirs in ((U, seed.nonce_u, seed.nonce_v), (V, seed.nonce_v, seed.nonce_u)):
        ident = {"org": seed.org, "namespace": seed.ns, "agent": "a", "user_id": user}
        got = _write(
            control_plane_url,
            tok[user],
            "session.brief_get",
            {**ident, "brief_key": key},
        )
        assert got["found"] and got["brief_key"] == key and mine in got["rendered"], got
        assert theirs not in str(got)


def test_every_listed_brief_key_opens_and_deletes_for_the_token_that_listed_it(
    control_plane_url: str,
) -> None:
    org, ns = unique_org("brief-keys"), f"bk-{uuid.uuid4().hex[:8]}"
    ident = {"org": org, "namespace": ns, "agent": "a", "user_id": U, "session_id": "s1"}
    tok = {
        "namespace": _mint(org, ns),
        "own_user": _mint(org, ns, read_scope="own_user", read_user_id=U),
        "own_session": _mint(org, ns, read_scope="own_session", read_user_id=U),
    }

    def listed(t: str) -> list[str]:
        out = _write(control_plane_url, tok[t], "session.brief_list", ident)
        return [b["brief_key"] for b in out["briefs"]]

    for t in tok:
        body = {**ident, "brief_key": f"k-{t}", "trigger": "model", "sections": {"goal": t}}
        _write(control_plane_url, tok[t], "session.brief_save", body)
    assert listed("own_session") == ["k-own_session"]
    assert listed("own_user") == ["k-own_user"]
    everything = listed("namespace")
    assert len(everything) == 3 and "k-namespace" in everything, everything
    owned = [k for k in everything if k.startswith("~")]
    # A narrowed token can't name a stored owner key, its own or anyone's.
    for k in [*owned, "~bob~k-own_user"]:
        r = _post(
            control_plane_url, tok["own_user"], "session.brief_get", {**ident, "brief_key": k}
        )
        assert r.status_code == 422, f"{k}: {r.status_code} {r.text}"
    for t in ("own_session", "own_user", "namespace"):
        for k in listed(t):
            got = _write(control_plane_url, tok[t], "session.brief_get", {**ident, "brief_key": k})
            assert got["found"] and got["brief_key"] == k, got
            gone = _write(
                control_plane_url, tok[t], "session.brief_delete", {**ident, "brief_key": k}
            )
            assert gone["deleted"], gone
    assert listed("namespace") == []


def test_a_pinned_token_cannot_write_as_another_user(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    ident = {"org": seed.org, "namespace": seed.ns, "agent": "a", "user_id": V}
    for op, body in [
        ("memory.set", {**ident, "memory_key": seed.keys_v[0], "value": "poisoned"}),
        ("memory.delete", {**ident, "memory_key": seed.keys_v[0]}),
        ("session.brief_delete", {**ident, "brief_key": f"{seed.brief_key}-{V}"}),
    ]:
        r = _post(control_plane_url, tokens["pinned_u"], op, body)
        assert r.status_code == 403, f"{op}: {r.status_code} {r.text}"
    r = _post(
        control_plane_url,
        tokens["namespace"],
        "memory.get",
        {**ident, "memory_key": seed.keys_v[0], "session_id": "sess-1"},
    )
    assert r.status_code == 200 and seed.nonce_v in r.text, r.text


def test_another_users_memory_id_is_not_found(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    for mid in seed.memory_ids_v:
        r = _post(
            control_plane_url,
            tokens["pinned_u"],
            "memory.get_by_id",
            {"org": seed.org, "memory_id": mid},
        )
        assert r.status_code == 200 and r.json().get("found") is False, r.text


def test_another_users_memory_ids_have_no_status(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    body = {"org": seed.org, "namespace": seed.ns, "memory_ids": seed.memory_ids_v}
    r = _post(control_plane_url, tokens["pinned_u"], "memory.statuses", body)
    assert r.status_code == 200 and r.json()["rows"] == [], r.text
    r = _post(control_plane_url, tokens["namespace"], "memory.statuses", body)
    assert r.status_code == 200 and len(r.json()["rows"]) == len(seed.memory_ids_v), r.text


def test_an_answer_cached_for_v_is_never_served_to_u(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    body = {
        "org": seed.org,
        "namespace": seed.ns,
        "agent": "*",
        "query": f"what vault code does {seed.entity} keep?",
        "namespace_scope": "this",
    }
    r = _post(control_plane_url, tokens["pinned_v"], "memory.ask", body)
    assert r.status_code == 200, r.text
    for _ in range(2):  # the second ask is the precedent's fast-reuse path
        r = _post(control_plane_url, tokens["pinned_u"], "memory.ask", body)
        assert r.status_code == 200, r.text
        assert seed.nonce_v not in r.text
    # The namespace-wide listing's counts are cached; a pinned page is not.
    listing = {"org": seed.org, "namespace": seed.ns, "agent": "*", "user_id": "*"}
    assert _post(control_plane_url, tokens["namespace"], "memory.list", listing).status_code == 200
    r = _post(control_plane_url, tokens["pinned_u"], "memory.list", {**listing, "user_id": "-"})
    assert r.status_code == 200 and seed.nonce_v not in r.text


def _v_ledgers(ch: Any, seed: _Seed, tags: dict[str, str]) -> dict[str, list[tuple[Any, ...]]]:
    """V's run, thread, messages, record, blob and procedure runs, row by row."""
    p = {"org": seed.org, "ns": seed.ns, **tags}
    where = "org = {org:String} AND namespace = {ns:String}"
    statements = {
        "run": f"SELECT user_id, status, goal, toString(completed_at), metadata "
        f"FROM loom.agent_runs FINAL WHERE {where} AND run_id = {{run:String}}",
        "thread": f"SELECT user_id, title, metadata FROM loom.conversation_threads FINAL "
        f"WHERE {where} AND thread_id = {{thread:String}}",
        "messages": f"SELECT user_id, msg_id, content FROM loom.conversations "
        f"WHERE {where} AND thread_id = {{thread:String}} ORDER BY msg_id",
        "record": f"SELECT user_id, status, record_json, version FROM loom.memory_records FINAL "
        f"WHERE {where} AND schema_name = {{schema:String}} ORDER BY version",
        "blob": f"SELECT user_id, content, toString(expires_at) FROM loom.context_blobs FINAL "
        f"WHERE {where} AND blob_key = {{blob:String}}",
        "procedure_runs": f"SELECT session_id, ok, note FROM loom.procedure_runs "
        f"WHERE {where} AND memory_key = {{proc:String}} ORDER BY ts",
    }
    return {
        name: [tuple(r) for r in ch.query(sql, parameters=p).result_rows]
        for name, sql in statements.items()
    }


def test_writes_aimed_at_vs_runs_threads_records_and_blobs_leave_them_alone(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str], ch_client: Any
) -> None:
    """W's token aims run.begin / run.complete at V's run, the conversation writes at V's
    thread, records and decisions at V's record key, procedure.record_run at V's
    procedure, context.offload at V's blob, and a set / bulk_set next to V's pinned fact.
    The run moves start a new run or answer not-found; the others are refused; the pinned
    row still blocks the close write (a namespace-wide invariant) but no answer names it;
    V's rows read back unchanged."""
    ns_tok, w_tok = tokens["namespace"], tokens["pinned_w"]
    tag = uuid.uuid4().hex[:8]
    tags = {
        "run": f"run-v-{tag}",
        "thread": f"thread-v-{tag}",
        "schema": f"schema_v_{tag}",
        "blob": f"blob/v-{tag}",
    }
    v = {"org": seed.org, "namespace": seed.ns, "agent": "a", "user_id": V, "session_id": "sess-1"}
    w = {"org": seed.org, "namespace": seed.ns, "agent": "a", "user_id": "-"}
    ns = {"org": seed.org, "namespace": seed.ns}

    _write(control_plane_url, ns_tok, "run.begin", {**v, "run_id": tags["run"], "goal": "v's run"})
    _write(control_plane_url, ns_tok, "conversation.set_logging", {**ns, "enabled": True})
    _write(
        control_plane_url,
        ns_tok,
        "conversation.create",
        {**v, "thread_id": tags["thread"], "title": "v's"},
    )
    _write(
        control_plane_url,
        ns_tok,
        "conversation.append",
        {**v, "thread_id": tags["thread"], "role": "user", "content": f"v says {seed.nonce_v}"},
    )
    _write(
        control_plane_url,
        ns_tok,
        "memory_schema.register",
        {**ns, "name": tags["schema"], "fields": [{"name": "code", "identity": True}]},
    )
    record = {"schema_name": tags["schema"], "memory_key": f"rec/{tag}"}
    _write(
        control_plane_url,
        ns_tok,
        "memory.record.write",
        {**v, **record, "fields": {"code": {"value": seed.nonce_v}}},
    )
    _write(
        control_plane_url,
        ns_tok,
        "context.offload",
        {**v, "blob_key": tags["blob"], "content": f"v's blob {seed.nonce_v}"},
    )
    procs = _write(
        control_plane_url,
        ns_tok,
        "procedure.list",
        {**v, "agent": "*"},
    )
    tags["proc"] = next(
        p["memory_key"] for p in procs["procedures"] if V in str(p.get("name") or p)
    )
    _write(control_plane_url, ns_tok, "procedure.record_run", {**v, "memory_key": tags["proc"]})
    # Not hex, so it can't turn up inside an id or a hash in the answer.
    code = "zulu-4417"
    pinned_text = f"the vault on pier {tag} stays shut on sundays and its alarm code is {code}"
    _write(
        control_plane_url,
        ns_tok,
        "memory.set",
        {**v, "memory_key": f"pinned/{tag}", "value": pinned_text, "pinned": True},
    )
    near = {"memory_key": f"near/{tag}", "value": pinned_text.replace(code, "unknown")}
    # Control: an unbounded writer is blocked by the pinned row, so the probe can see it.
    control = _write(
        control_plane_url,
        ns_tok,
        "memory.set",
        {**w, "user_id": W, **near, "dry_run": True},
    )
    assert control["conflict_verdict"] == "rejected", control

    before = _v_ledgers(ch_client, seed, tags)
    assert all(before[k] for k in before), before

    leaks: dict[str, str] = {}
    bad: dict[str, Any] = {}
    begun = _post(control_plane_url, w_tok, "run.begin", {**w, "run_id": tags["run"], "goal": "x"})
    if begun.status_code != 200 or begun.json().get("run_id") == tags["run"]:
        bad["run.begin"] = (begun.status_code, begun.text[:200])
    done = _post(
        control_plane_url,
        w_tok,
        "run.complete",
        {**ns, "run_id": tags["run"], "status": "failed", "reflection": {"what_worked": ["x"]}},
    )
    if done.status_code != 404:
        bad["run.complete"] = (done.status_code, done.text[:200])
    refused = [
        ("conversation.create", {**w, "thread_id": tags["thread"], "title": "taken"}),
        (
            "conversation.append",
            {**w, "thread_id": tags["thread"], "role": "user", "content": f"v says {seed.nonce_v}"},
        ),
        ("memory.record.write", {**w, **record, "fields": {"code": {"value": "overwritten"}}}),
        (
            "decision.record",
            {**w, "category": "c", "scenario": "s", "reasoning": "r", "outcome": "o"},
        ),
        ("procedure.record_run", {**w, "memory_key": tags["proc"], "ok": False}),
        ("context.offload", {**w, "blob_key": tags["blob"], "content": "overwritten"}),
        ("context.offload", {**w, "content": f"v's blob {seed.nonce_v}", "ttl_seconds": 60}),
    ]
    for op, body in refused:
        r = _post(control_plane_url, w_tok, op, body)
        if r.status_code != 403:
            bad[op] = (r.status_code, r.text[:200])
    probe = _post(control_plane_url, w_tok, "memory.set", {**w, **near, "dry_run": True})
    if probe.status_code != 200 or probe.json().get("conflict_verdict") != "rejected":
        bad["memory.set near a pinned row"] = (probe.status_code, probe.text[:200])
    bulk = _post(control_plane_url, w_tok, "memory.bulk_set", {**w, "entries": [near]})
    if bulk.status_code != 200:
        bad["memory.bulk_set near a pinned row"] = (bulk.status_code, bulk.text[:200])
    pinned_id = next(i for i, row in _v_rows(ch_client, seed).items() if row[0] == f"pinned/{tag}")
    for name, r in (("run.begin", begun), ("run.complete", done), ("set", probe), ("bulk", bulk)):
        named = [m for m in (seed.nonce_v, code, f"pinned/{tag}", pinned_id) if m and m in r.text]
        if named:
            leaks[name] = r.text[:300]

    after = _v_ledgers(ch_client, seed, tags)
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not changed, f"V's rows changed: {changed}"
    assert not leaks, f"V's rows reached W's token: {leaks}"
    assert not bad, f"unexpected answers: {bad}"


def _rows_by_user(ch: Any, org: str, ns: str) -> dict[str, list[tuple[Any, ...]]]:
    rows = ch.query(
        """
        SELECT user_id, memory_key, toString(status), superseded_by
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
        ORDER BY user_id, memory_key
        """,
        parameters={"org": org, "ns": ns},
    ).result_rows
    out: dict[str, list[tuple[Any, ...]]] = {}
    for r in rows:
        out.setdefault(str(r[0]), []).append(tuple(r[1:]))
    return out


async def _drain_queue(org: str, ns: str) -> None:
    """One worker pass over this namespace's queue, in-process (no auth bound, as
    the worker runs); the CI server runs with LOOM_BACKGROUND_WORKERS=off."""
    from server.core.ch import get_client
    from server.memory.app import extraction_queue as q

    ch = get_client()
    await ch.connect()  # the in-process client, on this test's event loop
    try:
        rows = await q.claim_batch(limit=10, lease_seconds=120, org_filter=org, namespace_filter=ns)
        assert rows, "nothing was queued"
        for row in rows:
            await q.mark_done(row, written=await q.replay_row(row))
    finally:
        await ch.disconnect()


@pytest.mark.asyncio
async def test_a_deferred_extraction_replays_inside_the_writers_scope(
    control_plane_url: str, seed: _Seed, ch_client: Any
) -> None:
    """Under `newest_wins` a same-key write supersedes the older row. W's pinned
    token defers a set_from_messages whose fact lands on the key V holds: the replay
    runs under W's scope, so V's row stays active. A namespace-wide writer's deferred
    write of the same key does supersede it (the control)."""
    ns = f"rs-defer-{uuid.uuid4().hex[:8]}"
    ns_tok = _mint(seed.org, ns)
    w_tok = _mint(seed.org, ns, read_scope="own_user", read_user_id=W)
    base = {"org": seed.org, "namespace": ns, "agent": "a"}
    _write(
        control_plane_url,
        ns_tok,
        "namespace.register",
        {**base, "owner_email": "rs@example.com", "default_conflict_policy": "newest_wins"},
    )

    def say(badge: str) -> list[dict[str, str]]:
        # The fake extractor gives each of these the same key.
        return [{"role": "user", "content": f"Quux keeps the badge number {badge} today."}]

    v_out = _write(
        control_plane_url,
        ns_tok,
        "memory.set_from_messages",
        {**base, "user_id": V, "session_id": "sess-1", "messages": say("1111")},
    )
    assert v_out["written"], v_out
    v_key = v_out["written"][0]["memory_key"]

    deferred = _write(
        control_plane_url,
        w_tok,
        "memory.set_from_messages",
        {**base, "session_id": "sess-1", "messages": say("2222"), "defer": True},
    )
    assert deferred["deferred"] is True, deferred
    await _drain_queue(seed.org, ns)
    rows = _rows_by_user(ch_client, seed.org, ns)
    assert any(r[0] == v_key for r in rows.get(W, [])), f"W's replay wrote nothing: {rows}"
    assert rows[V] == [(v_key, "active", "")], f"W's deferred write changed V's row: {rows}"

    _write(
        control_plane_url,
        ns_tok,
        "memory.set_from_messages",
        {**base, "user_id": "user-x", "messages": say("3333"), "defer": True},
    )
    await _drain_queue(seed.org, ns)
    rows = _rows_by_user(ch_client, seed.org, ns)
    assert rows[V][0][1] == "superseded", f"the control superseded nothing: {rows}"


def _mark_duplicate_of(ch: Any, org: str, ns: str, memory_id: str, canonical_id: str) -> None:
    """A newer loom.memory version of the row as `duplicate` of `canonical_id`.
    Its memory_embeddings row stays `active`: the divergence older duplicates carry."""
    ch.command(
        """
        INSERT INTO loom.memory
        SELECT * REPLACE ('duplicate' AS status, {cid:String} AS duplicate_of,
                          version + 1 AS version)
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id = {mid:UUID}
        """,
        parameters={"org": org, "ns": ns, "mid": memory_id, "cid": canonical_id},
    )


@pytest.mark.parametrize(
    ("canon_user", "canon_session", "claims", "dup_session"),
    [
        (V, "sess-1", {"read_scope": "own_user", "read_user_id": U}, "sess-1"),
        (U, "sess-1", {"read_scope": "own_session", "read_user_id": U}, "sess-2"),
    ],
    ids=["cross_user", "cross_session"],
)
def test_a_duplicate_never_resolves_to_a_canonical_outside_the_scope(
    control_plane_url: str,
    seed: _Seed,
    ch_client: Any,
    canon_user: str,
    canon_session: str,
    claims: dict[str, str],
    dup_session: str,
) -> None:
    """U's duplicate names a canonical U's token may not read (V's row, or U's
    row in another session): memory.search and memory.get under that token show
    neither the canonical's key, id nor text."""
    ns = f"rs-dup-{uuid.uuid4().hex[:8]}"
    ns_tok = _mint(seed.org, ns)
    nonce = f"CANON{uuid.uuid4().hex[:10]}"
    base = {"org": seed.org, "namespace": ns, "agent": "a"}
    canon_key = f"canon-{uuid.uuid4().hex[:6]}"
    canon = _write(
        control_plane_url,
        ns_tok,
        "memory.set",
        {
            **base,
            "user_id": canon_user,
            "session_id": canon_session,
            "memory_key": canon_key,
            "value": f"the private canonical text {nonce}",
        },
    )
    dup = _write(
        control_plane_url,
        ns_tok,
        "memory.set",
        {
            **base,
            "user_id": U,
            "session_id": dup_session,
            "memory_key": "u-dup",
            "value": "Glimmerfox keeps the lantern in the attic",
        },
    )
    canon_id = str(canon["memory_id"])
    _mark_duplicate_of(ch_client, seed.org, ns, str(dup["memory_id"]), canon_id)
    get = {**base, "memory_key": "u-dup", "user_id": U, "session_id": dup_session}

    # Control: a namespace-wide read does resolve the duplicate to the canonical.
    whole = _write(control_plane_url, ns_tok, "memory.get", get)
    assert whole["canonical_key"] == canon_key and whole["duplicate_of"] == canon_id, whole

    tok = _mint(seed.org, ns, **claims)
    search = {**base, "session_id": dup_session, "query": "Glimmerfox lantern attic", "top_k": 10}

    def _u_dup_searched() -> bool:
        r = _post(control_plane_url, tok, "memory.search", {**search, "include_duplicates": True})
        return r.status_code == 200 and "u-dup" in r.text

    wait_until(_u_dup_searched, max_wait=30.0, description="U's duplicate to reach the pool")
    for op, body in (("memory.search", search), ("memory.get", get)):
        r = _post(control_plane_url, tok, op, body)
        assert r.status_code == 200, f"{op}: {r.status_code} {r.text}"
        for secret in (canon_key, canon_id, nonce):
            assert secret not in r.text, f"{op} named {secret}: {r.text}"


def _mark_superseded(
    ch: Any, org: str, ns: str, user: str, key: str, successor_id: str, bump: int
) -> None:
    """A newer version of the user's row under `key` (both tables) as `superseded`
    by `successor_id`; `bump` orders two users' versions of one key."""
    where = (
        "org = {org:String} AND namespace = {ns:String}"
        " AND user_id = {uid:String} AND memory_key = {key:String}"
    )
    params = {"org": org, "ns": ns, "uid": user, "key": key, "sid": successor_id, "b": bump}
    ch.command(
        f"""
        INSERT INTO loom.memory
        SELECT * REPLACE ('superseded' AS status, {{sid:String}} AS superseded_by,
                          version + {{b:UInt64}} AS version)
        FROM loom.memory FINAL WHERE {where}
        """,
        parameters=params,
    )
    ch.command(
        f"""
        INSERT INTO loom.memory_embeddings
        SELECT * REPLACE ('superseded' AS status, version + {{b:UInt64}} AS version)
        FROM loom.memory_embeddings FINAL WHERE {where}
        """,
        parameters=params,
    )


def test_a_superseded_hit_never_names_a_successor_outside_the_scope(
    control_plane_url: str, seed: _Seed, ch_client: Any
) -> None:
    """U's row superseded by V's row, and V's row superseded under a key U also
    holds superseded: a search pinned to U that asks for superseded rows names
    none of V's keys or ids."""
    ns = f"rs-sup-{uuid.uuid4().hex[:8]}"
    ns_tok = _mint(seed.org, ns)
    nonce = f"SUCC{uuid.uuid4().hex[:10]}"
    base = {"org": seed.org, "namespace": ns, "agent": "a", "session_id": "sess-1"}

    def put(user: str, key: str, value: str) -> str:
        body = {**base, "user_id": user, "memory_key": key, "value": value}
        return str(_write(control_plane_url, ns_tok, "memory.set", body)["memory_id"])

    v_succ_key, v_next_key = f"v-succ-{nonce}", f"v-next-{nonce}"
    v_succ = put(V, v_succ_key, f"V's successor text {nonce}")
    v_next = put(V, v_next_key, f"V's other successor {nonce}")
    u_succ = put(U, "u-succ", "U's own successor note")
    put(U, "u-old", "Quillbeak stores the compass in the cellar")
    put(U, "both", "Quillbeak hides the map in the barn")
    put(V, "both", f"Quillbeak V copy {nonce}")
    # U's row retired by V's; and under "both", V's retirement is the newer one.
    _mark_superseded(ch_client, seed.org, ns, U, "u-old", v_succ, 1)
    _mark_superseded(ch_client, seed.org, ns, U, "both", u_succ, 1)
    _mark_superseded(ch_client, seed.org, ns, V, "both", v_next, 10**12)

    search = {**base, "query": "Quillbeak compass map", "top_k": 10, "include_superseded": True}
    # Control: a namespace-wide read names V's successor on U's retired row.
    wait_until(
        lambda: v_succ_key in _post(control_plane_url, ns_tok, "memory.search", search).text,
        max_wait=30.0,
        description="the superseded rows to be searchable",
    )
    tok = _mint(seed.org, ns, read_scope="own_user", read_user_id=U)
    r = _post(control_plane_url, tok, "memory.search", search)
    assert r.status_code == 200, r.text
    assert "u-old" in r.text and '"both"' in r.text, r.text
    for secret in (nonce, v_succ, v_next):
        assert secret not in r.text, f"memory.search named {secret}: {r.text}"


def test_automatic_retirements_list_only_the_callers_users(
    control_plane_url: str, seed: _Seed
) -> None:
    """Under `newest_wins` V's second session rewrites V's key, and Loom retires the first
    row. memory.auto_retirements names the key to a token narrowed to V, never to one
    narrowed to U."""
    ns = f"rs-retire-{uuid.uuid4().hex[:8]}"
    ns_tok = _mint(seed.org, ns)
    base = {"org": seed.org, "namespace": ns, "agent": "a"}
    _write(
        control_plane_url,
        ns_tok,
        "namespace.register",
        {**base, "owner_email": "rs@example.com", "default_conflict_policy": "newest_wins"},
    )
    key = f"canary/retired-{seed.nonce_v}"
    for session, city in (("sess-1", "Paris"), ("sess-2", "Rome")):
        _write(
            control_plane_url,
            ns_tok,
            "memory.set",
            {**base, "user_id": V, "session_id": session, "memory_key": key, "value": city},
        )

    def keys(tok: str) -> list[str]:
        r = _post(control_plane_url, tok, "memory.auto_retirements", base)
        assert r.status_code == 200, r.text
        return [row["memory_key"] for row in r.json()["rows"]]

    v_tok = _mint(seed.org, ns, read_scope="own_user", read_user_id=V)
    u_tok = _mint(seed.org, ns, read_scope="own_user", read_user_id=U)
    wait_until(lambda: keys(v_tok) == [key], description="V's retirement is recorded")
    assert keys(u_tok) == []
