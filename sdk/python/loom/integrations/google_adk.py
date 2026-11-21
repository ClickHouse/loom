"""Loom as a Google ADK memory service.

``LoomMemoryService`` implements ``google.adk.memory.BaseMemoryService``, the
service a ``Runner`` takes as ``memory_service=``. ADK's memory tools read it:
``preload_memory`` searches with the user's message before each model call,
``load_memory`` lets the model search. A callback saves each turn::

    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import preload_memory
    from google.genai import types
    from loom import Agent
    from loom.integrations.google_adk import LoomMemoryService


    async def save_turn(ctx):
        turn = [e for e in ctx.session.events if e.invocation_id == ctx.invocation_id]
        await ctx.add_events_to_memory(events=turn)


    async with Agent(org="acme", namespace="support") as loom_agent:
        sessions = InMemorySessionService()
        runner = Runner(
            app_name="support",
            agent=LlmAgent(
                name="assistant",
                model="gemini-flash-latest",
                tools=[preload_memory],
                after_agent_callback=save_turn,
            ),
            session_service=sessions,
            memory_service=LoomMemoryService(loom_agent, app_name="support"),
        )
        session = await sessions.create_session(app_name="support", user_id="alice")
        message = types.Content(role="user", parts=[types.Part(text="I prefer aisle seats")])
        async for event in runner.run_async(
            user_id="alice", session_id=session.id, new_message=message
        ):
            ...

ADK scopes memory by app and user. Loom's boundary is the namespace, so a
service serves one ADK app, the namespace of its Loom agent, and refuses a
call for another app; the ADK user is the Loom ``user_id``, and extraction runs
under a Loom session derived from the user and the ADK session. Install with
``pip install 'clickhouse-loom[google-adk]'``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

try:
    from google.adk.events import Event, get_active_events
    from google.adk.memory import BaseMemoryService
    from google.adk.memory.base_memory_service import SearchMemoryResponse
    from google.adk.memory.memory_entry import MemoryEntry
    from google.adk.sessions import Session
    from google.genai import types
except ImportError as e:  # the framework is an optional extra
    raise ImportError(
        "loom.integrations.google_adk needs google-adk: pip install 'clickhouse-loom[google-adk]'"
    ) from e

from loom.sync import SyncAgent

if TYPE_CHECKING:
    from loom.agent import Agent


# The server's cap on the messages of one memory.set_from_messages call.
_MAX_MESSAGES = 400


class LoomMemoryService(BaseMemoryService):
    """ADK ``BaseMemoryService`` over a Loom ``Agent``, for one ADK app.

    Saved sessions and turns go to ``memory.set_from_messages``, ``add_memory`` to
    ``memory.set``. ``search_memory`` reads the ADK user's memories first, then
    fills its remaining slots from the app namespace's knowledge documents. A fact's
    ``id`` names it for ``delete_memory``. ADK
    awaits its memory service, so the service takes a ``loom.Agent``. The mapping of
    each method is in ``docs/features/integrations/frameworks.md`` (Google ADK).
    """

    def __init__(
        self,
        agent: Agent,
        *,
        app_name: str,
        top_k: int = 10,
        honour_abstain: bool = True,
        defer: bool = True,
        key_prefix: str = "adk",
    ) -> None:
        if isinstance(agent, SyncAgent):
            raise TypeError("LoomMemoryService needs a loom.Agent: ADK awaits its memory service")
        if not app_name:
            raise ValueError("app_name must name the ADK app this service serves")
        if not 1 <= top_k <= 500:  # memory.search's bounds
            raise ValueError("top_k must be between 1 and 500")
        key_prefix = key_prefix.strip("/")
        if not key_prefix:
            raise ValueError("key_prefix must not be empty")
        self._agent = agent
        self._app_name = app_name
        self._top_k = top_k
        self._honour_abstain = honour_abstain
        self._defer = defer
        self._key_prefix = key_prefix

    async def add_session_to_memory(self, session: Session) -> None:
        self._check_scope(session.app_name, session.user_id)
        await self._extract(session.user_id, session.id, get_active_events(session.events))

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._check_scope(app_name, user_id)
        if not session_id:
            # Loom files a turn's verbatim chunks by session alone, not by user.
            raise ValueError("add_events_to_memory needs the ADK session_id")
        await self._extract(user_id, session_id, get_active_events(list(events)))

    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._check_scope(app_name, user_id)
        for memory in memories:
            text = _text(memory.content)
            if not text:
                continue
            # The user is in the key: Loom files a long value's passages by key alone.
            digest = _digest(user_id, text)
            await self._agent.memory.set(
                f"{self._key_prefix}/{digest}",
                text,
                user_id=user_id,
                valid_at=_iso_time(memory.timestamp),
            )

    async def search_memory(
        self, *, app_name: str, user_id: str, query: str
    ) -> SearchMemoryResponse:
        self._check_scope(app_name, user_id)
        if not query.strip():
            return SearchMemoryResponse()
        result = await self._agent.memory.search(
            query,
            top_k=self._top_k,
            user_id=user_id,
            # Read the namespace this service writes to, whatever the deployment's default.
            namespace_scope="this",
            # Pinned so every top_k gets full bodies and no per-hit explanation
            # (the server's defaults switch at 15).
            bodies="full",
            explain=False,
        )
        hits = [] if self._honour_abstain and result.abstain else list(result)
        if self._honour_abstain:
            # A response that does not abstain can still carry rows below the floor.
            hits = [hit for hit in hits if not hit.below_threshold]
        memories = [
            MemoryEntry(
                # Stored facts support exact deletion; chunks and summaries need erasure.
                id=_entry_id(hit.memory_key, hit.session_id, hit.agent, hit.version)
                if hit.source in ("fact", "anchor")
                and hit.agent
                and hit.agent != "*"
                and hit.version > 0
                else None,
                content=types.Content(role="user", parts=[types.Part(text=hit.content_excerpt)]),
                custom_metadata={"similarity": hit.similarity, "source": hit.source},
                timestamp=hit.valid_at or None,
            )
            # Derived rows may come back above the top_k slice: cut after filtering.
            for hit in hits[: self._top_k]
        ]
        if len(memories) < self._top_k:
            documents = await self._agent.knowledge.search(
                query, top_k=min(100, self._top_k - len(memories)), namespace_scope="this"
            )
            memories.extend(
                MemoryEntry(
                    content=types.Content(role="user", parts=[types.Part(text=hit.content)]),
                    custom_metadata={
                        "source": "knowledge",
                        "source_uri": hit.source_uri,
                        "chunk_idx": hit.chunk_idx,
                    },
                )
                for hit in documents[: self._top_k - len(memories)]
            )
        return SearchMemoryResponse(memories=memories)

    async def delete_memory(
        self, *, app_name: str, user_id: str, memory_id: str | None = None
    ) -> None:
        self._check_scope(app_name, user_id)
        if not memory_id:  # no id, nothing to delete: ADK's InMemoryMemoryService also returns
            return
        key, session, writer, version = _parse_entry_id(memory_id)
        # Exact identity and version: a removed or rewritten target cannot select another row.
        await self._agent.call(
            "memory.delete_version",
            memory_key=key,
            version=version,
            agent_id=writer,
            user_id=user_id,
            session_id=session,
        )

    def _check_scope(self, app_name: str, user_id: str) -> None:
        # Loom merges a write into a matching memory of the namespace whichever agent
        # wrote it, so two apps in one namespace would share and lose memories.
        if app_name != self._app_name:
            raise ValueError(
                f"this LoomMemoryService serves the ADK app {self._app_name!r}, not "
                f"{app_name!r}: give each app its own Loom namespace and service"
            )
        # Reads trim user ids; refusing noncanonical ids keeps writes and reads on one user.
        if user_id in ("", "-", "*") or user_id != user_id.strip():
            raise ValueError("user_id must name a Loom user without surrounding whitespace")

    async def _extract(self, user_id: str, session_id: str, events: Sequence[Event]) -> None:
        messages: list[dict[str, object]] = []
        for event in events:
            text = _text(event.content)
            if text:
                messages.append(
                    {
                        "role": "user" if event.author == "user" else "assistant",
                        "content": text,
                        "timestamp": datetime.fromtimestamp(event.timestamp, UTC).isoformat(),
                    }
                )
        session = _loom_session(user_id, session_id)
        for start in range(0, len(messages), _MAX_MESSAGES):
            await self._agent.memory.set_from_messages(
                messages[start : start + _MAX_MESSAGES],
                user_id=user_id,
                session_id=session,
                defer=self._defer,
            )


def _entry_id(memory_key: str, session_id: str, agent: str, version: int) -> str:
    """The complete row identity and version keep another writer's fact out of a delete."""
    return json.dumps([memory_key, session_id or "-", agent, str(version)])


