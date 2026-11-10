"""A compiler-tier delta is read from its file one value at a time and applied a round
at a time, so the memory it costs is one round's however large the file; a delta over
a cap, or one that does not parse, is refused before anything is applied."""

from __future__ import annotations

import json
import os
import tracemalloc
from pathlib import Path
from typing import Any

import pydantic
import pytest

from server.code.app.index.delta_file import open_delta
from server.code.app.index.scip_delta import ScipDelta, apply_delta
from server.code.app.index.untrusted_files import FileRefused
from server.settings import settings


def _entry(i: int) -> dict[str, Any]:
    symbol = {
        "qualified_name": f"DB::Storage{i}::read",
        "name": "read",
        "signature": "void read(const Settings & settings, size_t max_block_size)",
        "line_start": 10,
        "line_end": 90,
    }
    edge = {
        "from_qualified_name": f"DB::Storage{i}::read",
        "to_qualified_name": "DB::f",
        "line": 12,
    }
    return {"path": f"src/Storages/S{i}.cpp", "symbols": [symbol] * 20, "edges": [edge] * 80}


def _write(path: Path, files: int, **head: Any) -> None:
    """A delta as the harness's converter writes it: the header, one entry per line."""
    with path.open("w") as fh:
        fh.write(json.dumps({"delta_version": 1, "commit": "c" * 40, **head})[:-1] + ',"files":[')
        for i in range(files):
            fh.write(("\n" if i == 0 else ",\n") + json.dumps(_entry(i), separators=(",", ":")))
        fh.write('\n],"deleted_files":["src/gone.cpp"],"features":["ctor_calls"]}\n')


def _entry_nodes() -> int:
    line = json.dumps(_entry(0), separators=(",", ":"))
    return line.count("{") + line.count("[") + line.count(",")


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_scip_delta_file_max_mb", 1024)
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 100_000_000)
    # a round holds 14 entries
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", 14 * _entry_nodes() + 1)


def test_the_header_paths_and_tail_are_read_before_any_round(tmp_path: Path, caps: None) -> None:
    path = tmp_path / "delta.json"
    _write(path, 30)
    delta = open_delta(path)
    assert delta.commit == "c" * 40 and delta.features == ["ctor_calls"]
    assert delta.deleted_files == ["src/gone.cpp"]
    assert delta.paths == [f"src/Storages/S{i}.cpp" for i in range(30)]
    rounds = list(delta.rounds(100, lambda p: True))
    # the deleted path joins the last round
    assert [len(r) for r in rounds] == [14, 14, 3]
    assert [f.path for r in rounds for f in r] == [*delta.paths, "src/gone.cpp"]


def test_a_large_delta_costs_one_rounds_memory(tmp_path: Path, caps: None) -> None:
    path = tmp_path / "delta.json"
    _write(path, 6_000)  # about 60 MB
    size = path.stat().st_size
    tracemalloc.start()
    try:
        delta = open_delta(path)
        for batch in delta.rounds(100, lambda p: True):
            del batch
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert size > 50_000_000
    # the bytes alone would be the size; a whole parse holds many times it
    assert peak < size / 5, (peak, size)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b'{"commit":"c","files":[{"path":"a"},{"path":', ValueError),
        (b'{"commit":"c","files":[{"path":"a"},{"symbols":0}]}', ValueError),
        (b'{"files":[]}', ValueError),  # no commit
        (b'{"commit":"c","files":[]} x', ValueError),
    ],
    ids=["cut-off", "bad-entry", "no-commit", "trailing"],
)
def test_an_invalid_delta_is_refused_before_any_round(
    tmp_path: Path, caps: None, body: bytes, error: type[Exception]
) -> None:
    path = tmp_path / "delta.json"
    path.write_bytes(body)
    with pytest.raises(error, match="delta invalid"):
        open_delta(path)


