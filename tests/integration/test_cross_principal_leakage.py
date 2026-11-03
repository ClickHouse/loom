"""Cross-principal leakage suite.

Tenant A writes canary memories carrying unique nonces into namespace A;
every read-shaped operation loom exposes is then driven as tenant B (a
writer and a viewer scoped to namespace B in the same org) and as a
principal from another org, against namespace A directly and against B's
own namespace with the canary text as the query. No response body may
contain a nonce, and every call must end in 200 (empty), 403 or 404 —
never 500. A positive control proves tenant A itself finds the canary, so
an empty result set cannot pass by accident.

The probed set is every `read_only` operation in the registry plus the
read-shaped ops of the planes in `_GROUPS`; a read-only op is exempt only by
name in `_UNPROBED_READS` with its reason, so a new read surface is probed
the day it is registered. Payloads are synthesised from each operation's
Pydantic input model; an op whose body cannot be synthesised is reported,
and a core list of surfaces must always be exercised. The cross-principal leakage rate under this scripted query
suite is a CI gate: it is asserted to be zero.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
import typing
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration._eventually import ACL_OPS, acl_change_adopted, wait_until
from tests.integration._resources import unique_org

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Ops that mutate on behalf of the caller's own namespace or are not reads;
# probing them as B against A only tests the writer gate, which test_grants
# already covers, and some (namespace.register) would create rows.
_SKIP_OPS = {
    "namespace.register",
    "namespace.join",
    "namespace.leave",
    "namespace.restore",
    "namespace.mark_seen",
    "namespace.cancel_request",
    "namespace.register_category",
    "namespace.register_ontology",
    "namespace.delete",
    "namespace.rollback",
    "memory.consolidate",
    "memory.reindex",
    "memory.import_curated",
    "memory.import",
    "memory.copy_to",
    "memory.cross_promote",
    "memory.set_from_messages",
    "memory.bulk_set",
    "memory.set",
    "memory.update_by_id",
    "memory.delete",
    "memory.clear",
    "memory.restore",
    "memory.pin",
    "memory.touch",
    "memory.verify",
    "memory.redact",
    "memory.link",
    "memory.promote",
    "memory.demote",
    "memory.reject",
    "memory.outcome",
    "memory.outcome_run",
    "memory.assign_category",
    "memory.dedup_resolve",
    "memory.resolve_orphan_duplicates",
    "memory.record.write",
    "knowledge.add",
    "knowledge.bulk_add",
    "knowledge.upsert",
    "knowledge.delete",
    "knowledge.bulk_delete",
    "knowledge.update_metadata",
    "knowledge.dedup_resolve",
    "procedure.add",
    "procedure.record_run",
    "procedure.evolve",
    "procedure.revise",
    "procedure.fork",
    "conflicts.resolve",
    "conflicts.allowlist_remove",
    "facts.expire_by_adapter",
    # Creates an erasure saga in the caller's own org; the seed drives one
    # deliberately (below) and erasure.status / erasure.list probe it.
    "erasure.request",
    # The writes of the eight planes joined below. The
    # seed drives one of each as tenant A; probing them as B tests the writer
    # gate test_grants covers, and some (conversation.delete, policy.delete)
    # would remove the canary the reads are probed against.
    "conversation.create",
    "conversation.append",
    "conversation.update",
    "conversation.compact",
    "conversation.delete",
    "conversation.set_logging",
    "run.begin",
    "run.complete",
    "working_memory.set",
    "working_memory.append",
    "working_memory.replace",
    "working_memory.delete",
    "learnings.review",
    "policy.set",
    "policy.delete",
    # The session brief plane's writes (the seed saves one
    # brief as tenant A; a delete would remove the canary).
    "session.brief_save",
    "session.brief_delete",
    # The code plane's writes.
    "code.refresh",
    "code.reindex",
    "code.scip_ingest",
    "code.outcome",
    "code.calibrate",
}
# Code-plane reads this suite does not probe as tenant B: the LLM-synthesising
# op and the ops that resolve to the deployment's code namespace whatever the
# body names (code.* is exempt from the identity fill) — a probe against
# "namespace A" is a probe of the code plane, which the org gate covers.
# `code.search` and `code.grep` take the namespace the body names and
# are probed. Kept apart from `_SKIP_OPS`, which other suites import as the
# list of ops that are not reads (test_read_ops_never_5xx walks these).
_CODE_PLANE_UNPROBED = {
    "code.scip_status",
    "code.ask",
    "code.callers",
    "code.callees",
    "code.context_pack",
    "code.context_packs",
    "code.coverage",
    "code.enclosing",
    "code.examples",
    "code.history",
    "code.impact",
    "code.index_errors",
    "code.index_status",
    "code.map",
    "code.outline",
    "code.recent_commits",
    "code.repos",
    "code.symbol",
    "code.test_gate",
    "code.trace",
    "code.verify",
}
# Read-only ops outside the code plane that the walk does not drive, each with
# the reason; test_every_read_only_op_is_probed_or_exempt holds this closed.
_UNPROBED_READS: dict[str, str] = {
    # Connects to the host, port and credentials named in the body; a probe
    # would be an outbound connection attempt, not a read of tenant rows.
    "tracker.source.inspect": "dials a caller-named external host",
}
# blacklist / erasure / grants: each is seeded with a canary and probed as
# tenant B. The id-addressed ops (blacklist.remove / sweep, erasure.status)
# are the second-gate surfaces.
# conversation / run / working_memory / learnings / policy / audit /
# consolidations / code: the read-shaped ops that carry tenant text (raw
# turns, a run's goal, a scratchpad, a policy's description, the audit
# subjects).
_GROUPS = (
    "memory.",
    "facts.",
    "knowledge.",
    "namespace.",
    "procedure.",
    "conflicts.",
    "blacklist.",
    "erasure.",
    "grants.",
    "conversation.",
    "run.",
    "working_memory.",
    "learnings.",
    "policy.",
    "audit.",
    "consolidations.",
    "code.",
    # A session brief carries a session's goal, decisions and paths
    "session.",
)
# The end user tenant A's saved-message canary belongs to.
_SAVED_USER = "saved-canary-user"
# Surfaces the suite must exercise with a well-formed body (no 422) or fail.
_CORE = {
    "memory.search",
    "memory.ask",
    "memory.get",
    "memory.get_by_id",
    "memory.list",
    "memory.bulk_get",
    "memory.history",
    "memory.provenance",
    "memory.timeline",
    "memory.related",
    "memory.search_trace",
    "memory.lineage",
    "memory.inspect",
    "facts.list",
    "facts.traverse",
    "knowledge.search",
    "namespace.snapshot",
    "namespace.get",
    "namespace.summary",
    "memory.record.query",
    "blacklist.list",
    "blacklist.remove",
    "erasure.status",
    "erasure.list",
    "grants.list",
    "conversation.get",
    "conversation.list",
    "conversation.search",
    "conversation.search_saved_messages",
    "conversation.saved_status",
    "conversation.threads",
    "run.get",
    "run.list",
    "working_memory.get",
    "working_memory.render",
    "learnings.list",
    "policy.list",
    "policy.decisions",
    "audit.list",
    "consolidations.list",
    "code.search",
    "code.grep",
    "session.brief_get",
    "session.brief_list",
    "task.get",
    "task.list",
    "task.history",
    "inbox.list",
    "profile.render",
    "context.fetch",
    "decision.precedents",
    "memory_schema.list",
}
_OK_STATUSES = {200, 403, 404}
# Response fields that echo the request's own query text; not a leak.
_ECHO_KEYS = {
    "query",
    "question",
    "queries",
    "embedded_queries",
    "rewritten_query",
    "sub_queries",
    "original_query",
    "query_text",
    "seed_entity",
    "entity",
    # memory.recall tokenises the caller's own query into the response
    "query_tokens",
}


def _canary() -> str:
    return f"zqcanary{uuid.uuid4().hex[:10]}"


class _Seed:
    def __init__(self, org: str, ns_a: str, ns_b: str) -> None:
        self.org, self.ns_a, self.ns_b = org, ns_a, ns_b
        self.nonces: list[str] = []
        self.keys: list[str] = []
        self.memory_ids: list[str] = []
        self.entity = ""
        self.value_text = ""
        # Canary rows in the blacklist / erasure / grants planes (empty when the seed
        # could not write one; the payload hints then fall back to random ids).
        self.pattern_id = ""
        self.erasure_id = ""
        self.ns_c = ""  # the throwaway namespace the erasure canary targets
        # Canaries in the conversation, run, working-memory and policy planes.
        self.thread_id = ""
        self.run_id = ""
        self.policy_id = ""
        # A session brief canary.
        self.brief_key = ""
        # The conversation turn's nonce, which the adversarial conversation
        # search queries with.
        self.conversation_nonce = ""
        # Canaries in the task, profile, offload, decision and schema planes.
        self.inbox = ""
        self.task_id = ""
        self.blob_key = ""
        # The saved-message canary's conversation and message.
        self.saved_conversation_id = ""
        self.saved_message_id = ""

    def leaked(self, text: str) -> list[str]:
        """Nonces present anywhere in a response body EXCEPT in fields that
        echo the caller's own query — an echo is not a leak, a hit is."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            low = text.lower()
            return [n for n in self.nonces if n in low]
        found: set[str] = set()

        def walk(node: Any, key: str = "") -> None:
            if key in _ECHO_KEYS:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, str(k))
            elif isinstance(node, list):
                for v in node:
                    walk(v, key)
            elif isinstance(node, str):
                low = node.lower()
                found.update(n for n in self.nonces if n in low)

        walk(data)
        return sorted(found)