def _parse_entry_id(memory_id: str) -> tuple[str, str, str, str]:
    try:
        parsed = json.loads(memory_id)
    except ValueError:
        parsed = None
    if not (
        isinstance(parsed, list)
        and len(parsed) == 4
        and all(isinstance(p, str) and p for p in parsed)
        and parsed[2] != "*"
        and len(parsed[3]) <= 20
        and parsed[3].isascii()
        and parsed[3].isdigit()
        and 0 < int(parsed[3]) < 2**64
    ):
        raise ValueError(f"{memory_id!r} is not an id this service's search returned")
    return parsed[0], parsed[1], parsed[2], parsed[3]


def _loom_session(user_id: str, session_id: str) -> str:
    """The Loom session of an ADK user's session: ADK makes a session id unique per
    user only, and Loom files a turn's verbatim chunks by session alone."""
    return f"adk-{_digest(user_id, session_id)}"


def _digest(*parts: str) -> str:
    """The first 20 hex of the sha256 of `parts` as a JSON list, so two different tuples
    never hash the same text."""
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def _iso_time(text: str | None) -> datetime | None:
    """An ISO 8601 timestamp; ADK allows any text, which is not a ``valid_at``. One
    without a zone is local time, as ADK formats its own entries' timestamps."""
    try:
        moment = datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
    return moment.astimezone() if moment and moment.tzinfo is None else moment


def _text(content: types.Content | None) -> str:
    """The text of a content, without the model's thoughts or tool calls."""
    if content is None or not content.parts:
        return ""
    return " ".join(p.text for p in content.parts if p.text and not p.thought).strip()
