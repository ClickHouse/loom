"""Reference client — one access layer for a loom consumer.

The shell each consumer archetype (QA agent, coding-agent plugin, memory app,
evaluation harness) would otherwise rebuild on top of the SDK's `Transport`:
identity and consumer tag on every call, fail-soft results, response-field
guards, a catalogue probe and outcome reporting.

It is deliberately dict-in / dict-out like the rest of the SDK: responses
gain fields between releases, so a typed model per response would be a second thing to
keep in step. `call()`
returns a `CallResult` whose `.data` is the raw response; `.require(...)`
asserts the fields the caller relies on and ignores everything else.

    from loom.reference_client import ReferenceClient

    async with ReferenceClient("acme", "support-bot", consumer="support-bot") as c:
        probe = await c.probe("memory_app")           # the ops this archetype needs
        assert probe.ok, probe.missing
        await c.register_namespace("owner@acme.example", traffic_class="production")
        r = await c.ask("when is the deploy window?")
        if r.ok and not r.data.get("abstain"):
            for key in r.data.get("memory_keys", []):
                await c.outcome(key, "used")           # feedback flows from call one

Every method is fail-soft by default: a 5xx after the transport's retries, a
429 past the wait budget, or any 4xx comes back as `CallResult(ok=False,
code=...)` instead of an exception, so a hook inside someone else's session
degrades to "no memory this turn" rather than a stack trace. Construct with
`fail_soft=False` to get the SDK's typed exceptions instead.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from loom.errors import LoomError
from loom.run import AMBIENT_RUN_HEADER, RECALL_READ_PATHS
from loom.transport import Transport, consumer_label

# Traffic classes `namespace.register` accepts as `traffic_class`.
TRAFFIC_CLASSES: tuple[str, ...] = ("production", "interactive", "bench")
TrafficClass = Literal["production", "interactive", "bench"]

# The operations each archetype needs served before its first real call.
# `probe(archetype)` asserts every one is in the deployment's catalogue.
ARCHETYPES: dict[str, tuple[str, ...]] = {
    # one client module, deterministic pipeline calls, outcome feedback
    "qa_agent": (
        "code.index_status",
        "code.search",
        "code.symbol",
        "code.outline",
        "code.callers",
        "code.outcome",
    ),
    # hook shunt + MCP tools inside someone else's session
    "plugin": (
        "code.search",
        "code.outline",
        "code.symbol",
        "code.grep",
        "code.outcome",
        "context.pack",
        "memory.search",
        "memory.for_file",
    ),
    # ask-heavy, one namespace per end user
    "memory_app": (
        "namespace.register",
        "memory.set",
        "memory.set_from_messages",
        "memory.search",
        "memory.ask",
        "memory.get",
        "memory.outcome",
    ),
    # bursty, thousands of namespaces
    "harness": (
        "namespace.register",
        "memory.set_from_messages",
        "memory.bulk_set",
        "memory.search",
        "memory.ask",
        "erasure.request",
        "cost.summary",
    ),
}

# Ops whose whole identity is (org, namespace): the client adds `agent` only
# to the ops that take one. Everything else gets org + namespace + consumer.
_NO_AGENT_OPS: frozenset[str] = frozenset(
    {
        "namespace.register",
        "namespace.get",
        "erasure.request",
        "erasure.status",
        "cost.summary",
        "code.index_status",
        "ops.capabilities",
    }
)
# Ops addressed by org alone (a scope or a window, not a namespace).
_NO_NAMESPACE_OPS: frozenset[str] = frozenset(
    {"ops.capabilities", "cost.summary", "erasure.request", "erasure.status"}
)


class MissingField(LoomError):
    """A response lacked a field the caller asserted with `CallResult.require`."""

    code = "MISSING_FIELD"


@dataclass
class CallResult:
    """One call's outcome. `data` is the raw response (a dict, `{}` on failure)."""

    op: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    code: str = ""
    error: str = ""
    attempts: int = 1
    elapsed_ms: float = 0.0
    # request fields the additive-field guard dropped because the catalogue
    # did not list them for this op (empty when no catalogue was read)
    dropped_fields: tuple[str, ...] = ()

    def require(self, *fields: str) -> dict[str, Any]:
        """The response with the named fields asserted present. Unknown fields
        are never an error — a client ignores what it does not know."""
        if not self.ok:
            raise MissingField(f"{self.op}: call failed [{self.code}] {self.error}")
        missing = [f for f in fields if f not in self.data]
        if missing:
            raise MissingField(f"{self.op}: response lacks {missing}")
        return self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