def _post(base: str, token: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    with httpx.Client(base_url=base, timeout=30.0) as c:
        return c.post(f"/v1/{op}", json=payload, headers={"Authorization": f"Bearer {token}"})


def _synth_payload(model: type, *, seed: _Seed, namespace: str) -> dict[str, Any] | None:
    """Minimal body for an input model, pointing every reference at tenant
    A's data. None when a required field cannot be synthesised."""
    hints: dict[str, Any] = {
        "org": seed.org,
        "namespace": namespace,
        "agent": "a",
        "target_namespace": namespace,
        "source_namespace": seed.ns_a,
        "memory_key": seed.keys[0],
        "memory_keys": seed.keys[:2],
        "memory_id": seed.memory_ids[0],
        "memory_ids": seed.memory_ids[:2],
        "query": seed.value_text,
        "question": seed.value_text,
        "text": seed.value_text,
        "seed_entity": seed.entity,
        "entity": seed.entity,
        "subject": seed.entity,
        "entities": [seed.entity],
        "prefix": "canary",
        "memory_key_prefix": "canary",
        "schema_name": "canary_schema",
        "record_id": str(uuid.uuid4()),
        "key": seed.keys[0],
        "name": "canary",
        "tag": "canary",
        "tags": ["canary"],
        "from_memory_key": seed.keys[0],
        "to_memory_key": seed.keys[1],
        "erasure_id": seed.erasure_id or str(uuid.uuid4()),
        "file": "canary.py",
        "path": "canary.py",
        "session_id": "-",
        "user_id": "-",
        "pattern_id": seed.pattern_id or str(uuid.uuid4()),
        "from_agent": "a",
        "scope_value": seed.ns_c or namespace,
        "requester": "alice",
        # Point the conversation / run / working-memory / policy reads at tenant A's canaries
        "thread_id": seed.thread_id or "canary-thread",
        "run_id": seed.run_id or "run-canary",
        "policy_id": seed.policy_id or "canary-policy",
        "block_key": "scratchpad",
        "block_keys": ["scratchpad"],
        "operation": "memory.set",
        "pattern": seed.nonces[0] if seed.nonces else "canary",
        "candidate_id": str(uuid.uuid4()),
        "queue_ids": [uuid.uuid4().hex],
        "inbox": seed.inbox or "canary-inbox",
        "task_id": seed.task_id or str(uuid.uuid4()),
        "blob_key": seed.blob_key or "canary-blob",
        "label": "canary",
        "conversation_id": seed.saved_conversation_id or "canary",
        "message_ids": [seed.saved_message_id or "m1"],
    }
    out: dict[str, Any] = {}
    for fname, field in model.model_fields.items():
        literal = typing.get_args(field.annotation)
        # A hint that is not one of a Literal field's values would only be refused.
        if fname in hints and (
            typing.get_origin(field.annotation) is not typing.Literal or hints[fname] in literal
        ):
            out[fname] = hints[fname]
            continue
        if not field.is_required():
            continue
        ann = field.annotation
        origin = typing.get_origin(ann)
        args = typing.get_args(ann)
        if ann is str:
            out[fname] = "canary"
        elif ann is int:
            out[fname] = 1
        elif ann is float:
            out[fname] = 1.0
        elif ann is bool:
            out[fname] = False
        elif origin in (list, tuple) or ann is list:
            out[fname] = []
        elif origin is dict or ann is dict:
            out[fname] = {}
        elif origin is typing.Literal and args:
            out[fname] = args[0]
        elif origin is typing.Union or str(origin) == "types.UnionType":
            non_none = [a for a in args if a is not type(None)]
            if str in non_none:
                out[fname] = "canary"
            elif non_none and typing.get_origin(non_none[0]) is typing.Literal:
                out[fname] = typing.get_args(non_none[0])[0]
            else:
                return None
        else:
            return None
    return out


def _probe_bodies(payload: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    """The walk's body, plus one naming the saved canary's owner when the body
    names no user, so only the gate stands between a caller and the canary."""
    if payload.get("user_id") == "-":
        return [payload, {**payload, "user_id": owner}]
    return [payload]


def _leak_in(seed: _Seed, op_name: str, response: httpx.Response) -> list[str]:
    """Canary nonces in a response. saved_status answers ids, not text, so any
    saved id it returns is the canary owner's."""
    found = seed.leaked(response.text)
    if op_name == "conversation.saved_status" and response.status_code == 200:
        found = found or response.json()["saved"]
    return found


def _read_ops() -> list[Any]:
    from server.operations.registry import ALL_OPERATIONS

    return [
        op
        for op in ALL_OPERATIONS
        if (op.read_only or op.name.startswith(_GROUPS))
        and op.name not in _SKIP_OPS
        and op.name not in _CODE_PLANE_UNPROBED
        and op.name not in _UNPROBED_READS
    ]


def test_every_read_only_op_is_probed_or_exempt() -> None:
    """The walk covers the registry, not a hand list: every read-only op is
    probed unless an exemption names it, and no exemption goes stale."""
    from server.operations.registry import ALL_OPERATIONS

    registered = {op.name for op in ALL_OPERATIONS}
    probed = {op.name for op in _read_ops()}
    exempt = _CODE_PLANE_UNPROBED | set(_UNPROBED_READS)
    unaccounted = sorted(
        op.name for op in ALL_OPERATIONS if op.read_only and op.name not in probed | exempt
    )
    assert not unaccounted, f"read-only ops neither probed nor exempt: {unaccounted}"
    stale = sorted((exempt | _SKIP_OPS | _CORE) - registered)
    assert not stale, f"leakage lists name ops the registry no longer has: {stale}"
    assert _CORE <= probed, f"core surfaces missing from the walk: {sorted(_CORE - probed)}"


_SEED_CACHE: dict[str, _Seed] = {}


@pytest.fixture
def seed(control_plane_url: str, mint_test_token) -> _Seed:
    # mint_test_token is function-scoped; seed once per server and reuse.
    if control_plane_url in _SEED_CACHE:
        return _SEED_CACHE[control_plane_url]
    org = f"leak-{uuid.uuid4().hex[:6]}"
    s = _Seed(org, f"tenant-a-{uuid.uuid4().hex[:6]}", f"tenant-b-{uuid.uuid4().hex[:6]}")
    s.ns_c = f"tenant-c-{uuid.uuid4().hex[:6]}"
    tok_a = mint_test_token(
        org=org, namespaces=[s.ns_a, s.ns_c], roles=["admin", "writer"], subject="alice"
    )
    # The walk drives every read op of the eighteen groups as three principals
    # against namespace A within seconds — 44 of the 114 probed ops share the
    # `default` bucket (burst 50, 10/s per namespace; the eight tenant-text planes are
    # mostly `default`-class) and 42 the `memory` bucket — so the seed raises
    # the caps on its own throwaway namespaces through the operator's override
    # (`admin/ratelimit.set`, as test_ratelimit_overrides does). A 429 is the
    # probe's own pace, not a verdict on the read path; the deployment's
    # defaults are untouched and every other namespace pays what it paid.
    # The override is deployment-wide, so it takes a platform admin.
    tok_platform = mint_test_token(org="*", roles=["platform_admin"], subject="leak-operator")
    for ns in (s.ns_a, s.ns_b, s.ns_c):
        for op_class in ("default", "memory"):
            r = httpx.post(
                f"{control_plane_url}/v1/admin/ratelimit.set",
                json={"namespace": ns, "op_class": op_class, "capacity": 2000, "rate": 200},
                headers={"Authorization": f"Bearer {tok_platform}"},
                timeout=30.0,
            )
            assert r.status_code == 200, r.text
    s.entity = f"Project {_canary()}"
    s.nonces.append(s.entity.split()[-1])
    for i in range(3):
        n = _canary()
        s.nonces.append(n)
        key = f"canary_{i}_{uuid.uuid4().hex[:4]}"
        value = f"The launch code for {s.entity} is {n}; do not share it."
        if i == 0:
            s.value_text = value
        r = _post(
            control_plane_url,
            tok_a,
            "memory.set",
            {
                "org": org,
                "namespace": s.ns_a,
                "agent": "a",
                "memory_key": key,
                "value": value,
                "memory_type": "semantic",
                "entities": [s.entity],
                "tags": ["canary"],
            },
        )
        assert r.status_code == 200, r.text
        s.keys.append(key)
        s.memory_ids.append(str(r.json().get("memory_id") or ""))
    # A typed triple so the facts plane carries a nonce too.
    r = _post(
        control_plane_url,
        tok_a,
        "memory.bulk_set",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "entries": [
                {
                    "memory_key": f"canary_rel_{uuid.uuid4().hex[:4]}",
                    "value": f"{s.entity} works_at {s.nonces[1]}",
                    "memory_type": "semantic",
                    "relations": [[s.entity, "works_at", s.nonces[1]]],
                    "entities": [s.entity, s.nonces[1]],
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    # Knowledge plane too.
    r = _post(
        control_plane_url,
        tok_a,
        "knowledge.add",
        {
            "org": org,
            "namespace": s.ns_a,
            "content": f"Runbook: the rotation secret is {s.nonces[2]}.",
            "source_uri": f"canary://{uuid.uuid4().hex[:6]}",
            "title": "canary runbook",
        },
    )
    assert r.status_code in (200, 422), r.text  # 422 only if the body shape moved
    # Blacklist plane: a namespace-scoped pattern whose text and
    # reason carry nonces. blacklist.list as B on namespace A must not show
    # it; blacklist.remove / sweep by id as B must not reach it.
    n_bl = _canary()
    s.nonces.append(n_bl)
    r = _post(
        control_plane_url,
        tok_a,
        "blacklist.add",
        {
            "org": org,
            "namespace": s.ns_a,
            "pattern": f"never-store-{n_bl}",
            "pattern_type": "substring",
            "scope": "namespace",
            "reason": f"consent canary {n_bl}",
            "created_by": "alice",
        },
    )
    assert r.status_code == 200, r.text
    s.pattern_id = str(r.json().get("pattern_id") or "")
    # Grants plane: a viewer grant on namespace A to a subject named by a
    # nonce; grants.list as B on namespace A must not show it.
    n_gr = _canary()
    s.nonces.append(n_gr)
    r = _post(
        control_plane_url,
        tok_a,
        "grants.create",
        {
            "org": org,
            "namespace": s.ns_a,
            "subject": f"{n_gr}@canary.invalid",
            "role": "viewer",
            "granted_by": "alice",
        },
    )
    assert r.status_code == 200, r.text
    # Erasure plane: a namespace erasure of the empty throwaway namespace C
    # whose requester is a nonce. erasure.request needs an admin GRANT on the
    # target (not the flat role), so alice grants herself first.
    n_er = _canary()
    s.nonces.append(n_er)
    r = _post(
        control_plane_url,
        tok_a,
        "grants.create",
        {
            "org": org,
            "namespace": s.ns_c,
            "subject": "alice",
            "role": "admin",
            "granted_by": "alice",
        },
    )
    assert r.status_code == 200, r.text
    r = _post(
        control_plane_url,
        tok_a,
        "erasure.request",
        {
            "org": org,
            "scope_type": "namespace",
            "scope_value": s.ns_c,
            "requester": f"{n_er}@canary.invalid",
        },
    )
    assert r.status_code == 200, r.text
    s.erasure_id = str(r.json().get("erasure_id") or "")
    # One canary in each tenant-text plane: a conversation turn, a run, a
    # working-memory block, a policy.
    n_cv = _canary()
    s.nonces.append(n_cv)
    s.thread_id = f"thread-{uuid.uuid4().hex[:6]}"
    r = _post(
        control_plane_url,
        tok_a,
        "conversation.create",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "thread_id": s.thread_id,
            "title": f"canary thread {n_cv}",
        },
    )
    assert r.status_code == 200, r.text
    r = _post(
        control_plane_url,
        tok_a,
        "conversation.append",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "thread_id": s.thread_id,
            "role": "user",
            "content": f"The rotation passphrase is {n_cv}; keep it off the record.",
        },
    )
    assert r.status_code == 200, r.text
    # The same canary saved for search: the saved-message plane is its own read.
    # It names the launch code's project so the walk's query, the memory canary's
    # text, matches it lexically too.
    s.saved_conversation_id = s.thread_id
    s.saved_message_id = f"saved-{uuid.uuid4().hex[:8]}"
    r = _post(
        control_plane_url,
        tok_a,
        "conversation.save_message",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "conversation_id": s.saved_conversation_id,
            "message_id": s.saved_message_id,
            "role": "assistant",
            "content": f"The saved rotation passphrase for the launch code of {s.entity} is {n_cv}.",
            "user_id": _SAVED_USER,
        },
    )
    assert r.status_code == 200 and r.json().get("indexed") is True, r.text
    s.conversation_nonce = n_cv
    n_run = _canary()
    s.nonces.append(n_run)
    r = _post(
        control_plane_url,
        tok_a,
        "run.begin",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "goal": f"rotate the {n_run} credential",
            "user_request": f"please rotate {n_run}",
        },
    )
    assert r.status_code == 200, r.text
    s.run_id = str(r.json().get("run_id") or "")
    n_wm = _canary()
    s.nonces.append(n_wm)
    r = _post(
        control_plane_url,
        tok_a,
        "working_memory.set",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "block_key": "scratchpad",
            "content": f"scratch: the vault code is {n_wm}",
        },
    )
    assert r.status_code == 200, r.text
    n_pol = _canary()
    s.nonces.append(n_pol)
    s.policy_id = f"canary-{uuid.uuid4().hex[:6]}"
    r = _post(
        control_plane_url,
        tok_a,
        "policy.set",
        {
            "org": org,
            "namespace": s.ns_a,
            "policy_id": s.policy_id,
            "effect": "forbid",
            "actions": ["memory.delete"],
            "condition": {
                "op": "breadth_within",
                "operation": ["memory.search"],
                "within": "1h",
                "cmp": "gte",
                "value": 3,
            },
            "description": f"canary policy {n_pol}",
            "created_by": "alice",
        },
    )
    assert r.status_code == 200, r.text
    n_brief = _canary()
    s.nonces.append(n_brief)
    s.brief_key = f"canary/brief-{uuid.uuid4().hex[:6]}"
    r = _post(
        control_plane_url,
        tok_a,
        "session.brief_save",
        {
            "org": org,
            "namespace": s.ns_a,
            "agent": "a",
            "brief_key": s.brief_key,
            "trigger": "model",
            "sections": {
                "goal": f"rotate the {n_brief} signing key",
                "decisions": [{"what": f"keep {n_brief} out of the logs", "why": "it is a secret"}],
            },
        },
    )
    assert r.status_code == 200, r.text
    _seed_task_and_agent_planes(control_plane_url, tok_a, s)
    _SEED_CACHE[control_plane_url] = s
    return s


