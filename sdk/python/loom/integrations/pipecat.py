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
        if user_id is not None and (user_id in ("", "-") or user_id != user_id.strip()):
            raise ValueError(
                "user_id must name a Loom user without surrounding whitespace, or be None"
            )
        self._agent = agent
        self._user_id = user_id
        self._session_id = session_id or uuid.uuid4().hex
        self._top_k = top_k
        self._honour_abstain = honour_abstain
        self._defer = defer
        self._last_query: str | None = None
        self._written = 0
        self._context: Any | None = None

    def recall_processor(self) -> LoomRecallProcessor:
        """The processor that recalls memories into the context of a new turn."""
        return LoomRecallProcessor(self)

    def capture_processor(self) -> LoomCaptureProcessor:
        """The processor that writes a completed turn to Loom."""
        return LoomCaptureProcessor(self)

    async def _recall(self, frame: LLMContextFrame) -> None:
        context = frame.context
        self._context = context
        if frame.speculation:
            return
        query = _latest_user_text(context.get_messages())
        if not query or query == self._last_query:
            return
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
        self._last_query = query
        hits = list(result)
        if self._honour_abstain:
            if result.abstain:
                _replace_memory_message(context, None)
                return
            # A response that does not abstain can still carry rows below the floor.
            hits = [hit for hit in hits if not hit.below_threshold]
        # Derived rows may come back above the top_k slice: cut after filtering.
        lines = [f"{i}. {hit.content_excerpt}" for i, hit in enumerate(hits[: self._top_k], 1)]
        text = f"{_MEMORY_PREFIX}\n" + "\n".join(lines) if lines else None
        _replace_memory_message(context, text)

    async def _capture(self, context: Any) -> None:
        messages = _texts(context.get_messages())
        new = messages[self._written :]
        if not new:
            return
        loom_session = _loom_session(self._user_id or "-", self._session_id)
        for start in range(0, len(new), _MAX_MESSAGES):
            await self._agent.memory.set_from_messages(
                new[start : start + _MAX_MESSAGES],
                user_id=self._user_id,
                session_id=loom_session,
                defer=self._defer,
            )
        self._written = len(messages)


class LoomRecallProcessor(FrameProcessor):
    """Inserts Loom memories into an ``LLMContextFrame``; other frames pass through."""

    def __init__(self, service: LoomMemoryService) -> None:
        super().__init__()
        self._service = service

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            try:
                await self._service._recall(frame)
            except LoomError:
                # The turn still reaches the model; audio and other frames are unchanged.
                pass
        await self.push_frame(frame, direction)


class LoomCaptureProcessor(FrameProcessor):
    """Writes a completed assistant turn to Loom; other frames pass through."""

    def __init__(self, service: LoomMemoryService) -> None:
        super().__init__()
        self._service = service

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextAssistantTurnFrame):
            # The aggregator writes the assistant message before it broadcasts this frame.
            await self._capture_from_last_context()
        await self.push_frame(frame, direction)

    async def _capture_from_last_context(self) -> None:
        context = getattr(self._service, "_context", None)
        if context is None:
            return
        try:
            await self._service._capture(context)
        except LoomError:
            return


def _latest_user_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if _is_memory_message(message):
            continue
        if _role(message) == "user":
            text = _message_text(message)
            if text.strip():
                return text
    return ""


def _texts(messages: Sequence[Any]) -> list[dict[str, object]]:
    """The user and assistant text of `messages`; memory blocks and tool calls are left out."""
    out: list[dict[str, object]] = []
    for message in messages:
        if _is_memory_message(message):
            continue
        role = _role(message)
        text = _message_text(message)
        if role in ("user", "assistant") and text.strip():
            out.append({"role": role, "content": text})
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
