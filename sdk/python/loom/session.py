"""`Session` — the unit an agent framework holds.

An agent's persistence is a session: resume it after a restart, append
turns as they happen, park a large tool result and keep a pointer in the
transcript, drop notes the next turn must see. Loom has an op for each;
`Session` binds them to one (user, session) scope so an agent loop calls
four verbs::

    s = bot.session("thread-42", user_id="u1")
    resumed = await s.resume(query="resume the billing migration")
    system_prompt, history = resumed.system, resumed.messages
    await s.append({"role": "user", "content": "..."}, {"role": "assistant", "content": "..."})
    blob = await s.offload(build_log, summary="pytest run 812")   # keep blob.pointer
    await s.close()                                                 # flush → extraction

Appended turns go to the conversation transcript (`conversation.append`,
replayed by `history()` / `resume()`) and, in batches of `flush_every`, to
`memory.set_from_messages`, where the server extracts the facts worth
keeping. `resume()` is `context.pack` for this scope plus the transcript
tail. The blocking twin is `SyncAgent.session(...)`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom._logging import logger
from loom.errors import NotFound

if TYPE_CHECKING:
    from loom.agent import Agent

Turn = Mapping[str, Any]


@dataclass
class Resumed:
    """What a restarted process needs: the rendered context block for the
    system prompt, the pack it came from, and the recent transcript turns
    (oldest first; `[]` when the session never wrote one)."""

    system: str
    pack: dict[str, Any]
    messages: list[dict[str, Any]] = field(default_factory=list)


class Session:
    """One conversation under one (user, session) scope. See the module doc."""

    def __init__(
        self,
        agent: Agent,
        session_id: str,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        flush_every: int = 8,
        transcript: bool = True,
        carry_over: int = 4,
        extraction: Mapping[str, Any] | None = None,
    ) -> None:
        self.agent = agent
        self.session_id = session_id
        self.user_id = user_id
        self.run_id = run_id
        #: Turns buffered before `memory.set_from_messages` runs; 0 = only on flush()/close().
        self.flush_every = max(0, int(flush_every))
        #: Also write every turn to the conversation transcript.
        self.transcript = bool(transcript)
        #: Turns from the previous flush handed to the extractor as `last_messages`.
        self.carry_over = max(0, int(carry_over))
        self._extraction: dict[str, Any] = dict(extraction or {})
        self._pending: list[dict[str, Any]] = []
        self._last_flushed: list[dict[str, Any]] = []
        self._thread_created = False
        #: None until the first append; False when the namespace has conversation
        #: logging off (an admin turns it on with `conversation.set_logging`).
        self.transcript_stored: bool | None = None

    def scope(self) -> dict[str, str | None]:
        """The per-call identity every op in this session carries."""
        return {"session_id": self.session_id, "user_id": self.user_id, "run_id": self.run_id}

    async def _call(self, op: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.agent.call(
            op, body, user_id=self.user_id, session_id=self.session_id, run_id=self.run_id
        )

    @property
    def buffered(self) -> list[dict[str, Any]]:
        """Turns appended since the last flush (a copy)."""
        return list(self._pending)

    async def resume(
        self,
        *,
        query: str = "",
        mode: str = "startup",
        token_budget: int = 2000,
        history: int = 20,
        **pack_fields: Any,
    ) -> Resumed:
        """The context pack for this scope (the system prompt) and the recent
        transcript. `history=0` skips the transcript read."""
        pack = await self._call(
            "context.pack",
            {"query": query, "mode": mode, "token_budget": token_budget, **pack_fields},
        )
        messages: list[dict[str, Any]] = []
        if history > 0 and self.transcript:
            messages = await self.history(history)
        return Resumed(system=str(pack.get("rendered") or ""), pack=pack, messages=messages)

    async def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """The most recent transcript turns, oldest first; `[]` for a new thread."""
        try:
            got = await self._call(
                "conversation.get",
                {"thread_id": self.session_id, "include_messages": True, "limit": limit},
            )
        except NotFound:
            return []
        if not got.get("found"):
            return []
        msgs = got.get("messages") or []
        return [dict(m) for m in msgs if isinstance(m, Mapping)]

    async def append(self, *turns: Turn) -> dict[str, Any] | None:
        """Record turns: each goes to the transcript now; extraction runs
        once `flush_every` turns are buffered (its result is returned then)."""
        if not turns:
            return None
        if self.transcript and self.transcript_stored is not False:
            await self._ensure_thread()
            for t in turns:
                body: dict[str, Any] = {
                    "thread_id": self.session_id,
                    "msg_id": str(uuid.uuid4()),
                    "role": t.get("role", "user"),
                    "content": t["content"],
                }
                if t.get("name"):
                    body["metadata"] = {"name": t["name"]}
                res = await self._call("conversation.append", body)
                if res.get("stored") is False and res.get("reason") == "logging_disabled":
                    self.transcript_stored = False
                    logger.warning(
                        "session %s: conversation logging is off for namespace %s; turns are "
                        "not stored in the transcript (an admin enables it with "
                        "conversation.set_logging). Extraction still runs.",
                        self.session_id,
                        self.agent.namespace,
                    )
                    break
                self.transcript_stored = True
        self._pending.extend(dict(t) for t in turns)
        if self.flush_every and len(self._pending) >= self.flush_every:
            return await self.flush()
        return None

    async def flush(self) -> dict[str, Any] | None:
        """Send the buffered turns to `memory.set_from_messages` now."""
        if not self._pending:
            return None
        batch, self._pending = self._pending, []
        body: dict[str, Any] = {**self._extraction, "messages": batch}
        if self._last_flushed and self.carry_over:
            body["last_messages"] = self._last_flushed[-self.carry_over :]
        try:
            out = await self._call("memory.set_from_messages", body)
        except Exception:
            self._pending = batch + self._pending  # not lost; the next flush retries
            raise
        self._last_flushed = batch
        return out

    async def offload(
        self,
        content: str,
        *,
        summary: str = "",
        blob_key: str = "",
        ttl_seconds: int | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Park a large tool result once; keep `["pointer"]` in the transcript."""
        body: dict[str, Any] = {"content": content, "summary": summary, "blob_key": blob_key}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        if content_type is not None:
            body["content_type"] = content_type
        return await self._call("context.offload", body)

    async def fetch(
        self,
        blob_key: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        pattern: str | None = None,
        context_lines: int | None = None,
    ) -> dict[str, Any]:
        """Read a parked result back: a character slice, or the lines matching `pattern`."""
        body: dict[str, Any] = {"blob_key": blob_key}
        for k, v in (
            ("offset", offset),
            ("limit", limit),
            ("pattern", pattern),
            ("context_lines", context_lines),
        ):
            if v is not None:
                body[k] = v
        return await self._call("context.fetch", body)

    async def note(
        self, addition: str, *, block_key: str | None = None, separator: str | None = None
    ) -> dict[str, Any]:
        """Append to the working-memory block the next turn's pack carries."""
        body: dict[str, Any] = {"addition": addition}
        if block_key is not None:
            body["block_key"] = block_key
        if separator is not None:
            body["separator"] = separator
        return await self._call("working_memory.append", body)

    async def close(self) -> dict[str, Any] | None:
        """Flush what is buffered; call it when the conversation or the process ends."""
        return await self.flush()

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _ensure_thread(self) -> None:
        if self._thread_created:
            return
        await self._call("conversation.create", {"thread_id": self.session_id})
        self._thread_created = True