def _seed_task_and_agent_planes(base: str, tok_a: str, s: _Seed) -> None:
    """One canary each in the task inbox, profile, context offload, decision
    and record-schema planes, all in namespace A."""
    ident = {"org": s.org, "namespace": s.ns_a, "agent": "a"}
    n_inbox, n_task, n_prof, n_blob, n_dec, n_schema = (_canary() for _ in range(6))
    s.nonces.extend([n_inbox, n_task, n_prof, n_blob, n_dec, n_schema])
    s.inbox = f"canary-inbox-{uuid.uuid4().hex[:6]}"
    r = _post(
        base,
        tok_a,
        "inbox.register",
        {**ident, "inbox": s.inbox, "owner_agent": "a", "description": f"triage {n_inbox}"},
    )
    assert r.status_code == 200, r.text
    r = _post(
        base,
        tok_a,
        "task.create",
        {
            **ident,
            "inbox": s.inbox,
            "kind": "canary",
            "title": f"rotate {n_task}",
            "brief": {"note": f"the key is {n_task}"},
        },
    )
    assert r.status_code == 200, r.text
    s.task_id = str(r.json().get("task_id") or (r.json().get("task") or {}).get("task_id") or "")
    r = _post(base, tok_a, "profile.set", {**ident, "label": "canary", "value": f"pin {n_prof}"})
    assert r.status_code == 200, r.text
    r = _post(
        base,
        tok_a,
        "context.offload",
        {**ident, "content": f"tool output: the token is {n_blob}", "summary": "canary output"},
    )
    assert r.status_code == 200, r.text
    s.blob_key = str(r.json().get("blob_key") or "")
    r = _post(
        base,
        tok_a,
        "decision.record",
        {
            **ident,
            "category": "canary",
            "scenario": f"launch code {s.entity} {n_dec}",
            "reasoning": f"kept {n_dec} private",
            "outcome": "held",
        },
    )
    assert r.status_code == 200, r.text
    r = _post(
        base,
        tok_a,
        "memory_schema.propose",
        {
            "org": s.org,
            "namespace": s.ns_a,
            "name": "canary",
            "description": f"schema {n_schema}",
            "fields": [{"name": "code"}],
        },
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def tokens(seed: _Seed, mint_test_token) -> dict[str, str]:
    return {
        "a_writer": mint_test_token(
            org=seed.org, namespaces=[seed.ns_a], roles=["admin", "writer"], subject="alice"
        ),
        "b_writer": mint_test_token(
            org=seed.org, namespaces=[seed.ns_b], roles=["writer"], subject="bob"
        ),
        "b_viewer": mint_test_token(
            org=seed.org, namespaces=[seed.ns_b], roles=["viewer"], subject="carol"
        ),
        "other_org": mint_test_token(
            org=f"{seed.org}-other", namespaces=["*"], roles=["admin", "writer"], subject="mallory"
        ),
        # ADMIN of namespace B only — the principal the gate stops from writing
        # an org- or global-scoped pattern, or removing one.
        "b_admin": mint_test_token(
            org=seed.org, namespaces=[seed.ns_b], roles=["admin", "writer"], subject="dave"
        ),
    }


def test_positive_control_tenant_a_finds_its_own_canary(control_plane_url, seed, tokens) -> None:
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.search",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "query": seed.value_text,
            "top_k": 5,
            "namespace_scope": "this",
        },
    )
    assert r.status_code == 200, r.text
    assert seed.leaked(r.text), "tenant A cannot see its own canary — the suite would be vacuous"
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.get",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "memory_key": seed.keys[0],
        },
    )
    assert r.status_code == 200 and r.json().get("found") is True


