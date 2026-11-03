"""A replayed observation is a line, not a payload."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import chunk_projection as chunk_projection
from server.memory.app import observe_replay as replay
from server.memory.workers import observe_extract as ox

NARRATIVE = (
    "[tool_use] tool=mcp__plugin_loom_loom__loom_code_symbol\n"
    'input: {"name":"StorageSharedMergeTree::mergeSelectingTask","namespace":"code-example-private",'
    '"include_body":true,"token_budget":2500}\n'
    'output: {"definitions":[{"unit_id":"84ad0bfe5df4b1a1f66c907d13c7460f","path":"src/Storages/'
    'StorageSharedMergeTree.cpp","start_line":2635,"end_line":2720,"body":"void StorageSharedMergeTree::'
    "mergeSelectingTask()\\n{\\n    auto component_guard = ..."
)


def test_compact_keeps_head_error_paths_and_short_plain_payloads() -> None:
    out = replay.compact_narrative(NARRATIVE, ["src/Storages/StorageSharedMergeTree.cpp"])
    assert out == (
        "[tool_use] tool=mcp__plugin_loom_loom__loom_code_symbol\n"
        "paths: src/Storages/StorageSharedMergeTree.cpp"
    )
    # a shell command and its one-line result are the session's own words: kept
    assert replay.compact_narrative(
        "[command_run] tool=Bash\ninput: pytest -q\noutput: 1 failed"
    ) == ("[command_run] tool=Bash\ninput: pytest -q\noutput: 1 failed")
    with_error = '[tool_error] tool=Bash\ninput: {"command":"make"}\nerror: exit 2: missing header\noutput: ...'
    assert (
        replay.compact_narrative(with_error)
        == "[tool_error] tool=Bash\nerror: exit 2: missing header\noutput: ..."
    )
    # a pretty-printed JSON payload's continuation lines go with it; a long plain one is dropped too
    multi = (
        '[tool_use] tool=Read\ninput: {\n  "file_path": "a.cpp"\n}\noutput: '
        + "x" * 300
        + "\nmore\npaths: a.cpp"
    )
    assert replay.compact_narrative(multi) == "[tool_use] tool=Read\npaths: a.cpp"
    assert replay.compact_narrative("") == ""


def test_observation_narrative_compacts_the_payloads() -> None:
    row = SimpleNamespace(
        value={
            "narrative": NARRATIVE,
            "kind": "tool_use",
            "tool_name": "x",
            "file_paths": ["src/a.cpp"],
        }
    )
    text = replay._observation_narrative(row)
    assert "input:" not in text and "output:" not in text and "paths: src/a.cpp" in text
    # no narrative: the structured fallback is unchanged
    assert replay._observation_narrative(
        SimpleNamespace(value={"kind": "tool_use", "tool_name": "Read"})
    ) == ("[tool_use] tool=Read")


def test_replay_turns_carry_the_compact_line() -> None:
    # Only an observation that carries a result replays; an error does.
    obs = [SimpleNamespace(value={"narrative": NARRATIVE, "error": "exit 1"}, created_at=None)]
    turns = replay.replay_messages(obs, agent="claude-code")
    assert len(turns) == 1 and turns[0].role == "user"
    assert turns[0].content.startswith("[tool_use] tool=") and "output:" not in turns[0].content


@pytest.mark.asyncio
async def test_chunk_layer_is_a_no_op_while_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []

    class _Boom:
        def __getattr__(self, name):  # any client use would be a bug under suppression
            called.append(name)
            raise AssertionError("chunk writer touched the client while suppressed")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Boom())
    inp = SimpleNamespace(org="o", namespace="n", run_id="-")
    msgs = [{"role": "user", "content": "x" * 3000}]
    assert not chunk_projection.chunk_layer_suppressed()
    with chunk_projection.suppress_chunk_layer():
        assert chunk_projection.chunk_layer_suppressed()
        await chunk_projection.write_memory_chunks(inp, msgs)
    assert not chunk_projection.chunk_layer_suppressed()
    assert called == []


def test_the_worker_replays_under_suppression() -> None:
    # The replay runs in `_extract_replay`, which `_distill_session` calls.
    assert "_extract_replay(" in inspect.getsource(ox._distill_session)
    src = inspect.getsource(replay._extract_replay)
    assert "with suppress_chunk_layer():" in src
    assert src.index("with suppress_chunk_layer():") < src.index(
        "handle_memory_set_from_messages(extract_in)"
    )


_SESSION = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "s"}


def _obs(
    key: str, value: Any, *, sensitivity: str = "", version: int = 10, tags: list[str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        **_SESSION,
        memory_key=key,
        value=value,
        content_sha256=repr(value),
        sensitivity=sensitivity,
        tags=list(tags or []),
        valid_at=None,
        version=version,
    )


def _heads(monkeypatch: pytest.MonkeyPatch, heads: list[Any], order: list[str]) -> None:
    """The re-read before the DONE insert answers `heads`, keyed by identity."""
    import server.memory.store as memory_repo

    async def _read(keys: list[Any], **_k: Any) -> dict[Any, Any]:
        order.append("re-read")
        live = {ox._identity(h): h for h in heads}
        return {k: live[k] for k in keys if k in live}

    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", _read)


async def test_done_is_built_from_the_head_as_it_is_after_the_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # During the replay one observation is deleted, one given new text, one relabelled
    # and one tagged READY. The DONE rows must not bring the deleted row or the old text
    # back, and carry what the others hold now.
    import server.memory.store as memory_repo

    kept = _obs("kept", {"narrative": "ran the tests"})
    deleted = _obs("deleted", {"narrative": "pasted the customer list"})
    rewritten = _obs("rewritten", {"narrative": "old"})
    relabelled = _obs("relabelled", {"narrative": "my phone is 555"})
    readied = _obs("readied", {"narrative": "opened the file"})
    observations = [kept, deleted, rewritten, relabelled, readied]
    now_high = _obs("relabelled", relabelled.value, sensitivity="high", version=20)
    now_ready = _obs("readied", readied.value, version=20, tags=["crystallize:ready"])
    order: list[str] = []
    _heads(
        monkeypatch,
        [kept, _obs("rewritten", {"narrative": "new"}, version=20), now_high, now_ready],
        order,
    )
    built: list[tuple[Any, dict[str, Any]]] = []
    done: list[Any] = []

    async def _replay(*_a: Any, **_k: Any) -> int:
        return 0

    async def _extras(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(events_written=0, procedure_written=0, hint_written=0, failures=[])

    async def _context(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _insert(rows: list[Any], **_k: Any) -> None:
        done.extend(rows)

    def _build(o: Any, **k: Any) -> Any:
        built.append((o, k))
        return o

    def _stamp() -> int:
        order.append("stamp")
        return 99

    monkeypatch.setattr(ox, "_extract_replay", _replay)
    monkeypatch.setattr(ox, "_distill_extras", _extras)
    monkeypatch.setattr(ox, "_load_session_context", _context)
    monkeypatch.setattr(ox, "get_client_for_context", lambda: None)
    monkeypatch.setattr(ox, "next_version", _stamp)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(memory_repo, "build_memory_version_row", _build)
    monkeypatch.setattr(memory_repo, "insert_memory_versions", _insert)
    distilled = await ox._distill_session(_SESSION, observations)
    assert done == [kept, now_high, now_ready]
    assert [k for _, k in built] == [{"version": 99, "tags": ["crystallize:done"]}] * 3
    # the stamp is drawn before the re-read, so a write drawn after the re-read wins
    assert order == ["stamp", "re-read"]
    assert distilled.observations == 3


async def test_a_high_observation_is_never_sent_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sweep labels an observation holding a credential 'high'; the distill pass replays
    # and summarises only the others, and still marks every one DONE.
    import server.memory.store as memory_repo

    plain = _obs("plain", {"narrative": "ran the tests"})
    high = _obs("high", {"narrative": "token sk-123"}, sensitivity="high")
    # capture writes an observation unlabelled; the sweep labels it later
    unlabelled = _obs("unlabelled", {"narrative": "export GITHUB_TOKEN=ghp_" + "a" * 36})
    seen: dict[str, Any] = {}

    def _replay(obs: list[Any], **_k: Any) -> list[Any]:
        seen["replayed"] = obs
        return []

    async def _extras(_s: Any, obs: list[Any], **_k: Any) -> Any:
        seen["summarised"] = obs
        return SimpleNamespace(events_written=0, procedure_written=0, hint_written=0, failures=[])

    async def _context(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _insert(rows: list[Any], **_k: Any) -> None:
        seen["done"] = rows

    monkeypatch.setattr(ox, "replay_messages", _replay)
    monkeypatch.setattr(ox, "_distill_extras", _extras)
    monkeypatch.setattr(ox, "_load_session_context", _context)
    monkeypatch.setattr(ox, "get_client_for_context", lambda: None)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(memory_repo, "build_memory_version_row", lambda o, **_k: o)
    monkeypatch.setattr(memory_repo, "insert_memory_versions", _insert)
    # a token on its own line, and a quoted password: JSON-escaped, neither reads as one
    own_line = _obs("own_line", {"narrative": "[command_run] tool=Bash\noutput:\nghp_" + "b" * 36})
    quoted = _obs("quoted", {"narrative": 'config: password: "hunter2secret"'})
    observations = [plain, high, unlabelled, own_line, quoted]
    _heads(monkeypatch, observations, [])
    await ox._distill_session(_SESSION, observations)
    assert seen["replayed"] == [plain] and seen["summarised"] == [plain]
    assert seen["done"] == [plain, high, unlabelled, own_line, quoted]