def test_one_value_over_the_round_budget_is_refused(tmp_path: Path, caps: None) -> None:
    path = tmp_path / "delta.json"
    edge = {"from_qualified_name": "a", "to_qualified_name": "b"}
    big = {"path": "a", "edges": [edge] * (settings.code_scip_delta_round_max_nodes // 2)}
    path.write_text(json.dumps({"commit": "c", "files": [big]}))
    with pytest.raises(FileRefused, match=r"file entry 0: .*\(code_scip_delta_round_max_nodes\)"):
        open_delta(path)


@pytest.mark.parametrize("key", ["deleted_files", "commit", "notes"])
def test_a_value_over_the_round_budget_outside_files_is_invalid(
    tmp_path: Path, caps: None, key: str
) -> None:
    """Only a file entry can be too large for a round; a path, a header value or an
    ignored key that large is not one the harness writes, so no cap's fix applies."""
    path = tmp_path / "delta.json"
    big = "," * (settings.code_scip_delta_round_max_nodes + 1)
    body: dict[str, Any] = {"commit": "c" * 40, "files": []}
    body[key] = [big] if key == "deleted_files" else big
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="delta invalid: a value outside `files`"):
        open_delta(path)


def test_the_whole_file_node_cap_still_holds(tmp_path: Path, caps: None, monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 10)
    path = tmp_path / "delta.json"
    path.write_bytes(b'{"commit":"c","files":[' + b",".join([b'{"path":""}'] * 5) + b"]}")
    with pytest.raises(FileRefused, match="code_scip_delta_max_nodes"):
        open_delta(path)


def test_a_link_is_refused_and_a_missing_file_is_not_found(tmp_path: Path, caps: None) -> None:
    (tmp_path / "link.json").symlink_to("/dev/zero")
    with pytest.raises(FileRefused):
        open_delta(tmp_path / "link.json")
    with pytest.raises(FileNotFoundError):
        open_delta(tmp_path / "absent.json")


class _Repo:
    """Records the files each insert round carries."""

    supports_round_reads = True

    def __init__(self) -> None:
        self.rounds: list[list[str]] = []
        self.state: dict[str, str] = {}

    async def live_symbols_for_paths(self, org: str, ns: str, paths: list[str], tier: str) -> Any:
        if tier == "scip":
            self.rounds.append(paths)
        return {}

    async def live_edges_for_paths(self, *a: Any) -> Any:
        return {}

    async def chunk_spans_for_paths(self, *a: Any) -> Any:
        return {}

    async def insert_rows(self, table: str, rows: list[Any]) -> None:
        return None

    async def set_state(self, org: str, ns: str, key: str, value: str, **_: Any) -> None:
        self.state[key] = value


@pytest.mark.asyncio
async def test_the_apply_takes_the_file_a_round_at_a_time(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("server.code.app.index.scip_delta.check_held", lambda: None)
    path = tmp_path / "delta.json"
    _write(path, 30)
    repo = _Repo()
    stats = await apply_delta(repo, "o", "code-x", open_delta(path))  # type: ignore[arg-type]
    assert stats.files == 31  # 30 entries and the deleted path
    assert [len(r) for r in repo.rounds] == [14, 14, 3]
    assert repo.rounds[-1][-1] == "src/gone.cpp" and stats.ctor_calls_recorded


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_no_read_keeps_the_file_open(tmp_path: Path, caps: None) -> None:
    path = tmp_path / "delta.json"
    _write(path, 30)
    before = _fds()
    delta = open_delta(path)
    assert _fds() == before
    rounds = delta.rounds(100, lambda p: True)
    next(rounds)
    rounds.close()  # a pass cancelled mid-apply drops its rounds
    assert _fds() == before


def test_the_round_budget_is_the_one_the_delta_was_opened_with(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "delta.json"
    _write(path, 30)
    delta = open_delta(path)
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", 10)  # lowered mid-pass
    assert [len(r) for r in delta.rounds(100, lambda p: True)] == [14, 14, 3]


def test_a_file_changed_since_it_was_opened_is_refused(tmp_path: Path, caps: None) -> None:
    path = tmp_path / "delta.json"
    _write(path, 3)
    delta = open_delta(path)
    _write(path, 4)
    with pytest.raises(FileRefused, match="changed while it was read"):
        list(delta.rounds(100, lambda p: True))


def test_only_kept_paths_are_validated_into_rounds(tmp_path: Path, caps: None) -> None:
    path = tmp_path / "delta.json"
    _write(path, 30)
    delta = open_delta(path)
    rounds = list(delta.rounds(5, lambda p: p.endswith(("S1.cpp", "S2.cpp"))))
    assert [[f.path for f in r] for r in rounds] == [
        ["src/Storages/S1.cpp", "src/Storages/S2.cpp", "src/gone.cpp"]
    ]


@pytest.mark.parametrize("cut", range(1, 40))
def test_a_value_cut_by_a_read_anywhere_parses_whole(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch, cut: int
) -> None:
    from server.code.app.index import delta_file

    monkeypatch.setattr(delta_file, "_CHUNK", cut)
    path = tmp_path / "delta.json"
    # a number, a literal, an escape and a 3-byte character, each cut somewhere
    path.write_text(
        '{"delta_version":1,"commit":"c\\u00e9","base":"","features":[],'
        '"files":[{"path":"a/€.cpp","symbols":[{"qualified_name":"f","line_start":12345}]}],'
        '"deleted_files":["d"],"x":true}',
        encoding="utf-8",
    )
    delta = open_delta(path)
    assert delta.commit == "c\u00e9" and delta.paths == ["a/€.cpp"]
    (batch,) = list(delta.rounds(100, lambda p: True))
    assert batch[0].symbols[0].line_start == 12345 and batch[-1].path == "d"


def test_a_value_larger_than_one_read_grows_the_buffer(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", 100_000)
    path = tmp_path / "delta.json"
    big = {"path": "a", "symbols": [{"qualified_name": "x" * 3_000_000}]}
    path.write_text(json.dumps({"commit": "c", "files": [big]}))
    (batch,) = list(open_delta(path).rounds(100, lambda p: True))
    assert len(batch[0].symbols[0].qualified_name) == 3_000_000


@pytest.mark.parametrize(
    "body",
    [
        b'{"commit":"c","files":[{"path":"a"} x {"path":"b"}]}',
        b'{"commit":"c","files":[' + b"[" * 5000 + b"]" * 5000 + b"]}",
        b'{"commit":"c","files":[],"files":[]}',
        b'{"commit":"c","commit":"d","files":[]}',
        b'{"commit":"c","files":[{"path":"\\ud800"}]}',
        b'{"commit":"c","files":[{"path":"a","symbols":[{"qualified_name":"f","line_start":'
        + b"9" * 5000
        + b"}]}]}",
    ],
    ids=["stray-byte", "nested-deep", "files-twice", "key-twice", "lone-surrogate", "huge-int"],
)
def test_a_bad_delta_reads_invalid_not_over_a_cap(tmp_path: Path, caps: None, body: bytes) -> None:
    path = tmp_path / "delta.json"
    path.write_bytes(body + b" " * (3 * 1024 * 1024))  # more to read after the bad byte
    with pytest.raises(ValueError, match="delta invalid"):
        open_delta(path)


def test_the_worst_round_costs_its_documented_bytes_per_counted_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The densest entry repository code can write (minimal symbols) filling a round:
    its cost per counted byte is what `code-index-operations.md` sizes the worst round
    by (measured at a third of the default budget, to keep the test light)."""
    budget = 1_000_000
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", budget)
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 10 * budget)
    monkeypatch.setattr(settings, "code_scip_delta_file_max_mb", 1024)
    symbols = budget // 2 - 10  # each `{"qualified_name":""}` and its comma
    path = tmp_path / "delta.json"
    path.write_text(
        '{"commit":"c","files":[{"path":"a","symbols":['
        + ",".join(['{"qualified_name":""}'] * symbols)
        + "]}]}"
    )
    delta = open_delta(path)
    tracemalloc.start()
    try:
        (batch,) = list(delta.rounds(100, lambda p: True))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(batch[0].symbols) == symbols
    # the docs give about 330 B per counted byte
    assert peak < 512 * budget, (peak, peak // budget)


def test_a_round_is_released_before_the_next_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three rounds of the densest entries, each filling the budget: the read holds one
    round at a time, not two."""
    budget = 100_000
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", budget)
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 10 * budget)
    monkeypatch.setattr(settings, "code_scip_delta_file_max_mb", 1024)
    entry = (
        '{"path":"%s","symbols":[' + ",".join(['{"qualified_name":""}'] * (budget // 2 - 10)) + "]}"
    )

    def peak_of(n: int) -> int:
        path = tmp_path / f"delta{n}.json"
        path.write_text('{"commit":"c","files":[' + ",".join(entry % i for i in range(n)) + "]}")
        delta = open_delta(path)
        tracemalloc.start()
        try:
            for batch in delta.rounds(100, lambda p: True):
                assert len(batch) == 1
                del batch
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    one, three = peak_of(1), peak_of(3)
    assert three < 1.5 * one, (one, three)


@pytest.mark.asyncio
async def test_a_failed_apply_releases_the_file(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import scip_delta

    path = tmp_path / "delta.json"
    _write(path, 30)
    delta = open_delta(path)

    async def failing(*a: Any) -> None:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(scip_delta, "_apply_files", failing)
    before = _fds()
    with pytest.raises(RuntimeError):
        await apply_delta(_Repo(), "o", "code-x", delta)  # type: ignore[arg-type]
    assert _fds() == before


@pytest.mark.asyncio
async def test_an_apply_cancelled_mid_read_releases_the_file_once_the_read_ends(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import gc
    import threading

    from server.code.app.index import delta_file

    path = tmp_path / "delta.json"
    _write(path, 30)
    delta = open_delta(path)
    in_read, cancelled, done = threading.Event(), threading.Event(), threading.Event()
    real = delta_file._entry

    def slow(text: str, index: int) -> Any:
        if index == 0:
            in_read.set()
            cancelled.wait(10)  # the thread is mid-read until the pass has been cancelled
            done.set()
        return real(text, index)

    monkeypatch.setattr(delta_file, "_entry", slow)
    before = _fds()
    task = asyncio.create_task(apply_delta(_Repo(), "o", "code-x", delta))  # type: ignore[arg-type]
    assert await asyncio.to_thread(in_read.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled.set()
    assert await asyncio.to_thread(done.wait, 10)
    for _ in range(50):
        gc.collect()
        if _fds() == before:
            break
        await asyncio.sleep(0.02)
    assert _fds() == before


def test_deleted_paths_past_the_last_rounds_room_get_rounds_of_their_own(
    tmp_path: Path, caps: None
) -> None:
    path = tmp_path / "delta.json"
    path.write_text(
        json.dumps(
            {
                "commit": "c",
                "files": [{"path": f"f{i}"} for i in range(3)],
                "deleted_files": [f"d{i}" for i in range(6)],
            }
        )
    )
    delta = open_delta(path)
    assert [[f.path for f in r] for r in delta.rounds(4, lambda p: True)] == [
        ["f0", "f1", "f2", "d0"],
        ["d1", "d2", "d3", "d4"],
        ["d5"],
    ]
    # no entry kept: the deleted paths alone
    assert [len(r) for r in delta.rounds(4, lambda p: False)] == [4, 2]


def test_many_deleted_paths_are_read_one_at_a_time_and_built_a_round_at_a_time(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", 1_000)
    path = tmp_path / "delta.json"
    deleted = [f"d{i}" for i in range(5_000)]  # one list over the round budget
    path.write_text(json.dumps({"commit": "c", "files": [{"path": "f"}], "deleted_files": deleted}))
    delta = open_delta(path)
    assert delta.deleted_files == deleted
    rounds = delta.rounds(100, lambda p: True)
    first = next(rounds)
    assert [f.path for f in first] == ["f", *deleted[:99]]
    assert sum(len(r) for r in rounds) == 5_000 - 99


def test_a_file_cut_short_since_it_was_opened_applies_no_round_past_it(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import delta_file

    path = tmp_path / "delta.json"
    _write(path, 30)
    delta = open_delta(path)
    stamp = delta._stamp
    _write(path, 20)
    monkeypatch.setattr(delta_file, "_open", lambda p, m: (os.open(p, os.O_RDONLY), stamp))
    rounds = delta.rounds(100, lambda p: True)
    assert len(next(rounds)) == 14  # the first full round is read
    with pytest.raises(FileRefused, match="changed while it was read"):
        next(rounds)  # the cut one is refused, not yielded short


def test_a_file_rewritten_between_the_count_and_the_parse_is_refused(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node cap counts the bytes the parse then reads: a same-size rewrite in
    between is caught by the stamp, not parsed uncounted."""
    from server.code.app.index import delta_file

    path = tmp_path / "delta.json"
    _write(path, 3)
    real = delta_file._Values.__init__

    def rewrite_then_parse(self: Any, p: Path, fd: int, round_nodes: int, size: int) -> None:
        os.utime(path, ns=(0, 0))  # changes ctime, whatever mtime is set to
        real(self, p, fd, round_nodes, size)

    monkeypatch.setattr(delta_file._Values, "__init__", rewrite_then_parse)
    with pytest.raises(FileRefused, match="changed while it was read"):
        open_delta(path)


def test_unknown_keys_and_bad_deleted_paths_are_not_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Values the delta model does not read are read past, never kept: several of them,
    each filling the round budget, cost about one value's memory."""
    budget = 100_000
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", budget)
    monkeypatch.setattr(settings, "code_scip_delta_max_nodes", 20 * budget)
    monkeypatch.setattr(settings, "code_scip_delta_file_max_mb", 1024)
    filler = "[" + ",".join(["{}"] * (budget // 2 - 10)) + "]"

    def peak_of(n: int) -> int:
        path = tmp_path / f"d{n}.json"
        path.write_text(
            '{"commit":"c",' + ",".join(f'"k{i}":{filler}' for i in range(n)) + ',"files":[]}'
        )
        tracemalloc.start()
        try:
            open_delta(path)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    one, six = peak_of(1), peak_of(6)
    assert six < 1.5 * one, (one, six)
    path = tmp_path / "bad.json"
    path.write_text('{"commit":"c","files":[],"deleted_files":["a",[{}]]}')
    with pytest.raises(ValueError, match="a deleted path is not a string"):
        open_delta(path)


def test_a_path_past_the_system_limit_is_refused(tmp_path: Path, caps: None) -> None:
    from server.code.app.index.delta_file import PATH_BYTES

    long = "a" * (PATH_BYTES - 1) + "\U0001f600"  # under the limit in characters, over in bytes
    for body in (
        {"commit": "c", "files": [{"path": long}]},
        {"commit": "c", "files": [], "deleted_files": [long]},
    ):
        path = tmp_path / "delta.json"
        path.write_text(json.dumps(body))
        with pytest.raises(ValueError, match=f"over {PATH_BYTES} bytes"):
            open_delta(path)


def test_a_file_still_growing_while_it_is_counted_is_refused(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import delta_file

    path = tmp_path / "delta.json"
    _write(path, 3)
    real = delta_file._open

    def opened_then_grown(p: Path, max_bytes: int) -> Any:
        fd, stamp = real(p, max_bytes)
        with path.open("a") as fh:
            fh.write(" " * (2 * 1024 * 1024))  # a writer still appending
        return fd, stamp

    monkeypatch.setattr(delta_file, "_open", opened_then_grown)
    with pytest.raises(FileRefused, match="changed while it was read"):
        open_delta(path)


def test_a_file_grown_between_the_count_and_the_parse_is_not_read_past_its_size(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import delta_file

    path = tmp_path / "delta.json"
    path.write_text('{"commit":"c","files":[{"path":"a"}')  # cut off by its writer
    real = delta_file._Values.__init__
    read: list[int] = []

    def grown(self: Any, p: Path, fd: int, round_nodes: int, size: int) -> None:
        with path.open("a") as fh:  # the writer goes on after the count
            fh.write(',{"path":"b"}]}')
        read.append(size)
        real(self, p, fd, round_nodes, size)

    monkeypatch.setattr(delta_file._Values, "__init__", grown)
    with pytest.raises(ValueError, match="delta invalid"):
        open_delta(path)  # the parse stops at the size it was opened at: cut off
    assert read == [len('{"commit":"c","files":[{"path":"a"}')]


def test_large_unknown_keys_are_not_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A key the delta model does not read is read past like its value: several large
    ones, each of 4-byte characters, cost about one key's memory."""
    budget = 100_000
    monkeypatch.setattr(settings, "code_scip_delta_round_max_nodes", budget)
    monkeypatch.setattr(settings, "code_scip_delta_file_max_mb", 1024)

    def peak_of(n: int) -> int:
        path = tmp_path / f"k{n}.json"
        keys = ",".join(f'"{i}{"\U0001f600" * 500_000}":0' for i in range(n))
        path.write_text('{"commit":"c",' + keys + ',"files":[]}', encoding="utf-8")
        tracemalloc.start()
        try:
            open_delta(path)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    one, six = peak_of(1), peak_of(6)
    assert six < 1.5 * one, (one, six)


def test_a_full_runs_replaced_deleted_paths_join_the_last_round(tmp_path: Path, caps: None) -> None:
    """The TypeScript and Python full runs replace `deleted_files` (`gone_documents`)
    before the apply: the rounds read the replacement."""
    path = tmp_path / "delta.json"
    path.write_text(json.dumps({"commit": "c", "files": [{"path": "a"}], "deleted_files": ["x"]}))
    delta = open_delta(path)
    delta.deleted_files = ["gone/1", "gone/2"]
    assert [[f.path for f in r] for r in delta.rounds(100, lambda p: True)] == [
        ["a", "gone/1", "gone/2"]
    ]


def test_an_entry_rewritten_to_another_path_after_the_plan_is_refused(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import delta_file

    path = tmp_path / "delta.json"
    path.write_text(json.dumps({"commit": "c", "files": [{"path": "src/a"}]}))
    delta = open_delta(path)
    stamp = delta._stamp
    path.write_text(json.dumps({"commit": "c", "files": [{"path": "zzz/a"}]}))  # same size
    monkeypatch.setattr(delta_file, "_open", lambda p, m: (os.open(p, os.O_RDONLY), stamp))
    with pytest.raises(FileRefused, match="changed while it was read"):
        list(delta.rounds(100, lambda p: p.startswith("src/")))


def test_text_over_the_fixed_cap_is_refused_and_splits_rounds(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import delta_file

    monkeypatch.setattr(delta_file, "_MAX_CHARS", 5_000)
    path = tmp_path / "delta.json"
    path.write_text(
        json.dumps(
            {
                "commit": "c",
                "files": [{"path": "a" * 10, "symbols": [{"qualified_name": "x" * 6_000}]}],
            }
        )
    )
    with pytest.raises(FileRefused, match=r"file entry 0: .*5000 characters \(a fixed cap"):
        open_delta(path)
    # entries of 2,000 characters each: two fit a 5,000-character round, not three
    entry = {"path": "p", "symbols": [{"qualified_name": "y" * 1_950}]}
    path.write_text(json.dumps({"commit": "c", "files": [entry] * 5}))
    assert [len(r) for r in open_delta(path).rounds(100, lambda p: True)] == [2, 2, 1]


@pytest.mark.parametrize(
    "head",
    [{"commit": "c" * 65}, {"base": "b" * 65}, {"features": ["x"] * 17}, {"features": ["x" * 65]}],
)
def test_a_header_past_its_small_caps_is_refused(
    tmp_path: Path, caps: None, head: dict[str, Any]
) -> None:
    """The header is kept for the whole apply, so it stays a sha and a few names."""
    path = tmp_path / "delta.json"
    body = {"delta_version": 1, "commit": "c" * 40, "files": [], **head}
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="delta invalid"):
        open_delta(path)
    # an upload's payload is the same model: it is refused at the same field
    with pytest.raises(pydantic.ValidationError) as e:
        ScipDelta.model_validate_json(json.dumps(body))
    assert e.value.errors()[0]["loc"][0] == next(iter(head))


def test_a_header_past_its_caps_is_refused_before_the_entries_are_parsed(
    tmp_path: Path, caps: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The header is checked as each value is read, so an oversized one is not held
    while the `files` array is walked."""
    from server.code.app.index import delta_file

    parsed: list[int] = []
    real = delta_file._entry
    monkeypatch.setattr(delta_file, "_entry", lambda text, i: parsed.append(i) or real(text, i))
    path = tmp_path / "delta.json"
    body = {"commit": "c" * 40, "features": ["x"] * 17, "files": [_entry(i) for i in range(3)]}
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError, match=r"^delta invalid: 'features'$"):
        open_delta(path)
    assert parsed == []