@pytest.mark.parametrize("who", ["b_writer", "b_viewer", "other_org"])
def test_every_read_op_against_namespace_a_leaks_nothing(
    control_plane_url, seed, tokens, who
) -> None:
    leaks: dict[str, list[str]] = {}
    bad_status: dict[str, int] = {}
    unbuildable: list[str] = []
    exercised: set[str] = set()
    for op in _read_ops():
        payload = _synth_payload(op.input_model, seed=seed, namespace=seed.ns_a)
        if payload is None:
            unbuildable.append(op.name)
            continue
        for body in _probe_bodies(payload, _SAVED_USER):
            label = f"{op.name}/{body.get('user_id', '-')}"
            r = _post(control_plane_url, tokens[who], op.name, body)
            if body is payload and r.status_code == 422:
                unbuildable.append(op.name)
                break
            # The named-user body may be unbuildable for this op; the base body may not.
            if body is not payload and r.status_code in (400, 422):
                continue
            exercised.add(op.name)
            if r.status_code not in _OK_STATUSES:
                bad_status[label] = r.status_code
            found = _leak_in(seed, op.name, r)
            if found:
                leaks[label] = found
    assert not leaks, f"{who}: canary nonces in responses: {leaks}"
    assert not bad_status, (
        f"{who}: unexpected statuses (a 5xx is a leak of a different kind): {bad_status}"
    )
    missing_core = sorted(_CORE - exercised)
    assert not missing_core, (
        f"{who}: core surfaces not exercised (body shape moved?): {missing_core}; unbuildable={unbuildable}"
    )


@pytest.mark.parametrize("who", ["b_writer", "b_viewer"])
@pytest.mark.parametrize("scope", ["this", "all_mine"])
def test_search_from_own_namespace_with_the_canary_text_finds_nothing(
    control_plane_url, seed, tokens, who, scope
) -> None:
    """The adversarial query: B searches its own namespace with A's verbatim
    text. Namespace scoping is in PREWHERE ahead of the ANN scan; this is the
    check that it stays there, and that `all_mine` widens only to the token's
    own set."""
    for op, extra in (
        ("memory.search", {"agent": "a", "top_k": 10}),
        ("memory.ask", {"agent": "a", "top_k": 10}),
        ("knowledge.search", {"top_k": 10}),
    ):
        for q in (seed.value_text, seed.nonces[0], seed.nonces[2]):
            r = _post(
                control_plane_url,
                tokens[who],
                op,
                {
                    "org": seed.org,
                    "namespace": seed.ns_b,
                    "query": q,
                    "namespace_scope": scope,
                    **extra,
                },
            )
            assert r.status_code in _OK_STATUSES, f"{op} {r.status_code}: {r.text[:200]}"
            assert not seed.leaked(r.text), f"{who}/{scope}/{op}: {seed.leaked(r.text)}"


def test_org_level_lookups_do_not_bypass_the_namespace(control_plane_url, seed, tokens) -> None:
    """Ops addressed by an org-wide id (memory_id) must still refuse or hide a
    row whose namespace the token cannot read."""
    for who in ("b_writer", "b_viewer", "other_org"):
        for mid in seed.memory_ids:
            r = _post(
                control_plane_url,
                tokens[who],
                "memory.get_by_id",
                {"org": seed.org, "memory_id": mid},
            )
            assert r.status_code in _OK_STATUSES, r.text[:200]
            assert not seed.leaked(r.text), f"{who}: memory.get_by_id leaked {seed.leaked(r.text)}"
            if r.status_code == 200:
                assert r.json().get("found") in (False, None), r.text[:200]
        r = _post(
            control_plane_url,
            tokens[who],
            "memory.bulk_get",
            {
                "org": seed.org,
                "namespace": seed.ns_b,
                "agent": "a",
                "memory_keys": seed.keys,
            },
        )
        assert r.status_code in _OK_STATUSES and not seed.leaked(r.text)


def test_a_dash_org_is_the_tokens_own_org(control_plane_url, seed, tokens) -> None:
    """An org of "-" is absent to the gate, which authorizes the call in the
    token's org; the write lands there, and another org's token naming "-"
    reaches none of it."""
    nonce = _canary()
    key = f"dash_org_{uuid.uuid4().hex[:6]}"
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.set",
        {"org": "-", "namespace": seed.ns_a, "agent": "a", "memory_key": key, "value": nonce},
    )
    assert r.status_code == 200, r.text
    memory_id = r.json()["memory_id"]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.get_by_id",
        {"org": seed.org, "memory_id": memory_id},
    )
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["found"] is True, r.text[:200]
    assert (row["memory_id"], row["namespace"], row["agent"], row["memory_key"]) == (
        memory_id,
        seed.ns_a,
        "a",
        key,
    )
    assert (row["value"], row["user_id"], row["session_id"]) == (nonce, "-", "-")
    own = {"namespace": seed.ns_a, "agent": "a", "memory_key": key}
    for body in ({"org": "-", **own}, {"org": "-", **own, "namespace": "-"}):
        r = _post(control_plane_url, tokens["other_org"], "memory.get", body)
        assert r.status_code in _OK_STATUSES, r.text[:200]
        assert nonce not in r.text, f"another org read a '-'-org write: {r.text[:200]}"


def test_error_bodies_do_not_echo_canaries(control_plane_url, seed, tokens) -> None:
    """A 403 that quotes the forbidden row is still a leak."""
    r = _post(
        control_plane_url,
        tokens["b_viewer"],
        "memory.get",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "memory_key": seed.keys[0],
        },
    )
    assert r.status_code in (403, 404), r.text[:200]
    body = (
        json.dumps(r.json())
        if r.headers.get("content-type", "").startswith("application/json")
        else r.text
    )
    assert not seed.leaked(body)
    assert not re.search(r"zqcanary[0-9a-f]{10}", body)


