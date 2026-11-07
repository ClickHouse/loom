"""Restricted namespaces under the leakage protocol.

A code namespace is classified `restricted` in the directory and seeded
through the ingest path with a canary function whose name is a nonce. Then,
as the cross-principal suite does for memory namespaces, every read-shaped
`code.*` op is driven by principals the gate must refuse — a same-org token
that names the namespace but holds no grant, one that holds a grant but
does not name it, a wildcard admin with a grant, another org — against the
restricted namespace itself and through the cross-plane hop
(`memory.ask` with `code_namespace`). No response may carry the nonce and
every call ends in 200 (empty), 403 or 404. A positive control proves the
one principal who passes both factors reads the canary, so an empty result
cannot pass by accident; `code.repos` hides the namespace from the refused.

Needs a running server (LOOM_BASE_URL) with the code plane enabled and the
same ClickHouse the tests reach (LOOM_CH_*); skips otherwise.
"""

from __future__ import annotations

import asyncio
import json
import time
import typing
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

_OK_STATUSES = {200, 403, 404}
_ECHO_KEYS = {"query", "question", "pattern", "name", "path", "queries", "expanded_terms"}
_SKIP = {
    # writes / admin / feedback — the writer gate is covered by test_grants
    "code.refresh",
    "code.scip_ingest",
    "code.reindex",
    "code.calibrate",
    "code.outcome",
    # Adding a repository / branch is a write (org-level or
    # ADMIN-gated); nothing of a restricted index can come back from it
    "code.repo.add",
    "code.repo.add_branch",
    # Asks GitHub (git ls-remote) about a repository named in
    # the body and reads no loom table — nothing of a restricted index can
    # come back from it, and the test server has no GitHub to ask
    "code.repo.remote_branches",
}
_CORE = {
    "code.search",
    "code.grep",
    "code.ask",
    "code.symbol",
    "code.context_pack",
    "code.repos",
    "code.outline",
    "code.enclosing",
    "code.callers",
    "code.history",
}


