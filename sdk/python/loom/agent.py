"""Agent — the SDK's primary entrypoint.

Async-first. The user opens an ``async with Agent(...) as agent``
block; synchronous workloads use :class:`loom.sync.SyncAgent`, which
wraps this client behind a private event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Literal

from loom._logging import logger
from loom.account import AccountClient
from loom.audit import AuditClient
from loom.blacklist import BlacklistClient
from loom.briefs import BriefsClient
from loom.calibration import CalibrationClient
from loom.capabilities import Capabilities
from loom.client import OpsClient
from loom.code import CodeClient
from loom.conflicts import ConflictsClient
from loom.context import ContextClient
from loom.conversations import ConversationsClient
from loom.cost import CostClient
from loom.credentials import (
    DEFAULT_SERVER,
    StoredLogin,
    same_server,
    signed_in_server,
    stored_login,
)
from loom.decision import DecisionClient
from loom.deployment import DeploymentClient
from loom.erasure import ErasureClient
from loom.errors import AuthInvalid
from loom.facts import FactsClient
from loom.grants import GrantsClient
from loom.knowledge import KnowledgeClient
from loom.learnings import LearningsClient
from loom.machine import MachineLogin, machine_login
from loom.memory import SENTINEL, MemoryClient
from loom.namespace import NamespaceClient
from loom.policy import PolicyClient
from loom.procedure import ProcedureClient
from loom.profile import ProfileClient
from loom.retry import RetryPolicy
from loom.run import AmbientRun, RunClient, current_run_id, stamp_write
from loom.session import Session
from loom.tasks import TasksClient
from loom.tools import AgentTools
from loom.tracker import TrackerClient
from loom.transport import CallResult, Transport, token_from_file
from loom.working_memory import WorkingMemoryClient

ANONYMOUS_AGENTS = frozenset({"", "-", "*"})


def default_agent_name(agent: str | None) -> str:
    """A real name for an anonymous agent — the process's
    script under an `sdk-python:` prefix — so per-agent profiles, costs and
    provenance are per bot. A given name is returned unchanged."""
    given = (agent or "").strip()
    if given not in ANONYMOUS_AGENTS:
        return given
    script = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    script = script.rsplit(".", 1)[0] if script.endswith(".py") else script
    return f"sdk-python:{script or 'bot'}"


def _credentials(
    base_url: str | None, api_key: str | None, profile: str | None
) -> tuple[str, str | None, StoredLogin | MachineLogin | None]:
    """The server and credential to use. A `profile` named here is the
    `loom login` session to use, on the server it signed in to. Otherwise
    the server is `base_url=`, else LOOM_BASE_URL, else localhost, and the
    credential, first found wins: `api_key=`, LOOM_API_KEY, a bot's
    LOOM_CLIENT_ID (loom.machine), the session of
    LOOM_PROFILE (else `default`) for that server, `~/.loom/token` (what
    `loom mint-token --save` writes)."""
    if profile:
        server = signed_in_server(profile)
        if server is None:
            raise AuthInvalid(f"not signed in (profile {profile!r}); run `loom login`")
        if api_key or (base_url and not same_server(base_url, server)):
            raise ValueError(
                f"profile {profile!r} is a session for {server}; pass one or the other"
            )
        return server, None, StoredLogin(profile, server)
    base_url = base_url or os.environ.get("LOOM_BASE_URL") or DEFAULT_SERVER
    api_key = api_key or os.environ.get("LOOM_API_KEY")
    if api_key:
        return base_url, api_key, None
    bot = machine_login(base_url)
    if bot is not None:
        return base_url, None, bot
    auth = stored_login(base_url)
    return base_url, None if auth else token_from_file(), auth


class Agent:
    """Loom client bound to an (org, namespace, agent) tuple.

    Usage::

        async with Agent(org="clickhouse",
                         namespace="eng-tooling",
                         agent="agent-detector",
                         base_url="http://localhost:7777") as agent:
            await agent.memory.set("last_pr", 12345, memory_type="semantic")
            entry = await agent.memory.get("last_pr")
    """

    def __init__(
        self,
        org: str,
        namespace: str,
        agent: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        pacing: bool = True,
        coalesce_sets: bool = True,
        ratelimit_max_wait_s: float | None = None,
        auto_run: bool | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        consumer: str = "",
        mentor_brief: bool | None = None,
        profile: str | None = None,
    ) -> None:
        """The options are described in docs/reference/python-sdk.md ("Construction").
        `auto_run` (None reads `LOOM_AUTO_RUN`) is off by default because a
        write under a run is scoped to it: a restatement of another scope's
        row is inserted as a `duplicate`, not merged. `consumer` is stamped
        on every op body that names none; '' stamps nothing."""
        self.org = org
        self.namespace = namespace
        # An agent that names itself "", "-" or "*" is every
        # other unnamed caller to the ledger, the mentor and the cost views.
        self.agent = default_agent_name(agent)
        if self.agent != agent:
            logger.warning(
                "loom: agent name %r is anonymous; using %r — pass a real agent name so the "
                "mentor, cost and provenance can tell this bot apart",
                agent,
                self.agent,
            )
        # Opt-in (or LOOM_MENTOR_BRIEF=on): a hidden request surprises a
        # test that counts them.
        if mentor_brief is None:
            mentor_brief = str(os.environ.get("LOOM_MENTOR_BRIEF", "")).strip().lower() in (
                "1",
                "on",
                "true",
                "yes",
            )
        self.mentor_brief = bool(mentor_brief)
        self._mentor_brief_started = False
        self._mentor_brief_task: asyncio.Task[None] | None = None
        self.coalesce_sets = bool(coalesce_sets)
        if auto_run is None:
            auto_run = str(os.environ.get("LOOM_AUTO_RUN", "")).strip().lower() in (
                "1",
                "true",
                "on",
                "yes",
            )
        self.auto_run = bool(auto_run)

        base_url, api_key, auth = _credentials(base_url, api_key, profile)

        self.transport = Transport(
            base_url=base_url,
            api_key=api_key,
            timeout_s=timeout_s,
            pacing=pacing,
            ratelimit_max_wait_s=ratelimit_max_wait_s,
            retry=retry,
            consumer=consumer,
            auth=auth,
        )
        #: The consumer label as the transport stamps it ('' = none).
        self.consumer: str = self.transport.consumer
        self._caps: Capabilities | None = None
        # Every served op as a generated method (`a.client.tracker.source_add`),
        # the same surface as the TypeScript `LoomClient`; the clients below are
        # the hand-written high-level layer.
        self.client = OpsClient(self)
        self.account = AccountClient(self)
        self.deployment = DeploymentClient(self)
        self.memory = MemoryClient(self)
        self.working_memory = WorkingMemoryClient(self)
        # Session briefs: a.briefs.save / get / list / delete.
        self.briefs = BriefsClient(self)
        self.knowledge = KnowledgeClient(self)
        self.conversations = ConversationsClient(self)
        self.learnings = LearningsClient(self)
        self.conflicts = ConflictsClient(self)
        self.blacklist = BlacklistClient(self)
        self.profile = ProfileClient(self)
        self.grants = GrantsClient(self)
        self.erasure = ErasureClient(self)
        self.cost = CostClient(self)
        self.audit = AuditClient(self)
        self.facts = FactsClient(self)
        # `namespaces` (plural) is the registry client; `namespace` (singular)
        # remains this Agent's bound namespace string.
        self.namespaces = NamespaceClient(self)
        self.procedure = ProcedureClient(self)
        self.run = RunClient(self)
        # The task inbox plane: a.tasks.pull / create / complete …
        self.tasks = TasksClient(self)
        self.policy = PolicyClient(self)
        self.decision = DecisionClient(self)
        self.calibration = CalibrationClient(self)
        self.code = CodeClient(self)
        self.tracker = TrackerClient(self)
        self.context = ContextClient(self)
        # The ambient run, installed as the transport's
        # before-post hook; None when auto_run is off.
        self.ambient_run: AmbientRun | None = None
        if self.auto_run:
            self.ambient_run = AmbientRun(self.run)
            self.transport.before_post = self.ambient_run.before_post

    @property
    def ambient_run_id(self) -> str:
        """The ambient run's id once its first memory-plane call opened it
        ('' before that, or when auto_run is off)."""
        return self.ambient_run.run_id if self.ambient_run is not None else ""

    def run_scope(self, **begin_kwargs: Any) -> Any:
        """One agent execution as an `async with` block:
        begins a run, credits the memories the block's recalls cited on
        exit, stamps the run_id on the writes inside and labels the reads with
        it. See loom.run.RunScope."""
        return self.run.scope(**begin_kwargs)

    async def __aenter__(self) -> Agent:
        return self

    # ---- the generic typed call and the capabilities probe ---

    SENTINEL = SENTINEL

    def identity(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, str]:
        """The identity tuple this Agent stamps on a call: (org, namespace,
        agent) from the constructor, the per-call scope with the wire's
        sentinel `"-"` where unset."""
        return {
            "org": self.org,
            "namespace": self.namespace,
            "agent": agent_id or self.agent,
            "user_id": user_id or self.SENTINEL,
            "session_id": session_id or self.SENTINEL,
            "run_id": run_id or self.SENTINEL,
        }

    def _prepare_call(
        self,
        op: str,
        body: Mapping[str, Any] | None,
        *,
        user_id: str | None,
        session_id: str | None,
        run_id: str | None,
        agent_id: str | None,
        idempotency_key: str | None,
        fields: dict[str, Any],
    ) -> tuple[str, dict[str, Any], str | None]:
        """(path, payload, idempotency key) for `call` / `call_with_meta`."""
        from loom.ops import spec as _spec

        payload: dict[str, Any] = {
            **self.identity(
                user_id=user_id, session_id=session_id, run_id=run_id, agent_id=agent_id
            ),
            **(body or {}),
            **fields,
        }
        path = f"/v1/{op}"
        stamp_write(path, payload, current_run_id())
        s = _spec(op)
        key = idempotency_key
        if key is None and s is not None and s.idempotent and not s.read_only:
            import uuid

            key = str(uuid.uuid4())
        return path, payload, key

    async def call(
        self,
        /,
        op: str,
        body: Mapping[str, Any] | None = None,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_s: float | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Call any wire operation by its dotted name with the identity filled
        in — the escape hatch for an op or a field this SDK version has no
        method for yet (the wire adds fields and never renames them).

        `body` and `**fields` are merged over the identity, so what you pass
        wins. Ops the registry marks idempotent, read-only ones aside, get an
        `Idempotency-Key` unless you supply one. `loom.ops.OPS` names every served op and
        `loom.ops.<Op>Input` its fields, for completion and type checking.
        `call_with_meta` is the same call returning the response metadata.
        """
        path, payload, key = self._prepare_call(
            op,
            body,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            fields=fields,
        )
        if op != "mentor.brief":
            self._start_mentor_brief()
        # `retry` is passed only when given so a caller's own transport-shaped
        # object (tests, adapters) need not know the kwarg.
        extra: dict[str, Any] = {"retry": retry} if retry is not None else {}
        return await self.transport.post(path, payload, key, timeout_s=timeout_s, **extra)

    async def mentor(self, *, window_seconds: int | None = None) -> dict[str, Any]:
        """The store's advice to this agent — `mentor.brief`
        for (org, namespace, agent): the planes it never uses, the hints with
        the counts behind them, and whether past hints were followed. Logs up
        to three hints at INFO and returns the brief. Call it once at
        startup; a bot that never renders a context pack has no other
        session-start channel. Per-call hints still arrive on every response
        (`X-Loom-Hint`, logged by the transport) without this call."""
        body = {"window_seconds": int(window_seconds)} if window_seconds else {}
        brief = await self.call("mentor.brief", body, timeout_s=10.0)
        hints = brief.get("hints") if isinstance(brief, dict) else None
        for h in (hints or [])[:3]:
            if isinstance(h, dict) and h.get("text"):
                logger.info(
                    "loom mentor (%s, %s): %s",
                    h.get("code", ""),
                    h.get("severity", ""),
                    h["text"],
                )
        return brief if isinstance(brief, dict) else {}

    async def guide(self, part: Literal["skill", "reference", "all"] = "skill") -> str:
        """How to use Loom, as Markdown — `mentor.guide`: the loop, where
        each kind of state goes and how to read answers (`part="skill"`),
        every plane with its ops (`"reference"`), or both (`"all"`). Put it
        in the system prompt of an agent that has no plugin to carry it."""
        out = await self.call("mentor.guide", {"part": part}, timeout_s=10.0)
        return str(out["text"])

    def _start_mentor_brief(self) -> None:
        """With `mentor_brief=True`, the first call also runs `mentor()` in
        the background, once; a failure logs nothing."""
        if self._mentor_brief_started or not self.mentor_brief:
            return
        self._mentor_brief_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _quiet() -> None:
            try:
                await self.mentor()
            except Exception:  # noqa: BLE001 — advice never fails the bot
                return

        self._mentor_brief_task = loop.create_task(_quiet())

    async def call_with_meta(
        self,
        /,
        op: str,
        body: Mapping[str, Any] | None = None,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_s: float | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        **fields: Any,
    ) -> CallResult:
        """`call`, returning a `CallResult`: `.data` is the
        body, `.request_id` finds the call in the server's ledger,
        `.schema_version`, `.server_timing_ms` and `.rate_limit` are what the
        server reported on the response."""
        path, payload, key = self._prepare_call(
            op,
            body,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            fields=fields,
        )
        return await self.transport.post_with_meta(
            path, payload, key, timeout_s=timeout_s, retry=retry
        )

    # ---- what the deployment serves and the token may call --

    async def capabilities(self, *, max_age_s: float = 60.0) -> Capabilities:
        """`ops.capabilities` paired with this token's claims, fetched once and
        reused for `max_age_s` (0 refetches)."""
        cached = self._caps
        if cached is not None and time.monotonic() - cached.fetched_at <= max_age_s:
            return cached
        raw = await self.transport.post("/v1/ops.capabilities", {})
        # A bot's or a session's token may be fetched here: off the event loop.
        claims = await asyncio.to_thread(self.transport.token_claims)
        caps = Capabilities.from_response(raw, claims)
        self._caps = caps
        return caps

    async def can_call(self, op: str) -> bool | None:
        """Whether this Agent's token may call `op` on its namespace: True /
        False when known, None when the SDK cannot tell — then make the call."""
        return (await self.capabilities()).can_call(op, self.namespace)

    # ---- what an agent framework holds -------------------

    def session(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        flush_every: int = 8,
        transcript: bool = True,
        carry_over: int = 4,
        extraction: Mapping[str, Any] | None = None,
    ) -> Session:
        """A conversation this Agent resumes, appends to and flushes: see `Session`."""
        return Session(
            self,
            session_id,
            user_id=user_id,
            run_id=run_id,
            flush_every=flush_every,
            transcript=transcript,
            carry_over=carry_over,
            extraction=extraction,
        )

    def tools(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        only: list[str] | None = None,
        max_result_chars: int = 8_000,
    ) -> AgentTools:
        """Loom's verbs as the model's tools, bound to a scope: see `AgentTools`."""
        return AgentTools(
            self,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            only=only,
            max_result_chars=max_result_chars,
        )

    async def probe(
        self,
        *,
        ops: list[str] | None = None,
        fields: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Ask the deployment what it serves (`ops.capabilities`) and compare
        with what this caller needs. Returns {ok, build_commit,
        schema_version, missing_ops, missing_fields, unknown_to_sdk}: the ops
        or request fields the deployment lacks, and the ops it serves that
        this SDK's generated table does not know (a newer server)."""
        from loom.ops import OPS

        caps = await self.capabilities(max_age_s=0)
        served = caps.ops
        missing_ops = [o for o in (ops or []) if o not in served]
        missing_fields: dict[str, list[str]] = {}
        for op_name, wanted in (fields or {}).items():
            have = set(served.get(op_name, {}).get("request_fields") or [])
            miss = [f for f in wanted if f not in have]
            if miss:
                missing_fields[op_name] = miss
        return {
            "ok": not missing_ops and not missing_fields,
            "build_commit": caps.build_commit,
            "schema_version": caps.schema_version,
            "missing_ops": missing_ops,
            "missing_fields": missing_fields,
            "unknown_to_sdk": sorted(n for n in served if n not in OPS),
        }

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close(failed=exc_type is not None)

    async def close(self, failed: bool = False) -> None:
        # A brief still in flight would only reach a closed pool.
        if self._mentor_brief_task is not None:
            self._mentor_brief_task.cancel()
        # Sets still gathered for a coalesced bulk write go out before the
        # run completes and the pool closes; a caller never loses a write it
        # was awaiting, and the ambient run sees it.
        try:
            await self.memory.flush_sets()
            if self.ambient_run is not None:
                await self.ambient_run.complete(failed=failed)
        finally:
            try:
                # Always: sets made while the ambient run completed, or while a cancelled
                # close() was waiting, land too, up to one request's timeout, before the
                # pool closes under them.
                with contextlib.suppress(TimeoutError):
                    # A Transport always has it; a stand-in without it waits unbounded.
                    timeout_s = getattr(self.transport, "_timeout_s", None)
                    await asyncio.wait_for(self.memory.flush_sets(), timeout_s)
            finally:
                await self.transport.close()  # even when the flush fails or is cancelled