def test_writes_addressed_by_id_do_not_cross_the_namespace(control_plane_url, seed, tokens) -> None:
    """memory.update_by_id / memory.redact take (org, memory_id) only. A
    writer or admin of namespace B must not be able to change A's row through
    them, and the response must not say whether the id exists."""
    before = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.get_by_id",
        {
            "org": seed.org,
            "memory_id": seed.memory_ids[0],
        },
    ).json()
    assert before.get("found") is True
    for who in ("b_writer", "other_org"):
        r = _post(
            control_plane_url,
            tokens[who],
            "memory.update_by_id",
            {
                "org": seed.org,
                "memory_id": seed.memory_ids[0],
                "value": "overwritten by tenant B",
            },
        )
        assert r.status_code in _OK_STATUSES, r.text[:200]
        if r.status_code == 200:
            assert r.json().get("updated") in (False, None), r.text[:200]
        assert not seed.leaked(r.text)
    r = _post(
        control_plane_url,
        tokens["b_writer"],
        "memory.redact",
        {
            "org": seed.org,
            "memory_id": seed.memory_ids[0],
            "version": int(before["version"]) - 1,
            "reason": "tenant B",
            "redacted_by": "bob",
        },
    )
    assert r.status_code in _OK_STATUSES, r.text[:200]
    if r.status_code == 200:
        assert r.json().get("redacted") in (False, None)
    after = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.get_by_id",
        {
            "org": seed.org,
            "memory_id": seed.memory_ids[0],
        },
    ).json()
    assert after.get("value") == before.get("value") and after.get("version") == before.get(
        "version"
    )


def _conflicted_pair(base: str, owner: str, org: str, ns: str) -> tuple[str, str]:
    """Two memories in `ns` flagged as conflicting with each other; their ids."""
    ident = {"org": org, "namespace": ns, "agent": "a"}
    # 0.60 admits the fake n-gram embedder's distance for these two texts
    # (test_memory_conflicts uses the same pair).
    r = _post(
        base,
        owner,
        "namespace.register",
        {**ident, "owner_email": "victim", "conflict_threshold": 0.60},
    )
    assert r.status_code == 200, r.text
    first = "Always use 2-space indentation"
    r = _post(base, owner, "memory.set", {**ident, "memory_key": "style", "value": first})
    assert r.status_code == 200, r.text
    wait_until(
        lambda: "style" in _post(base, owner, "memory.search", {**ident, "query": first}).text,
        max_wait=15,
        description="the first write's embedding",
    )
    r = _post(
        base,
        owner,
        "memory.set",
        {**ident, "memory_key": "style_v2", "value": first + " throughout the project"},
    )
    assert r.status_code == 200, r.text
    rows = wait_until(
        lambda: (
            len(rows := _post(base, owner, "conflicts.list", ident).json()["rows"]) == 2 and rows
        ),
        max_wait=15,
        description="both rows flagged conflicted",
    )
    by_key = {row["memory_key"]: row["memory_id"] for row in rows}
    return by_key["style"], by_key["style_v2"]


def test_conflicts_resolve_cannot_reach_another_namespaces_rows(
    control_plane_url, mint_test_token
) -> None:
    """An admin of namespace X names namespace V's conflicted target, peer or
    allow pair. Every decision answers resolved=False, writes nothing, and V's
    rows read back unchanged."""
    org = unique_org("leak-resolve")
    ns_v, ns_x = f"victim-{uuid.uuid4().hex[:6]}", f"attacker-{uuid.uuid4().hex[:6]}"
    owner = mint_test_token(org=org, namespaces=[ns_v], roles=["admin", "writer"], subject="vic")
    attacker = mint_test_token(org=org, namespaces=[ns_x], roles=["admin", "writer"], subject="mal")
    target, peer = _conflicted_pair(control_plane_url, owner, org, ns_v)

    def _snapshot() -> dict[str, tuple[Any, ...]]:
        out = {}
        for mid in (target, peer):
            got = _post(
                control_plane_url, owner, "memory.get_by_id", {"org": org, "memory_id": mid}
            )
            body = got.json()
            assert body.get("found") is True, got.text[:300]
            out[mid] = (body["version"], body["value"], body.get("content_sha256"))
        return out

    before = _snapshot()
    for decision in ("contract", "revise", "allow"):
        for memory_id, peer_id in ((target, peer), (peer, target), (target, None)):
            r = _post(
                control_plane_url,
                attacker,
                "conflicts.resolve",
                {
                    "org": org,
                    "namespace": ns_x,
                    "memory_id": memory_id,
                    "peer_id": peer_id,
                    "decision": decision,
                    "resolved_by": "mal",
                },
            )
            assert r.status_code in _OK_STATUSES, f"{decision}: {r.status_code} {r.text[:200]}"
            if r.status_code == 200:
                body = r.json()
                assert body["resolved"] is False and body["affected_memory_ids"] == [], (
                    f"{decision} on {memory_id}: {body}"
                )
    assert _snapshot() == before, "the attacker's resolve changed the victim's rows"
    ident = {"org": org, "namespace": ns_v}
    listed = _post(control_plane_url, owner, "conflicts.list", {**ident, "agent": "a"}).json()
    assert {row["memory_id"] for row in listed["rows"]} == {target, peer}, listed
    allow = _post(control_plane_url, owner, "conflicts.allowlist_list", ident).json()
    assert allow["rows"] == [], allow

    # Positive control: the owner's own resolve takes effect, so the refusals
    # above are refusals and not a malformed call.
    r = _post(
        control_plane_url,
        owner,
        "conflicts.resolve",
        {**ident, "memory_id": target, "decision": "allow", "resolved_by": "vic"},
    )
    assert r.status_code == 200 and r.json()["resolved"] is True, r.text[:300]


def test_positive_control_tenant_a_reads_its_canaries_in_the_four_planes(
    control_plane_url, seed, tokens
) -> None:
    """The blacklist / erasure / grants probes are not vacuous: tenant A sees each canary."""
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "blacklist.list",
        {"org": seed.org, "namespace": seed.ns_a},
    )
    assert r.status_code == 200 and seed.pattern_id in r.text, r.text[:300]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "grants.list",
        {"org": seed.org, "namespace": seed.ns_a},
    )
    assert r.status_code == 200 and seed.leaked(r.text), r.text[:300]
    r = _post(
        control_plane_url, tokens["a_writer"], "erasure.status", {"erasure_id": seed.erasure_id}
    )
    assert r.status_code == 200 and r.json().get("found") is True and seed.leaked(r.text), r.text[
        :300
    ]


def test_a_namespace_admin_cannot_write_or_remove_a_wider_pattern(
    control_plane_url, seed, tokens
) -> None:
    """An ADMIN of namespace B: scope=org and scope=global
    are refused (a global regex would refuse every tenant's writes), and a write in A that the attempted pattern would have
    matched still lands. blacklist.remove by id from B neither confirms nor
    removes A's namespace pattern, nor a pattern wider than B's authority."""
    marker = f"blocked-if-gate-fails-{uuid.uuid4().hex[:6]}"
    for scope in ("org", "global"):
        r = _post(
            control_plane_url,
            tokens["b_admin"],
            "blacklist.add",
            {
                "org": seed.org,
                "namespace": seed.ns_b,
                "pattern": marker,
                "pattern_type": "substring",
                "scope": scope,
                "reason": "escalation probe",
            },
        )
        assert r.status_code == 403, f"scope={scope}: {r.status_code} {r.text[:200]}"
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "memory.set",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "memory_key": f"probe_{uuid.uuid4().hex[:4]}",
            "value": f"{marker} must still be writable",
            "memory_type": "semantic",
        },
    )
    assert r.status_code == 200, r.text[:300]
    # A regex pattern is bounded at add: a catastrophic shape is a 422, not a row.
    r = _post(
        control_plane_url,
        tokens["b_admin"],
        "blacklist.add",
        {
            "org": seed.org,
            "namespace": seed.ns_b,
            "pattern": "(a+)+$",
            "pattern_type": "regex",
        },
    )
    assert r.status_code == 422, r.text[:300]
    assert "BLACKLIST_PATTERN_INVALID" in r.text
    # remove by id from B: A's namespace pattern is hidden, not confirmed.
    r = _post(
        control_plane_url,
        tokens["b_admin"],
        "blacklist.remove",
        {
            "org": seed.org,
            "pattern_id": seed.pattern_id,
            "removed_by": "dave",
        },
    )
    assert r.status_code == 200 and r.json().get("removed") is False, r.text[:300]
    r = _post(
        control_plane_url,
        tokens["other_org"],
        "blacklist.remove",
        {
            "org": seed.org,
            "pattern_id": seed.pattern_id,
            "removed_by": "mallory",
        },
    )
    assert r.status_code in (200, 403), r.text[:300]
    if r.status_code == 200:
        assert r.json().get("removed") is False
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "blacklist.list",
        {"org": seed.org, "namespace": seed.ns_a},
    )
    assert seed.pattern_id in r.text, "tenant A's pattern was removed by another principal"