def _post(base: str, token: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    """One call; a 429 (the per-namespace `code` bucket is small on a test
    server and a refused request still spends it) waits `retry_after_s` and
    retries a few times — the suite measures leaks, not throughput."""
    with httpx.Client(base_url=base, timeout=60.0) as c:
        for _ in range(8):
            r = c.post(f"/v1/{op}", json=payload, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 429:
                return r
            try:
                wait = float(r.json().get("detail", {}).get("retry_after_s", 1))
            except (ValueError, AttributeError):
                wait = 1.0
            time.sleep(min(max(wait, 0.2), 5.0))
        return r


def _leaked(nonce: str, text: str) -> bool:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return nonce.lower() in text.lower()
    hit = False

    def walk(node: Any, key: str = "") -> None:
        nonlocal hit
        if key in _ECHO_KEYS:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, str) and nonce.lower() in node.lower():
            hit = True

    walk(data)
    return hit


class _Seed:
    def __init__(self) -> None:
        self.org = f"rleak-{uuid.uuid4().hex[:6]}"
        self.ns_code = f"code-priv-{uuid.uuid4().hex[:6]}"
        self.ns_mem = f"mem-{uuid.uuid4().hex[:6]}"
        self.nonce = f"zqcanary{uuid.uuid4().hex[:10]}"
        self.path = "src/Canary.cpp"
        self.symbol = f"DB::Canary::{self.nonce}"


_SEED: dict[str, _Seed] = {}


def _code_intel_enabled(base: str, token: str, org: str, ns: str) -> bool:
    r = _post(base, token, "code.index_status", {"org": org, "namespace": ns})
    return r.status_code != 412


@pytest.fixture
def seed(control_plane_url: str, ch_client, mint_test_token) -> _Seed:
    if control_plane_url in _SEED:
        return _SEED[control_plane_url]
    s = _Seed()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    # The directory row: kind=code, restricted, private. Written before any
    # request names the org, so the server's first snapshot for it already
    # carries the restriction.
    ch_client.insert(
        "loom.namespaces",
        [
            [
                s.org,
                s.ns_code,
                "ops@canary.invalid",
                "restricted canary",
                ["code"],
                True,
                "code",
                "private",
                "restricted",
                now,
                now,
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "owner_email",
            "description",
            "tags",
            "active",
            "kind",
            "visibility",
            "classification",
            "created_at",
            "updated_at",
        ],
    )
    # Direct grants: `granted` names the namespace AND holds one; `grant_only`
    # holds one but its token will not name the namespace; `wild` is an admin
    # with a grant whose token says `*`.
    rows = []
    for subj, role in (
        ("granted@canary.invalid", "viewer"),
        ("grantonly@canary.invalid", "admin"),
        ("wild@canary.invalid", "admin"),
    ):
        rows.append([s.org, s.ns_code, subj, role, now, "ops@canary.invalid", None, 1, "active"])
    ch_client.insert(
        "loom.namespace_grants",
        rows,
        column_names=[
            "org",
            "namespace",
            "subject",
            "role",
            "granted_at",
            "granted_by",
            "revoked_at",
            "version",
            "status",
        ],
    )
    # The canary through the ingest path (chunks, symbols, files).
    source = (
        "namespace DB {\nclass Canary {\npublic:\n"
        f"    /// {s.nonce} guards the launch code.\n"
        f"    void {s.nonce}() {{ helper_{s.nonce}(); }}\n"
        f"    void helper_{s.nonce}() {{ }}\n"
        "};\n}\n"
    ).encode()

    async def _ingest() -> None:
        from server.code.app.index.ingest import ingest_file
        from server.code.store.index import CodeRepo
        from server.core.ch import get_client
        from server.embedding.fake import FakeNGramEmbedder
        from server.settings import settings

        await get_client().connect()
        stats = await ingest_file(
            CodeRepo(),
            s.org,
            s.ns_code,
            path=s.path,
            source=source,
            commit_sha="a" * 40,
            embedding_model="fake-ngram-1024",
            embedder=FakeNGramEmbedder(dim=settings.embedding_dim),
        )
        assert stats.chunks_new > 0, stats

    asyncio.run(_ingest())
    probe = mint_test_token(
        org=s.org,
        namespaces=[s.ns_code, s.ns_mem],
        roles=["viewer"],
        subject="granted@canary.invalid",
    )
    if not _code_intel_enabled(control_plane_url, probe, s.org, s.ns_code):
        pytest.skip("code plane disabled on this server (LOOM_CODE_INTEL_ENABLED=false)")
    _SEED[control_plane_url] = s
    return s


@pytest.fixture
def tokens(seed: _Seed, mint_test_token) -> dict[str, str]:
    m = mint_test_token
    return {
        "granted": m(
            org=seed.org,
            namespaces=[seed.ns_code, seed.ns_mem],
            roles=["viewer"],
            subject="granted@canary.invalid",
        ),
        "claim_only": m(
            org=seed.org,
            namespaces=[seed.ns_code, seed.ns_mem],
            roles=["admin", "writer"],
            subject="claimonly@canary.invalid",
        ),
        "grant_only": m(
            org=seed.org,
            namespaces=[seed.ns_mem],
            roles=["admin", "writer"],
            subject="grantonly@canary.invalid",
        ),
        "wild": m(
            org=seed.org, namespaces=["*"], roles=["admin", "writer"], subject="wild@canary.invalid"
        ),
        "other_org": m(
            org=f"{seed.org}-other",
            namespaces=["*"],
            roles=["admin", "writer"],
            subject="mallory@canary.invalid",
        ),
    }


def _read_ops() -> list[Any]:
    from server.operations.registry import ALL_OPERATIONS

    return [op for op in ALL_OPERATIONS if op.name.startswith("code.") and op.name not in _SKIP]


def _hints(seed: _Seed) -> dict[str, Any]:
    return {
        "org": seed.org,
        "namespace": seed.ns_code,
        "agent": "a",
        "query": seed.nonce,
        "question": f"what does {seed.nonce} do?",
        "pattern": seed.nonce,
        "name": seed.symbol,
        "symbol_id": "",
        "path": seed.path,
        "paths": [seed.path],
        "files": [seed.path],
        "numbers": [],
        "pr_number": 0,
        "line": 5,
        "locations": [f"{seed.path}:5"],
        "frames": [f"{seed.symbol}"],
        "trace": seed.symbol,
        "claims": [],
        "text": seed.nonce,
        "consumer": "leak-suite",
        # code.tests_for needs at least one facet; the nonce
        # as a setting name must come back as no rows, never as a leak
        "facets": [{"kind": "setting", "name": seed.nonce}],
    }


_IDENTITY_KEYS = {"org", "namespace", "agent", "consumer"}


def _payload(model: type, seed: _Seed, *, identity_only: bool = False) -> dict[str, Any] | None:
    """The op's payload from the hints table (a subject per field name) and
    the fields the model itself requires, filled by type. `identity_only`
    drops the hint of every OPTIONAL field — the subjects a "one of" rule
    chooses among — and keeps identity and the fields the model requires
    (a required `pattern` or a `min_length=1` list is a per-field rule the
    suite has always satisfied): the argument-less call a caller who knows
    nothing sends. With every hint filled, a model-level "one of" rule
    (`code.symbol_history`'s "name or symbol_id is required") would never
    fire here while it answers a 422 before the gate on the wire."""
    hints = _hints(seed)
    out: dict[str, Any] = {}
    for fname, field in model.model_fields.items():
        if fname in hints and (not identity_only or fname in _IDENTITY_KEYS or field.is_required()):
            out[fname] = hints[fname]
            continue
        if not field.is_required():
            continue
        ann = field.annotation
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)
        if ann is str:
            out[fname] = seed.nonce
        elif ann is int:
            out[fname] = 1
        elif ann is bool:
            out[fname] = False
        elif origin in (list, tuple) or ann is list:
            out[fname] = []
        elif origin is dict or ann is dict:
            out[fname] = {}
        elif origin is typing.Literal and args:
            out[fname] = args[0]
        else:
            return None
    return out


def test_positive_control_the_two_factor_principal_reads_the_canary(
    control_plane_url, seed, tokens
) -> None:
    r = _post(
        control_plane_url,
        tokens["granted"],
        "code.grep",
        {"org": seed.org, "namespace": seed.ns_code, "pattern": seed.nonce},
    )
    assert r.status_code == 200, r.text
    assert _leaked(seed.nonce, r.text), "the granted principal must read its canary"
    r = _post(
        control_plane_url,
        tokens["granted"],
        "code.search",
        {"org": seed.org, "namespace": seed.ns_code, "query": seed.nonce},
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("who", ["claim_only", "grant_only", "wild", "other_org"])
def test_every_code_read_op_leaks_nothing_to_a_refused_principal(
    control_plane_url, seed, tokens, who
) -> None:
    exercised: set[str] = set()
    failures: list[str] = []
    for op in _read_ops():
        # two shapes per op: the subject-bearing call and the argument-less
        # one — a model-level "one of" rule answers the
        # second with a 422 before the gate, which this suite rejects
        for identity_only in (False, True):
            payload = _payload(op.input_model, seed, identity_only=identity_only)
            if payload is None:
                failures.append(f"{op.name}: no payload could be built — add a hint")
                break
            r = _post(control_plane_url, tokens[who], op.name, payload)
            exercised.add(op.name)
            shape = "identity-only" if identity_only else "subject"
            if r.status_code not in _OK_STATUSES:
                failures.append(f"{op.name} ({shape}): status {r.status_code} {r.text[:160]}")
                continue
            if _leaked(seed.nonce, r.text):
                failures.append(f"{op.name} ({shape}): nonce in body {r.text[:160]}")
    assert not failures, "\n".join(failures)
    missing = _CORE - exercised
    assert not missing, f"core code surfaces not exercised: {sorted(missing)}"
    registered = {op.name for op in _read_ops()}
    assert exercised == registered, (
        f"registered code reads not exercised: {sorted(registered - exercised)}"
    )


@pytest.mark.parametrize("who", ["claim_only", "grant_only", "wild"])
def test_code_repos_hides_the_restricted_namespace(control_plane_url, seed, tokens, who) -> None:
    """`code.repos` lists configured entries; a seeded namespace is not one,
    so this asserts only that the listing never names it and never errors
    (the configured-entry case is unit-tested in
    tests/unit/test_code_repos_hides_restricted.py)."""
    r = _post(
        control_plane_url, tokens[who], "code.repos", {"org": seed.org, "namespace": seed.ns_code}
    )
    assert r.status_code in _OK_STATUSES, r.text
    if r.status_code == 200:
        assert seed.ns_code not in r.text
    else:
        # a refusal names the namespace the caller itself addressed — an echo,
        # not a leak — and nothing else
        assert seed.nonce not in r.text


@pytest.mark.parametrize("who", ["claim_only", "grant_only", "wild"])
def test_memory_ask_cannot_reach_the_restricted_code_index(
    control_plane_url, seed, tokens, who
) -> None:
    """The cross-plane hop authorizes the current principal: a
    memory.ask in a namespace the token holds, pointed at the restricted
    code namespace, grounds nothing from it."""
    r = _post(
        control_plane_url,
        tokens[who],
        "memory.ask",
        {
            "org": seed.org,
            "namespace": seed.ns_mem,
            "agent": "a",
            "query": f"what does {seed.nonce} do?",
            "code_namespace": seed.ns_code,
            "code_org": seed.org,
        },
    )
    assert r.status_code in _OK_STATUSES, r.text
    assert not _leaked(seed.nonce, r.text), r.text[:300]


def test_code_ask_cache_does_not_carry_across_principals(control_plane_url, seed, tokens) -> None:
    q = {"org": seed.org, "namespace": seed.ns_code, "question": f"what does {seed.nonce} do?"}
    r = _post(control_plane_url, tokens["granted"], "code.ask", q)
    assert r.status_code == 200, r.text
    r = _post(control_plane_url, tokens["claim_only"], "code.ask", q)
    assert r.status_code in _OK_STATUSES and not _leaked(seed.nonce, r.text), r.text[:300]


def test_error_bodies_do_not_echo_the_canary(control_plane_url, seed, tokens) -> None:
    r = _post(
        control_plane_url,
        tokens["claim_only"],
        "code.symbol",
        {"org": seed.org, "namespace": seed.ns_code, "name": seed.symbol},
    )
    assert r.status_code in (403, 404), r.text
    assert seed.nonce not in r.text


def test_restricted_read_leaves_an_audit_row(control_plane_url, seed, tokens, ch_client) -> None:
    _post(
        control_plane_url,
        tokens["granted"],
        "code.grep",
        {"org": seed.org, "namespace": seed.ns_code, "pattern": seed.nonce},
    )
    rows = None
    for _ in range(20):
        rows = ch_client.query(
            "SELECT count() FROM loom.audit_log WHERE org = %(org)s AND namespace = %(ns)s "
            "AND event_type = 'namespace.restricted_access'",
            parameters={"org": seed.org, "ns": seed.ns_code},
        ).result_rows
        if rows and rows[0][0] > 0:
            break
        time.sleep(0.5)
    assert rows and rows[0][0] > 0, "no namespace.restricted_access row for the admitted read"