@dataclass
class ProbeReport:
    """What the catalogue said about the ops an archetype needs."""

    archetype: str
    source: str  # ops.capabilities | none
    build_commit: str
    served: list[str]
    missing: list[str]
    request_fields: dict[str, list[str]]
    unverified: list[str] = field(default_factory=list)  # needed, not listed by the source

    @property
    def ok(self) -> bool:
        return not self.missing

    def has_field(self, op: str, name: str) -> bool | None:
        """True / False when the catalogue lists the op's request fields; None
        when it does not (the short listing carries no fields)."""
        fields = self.request_fields.get(op)
        if fields is None:
            return None
        return name in fields

    def summary(self) -> str:
        head = f"probe[{self.archetype}] via {self.source or 'none'}"
        if self.build_commit:
            head += f" build {self.build_commit[:12]}"
        parts = [head, f"served {len(self.served)}/{len(self.served) + len(self.missing)}"]
        if self.missing:
            parts.append("missing " + ", ".join(self.missing))
        if self.unverified:
            parts.append("unverified " + ", ".join(self.unverified))
        return " · ".join(parts)


def normalize_consumer(consumer: str) -> str:
    """The tag as the server stores it (`consumer_label`). A client that names
    no consumer is recorded under its token's subject class, so every
    archetype must send one."""
    tag = consumer_label(consumer)
    if not tag:
        raise ValueError("consumer must be a non-empty printable string")
    return tag