def test_positive_control_tenant_a_reads_its_canaries_in_the_tenant_text_planes(
    control_plane_url, seed, tokens
) -> None:
    """The conversation, run, working-memory and policy canaries are real —
    tenant A reads each back — so an empty result for
    tenant B in those planes is a refusal, not a vacuous probe."""
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "conversation.get",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "thread_id": seed.thread_id,
            "include_messages": True,
        },
    )
    assert r.status_code == 200 and r.json().get("found") is True and seed.leaked(r.text), r.text[
        :300
    ]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "run.get",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "run_id": seed.run_id,
        },
    )
    assert r.status_code == 200 and r.json().get("found") is True and seed.leaked(r.text), r.text[
        :300
    ]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "working_memory.get",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "block_key": "scratchpad",
        },
    )
    assert r.status_code == 200 and r.json().get("found") is True and seed.leaked(r.text), r.text[
        :300
    ]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "policy.list",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
        },
    )
    assert r.status_code == 200 and seed.leaked(r.text), r.text[:300]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "session.brief_get",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
            "brief_key": seed.brief_key,
        },
    )
    assert r.status_code == 200 and r.json().get("found") is True and seed.leaked(r.text), r.text[
        :300
    ]
    r = _post(
        control_plane_url,
        tokens["a_writer"],
        "session.brief_list",
        {
            "org": seed.org,
            "namespace": seed.ns_a,
            "agent": "a",
        },
    )
    assert r.status_code == 200 and seed.leaked(r.text), r.text[:300]


@pytest.mark.parametrize(
    "op_name",
    [
        "conversation.list_saved_messages",
        "conversation.search_saved_messages",
        "conversation.saved_status",
    ],
)
def test_positive_control_tenant_a_reads_its_saved_canary_with_the_walks_body(
    control_plane_url, seed, tokens, op_name
) -> None:
    """The namespace-A walk's named-user body finds the saved canary, so tenant
    B's empty answer to it is a refusal, not a miss."""
    from server.operations.registry import ALL_OPERATIONS

    op = next(o for o in ALL_OPERATIONS if o.name == op_name)
    payload = _synth_payload(op.input_model, seed=seed, namespace=seed.ns_a)
    assert payload is not None
    body = _probe_bodies(payload, _SAVED_USER)[1]
    r = _post(control_plane_url, tokens["a_writer"], op_name, body)
    assert r.status_code == 200, r.text
    if op_name == "conversation.saved_status":
        # The answer is the saved ids, which carry no nonce.
        assert r.json()["saved"] == [seed.saved_message_id], r.text
    else:
        assert seed.leaked(r.text), f"{op_name} misses the saved canary: the walk is vacuous"


def test_saved_message_search_from_own_namespace_with_the_canary_text_finds_nothing(
    control_plane_url, seed, tokens
) -> None:
    """B searches its own namespace's saved messages with A's saved text, naming
    A's user: the namespace boundary holds whatever user the body names."""
    for who in ("b_writer", "b_viewer"):
        r = _post(
            control_plane_url,
            tokens[who],
            "conversation.search_saved_messages",
            {
                "org": seed.org,
                "namespace": seed.ns_b,
                "agent": "a",
                "user_id": _SAVED_USER,
                "query": f"saved rotation passphrase {seed.conversation_nonce}",
            },
        )
        assert r.status_code in _OK_STATUSES, f"{who}: {r.status_code} {r.text[:200]}"
        assert not seed.leaked(r.text), f"{who}: {seed.leaked(r.text)}"


def test_conversation_search_from_own_namespace_with_the_canary_text_finds_nothing(
    control_plane_url, seed, tokens
) -> None:
    """The adversarial query in the conversation plane —
    B searches its own namespace with A's turn verbatim, under both scopes."""
    for who in ("b_writer", "b_viewer"):
        for scope in ("this", "all_mine"):
            r = _post(
                control_plane_url,
                tokens[who],
                "conversation.search",
                {
                    "org": seed.org,
                    "namespace": seed.ns_b,
                    "agent": "a",
                    "query": f"rotation passphrase {seed.conversation_nonce}",
                    "namespace_scope": scope,
                    "top_k": 10,
                },
            )
            assert r.status_code in _OK_STATUSES, f"{who}/{scope}: {r.status_code} {r.text[:200]}"
            assert not seed.leaked(r.text), f"{who}/{scope}: {seed.leaked(r.text)}"


def test_a_pending_join_request_reads_nothing(
    control_plane_url, seed, tokens, mint_test_token
) -> None:
    """`namespace.join` on an approval-gated namespace files a pending request.
    Until an admin approves it, the requester's `all_mine` fan-out — which
    trusts the caller's namespace list — must not reach the namespace."""
    ns_p = f"tenant-p-{uuid.uuid4().hex[:6]}"
    owner = mint_test_token(
        org=seed.org, namespaces=[ns_p], roles=["admin", "writer"], subject="alice"
    )
    nonce = _canary()
    r = _post(
        control_plane_url,
        owner,
        "namespace.register",
        {"org": seed.org, "namespace": ns_p, "owner_email": "alice", "join_policy": "approval"},
    )
    assert r.status_code == 200, r.text
    r = _post(
        control_plane_url,
        owner,
        "memory.set",
        {
            "org": seed.org,
            "namespace": ns_p,
            "agent": "a",
            "memory_key": f"pending_canary_{uuid.uuid4().hex[:4]}",
            "value": f"The pending-gate code is {nonce}.",
        },
    )
    assert r.status_code == 200, r.text
    query = {"org": seed.org, "agent": "a", "query": f"pending-gate code {nonce}", "top_k": 10}
    # Positive control: the owner finds it.
    r = _post(control_plane_url, owner, "memory.search", {**query, "namespace": ns_p})
    assert r.status_code == 200 and nonce in r.text, r.text[:300]

    r = _post(
        control_plane_url,
        tokens["b_writer"],
        "namespace.join",
        {"org": seed.org, "namespace": ns_p, "role": "viewer"},
    )
    assert r.status_code == 200 and r.json()["status"] == "pending", r.text
    for op in ("memory.search", "memory.ask"):
        r = _post(
            control_plane_url,
            tokens["b_writer"],
            op,
            {**query, "namespace": seed.ns_b, "namespace_scope": "all_mine"},
        )
        assert r.status_code in _OK_STATUSES, f"{op} {r.status_code}: {r.text[:200]}"
        assert nonce not in r.text, f"{op}: a pending request read {ns_p}"


def test_positive_control_tenant_a_reads_its_canaries_in_the_task_and_agent_planes(
    control_plane_url, seed, tokens
) -> None:
    """The task, profile, offload, decision and schema canaries are real:
    tenant A reads each back through the op the walk probes as B."""
    ident = {"org": seed.org, "namespace": seed.ns_a, "agent": "a"}
    assert seed.task_id and seed.blob_key, "the seed did not record a task id and blob key"
    for op, body in (
        ("task.get", {**ident, "task_id": seed.task_id}),
        ("task.list", {**ident, "inbox": seed.inbox}),
        ("inbox.list", ident),
        ("profile.render", ident),
        ("context.fetch", {**ident, "blob_key": seed.blob_key}),
        ("decision.precedents", {**ident, "query": f"launch code {seed.entity}"}),
        ("memory_schema.list", {**ident, "include_proposed": True}),
    ):
        r = _post(control_plane_url, tokens["a_writer"], op, body)
        assert r.status_code == 200 and seed.leaked(r.text), f"{op}: {r.status_code} {r.text[:300]}"


def test_an_org_admin_neither_reads_nor_sets_another_orgs_model_calls(
    control_plane_url, mint_test_token
) -> None:
    """Org B's admin is refused org A's switch both ways, and A's switch is as
    A's admin left it."""
    org_a, org_b = unique_org("ai-a"), unique_org("ai-b")
    admin_a = mint_test_token(org=org_a, namespaces=["*"], roles=["org_admin"], subject="ann")
    admin_b = mint_test_token(org=org_b, namespaces=["*"], roles=["org_admin"], subject="ben")
    off = {"org": org_a, "enabled": False, "reason": "AI off"}
    r = _post(control_plane_url, admin_a, "org.set_model_calls", off)
    assert r.status_code == 200 and r.json()["enabled"] is False, r.text[:300]
    r = _post(control_plane_url, admin_b, "org.model_calls", {"org": org_a})
    assert r.status_code == 403 and "AI off" not in r.text, r.text[:300]
    r = _post(control_plane_url, admin_b, "org.set_model_calls", {"org": org_a, "enabled": True})
    assert r.status_code == 403, r.text[:300]
    r = _post(control_plane_url, admin_a, "org.model_calls", {"org": org_a})
    assert r.status_code == 200, r.text[:300]
    read = r.json()
    assert (read["enabled"], read["reason"], read["updated_by"]) == (False, "AI off", "ann")


