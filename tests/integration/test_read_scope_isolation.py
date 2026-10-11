"""Read scope inside one namespace: a token pinned to user U never sees user V.

Users U and V write into the SAME namespace: facts (one key both hold, the
rest V's alone, across two sessions), raw turns through set_from_messages
(the same session id for both), an entity both rows name, a profile section, a session brief, a working-memory block and a procedure. V's rows
carry a nonce. A token minted with `read_user_id=U` then drives every op the
registry holds:

* an op in `SCOPED_OPS` answers, and no answer carries V's nonce;
* the same op asked for `user_id="*"` or `user_id=V` is refused (403);
* every other op that does not return memory-free data is refused (403), so
  the entity graph, conversations and exports never answer such a token;
* every write a bounded token may call, aimed at V's keys, ids and brief by a
  token pinned to a third user W, answers without V's nonce and leaves V's
  rows as they were.

An op the walks cannot build a body for fails the test unless `_UNBUILDABLE`
names it with the reason.

A positive control proves U's token finds U's own canary and a
namespace-scoped token finds V's, so an empty answer cannot pass by accident.
The cache case asks the same question as V, then as U: neither the precedent
store nor the LLM response cache may hand V's evidence to U.

Skips without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import typing
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

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
                "value": f"{s.entity} keeps the vault code {nonce} in drawer {i}",
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
            "value": f"the shared key holds {nonce}",
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
    )
    writer = _mint(org, s.ns)
    _seed_user(control_plane_url, writer, s, U, s.nonce_u)
    s.memory_ids_v = _seed_user(control_plane_url, writer, s, V, s.nonce_v)
    s.keys_v = [f"canary/{V}/0", f"canary/{V}/1", "canary/shared"]

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
        "memory_key": seed.keys_v[0],
        "memory_keys": seed.keys_v,
        "memory_id": seed.memory_ids_v[0],
        "memory_ids": seed.memory_ids_v,
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
        "messages": [{"role": "user", "content": "canary"}],
        "entries": [{"memory_key": seed.keys_v[0], "value": "canary"}],
    }
    out: dict[str, Any] = {}
    fields = model.model_fields
    for name, f in fields.items():
        if name == "memory_keys" and "memory_key" in fields:
            continue  # an op taking either names one key
        if name in hints:
            out[name] = hints[name]
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
    NO_MEMORY_CONTENT names as a write. A group key (`code.`) is not walked: its writes
    reach no memory row."""
    from server.identity.domain.read_scope import NO_MEMORY_CONTENT, SCOPED_OPS
    from server.operations.registry import OPERATIONS_BY_NAME

    names = {n for n, why in NO_MEMORY_CONTENT.items() if why.startswith("a write")}
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
        if r.status_code not in (403, 422):
            served[op.name] = r.status_code
    assert not served, f"served to a token bounded to one user: {served}"
    assert set(unbuildable) <= set(_UNBUILDABLE), unbuildable


def test_writes_aimed_at_vs_rows_never_answer_or_change_them(
    control_plane_url: str, seed: _Seed, tokens: dict[str, str]
) -> None:
    """A token pinned to W (who holds nothing) sends every write a bounded token may
    call at V's keys, ids and brief. No answer carries V's nonce, a refusal is a 409 or
    a 423 (W's own lock) at most, and V's rows read back unchanged afterwards."""
    leaks: dict[str, str] = {}
    bad: dict[str, int] = {}
    unbuildable: list[str] = []
    walked: set[str] = set()
    for op in _bounded_writes():
        body = _payload(op.input_model, seed, "-")
        if body is None:
            unbuildable.append(op.name)
            continue
        r = _post(control_plane_url, tokens["pinned_w"], op.name, body)
        if r.status_code == 422:
            unbuildable.append(op.name)
            continue
        walked.add(op.name)
        if r.status_code not in {200, 404, 409, 423}:  # 409/423: a refused write
            bad[op.name] = r.status_code
        if seed.nonce_v in r.text:
            leaks[op.name] = r.text[:300]
    assert not leaks, f"V's canary reached W's token: {leaks}"
    assert not bad, f"unexpected statuses: {bad}"
    assert set(unbuildable) <= set(_UNBUILDABLE), unbuildable
    assert {"session.brief_save", "session.brief_delete", "memory.set"} <= walked
    ident = {"org": seed.org, "namespace": seed.ns, "agent": "*", "user_id": "*"}
    for op, body in [
        ("memory.list", {**ident, "session_id": "*"}),
        ("session.brief_get", {**ident, "brief_key": f"{seed.brief_key}-{V}"}),
    ]:
        r = _post(control_plane_url, tokens["namespace"], op, body)
        assert r.status_code == 200 and seed.nonce_v in r.text, f"{op}: {r.text[:300]}"


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