class ReferenceClient:
    """One access layer for a consumer: identity + consumer tag on every call,
    timeouts, fail-soft retries, additive-field guards, outcome reporting and
    a catalogue probe."""

    def __init__(
        self,
        org: str,
        namespace: str,
        *,
        consumer: str,
        agent: str = "-",
        user_id: str | None = None,
        session_id: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        fail_soft: bool = True,
        max_retry_wait_s: float = 15.0,
        transport: Any | None = None,
    ) -> None:
        self.org = org
        self.namespace = namespace
        self.agent = agent
        self.user_id = user_id
        self.session_id = session_id
        self.consumer = normalize_consumer(consumer)
        self.timeout_s = timeout_s
        self.fail_soft = fail_soft
        self.max_retry_wait_s = max(0.0, float(max_retry_wait_s))
        self.run_id: str | None = None
        # Counters a bench or a dashboard reads: HTTP attempts (retries
        # included), calls per op, failures per code.
        self.attempts = 0
        self.calls_by_op: dict[str, int] = {}
        self.failures_by_code: dict[str, int] = {}
        self.catalogue: ProbeReport | None = None
        # An injected transport is the caller's: its consumer, its lifetime.
        self._owns_transport = transport is None
        self._transport: Any = transport or Transport(
            base_url or os.environ.get("LOOM_BASE_URL", "http://localhost:7777"),
            api_key or os.environ.get("LOOM_API_KEY"),
            timeout_s,
            ratelimit_max_wait_s=self.max_retry_wait_s,
            consumer=self.consumer,
        )

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> ReferenceClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_transport:
            await self._transport.close()

    @property
    def transport(self) -> Any:
        return self._transport

    # -- the one call path ---------------------------------------------------

    def _run_label(self, op: str) -> dict[str, str]:
        """The open run as the label header on a recall read, where a body run_id
        would filter; nothing on any other call."""
        if self.run_id and f"/v1/{op}" in RECALL_READ_PATHS:
            return {AMBIENT_RUN_HEADER: self.run_id}
        return {}

    def identity(self, op: str) -> dict[str, Any]:
        """The identity fields every request carries for this op."""
        ident: dict[str, Any] = {"org": self.org}
        if op not in _NO_NAMESPACE_OPS:
            ident["namespace"] = self.namespace
        if op not in _NO_AGENT_OPS and not op.startswith("namespace."):
            ident["agent"] = self.agent
            if self.user_id is not None:
                ident["user_id"] = self.user_id
            if self.session_id is not None:
                ident["session_id"] = self.session_id
            # On a recall read (RECALL_READ_PATHS) a body run_id filters to the run's own
            # rows; `call` labels those reads with the run's header instead.
            if self.run_id and op.startswith("memory.") and f"/v1/{op}" not in RECALL_READ_PATHS:
                ident["run_id"] = self.run_id
        return ident

    def guard_request(
        self, op: str, payload: Mapping[str, Any]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Additive-field guard on the request side: when the catalogue lists
        the op's request fields, a field it does not list is dropped (and
        named in the result) instead of being sent — the server would ignore
        it silently, and a client that depends on a field the deployment
        does not serve should learn that from the probe, not from a 422 or
        from nothing happening."""
        listed = self.catalogue.request_fields.get(op) if self.catalogue else None
        if listed is None:
            return dict(payload), ()
        allowed = set(listed)
        kept = {k: v for k, v in payload.items() if k in allowed}
        dropped = tuple(sorted(k for k in payload if k not in allowed))
        return kept, dropped

    async def call(
        self,
        op: str,
        payload: Mapping[str, Any] | None = None,
        *,
        required: Sequence[str] = (),
        timeout_s: float | None = None,
        with_identity: bool = True,
    ) -> CallResult:
        """POST /v1/<op> with the identity merged in (the transport adds the
        consumer tag). The transport retries per its policy and waits out a
        429 for at most `max_retry_wait_s`; a failure is a
        `CallResult(ok=False)` unless `fail_soft=False`."""
        body: dict[str, Any] = dict(payload or {})
        if with_identity:
            body = {**self.identity(op), **body}
        body, dropped = self.guard_request(op, body)
        self.calls_by_op[op] = self.calls_by_op.get(op, 0) + 1
        t0 = time.perf_counter()
        try:
            r = await self._transport.post_with_meta(
                f"/v1/{op}",
                body,
                timeout_s=timeout_s if timeout_s is not None else self.timeout_s,
                extra_headers=self._run_label(op),
            )
        except Exception as e:  # noqa: BLE001 — fail-soft: never crash the host session
            # A failed call reports no attempt count; it is counted once.
            self.attempts += 1
            code = e.code if isinstance(e, LoomError) else type(e).__name__
            return self._fail(op, code, str(e), 1, t0, dropped, exc=e)
        self.attempts += r.attempts
        data = dict(r.data) if isinstance(r.data, dict) else {}
        missing = [f for f in required if f not in data]
        if missing:
            return self._fail(
                op, "MISSING_FIELD", f"response lacks {missing}", r.attempts, t0, dropped, data=data
            )
        return CallResult(
            op=op,
            ok=True,
            data=data,
            attempts=r.attempts,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            dropped_fields=dropped,
        )

    def _fail(
        self,
        op: str,
        code: str,
        error: str,
        attempts: int,
        t0: float,
        dropped: tuple[str, ...],
        *,
        exc: BaseException | None = None,
        data: dict[str, Any] | None = None,
    ) -> CallResult:
        self.failures_by_code[code] = self.failures_by_code.get(code, 0) + 1
        if not self.fail_soft:
            if exc is not None:
                raise exc
            raise MissingField(f"{op}: {error}")
        return CallResult(
            op=op,
            ok=False,
            data=data or {},
            code=code,
            error=error,
            attempts=attempts,
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
            dropped_fields=dropped,
        )

    # -- the catalogue probe ----------------------------------------------------

    async def probe(self, archetype: str, *, extra_ops: Iterable[str] = ()) -> ProbeReport:
        """Read the deployment's catalogue and assert the ops this archetype
        needs are served, from `ops.capabilities` (every op with its request
        fields). The report is kept on `self.catalogue` so `guard_request` can
        use it."""
        if archetype not in ARCHETYPES:
            raise ValueError(f"unknown archetype {archetype!r}; one of {sorted(ARCHETYPES)}")
        needed = list(dict.fromkeys([*ARCHETYPES[archetype], *extra_ops]))
        source, build, ops, fields = await self._read_catalogue()
        listed = set(ops)
        served = [o for o in needed if o in listed]
        missing = [o for o in needed if o not in listed]
        unverified: list[str] = []
        if source == "none":
            unverified, missing = missing, []
        report = ProbeReport(
            archetype=archetype,
            source=source,
            build_commit=build,
            served=served,
            missing=missing,
            request_fields=fields,
            unverified=unverified,
        )
        self.catalogue = report
        return report

    async def _read_catalogue(self) -> tuple[str, str, list[str], dict[str, list[str]]]:
        """(source, build_commit, served op names, request fields by op) from
        `ops.capabilities`; `("none", "", [], {})` when it does not answer."""
        saved = self.fail_soft
        # a probe miss is expected, not a failure: the counters a bench prints
        # must not count the probe's own misses
        saved_failures = dict(self.failures_by_code)
        self.fail_soft = True
        try:
            r = await self.call("ops.capabilities", {}, with_identity=False)
        finally:
            self.fail_soft = saved
            self.failures_by_code = saved_failures
        if not (r.ok and isinstance(r.data.get("ops"), list)):
            return "none", "", [], {}
        ops = [str(o.get("name", "")) for o in r.data["ops"] if isinstance(o, dict)]
        fields = {
            str(o["name"]): [str(f) for f in o.get("request_fields", [])]
            for o in r.data["ops"]
            if isinstance(o, dict) and o.get("name")
        }
        return "ops.capabilities", str(r.data.get("build_commit", "")), ops, fields

    # -- namespace registration (the first call) ---------------------------------

    async def register_namespace(
        self,
        owner_email: str,
        *,
        traffic_class: str = "production",
        kind: str | None = None,
        description: str = "",
        tags: Iterable[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> CallResult:
        """`namespace.register` with an owner and a traffic class — the first
        call a consumer makes (every live namespace has an
        owner row; a bare first write registers one with the token subject
        as owner, this call names the owner and the class explicitly).

        `kind` defaults to `bench` for the bench class and `memory` otherwise.
        Never sets `retention_days`: a per-namespace retention is a TTL
        clause on eight tables."""
        if traffic_class not in TRAFFIC_CLASSES:
            raise ValueError(
                f"traffic_class must be one of {TRAFFIC_CLASSES}, got {traffic_class!r}"
            )
        body: dict[str, Any] = {
            "owner_email": owner_email,
            "description": description or f"{self.consumer} ({traffic_class})",
            "kind": kind or ("bench" if traffic_class == "bench" else "memory"),
            "tags": list(dict.fromkeys(list(tags) + [f"consumer:{self.consumer}"])),
            "traffic_class": traffic_class,
        }
        if extra:
            body.update(dict(extra))
        return await self.call("namespace.register", body, required=("registered",))

    # -- memory plane -------------------------------------------------------------

    async def set(self, memory_key: str, value: Any, **fields: Any) -> CallResult:
        return await self.call(
            "memory.set",
            {"memory_key": memory_key, "value": value, **fields},
            required=("memory_key", "version"),
        )

    async def get(self, memory_key: str, **fields: Any) -> CallResult:
        return await self.call(
            "memory.get", {"memory_key": memory_key, **fields}, required=("found",)
        )

    async def search(self, query: str, *, top_k: int = 10, **fields: Any) -> CallResult:
        return await self.call(
            "memory.search", {"query": query, "top_k": top_k, **fields}, required=("results",)
        )

    async def ask(
        self, query: str, *, top_k: int = 15, timeout_s: float | None = None, **fields: Any
    ) -> CallResult:
        """`memory.ask` is the one synthesized read; it takes seconds, so it
        carries its own timeout (default 2× the client's)."""
        return await self.call(
            "memory.ask",
            {"query": query, "top_k": top_k, **fields},
            required=("answer", "abstain"),
            timeout_s=timeout_s if timeout_s is not None else self.timeout_s * 2,
        )

    async def set_from_messages(
        self, messages: Sequence[Mapping[str, Any]], **fields: Any
    ) -> CallResult:
        return await self.call(
            "memory.set_from_messages",
            {"messages": list(messages), **fields},
            timeout_s=max(self.timeout_s, 120.0),
        )

    async def bulk_set(self, entries: Sequence[Mapping[str, Any]], **fields: Any) -> CallResult:
        return await self.call("memory.bulk_set", {"entries": list(entries), **fields})

    # -- code plane ------------------------------------------------------------------

    async def code_search(self, query: str, *, top_k: int = 5, **fields: Any) -> CallResult:
        return await self.call(
            "code.search", {"query": query, "top_k": top_k, **fields}, required=("results",)
        )

    async def code_symbol(self, name: str, **fields: Any) -> CallResult:
        return await self.call("code.symbol", {"name": name, **fields}, required=("definitions",))

    async def code_outline(self, path: str, **fields: Any) -> CallResult:
        return await self.call("code.outline", {"path": path, **fields})

    async def code_callers(self, name: str, *, depth: int = 1, **fields: Any) -> CallResult:
        return await self.call(
            "code.callers", {"name": name, "depth": depth, **fields}, required=("edges",)
        )

    async def code_grep(self, pattern: str, **fields: Any) -> CallResult:
        return await self.call("code.grep", {"pattern": pattern, **fields})

    # -- outcome reporting ---------------------------------------------------------------

    async def outcome(
        self,
        memory_key: str,
        outcome: Literal["positive", "negative", "used"],
        *,
        weight: int = 1,
        source: str | None = None,
    ) -> CallResult:
        """`memory.outcome` by key: `used` says the agent relied on the row,
        `positive` / `negative` say how the work it relied on ended.
        `source` defaults to the consumer tag so the
        feedback joins the consumer's own traffic."""
        return await self.call(
            "memory.outcome",
            {
                "memory_key": memory_key,
                "agent": "*",
                "outcome": outcome,
                "weight": weight,
                "source": source or self.consumer,
            },
            required=("recorded",),
        )

    async def code_outcome(
        self,
        action: str,
        *,
        trace_run_id: str = "",
        targets: Sequence[str] = (),
        spans: Sequence[str] = (),
        tokens_saved: int = 0,
        source: str | None = None,
        reason: str = "",
    ) -> CallResult:
        """`code.outcome`: what the agent did with a code answer
        (`opened_file` / `used_in_answer` / `fell_back_grep` / `ignored`,
        with the answer's `trace_run_id`) or a hook decision (`shunt_hit` /
        `shunt_pass`, no trace id; `source` is the pass reason for a pass).
        `reason` says why a `fell_back_grep` / `ignored`
        happened: abstained | empty | too_slow | wrong_shape | not_indexed
        | stale | preference; the server records anything else as
        `unattributed` and never rejects it."""
        body: dict[str, Any] = {
            "action": action,
            "targets": list(targets),
            "spans": list(spans),
            "tokens_saved": int(tokens_saved),
            "source": source or self.consumer,
        }
        if trace_run_id:
            body["trace_run_id"] = trace_run_id
        if reason:
            body["reason"] = reason
        return await self.call("code.outcome", body, required=("recorded",))

    async def run_begin(self, goal: str, **fields: Any) -> CallResult:
        """Open a run: until `run_complete`, memory calls carry its `run_id`,
        except recall reads (`RECALL_READ_PATHS`), where a body run_id filters
        to the run's own rows; those are labelled with it (`X-Loom-Run-Id`)
        instead, so the ledger still joins what the run read to what it did."""
        r = await self.call("run.begin", {"goal": goal, **fields}, required=("run_id",))
        if r.ok:
            self.run_id = str(r.data["run_id"])
        return r

    async def run_complete(
        self,
        status: Literal["completed", "failed", "cancelled"] = "completed",
        *,
        memories_used: Sequence[str] = (),
        outcome: Literal["", "positive", "negative", "none"] = "",
        final_response: str = "",
        **fields: Any,
    ) -> CallResult:
        if not self.run_id:
            return CallResult(op="run.complete", ok=False, code="NO_RUN", error="no open run")
        body: dict[str, Any] = {
            "run_id": self.run_id,
            "status": status,
            "memories_used": list(memories_used),
            "final_response": final_response[:8000],
            **fields,
        }
        if outcome:
            body["outcome"] = outcome
        r = await self.call("run.complete", body)
        self.run_id = None
        return r

    # -- economy readout -------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """What this client spent on the wire: attempts, calls per op,
        failures per code — the numbers an acceptance bench prints second."""
        calls = sum(self.calls_by_op.values())
        return {
            "consumer": self.consumer,
            "calls": calls,
            "attempts": self.attempts,
            "retries": max(0, self.attempts - calls),
            "calls_by_op": dict(sorted(self.calls_by_op.items())),
            "failures_by_code": dict(sorted(self.failures_by_code.items())),
        }


__all__ = [
    "ARCHETYPES",
    "TRAFFIC_CLASSES",
    "CallResult",
    "MissingField",
    "ProbeReport",
    "ReferenceClient",
    "normalize_consumer",
]
