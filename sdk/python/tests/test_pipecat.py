"""LoomMemoryService: Pipecat recall before the model and capture after the
turn, over a fake Loom agent — one search per turn, the write after it, no
Loom call on audio frames, abstention, sessions and the refusals."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import sys
from typing import Any

import pytest
from loom import Agent as LoomAgent
from loom import SyncAgent
from loom.errors import LoomError
from loom.integrations.pipecat import (
    _MEMORY_PREFIX,
    LoomMemoryService,
    _loom_session,
)
from loom.memory import MemorySearchHit, MemorySearchResult
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMContextAssistantTurnFrame,
    LLMContextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class _FakeMemory:
    def __init__(self, hits: list[MemorySearchHit] | None = None, abstain: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._hits = hits or []
        self._abstain = abstain

    async def search(self, query: str, **kw: Any) -> MemorySearchResult:
        self.calls.append(("search", (query,), kw))
        result = MemorySearchResult(self._hits)
        result.abstain = self._abstain
        return result

    async def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> None:
        self.calls.append(("set_from_messages", (messages,), kw))


class _Loom:
    def __init__(self, memory: _FakeMemory) -> None:
        self.memory = memory


def _hit(key: str, text: str, **kw: Any) -> MemorySearchHit:
    return MemorySearchHit(
        memory_key=key, memory_type="semantic", content_excerpt=text, similarity=0.9, **kw
    )


def _service(fake: _FakeMemory, **kw: Any) -> LoomMemoryService:
    return LoomMemoryService(_Loom(fake), **{"user_id": "alice", "session_id": "call-1", **kw})  # type: ignore[arg-type]


async def _feed(
    processor: FrameProcessor, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
) -> list[tuple[Frame, FrameDirection]]:
    pushed: list[tuple[Frame, FrameDirection]] = []

    async def capture(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pushed.append((frame, direction))

    processor.push_frame = capture  # type: ignore[method-assign]
    await processor.process_frame(frame, direction)
    return pushed


def _audio() -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=b"\x00\x00" * 16, sample_rate=16000, num_channels=1)


def _context(*pairs: tuple[str, str]) -> LLMContext:
    return LLMContext([{"role": role, "content": text} for role, text in pairs])


def _memory_text(context: LLMContext) -> str:
    for message in context.get_messages():
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.startswith(_MEMORY_PREFIX):
                return content
    return ""


def _recall_and_capture(
    service: LoomMemoryService, context: LLMContext, reply: str = "Noted."
) -> None:
    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=context))
        context.add_message({"role": "assistant", "content": reply})
        await _feed(
            service.capture_processor(),
            LLMContextAssistantTurnFrame(text=reply, timestamp="2026-01-01T00:00:00Z"),
        )

    asyncio.run(go())


def test_the_memories_reach_the_model_once_per_turn() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats"), _hit("k2", "Alice is vegetarian")])
    context = _context(("system", "Be brief."), ("user", "Where should I sit?"))
    service = _service(fake, top_k=3)

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=context))
        # A second context frame for the same user text is not a new turn.
        await _feed(service.recall_processor(), LLMContextFrame(context=context))

    asyncio.run(go())
    (search,) = [c for c in fake.calls if c[0] == "search"]
    assert search[1] == ("Where should I sit?",)
    assert search[2] == {
        "top_k": 3,
        "user_id": "alice",
        "namespace_scope": "this",
        "bodies": "full",
        "explain": False,
    }
    text = _memory_text(context)
    assert "1. Alice prefers aisle seats" in text and "2. Alice is vegetarian" in text
    assert [m for m in context.get_messages() if isinstance(m, dict) and m.get("role") == "system"]


def test_the_turn_is_sent_to_loom_after_the_turn() -> None:
    fake = _FakeMemory()
    service = _service(fake)
    context = _context(("user", "I prefer aisle seats"))
    _recall_and_capture(service, context, "Noted.")
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    searches = [c for c in fake.calls if c[0] == "search"]
    assert len(searches) == 1
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "I prefer aisle seats"), ("assistant", "Noted.")]
    ]
    loom_session = _loom_session("alice", "call-1")
    assert [c[2] for c in extracts] == [
        {"user_id": "alice", "session_id": loom_session, "defer": True}
    ]


def test_audio_frames_do_not_call_loom() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats")])
    service = _service(fake)
    context = _context(("user", "Where should I sit?"))

    async def go() -> None:
        recall, capture = service.recall_processor(), service.capture_processor()
        for frame in (_audio(), _audio()):
            for proc in (recall, capture):
                pushed = await _feed(proc, frame)
                assert pushed == [(frame, FrameDirection.DOWNSTREAM)]
        assert fake.calls == []
        await _feed(recall, LLMContextFrame(context=context))
        await _feed(recall, _audio())
        await _feed(capture, _audio())
        context.add_message({"role": "assistant", "content": "Aisle."})
        await _feed(
            capture,
            LLMContextAssistantTurnFrame(text="Aisle.", timestamp="2026-01-01T00:00:00Z"),
        )
        await _feed(recall, _audio())
        await _feed(capture, _audio())

    asyncio.run(go())
    assert [c[0] for c in fake.calls] == ["search", "set_from_messages"]


def test_a_speculative_context_frame_does_not_search() -> None:
    fake = _FakeMemory([_hit("k1", "fact")])
    context = _context(("user", "Where should I sit?"))
    service = _service(fake)
    asyncio.run(
        _feed(service.recall_processor(), LLMContextFrame(context=context, speculation=True))
    )
    assert fake.calls == []
    assert _memory_text(context) == ""


def test_a_cancelled_speculation_does_not_replace_the_committed_capture() -> None:
    """A provisional context must not become the source of the durable write."""
    fake = _FakeMemory()
    service = _service(fake)
    committed = _context(("user", "I prefer aisle seats"))
    speculative = _context(("user", "unconfirmed guess"))

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=committed))
        await _feed(
            service.recall_processor(),
            LLMContextFrame(context=speculative, speculation=True),
        )
        committed.add_message({"role": "assistant", "content": "Noted."})
        await _feed(
            service.capture_processor(),
            LLMContextAssistantTurnFrame(text="Noted.", timestamp="2026-01-01T00:00:00Z"),
        )

    asyncio.run(go())
    searches = [c[1][0] for c in fake.calls if c[0] == "search"]
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert searches == ["I prefer aisle seats"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "I prefer aisle seats"), ("assistant", "Noted.")]
    ]


def test_an_accepted_speculation_still_captures_the_committed_turn() -> None:
    """After a provisional frame, the committed context remains the capture source."""
    fake = _FakeMemory()
    service = _service(fake)
    committed = _context(("user", "I prefer aisle seats"))
    speculative = _context(("user", "I prefer aisle seats"))

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=committed))
        await _feed(
            service.recall_processor(),
            LLMContextFrame(context=speculative, speculation=True),
        )
        await _feed(service.recall_processor(), LLMContextFrame(context=committed))
        committed.add_message({"role": "assistant", "content": "Noted."})
        await _feed(
            service.capture_processor(),
            LLMContextAssistantTurnFrame(text="Noted.", timestamp="2026-01-01T00:00:00Z"),
        )

    asyncio.run(go())
    searches = [c[1][0] for c in fake.calls if c[0] == "search"]
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert searches == ["I prefer aisle seats"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "I prefer aisle seats"), ("assistant", "Noted.")]
    ]


def test_the_first_confirmed_speculation_uses_the_assistant_context() -> None:
    fake = _FakeMemory()
    service = _service(fake)
    committed = _context(("user", "I prefer aisle seats"), ("assistant", "Noted."))
    provisional = _context(("user", "unconfirmed guess"))

    async def go() -> None:
        capture = service.capture_processor()
        await _feed(
            service.recall_processor(), LLMContextFrame(context=provisional, speculation=True)
        )
        # Accepted native speculation omits a committed recall frame. The assistant
        # aggregator supplies its committed context before the completed-turn frame.
        await _feed(capture, LLMContextFrame(context=committed))
        await _feed(capture, LLMContextFrame(context=provisional, speculation=True))
        await _feed(
            capture,
            LLMContextAssistantTurnFrame(text="Noted.", timestamp="2026-01-01T00:00:00Z"),
        )

    asyncio.run(go())
    assert [c for c in fake.calls if c[0] == "search"] == []
    extracts = [c[1][0] for c in fake.calls if c[0] == "set_from_messages"]
    assert extracts == [
        [
            {"role": "user", "content": "I prefer aisle seats"},
            {"role": "assistant", "content": "Noted."},
        ]
    ]


def test_recycled_integer_ids_do_not_hide_a_replacement_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Model reused integer identities after compaction without depending on the
    # allocator's timing. Source message objects and their contents are distinct.
    adapter = importlib.import_module("loom.integrations.pipecat")
    monkeypatch.setattr(
        adapter, "id", lambda message: 1 if message["role"] == "user" else 2, raising=False
    )
    fake = _FakeMemory()
    service = _service(fake)
    context = _context(("user", "first turn"))
    _recall_and_capture(service, context, "first reply")
    context.set_messages([{"role": "user", "content": "replacement turn"}])
    _recall_and_capture(service, context, "replacement reply")
    assert [c[1][0] for c in fake.calls if c[0] == "search"] == ["first turn", "replacement turn"]
    extracts = [c[1][0] for c in fake.calls if c[0] == "set_from_messages"]
    assert extracts == [
        [
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "first reply"},
        ],
        [
            {"role": "user", "content": "replacement turn"},
            {"role": "assistant", "content": "replacement reply"},
        ],
    ]


def test_partial_capture_retries_only_the_unacknowledged_batch() -> None:
    class FailingSecondBatch(_FakeMemory):
        async def set_from_messages(self, messages: list[dict[str, Any]], **kw: Any) -> None:
            await super().set_from_messages(messages, **kw)
            if len([c for c in self.calls if c[0] == "set_from_messages"]) == 2:
                raise LoomError("retry the unacknowledged tail")

    fake = FailingSecondBatch()
    service = _service(fake)
    context = LLMContext([{"role": "user", "content": f"fact {index}"} for index in range(402)])

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=context))
        capture = service.capture_processor()
        turn = LLMContextAssistantTurnFrame(text="Noted.", timestamp="2026-01-01T00:00:00Z")
        await _feed(capture, turn)
        await _feed(capture, turn)

    asyncio.run(go())
    batches = [c[1][0] for c in fake.calls if c[0] == "set_from_messages"]
    assert [len(batch) for batch in batches] == [400, 2, 2]
    assert batches[1] == batches[2]
    assert {message["content"] for message in batches[0]}.isdisjoint(
        message["content"] for message in batches[2]
    )


def test_a_replaced_context_still_writes_the_new_turn() -> None:
    """Compacting or replacing history must not skip the next completed turn."""
    fake = _FakeMemory()
    service = _service(fake)
    context = _context(("user", "I prefer aisle seats"))
    _recall_and_capture(service, context, "Noted.")
    context.set_messages([{"role": "user", "content": "I fly to Lisbon"}])

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=context))
        context.add_message({"role": "assistant", "content": "Safe travels."})
        await _feed(
            service.capture_processor(),
            LLMContextAssistantTurnFrame(text="Safe travels.", timestamp="2026-01-01T00:00:01Z"),
        )

    asyncio.run(go())
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "I prefer aisle seats"), ("assistant", "Noted.")],
        [("user", "I fly to Lisbon"), ("assistant", "Safe travels.")],
    ]


def test_a_repeated_utterance_is_a_new_turn() -> None:
    """Equal text on a new committed user message is a new turn, not a duplicate frame."""
    fake = _FakeMemory()
    service = _service(fake)
    context = _context(("user", "yes"))
    _recall_and_capture(service, context, "Go on.")
    context.add_message({"role": "user", "content": "yes"})

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=context))
        context.add_message({"role": "assistant", "content": "Understood."})
        await _feed(
            service.capture_processor(),
            LLMContextAssistantTurnFrame(text="Understood.", timestamp="2026-01-01T00:00:01Z"),
        )

    asyncio.run(go())
    searches = [c[1][0] for c in fake.calls if c[0] == "search"]
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert searches == ["yes", "yes"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "yes"), ("assistant", "Go on.")],
        [("user", "yes"), ("assistant", "Understood.")],
    ]


def test_two_turns_search_and_write_once_each() -> None:
    fake = _FakeMemory()
    service = _service(fake)
    context = _context(("user", "I prefer aisle seats"))
    _recall_and_capture(service, context, "Noted.")
    context.add_message({"role": "user", "content": "I fly to Lisbon"})

    async def go() -> None:
        await _feed(service.recall_processor(), LLMContextFrame(context=context))
        context.add_message({"role": "assistant", "content": "Safe travels."})
        await _feed(
            service.capture_processor(),
            LLMContextAssistantTurnFrame(text="Safe travels.", timestamp="2026-01-01T00:00:01Z"),
        )

    asyncio.run(go())
    searches = [c[1][0] for c in fake.calls if c[0] == "search"]
    extracts = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert searches == ["I prefer aisle seats", "I fly to Lisbon"]
    assert [[(m["role"], m["content"]) for m in c[1][0]] for c in extracts] == [
        [("user", "I prefer aisle seats"), ("assistant", "Noted.")],
        [("user", "I fly to Lisbon"), ("assistant", "Safe travels.")],
    ]


def test_the_loom_session_is_per_user_and_unambiguous() -> None:
    assert _loom_session("alice", "s1") != _loom_session("bob", "s1")
    assert _loom_session("a/b", "c") != _loom_session("a", "b/c")
    assert ":" not in _loom_session("a:b", "c")  # Loom's chunk keys are `chunk:<session>:<n>`


def test_memory_blocks_and_empty_text_are_not_sent() -> None:
    fake = _FakeMemory([_hit("k1", "Alice prefers aisle seats")])
    service = _service(fake, defer=False)
    context = _context(("system", "Be brief."), ("user", "  "), ("user", "hi"))
    _recall_and_capture(service, context, "Noted.")
    ((_, (messages,), kw),) = [c for c in fake.calls if c[0] == "set_from_messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hi"),
        ("assistant", "Noted."),
    ]
    assert kw["defer"] is False
    assert not any(
        isinstance(m, dict) and isinstance(m.get("content"), str) and _MEMORY_PREFIX in m["content"]
        for m in messages
    )


def test_abstention_and_rows_below_the_floor_add_nothing() -> None:
    weak = dataclasses.replace(_hit("k2", "weak"), below_threshold=True)
    context = _context(("user", "q"))
    asyncio.run(
        _feed(_service(_FakeMemory([weak])).recall_processor(), LLMContextFrame(context=context))
    )
    assert _memory_text(context) == ""
    context = _context(("user", "q"))
    asyncio.run(
        _feed(
            _service(_FakeMemory([_hit("k1", "strong")], abstain=True)).recall_processor(),
            LLMContextFrame(context=context),
        )
    )
    assert _memory_text(context) == ""
    context = _context(("user", "q"))
    asyncio.run(
        _feed(
            _service(_FakeMemory([weak]), honour_abstain=False).recall_processor(),
            LLMContextFrame(context=context),
        )
    )
    assert "1. weak" in _memory_text(context)


def test_derived_rows_are_cut_to_top_k_after_filtering() -> None:
    weak = dataclasses.replace(_hit("w", "weak"), below_threshold=True)
    hits = [weak] + [_hit(f"k{i}", f"fact {i}") for i in range(4)]
    context = _context(("user", "q"))
    asyncio.run(
        _feed(
            _service(_FakeMemory(hits), top_k=2).recall_processor(),
            LLMContextFrame(context=context),
        )
    )
    text = _memory_text(context)
    assert "1. fact 0" in text and "2. fact 1" in text
    assert "weak" not in text and "fact 2" not in text


def test_a_turn_without_user_text_reads_nothing() -> None:
    fake = _FakeMemory()
    context = LLMContext(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x"}}]}]
    )
    asyncio.run(_feed(_service(fake).recall_processor(), LLMContextFrame(context=context)))
    assert not [c for c in fake.calls if c[0] == "search"]


def test_a_long_turn_is_sent_in_batches_the_server_takes() -> None:
    fake = _FakeMemory()
    service = _service(fake)
    pairs = [("user", f"fact {i}") for i in range(401)]
    context = _context(*pairs)
    _recall_and_capture(service, context, "Noted.")
    sizes = [len(c[1][0]) for c in fake.calls if c[0] == "set_from_messages"]
    assert sizes == [400, 2]


def test_without_a_user_the_service_reads_and_writes_the_namespace() -> None:
    fake = _FakeMemory()
    service = _service(fake, user_id=None, session_id="call-1")
    context = _context(("user", "q"))
    _recall_and_capture(service, context, "Noted.")
    search, extract = fake.calls
    assert search[2]["user_id"] is None and extract[2]["user_id"] is None
    assert extract[2]["session_id"] == _loom_session("-", "call-1")


def test_it_refuses_a_sync_agent_a_top_k_out_of_bounds_and_a_user_naming_none() -> None:
    with pytest.raises(TypeError, match="awaits"):
        LoomMemoryService(object.__new__(SyncAgent))
    for top_k in (0, 501):
        with pytest.raises(ValueError, match="top_k"):
            _service(_FakeMemory(), top_k=top_k)
    for user in ("", "-", " ", " alice", "alice "):
        with pytest.raises(ValueError, match="user_id"):
            _service(_FakeMemory(), user_id=user)


def test_without_pipecat_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "pipecat" or name.startswith("pipecat."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "loom.integrations.pipecat", raising=False)
    with pytest.raises(ImportError, match=r"clickhouse-loom\[pipecat\]"):
        importlib.import_module("loom.integrations.pipecat")


def test_a_recall_error_still_passes_the_context_frame() -> None:
    class _Boom(_FakeMemory):
        async def search(self, query: str, **kw: Any) -> MemorySearchResult:
            self.calls.append(("search", (query,), kw))
            raise LoomError("loom down")

    fake = _Boom()
    context = _context(("user", "q"))
    frame = LLMContextFrame(context=context)
    pushed = asyncio.run(_feed(_service(fake).recall_processor(), frame))
    assert pushed == [(frame, FrameDirection.DOWNSTREAM)]
    assert _memory_text(context) == ""


def test_a_real_agent_posts_the_service_calls() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"results": []}

    loom = LoomAgent(org="acme", namespace="travel", agent="bot", base_url="http://x", api_key="k")
    loom.transport.post = post  # type: ignore[method-assign]
    service = LoomMemoryService(loom, user_id="alice", top_k=3, session_id="call-1")
    context = _context(("user", "seat"))
    _recall_and_capture(service, context, "Noted.")
    assert [p for p, _ in posts] == ["/v1/memory.search", "/v1/memory.set_from_messages"]
    search, extract = (body for _, body in posts)
    assert (search["query"], search["top_k"], search["user_id"]) == ("seat", 3, "alice")
    assert (search["namespace_scope"], search["bodies"], search["explain"]) == (
        "this",
        "full",
        False,
    )
    assert (extract["user_id"], extract["session_id"]) == (
        "alice",
        _loom_session("alice", "call-1"),
    )
    assert [m["content"] for m in extract["messages"]] == ["seat", "Noted."]
