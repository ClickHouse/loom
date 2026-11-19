"""Loom as a Pipecat memory service for voice agents.

``LoomMemoryService`` owns two frame processors that share one Loom agent and
user. The recall processor sits after the user context aggregator and before
the LLM: on each ``LLMContextFrame`` it runs ``memory.search`` with the turn's
latest user text and inserts the matches into the shared ``LLMContext``. The
capture processor sits after the assistant aggregator: when that aggregator
broadcasts ``LLMContextAssistantTurnFrame``, it sends the turn's user and
assistant text to ``memory.set_from_messages``. Audio frames pass through with
no Loom call::

    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
    )
    from loom import Agent
    from loom.integrations.pipecat import LoomMemoryService

    async with Agent(org="acme", namespace="support") as loom_agent:
        memory = LoomMemoryService(loom_agent, user_id="alice")
        context = LLMContext([{"role": "system", "content": "Be helpful."}])
        user, assistant = LLMContextAggregatorPair(context)
        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user,
                memory.recall_processor(),
                llm,
                tts,
                transport.output(),
                assistant,
                memory.capture_processor(),
            ]
        )

The agent keeps the call's conversation in the shared ``LLMContext``, and Loom
holds the memories across calls. Install with
``pip install 'clickhouse-loom[pipecat]'``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

try:
    from pipecat.frames.frames import Frame, LLMContextAssistantTurnFrame, LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMSpecificMessage
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.pipecat needs pipecat-ai: pip install 'clickhouse-loom[pipecat]'"
    ) from e

from loom.errors import LoomError
from loom.integrations._session_protection import _CallbackProtection
from loom.session_protection import AutomaticCaptureSuppressed, require_automatic_allowed
from loom.sync import SyncAgent

if TYPE_CHECKING:
    from loom.agent import Agent

# The server's cap on the messages of one memory.set_from_messages call.
_MAX_MESSAGES = 400
_MEMORY_PREFIX = "Relevant memory content from Loom:"


class LoomMemoryService:
    """Pipecat recall and capture processors over a Loom ``Agent``.

    ``recall_processor`` runs ``memory.search`` with the turn's latest user
    text and inserts the matches into the context; ``capture_processor`` sends
    the user and assistant text of the completed turn to
    ``memory.set_from_messages``. The mapping is in
    ``docs/features/integrations/frameworks.md`` (Pipecat). The processors are
    awaited on the pipeline, so the service takes a ``loom.Agent``.

    Use one service per conversation, bound to that conversation's logical
    session family. Native session ids label extracted turns; they do not select
    session protection. Constructor and callback contributors remain bound for
    the service's lifetime, including after the client is rebound.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        top_k: int = 5,
        honour_abstain: bool = True,
        defer: bool = True,
    ) -> None:
        if isinstance(agent, SyncAgent):
            raise TypeError("LoomMemoryService needs a loom.Agent: Pipecat awaits its processors")
        if not 1 <= top_k <= 500:
            raise ValueError("top_k must be between 1 and 500, memory.search's bounds")
        if user_id is not None and (user_id in ("", "-", "*") or user_id != user_id.strip()):
            raise ValueError(
                "user_id must name a Loom user without surrounding whitespace, or be None"
            )
        self._agent = agent
        self._protection = _CallbackProtection(agent.transport.session_protection)
        self._user_id = user_id
        self._session_id = session_id or uuid.uuid4().hex
        self._top_k = top_k
        self._honour_abstain = honour_abstain
        self._defer = defer
        self._last_user_message: Any | None = None
        self._written_messages: dict[int, Any] = {}
        self._context: Any | None = None

    def recall_processor(self) -> LoomRecallProcessor:
        """The processor that recalls memories into the context of a new turn."""
        return LoomRecallProcessor(self)

    def capture_processor(self) -> LoomCaptureProcessor:
        """The processor that writes a completed turn to Loom."""
        return LoomCaptureProcessor(self)

    async def _recall(self, frame: LLMContextFrame) -> None:
        # A speculative frame carries a provisional context that must not become
        # the capture source, and must not be treated as a committed user turn.
        if frame.speculation:
            return
        try:
            with self._protection.automatic(self._agent.transport.session_protection):
                context = frame.context
                self._context = context
                user_message = _latest_user_message(context.get_messages())
                if user_message is None:
                    return
                # Same committed message, including a second frame of this turn: already recalled.
                # A new message with the same text is a new turn and is searched again.
                if user_message is self._last_user_message:
                    return
                query = _message_text(user_message)
                result = await self._agent.memory.search(
                    query,
                    top_k=self._top_k,
                    user_id=self._user_id,
                    # Read the namespace this service writes to, whatever the deployment's default.
                    namespace_scope="this",
                    # Pinned so every top_k gets full bodies and no per-hit explanation
                    # (the server's defaults switch at 15).
                    bodies="full",
                    explain=False,
                )
                self._last_user_message = user_message
                hits = list(result)
                if self._honour_abstain:
                    if result.abstain:
                        _replace_memory_message(context, None)
                        return
                    # A response that does not abstain can still carry rows below the floor.
                    hits = [hit for hit in hits if not hit.below_threshold]
                # Derived rows may come back above the top_k slice: cut after filtering.
                lines = [
                    f"{i}. {hit.content_excerpt}" for i, hit in enumerate(hits[: self._top_k], 1)
                ]
                text = f"{_MEMORY_PREFIX}\n" + "\n".join(lines) if lines else None
                _replace_memory_message(context, text)
        finally:
            self._discard_protected_references()

    async def _capture(self, context: Any) -> None:
        try:
            with self._protection.automatic(self._agent.transport.session_protection):
                # Identity of the source message, not history length: compaction and
                # set_messages replace the list, which would skip a new turn if we sliced
                # from a stale cursor.
                items = _capturable(context.get_messages())
                # Keep tracked objects alive; discard objects removed by compaction.
                live = {id(source): source for source, _ in items}
                self._written_messages = {
                    key: source
                    for key, source in self._written_messages.items()
                    if live.get(key) is source
                }
                new = [
                    (source, message)
                    for source, message in items
                    if self._written_messages.get(id(source)) is not source
                ]
                if not new:
                    return
                loom_session = _loom_session(self._user_id or "-", self._session_id)
                for start in range(0, len(new), _MAX_MESSAGES):
                    batch = new[start : start + _MAX_MESSAGES]
                    await self._agent.memory.set_from_messages(
                        [message for _, message in batch],
                        user_id=self._user_id,
                        session_id=loom_session,
                        defer=self._defer,
                    )
                    require_automatic_allowed(self._agent.transport.session_protection)
                    self._written_messages.update((id(source), source) for source, _ in batch)
        finally:
            self._discard_protected_references()

    def _remember_protection(self) -> None:
        self._protection.remember(self._agent.transport.session_protection)

    def _discard_protected_references(self) -> None:
        if self._protection.protected:
            # Drop only our retry references, never the application's native history.
            self._context = None
            self._last_user_message = None
            self._written_messages.clear()