@pytest.mark.parametrize("who", ["writer", "ns admin", "other org admin", "end user"])
def test_only_the_orgs_admin_sets_reads_or_clears_its_extraction_policy(
    control_plane_url, mint_test_token, who: str
) -> None:
    """A writer, a namespace admin, an end user and another org's admin are each
    refused the policy of org A and of its chat namespace, every way, and it is
    as A's admin left it."""
    from tests import end_user_issuer

    if who == "end user" and not end_user_issuer.configured():
        pytest.skip("no test end-user issuer: run `python -m tests.end_user_issuer <dir>`")
    org, other = unique_org("xpol-a"), unique_org("xpol-b")
    ns = f"chat-{uuid.uuid4().hex[:6]}"
    policy = "Never store payment card data."
    admin = mint_test_token(org=org, namespaces=["*"], roles=["org_admin"], subject="ann")
    body = {"org": org, "scope": "org", "policy": policy}
    r = _post(control_plane_url, admin, "extraction_policy.set", body)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["effective"] == policy
    if who == "writer":
        token = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="app")
    elif who == "ns admin":
        token = mint_test_token(org=org, namespaces=[ns], roles=["admin", "writer"], subject="l")
    elif who == "other org admin":
        token = mint_test_token(org=other, namespaces=["*"], roles=["org_admin"], subject="ben")
    else:
        token = end_user_issuer.mint(org, "u1")
    for scope in (
        {"org": org, "scope": "org"},
        {"org": org, "scope": "namespace", "namespace": ns},
    ):
        for op, extra in (
            ("extraction_policy.set", {"policy": "Store everything."}),
            ("extraction_policy.clear", {}),
            ("extraction_policy.get", {}),
        ):
            r = _post(control_plane_url, token, op, {**scope, **extra})
            assert r.status_code == 403, (op, scope, r.text[:300])
            assert policy not in r.text
    body = {"org": org, "scope": "namespace", "namespace": ns}
    r = _post(control_plane_url, admin, "extraction_policy.get", body)
    assert r.status_code == 200, r.text[:300]
    read = r.json()
    assert (read["policy"], read["effective"]) == ("", policy)
    r = _post(control_plane_url, admin, "extraction_policy.get", {"org": org, "scope": "org"})
    assert (r.json()["policy"], r.json()["updated_by"]) == (policy, "ann")


def test_an_end_users_at_me_extraction_runs_under_the_policy_of_the_collection_it_resolves_to(
    control_plane_url, mint_test_token
) -> None:
    """`namespace: "@me"` resolves to the end user's personal collection before
    the handler reads the policy, so a policy set for that collection's name
    applies to the user's `@me` extractions, and its clear stops applying."""

    from server.core.principals import personal_name
    from tests import end_user_issuer
    from tests.integration._eventually import policy_change_adopted

    if not end_user_issuer.configured():
        pytest.skip("no test end-user issuer: run `python -m tests.end_user_issuer <dir>`")
    org, user = unique_org("xpol-me"), "u-policy"
    admin = mint_test_token(org=org, namespaces=["*"], roles=["org_admin"], subject="ann")
    target = {"org": org, "scope": "namespace", "namespace": personal_name(org, "user:" + user)}

    def key() -> str:
        body = {
            "org": org,
            "namespace": "@me",
            "agent": "chat",
            "messages": [{"role": "user", "content": "I moved to Lisbon in 2021."}],
            "observation_date": "2026-01-15",
            "pipeline_steps": [{"step_id": "loom.llm_extract"}],
        }
        r = _post_as(control_plane_url, org, user, "memory.set_from_messages", body)
        assert r.status_code == 200, r.text[:300]
        return str(r.json()["extract_prompt_keys"][0])

    bare = key()
    body = {**target, "policy": "Never store home addresses."}
    r = _post(control_plane_url, admin, "extraction_policy.set", body)
    assert r.status_code == 200, r.text[:300]
    policy_change_adopted(time.monotonic())
    assert key() != bare
    r = _post(control_plane_url, admin, "extraction_policy.clear", target)
    assert r.status_code == 200, r.text[:300]
    policy_change_adopted(time.monotonic())
    assert key() == bare


# ── knowledge sharing (docs/architecture/sharing.md) ─────────────────────────
#
# A service's end users, signed in with end-user tokens, own collections: a
# parent shared with a group and one user, a child that inherits it, and a
# personal collection holding a document and a saved message. A user once
# granted and then revoked, a member once listed and then removed, a stranger,
# the owner's id in another tenant, an org admin and a writer of another
# namespace then drive every read op against each of them.


def _post_as(base: str, org: str, user: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    """`op` as the end user `user:<user>` of `org`, under a token the test
    end-user issuer signs (tests/end_user_issuer.py)."""
    from tests import end_user_issuer

    with httpx.Client(base_url=base, timeout=30.0) as c:
        return c.post(
            f"/v1/{op}",
            json=payload,
            headers={"Authorization": f"Bearer {end_user_issuer.mint(org, user)}"},
        )


class _Shared:
    def __init__(self, base: str, org: str) -> None:
        from server.core.principals import personal_name as name_for

        self.org, self.other_org = org, f"{org}-other"
        tag = uuid.uuid4().hex[:6]
        self.parent, self.child = f"col-leak-{tag}", f"col-leak-{tag}-docs"
        self.personal = name_for(org, "user:owner")
        self.seed = _Seed(org, self.child, self.parent)
        self.seed.keys = ["canary-key-1", "canary-key-2"]
        self.seed.memory_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.seed.entity = "canary"
        self.base = base

    def ok(self, user: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = _post_as(self.base, self.org, user, op, {"org": self.org, **payload})
        assert r.status_code == 200, f"{op}: {r.status_code} {r.text}"
        if op in ACL_OPS:
            acl_change_adopted()
        return r.json()


_SHARED_CACHE: dict[str, _Shared] = {}


@pytest.fixture
def shared(control_plane_url: str, mint_test_token) -> _Shared:
    from tests import end_user_issuer

    if not end_user_issuer.configured():
        pytest.skip("no test end-user issuer: run `python -m tests.end_user_issuer <dir>`")
    if control_plane_url in _SHARED_CACHE:
        return _SHARED_CACHE[control_plane_url]
    s = _Shared(control_plane_url, unique_org("leak-share"))
    idp = mint_test_token(org=s.org, namespaces=["*"], roles=["org_admin"], subject="leak-idp")
    s.ok("owner", "collection.create", {"namespace": s.parent})
    s.ok("owner", "collection.create", {"namespace": s.child, "parent": s.parent})
    groups = httpx.post(
        f"{control_plane_url}/scim/v2/Groups",
        json={"externalId": "readers", "members": [{"value": "member"}, {"value": "removed"}]},
        headers={"Authorization": f"Bearer {idp}"},
    )
    assert groups.status_code == 201, groups.text
    acl_change_adopted()
    for subject in ("group:readers", "user:direct", "user:revoked"):
        s.ok(
            "owner", "grants.create", {"namespace": s.parent, "subject": subject, "role": "viewer"}
        )
    s.ok("owner", "grants.revoke", {"namespace": s.parent, "subject": "user:revoked"})
    removed = httpx.patch(
        f"{control_plane_url}/scim/v2/Groups/readers",
        json={"Operations": [{"op": "remove", "path": 'members[value eq "removed"]'}]},
        headers={"Authorization": f"Bearer {idp}"},
    )
    assert removed.status_code == 200, removed.text
    acl_change_adopted()
    for ns, uri in ((s.child, "doc/plan"), (s.parent, "doc/top"), ("@me", "doc/mine")):
        nonce = _canary()
        s.seed.nonces.append(nonce)
        content = f"{nonce} quarterly renewal plan"
        s.ok("owner", "knowledge.upsert", {"namespace": ns, "source_uri": uri, "content": content})
    # A saved message in the owner's `@me`; the walk also asks for it by the owner's id.
    nonce = _canary()
    s.seed.nonces.append(nonce)
    s.seed.saved_conversation_id, s.seed.saved_message_id = "canary", "m1"
    turn = {
        "conversation_id": s.seed.saved_conversation_id,
        "message_id": s.seed.saved_message_id,
        "role": "user",
        "user_id": "owner",
    }
    content = f"{nonce} quarterly renewal plan"
    saved = s.ok(
        "owner",
        "conversation.save_message",
        {"namespace": "@me", "agent": "a", **turn, "content": content},
    )
    assert saved["indexed"] is True, saved
    # The probes ask with the canary's words, not its nonce: an op that echoes
    # the question back is not a leak, and a hit carries the nonce.
    s.seed.value_text = "quarterly renewal plan"
    # The walk's pace is its own; the deployment's limits are left as they are.
    tok_platform = mint_test_token(org="*", roles=["platform_admin"], subject="leak-operator")
    for ns in (s.parent, s.child, s.personal):
        for op_class in ("default", "memory"):
            httpx.post(
                f"{control_plane_url}/v1/admin/ratelimit.set",
                json={"namespace": ns, "op_class": op_class, "capacity": 2000, "rate": 200},
                headers={"Authorization": f"Bearer {tok_platform}"},
            )
    _SHARED_CACHE[control_plane_url] = s
    return s


@pytest.mark.parametrize("user", ["owner", "direct", "member"])
def test_positive_control_the_grantees_read_the_shared_canary(
    control_plane_url, shared, user
) -> None:
    r = _post_as(
        control_plane_url,
        shared.org,
        user,
        "knowledge.search",
        {"org": shared.org, "namespace": shared.child, "query": shared.seed.value_text},
    )
    assert r.status_code == 200, r.text
    assert shared.seed.leaked(r.text), f"{user} cannot read the canary: the suite would be vacuous"


@pytest.mark.parametrize(
    "op_name",
    [
        "conversation.list_saved_messages",
        "conversation.search_saved_messages",
        "conversation.saved_status",
    ],
)
def test_positive_control_the_owner_reads_the_saved_canary_with_the_walks_body(
    control_plane_url, shared, op_name
) -> None:
    from server.operations.registry import ALL_OPERATIONS

    op = next(o for o in ALL_OPERATIONS if o.name == op_name)
    payload = _synth_payload(op.input_model, seed=shared.seed, namespace="@me")
    assert payload is not None
    # The bodies the walk sends: its own, and the one naming the owner.
    for body in _probe_bodies(payload, "owner"):
        r = _post_as(control_plane_url, shared.org, "owner", op_name, body)
        assert r.status_code == 200, r.text
        if op_name == "conversation.saved_status":
            # The answer is the saved ids, which carry no nonce.
            assert r.json()["saved"] == [shared.seed.saved_message_id], r.text
        else:
            assert shared.seed.leaked(r.text), (
                f"{op_name} with user_id={body['user_id']} misses the saved canary: the walk is vacuous"
            )


@pytest.mark.parametrize(
    "who", ["stranger", "revoked", "removed", "other_tenant", "org_admin", "b_writer"]
)
def test_every_read_op_against_a_collection_leaks_nothing(
    control_plane_url, shared, mint_test_token, who
) -> None:
    def post(op: str, payload: dict[str, Any]) -> httpx.Response:
        if who == "other_tenant":
            return _post_as(control_plane_url, shared.other_org, "owner", op, payload)
        if who in ("org_admin", "b_writer"):
            roles = ["org_admin", "admin", "writer"] if who == "org_admin" else ["writer"]
            names = ["*"] if who == "org_admin" else [f"tenant-b-{uuid.uuid4().hex[:6]}"]
            token = mint_test_token(org=shared.org, namespaces=names, roles=roles, subject=who)
            return _post(control_plane_url, token, op, payload)
        return _post_as(control_plane_url, shared.org, who, op, payload)

    leaks: dict[str, list[str]] = {}
    bad_status: dict[str, int] = {}
    for namespace in (shared.child, shared.parent, shared.personal, "@me"):
        for op in _read_ops():
            payload = _synth_payload(op.input_model, seed=shared.seed, namespace=namespace)
            if payload is None:
                continue
            for body in _probe_bodies(payload, "owner"):
                label = f"{op.name}@{namespace}/{body.get('user_id', '-')}"
                r = post(op.name, body)
                if r.status_code in (400, 422):  # unbuildable, or `@me` for a non-gateway caller
                    continue
                if r.status_code not in _OK_STATUSES:
                    bad_status[label] = r.status_code
                found = _leak_in(shared.seed, op.name, r)
                if found:
                    leaks[label] = found
    assert not leaks, f"{who}: canary nonces in responses: {leaks}"
    assert not bad_status, f"{who}: unexpected statuses: {bad_status}"


# ---- loom_search under a code scope: the repository's decisions ----


@dataclasses.dataclass
class _CodeDecision:
    org: str
    namespace: str
    key: str
    nonce: str
    topic: str


@pytest.fixture
def code_decision(control_plane_url: str, mint_test_token) -> typing.Iterator[_CodeDecision]:
    """A decision canary in the deployment's code namespace (its own trunk:
    one configured branch), recorded through decision.record by a writer on
    that namespace and deleted afterwards (which retires its record)."""
    org = os.environ.get("LOOM_CODE_ORG") or pytest.skip("LOOM_CODE_ORG unset: no code repo")
    ns = os.environ.get("LOOM_CODE_NAMESPACE") or pytest.skip("LOOM_CODE_NAMESPACE unset")
    nonce, topic = _canary(), f"zqtopic{uuid.uuid4().hex[:8]}"
    tok = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="maintainer")
    r = _post(
        control_plane_url,
        tok,
        "decision.record",
        {
            "org": org,
            "namespace": ns,
            "agent": "maintainer",
            "category": "cache",
            # The topic word keeps the question on this test's row.
            "scenario": f"Evict the {topic} cache entries early?",
            "outcome": "evict them early",
            "reasoning": f"{nonce} measured the memory bound",
        },
    )
    assert r.status_code == 200, r.text
    d = _CodeDecision(org, ns, r.json()["memory_key"], nonce, topic)
    yield d
    ident = {"org": org, "namespace": ns, "agent": "maintainer", "memory_key": d.key}
    r = _post(control_plane_url, tok, "memory.delete", ident)
    assert r.status_code == 200, r.text


def _loom_search_in_checkout(
    base: str, token: str, d: _CodeDecision, *, scoped: bool = True
) -> httpx.Response:
    """MCP `loom_search` over the caller's home namespace alone, asking the
    question the decision answers (without its nonce); `scoped` sends the
    checkout's code scope."""
    headers = {"Authorization": f"Bearer {token}"}
    if scoped:
        headers["X-Loom-Code-Scope"] = f"{d.org}/{d.namespace}"
    with httpx.Client(base_url=base, timeout=30.0) as c:
        return c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "loom_search",
                    "arguments": {
                        "query": f"why do we evict the {d.topic} cache entries early",
                        "namespace_scope": "this",
                    },
                },
            },
            headers=headers,
        )


def test_positive_control_a_reader_of_the_repository_reads_its_decision(
    control_plane_url, code_decision, mint_test_token
) -> None:
    d = code_decision
    home = f"reader-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(
        org=d.org, namespaces=[home, d.namespace], roles=["viewer"], subject="reader"
    )
    r = _loom_search_in_checkout(control_plane_url, tok, d)
    assert r.status_code == 200, r.text
    assert d.nonce in r.text, f"the decision leg did not serve the canary: {r.text[:2000]}"
    # The home namespace alone does not hold it: the code-scope leg served it.
    r = _loom_search_in_checkout(control_plane_url, tok, d, scoped=False)
    assert r.status_code == 200, r.text
    assert d.nonce not in r.text
    # Over HTTP the header sets no code scope: search.query reads no decisions.
    with httpx.Client(base_url=control_plane_url, timeout=30.0) as c:
        r = c.post(
            "/v1/search.query",
            json={
                "org": d.org,
                "namespace": home,
                "agent": "reader",
                "query": f"why do we evict the {d.topic} cache entries early",
                "namespace_scope": "this",
            },
            headers={
                "Authorization": f"Bearer {tok}",
                "X-Loom-Code-Scope": f"{d.org}/{d.namespace}",
            },
        )
    assert r.status_code == 200, r.text
    assert d.nonce not in r.text


@pytest.mark.parametrize("who", ["same_org", "other_org"])
def test_loom_search_under_a_code_scope_serves_no_decision_to_a_non_reader(
    control_plane_url, code_decision, mint_test_token, who
) -> None:
    d = code_decision
    org = d.org if who == "same_org" else unique_org("leak-code-other")
    tok = mint_test_token(
        org=org, namespaces=[f"tenant-b-{uuid.uuid4().hex[:6]}"], roles=["writer"], subject=who
    )
    if who == "same_org":
        # The case needs a code namespace not readable org-wide (the default,
        # LOOM_CODE_ORG_READABLE off, as CI runs it).
        r = _post(
            control_plane_url,
            tok,
            "memory.get",
            {"org": d.org, "namespace": d.namespace, "agent": "maintainer", "memory_key": d.key},
        )
        assert r.status_code == 403 or (r.status_code == 200 and r.json().get("found") is False), (
            r.text
        )
    r = _loom_search_in_checkout(control_plane_url, tok, d)
    assert r.status_code == 200, r.text
    assert d.nonce not in r.text, f"{who} read the repository's decision"