class LoomRecallProcessor(FrameProcessor):
    """Inserts Loom memories into an ``LLMContextFrame``; other frames pass through."""

    def __init__(self, service: LoomMemoryService) -> None:
        super().__init__()
        self._service = service

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        committed = isinstance(frame, LLMContextFrame) and not frame.speculation
        if committed:
            self._service._remember_protection()
        try:
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame):
                try:
                    await self._service._recall(frame)
                except (LoomError, AutomaticCaptureSuppressed):
                    # A protected turn skips automatic recall and still reaches the model.
                    pass
            await self.push_frame(frame, direction)
        finally:
            if committed:
                self._service._discard_protected_references()


class LoomCaptureProcessor(FrameProcessor):
    """Writes a completed assistant turn to Loom; other frames pass through."""

    def __init__(self, service: LoomMemoryService) -> None:
        super().__init__()
        self._service = service

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        committed = isinstance(frame, LLMContextFrame) and not frame.speculation
        completed = isinstance(frame, LLMContextAssistantTurnFrame)
        if committed or completed:
            self._service._remember_protection()
        try:
            await super().process_frame(frame, direction)
            if isinstance(frame, LLMContextFrame) and not frame.speculation:
                self._service._context = frame.context
            elif isinstance(frame, LLMContextAssistantTurnFrame):
                # The aggregator writes the assistant message before broadcasting this frame.
                await self._capture_from_last_context()
            await self.push_frame(frame, direction)
        finally:
            if committed or completed:
                self._service._discard_protected_references()

    async def _capture_from_last_context(self) -> None:
        context = getattr(self._service, "_context", None)
        if context is None:
            return
        try:
            await self._service._capture(context)
        except (LoomError, AutomaticCaptureSuppressed):
            # A protected turn skips automatic capture without blocking the pipeline.
            return


def _latest_user_message(messages: Sequence[Any]) -> Any | None:
    for message in reversed(messages):
        if _is_memory_message(message):
            continue
        if _role(message) == "user" and _message_text(message).strip():
            return message
    return None


def _capturable(messages: Sequence[Any]) -> list[tuple[Any, dict[str, object]]]:
    """The user and assistant text of `messages`, keyed by the source message identity."""
    out: list[tuple[Any, dict[str, object]]] = []
    for message in messages:
        if _is_memory_message(message):
            continue
        role = _role(message)
        text = _message_text(message)
        if role in ("user", "assistant") and text.strip():
            out.append((message, {"role": role, "content": text}))
    return out


def _role(message: Any) -> str | None:
    if isinstance(message, LLMSpecificMessage):
        return None
    if isinstance(message, dict):
        role = message.get("role")
        return role if isinstance(role, str) else None
    return None


def _message_text(message: Any) -> str:
    if isinstance(message, LLMSpecificMessage):
        return ""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        )
    return ""


def _is_memory_message(message: Any) -> bool:
    return _role(message) == "developer" and _message_text(message).startswith(_MEMORY_PREFIX)


def _replace_memory_message(context: Any, text: str | None) -> None:
    messages = [m for m in context.get_messages() if not _is_memory_message(m)]
    if text:
        # After the leading system or developer messages, so the model still
        # sees its instructions first.
        position = 0
        for i, message in enumerate(messages):
            if _role(message) in ("system", "developer"):
                position = i + 1
            else:
                break
        messages.insert(position, {"role": "developer", "content": text})
    context.set_messages(messages)


def _loom_session(user_id: str, session_id: str) -> str:
    """The Loom session of a Pipecat call: Loom files a turn's verbatim
    chunks by session alone, and a caller may choose its session ids."""
    pair = json.dumps([user_id, session_id])
    return f"pipecat-{hashlib.sha256(pair.encode()).hexdigest()[:20]}"
