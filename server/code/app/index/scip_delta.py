"""SCIP delta wire format (versioned) + application to the scip tier.

The producer (CI job or scripts/scip_harness) converts scip-clang protobuf
output into THIS normalized JSON shape — the server never parses protobuf.
Uploaded chunked via code.scip_ingest, assembled and applied by the
code_scip_apply worker. Symbol ids come from the SAME shared hash
(server/code/domain/extract/treesitter.symbol_id) the fast tier uses.

Apply semantics per touched file: upsert every symbol/edge the delta
emits for it, tombstone scip rows in that file the delta no longer emits;
files in `deleted_files` tombstone entirely. Idempotent — re-applying the
same delta converges to the same live set.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from server.code.app.index.entry_lock import check_held
from server.code.domain.extract.treesitter import symbol_id
from server.code.store.index import CodeRepo, build_row
from server.workers import pass_context

DELTA_VERSION = 1


class ScipSymbol(BaseModel):
    qualified_name: str
    name: str = ""
    kind: str = "function"
    signature: str = ""
    line_start: int = 0
    line_end: int = 0
    doc: str = ""
    scip_symbol: str = ""
    decl_paths: list[str] = Field(default_factory=list)


class ScipEdge(BaseModel):
    edge_kind: Literal["calls", "includes", "inherits", "overrides", "uses_type"] = "calls"
    from_qualified_name: str
    from_signature: str = ""
    to_qualified_name: str
    to_signature: str = ""
    line: int = 0
    # Compiler edges are exact (1.0). The one graded shape is a constructor reached
    # through a factory (`std::make_shared<T>`) on a class with several constructors:
    # the type is exact, the overload is not, so the converter emits one edge per
    # overload at 0.8. Absent from the wire when 1.0.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ScipFile(BaseModel):
    path: str
    deleted: bool = False
    symbols: list[ScipSymbol] = Field(default_factory=list)
    edges: list[ScipEdge] = Field(default_factory=list)


# Producer capabilities a delta may declare. `ctor_calls`: a FULL index converted with
# its source, so every file's constructor calls through factories are in the delta; the
# apply records it in code_index_state (`scip_ctor_calls`) and the read side stops
# labelling a constructor's empty answer `ctor_edges_absent` for scip-covered seeds.
FEATURE_CTOR_CALLS = "ctor_calls"
STATE_SCIP_CTOR_CALLS = "scip_ctor_calls"


class ScipDelta(BaseModel):
    delta_version: int = DELTA_VERSION
    commit: str
    base: str = ""
    files: list[ScipFile] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)


def parse_delta(raw: bytes | bytearray) -> ScipDelta:
    """A harness's JSON delta as a ScipDelta. An invalid one raises a short ValueError:
    a ValidationError names every bad item, so its text grows with the delta."""
    try:
        return ScipDelta.model_validate_json(raw)
    except ValidationError as e:
        raise ValueError(f"delta invalid: {e.error_count()} errors") from None


class ScipApplyStats(BaseModel):
    files: int = 0
    symbols_new: int = 0
    symbols_tombstoned: int = 0
    ctor_calls_recorded: bool = False
    edges_new: int = 0
    edges_tombstoned: int = 0
    edges_dropped_declarations: int = 0
    files_skipped_out_of_scope: int = 0
    files_skipped_excluded: int = 0  # the entry's path denylist


def _path_refusal(org: str, ns: str) -> Any:
    """`path -> reason` from the entry's path-level denylist; an unknown namespace
    refuses nothing."""
    try:
        from server.code.app.index.denylist import denylist_for
        from server.code.domain.index.repos import repo_for_namespace

        cfg = repo_for_namespace(org, ns)
        if cfg is None:
            return lambda _p: ""
        rules = denylist_for(cfg)
        return rules.reason
    except Exception:  # noqa: BLE001 — unknown namespace: apply unfiltered
        return lambda _p: ""


def _indexable_prefixes(org: str, ns: str) -> tuple[str, ...]:
    try:
        from server.code.domain.index.repos import repo_for_namespace

        cfg = repo_for_namespace(org, ns)
        return tuple(cfg.path_prefixes) if cfg else ()
    except Exception:  # noqa: BLE001 — unknown namespace: apply unfiltered
        return ()


async def _declaration_line_test(
    repo: CodeRepo, org: str, ns: str, path: str
) -> Callable[[int], bool] | None:
    """Cross-tier validation: scip-clang 0.4.0 emits no enclosing_range, so
    the converter attributes references to the nearest preceding function
    definition — which turns member-function DECLARATIONS in class bodies
    into false call edges from whatever inline method precedes them
    (e.g. ReplicatedMergeTreeSink::getName 'calling' commitPart from the
    declaration in ReplicatedMergeTreeSink.h). The treesitter
    chunker DOES know body extents: a reference line inside a class
    skeleton but inside no function body is a declaration, not a call.
    Returns None when the file has no treesitter coverage (keep all edges)."""
    try:
        spans = await repo.chunk_spans_for_path(org, ns, path)
    except Exception:  # noqa: BLE001 — validation is best-effort
        return None
    fn_symbol_spans: list[tuple[int, int]] = []
    try:
        fn_symbol_spans = _function_symbol_spans(
            await repo.live_symbols_for_path(org, ns, path, "treesitter")
        )
    except Exception:  # noqa: BLE001 — validation is best-effort
        pass
    return _declaration_line_test_from_spans(spans, fn_symbol_spans)


def _function_symbol_spans(rows: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """The (start, end) spans of the parser tier's function symbols among `rows`: a
    function SYMBOL's span is a body too. The chunker files a component declared through
    a wrapper (`const Grid = forwardRef((…) => …)`) as skeleton windows, and
    the chunk test alone would call every call inside it a "declaration"."""
    return [
        (int(r.get("start_line") or 0), int(r.get("end_line") or 0))
        for r in rows
        if str(r.get("kind") or "") in ("function", "method")
    ]


def _declaration_line_test_from_spans(
    spans: list[tuple[str, int, int]],
    fn_symbol_spans: list[tuple[int, int]] = (),  # type: ignore[assignment]
) -> Callable[[int], bool] | None:
    """`_declaration_line_test` over spans already read (a round's), plus the parser
    tier's function-symbol spans."""
    if not spans:
        return None
    fn_spans = [(s, e) for k, s, e in spans if k == "function"] + list(fn_symbol_spans)
    skel_spans = [(s, e) for k, s, e in spans if k == "class_skeleton"]
    if not skel_spans:
        return None

    def _test(line: int) -> bool:
        if any(s <= line <= e for s, e in fn_spans):
            return False
        return any(s <= line <= e for s, e in skel_spans)

    return _test


APPLY_ROUND_FILES = 100
"""Files per round of `apply_delta`: the live symbols, edges and chunk spans
of a round are read in one statement per table (the repository batches 100
paths a statement) and the round's rows land in one insert per table.
Walking files one at a time costs three reads and two inserts a file, each
insert waiting under async committed inserts (a lone writer waits the busy
timeout for company); per round the same rows take a few statements a
hundred files."""


class _RoundSink:
    """The rows a round collects for its one insert per table."""

    def __init__(self) -> None:
        self.symbols: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []


def _has_round_reads(repo: Any) -> bool:
    """The repository (or a stand-in that says so) serves the round reads.
    Attribute presence is not the test: a mock answers every name."""
    return isinstance(repo, CodeRepo) or getattr(repo, "supports_round_reads", False) is True


async def apply_delta(repo: CodeRepo, org: str, ns: str, delta: ScipDelta) -> ScipApplyStats:
    stats = ScipApplyStats()
    prefixes = _indexable_prefixes(org, ns)
    refused = _path_refusal(org, ns)
    files: list[ScipFile] = []
    for f in delta.files:
        # Server-side scope enforcement: a full scip index sweeps the whole
        # compile graph (contrib/, build/, system headers). Only the repo's
        # configured prefixes belong in the index.
        if prefixes and not any(f.path.startswith(p) for p in prefixes):
            stats.files_skipped_out_of_scope += 1
            continue
        # The entry's path denylist (exclude globs, the built-in rules) binds the
        # compiler tier too — a path the parser tier refuses must not enter through a
        # scip index. The content rule (generated headers) is the producer's
        # (`scip_to_delta.py --exclude-generated`): the delta carries no source.
        if refused(f.path):
            stats.files_skipped_excluded += 1
            continue
        files.append(f)
    files.extend(ScipFile(path=path, deleted=True) for path in delta.deleted_files)
    if _has_round_reads(repo):
        for start in range(0, len(files), APPLY_ROUND_FILES):
            # A pass that lost its workers' lease writes no further round.
            pass_context.check_stop()
            chunk = files[start : start + APPLY_ROUND_FILES]
            await _apply_round(repo, org, ns, delta.commit, chunk, stats)
    else:
        for f in files:
            pass_context.check_stop()
            await _apply_file(repo, org, ns, delta.commit, f, stats)
            stats.files += 1
    if FEATURE_CTOR_CALLS in delta.features:
        check_held()
        await repo.set_state(
            org,
            ns,
            STATE_SCIP_CTOR_CALLS,
            "1",
            detail=json.dumps({"commit": delta.commit, "at": int(time.time())}),
        )
        stats.ctor_calls_recorded = True
    return stats


async def _publish(repo: Any, table: str, rows: list[dict[str, Any]]) -> None:
    """Insert index rows, unless the entry lease of the pass was lost."""
    check_held()
    await repo.insert_rows(table, rows)


async def _apply_round(
    repo: Any,
    org: str,
    ns: str,
    commit: str,
    files: list[ScipFile],
    stats: ScipApplyStats,
) -> None:
    """One round: the three reads once for every path, the per-file
    key-diff unchanged, the rows of the round in one insert per table."""
    paths = sorted({f.path for f in files})
    symbols = await repo.live_symbols_for_paths(org, ns, paths, "scip")
    edges = await repo.live_edges_for_paths(org, ns, paths, "scip")
    spans_by_path = await repo.chunk_spans_for_paths(org, ns, paths)
    # The parser tier's function spans, one more read a round
    fast_symbols = await repo.live_symbols_for_paths(org, ns, paths, "treesitter")
    sink = _RoundSink()
    for f in files:
        spans = [
            (str(r["kind"]), int(r["start_line"]), int(r["end_line"]))
            for r in spans_by_path.get(f.path, [])
        ]
        await _apply_file(
            repo,
            org,
            ns,
            commit,
            f,
            stats,
            existing=symbols.get(f.path, []),
            existing_edges=edges.get(f.path, []),
            spans=spans,
            fn_symbol_spans=_function_symbol_spans(fast_symbols.get(f.path, [])),
            sink=sink,
        )
        stats.files += 1
    if sink.symbols:
        await _publish(repo, "code_symbols", sink.symbols)
    if sink.edges:
        await _publish(repo, "code_edges", sink.edges)


async def _apply_file(
    repo: CodeRepo,
    org: str,
    ns: str,
    commit: str,
    f: ScipFile,
    stats: ScipApplyStats,
    *,
    existing: list[dict[str, Any]] | None = None,
    existing_edges: list[dict[str, Any]] | None = None,
    spans: list[tuple[str, int, int]] | None = None,
    fn_symbol_spans: list[tuple[int, int]] | None = None,
    sink: _RoundSink | None = None,
) -> None:
    """One file's key-diff. Alone it reads its own live rows and inserts;
    inside a round (`_apply_round`) the live rows and chunk spans come in
    and the rows go to `sink` for the round's insert."""
    # ---- symbols ---------------------------------------------------------
    if existing is None:
        existing = await repo.live_symbols_for_path(org, ns, f.path, "scip")
    existing_keys = {(str(r["qualified_name"]), str(r["symbol_id"])): r for r in existing}
    new_keys: set[tuple[str, str]] = set()
    sym_rows = []
    if not f.deleted:
        for s in f.symbols:
            sid = symbol_id(s.qualified_name, s.signature)
            key = (s.qualified_name, sid)
            if key in new_keys:
                continue
            new_keys.add(key)
            live = existing_keys.get(key)
            if live is not None and (
                int(live.get("start_line") or 0) == int(s.line_start)
                and int(live.get("end_line") or 0) == int(s.line_end)
                and str(live.get("kind") or s.kind) == s.kind  # a row with no kind read is unmoved
            ):
                continue  # unchanged AND unmoved — keep live row
            # new symbol, the same symbol at a new line (an edit above it), or the same
            # symbol under a new kind (the id is name|signature, so a converter that
            # re-reads `const` as `function` changes nothing the key sees): write the
            # fresh row; same key + higher version replaces under FINAL
            sym_rows.append(
                build_row(
                    "code_symbols",
                    org=org,
                    namespace=ns,
                    tier="scip",
                    qualified_name=s.qualified_name,
                    symbol_id=sid,
                    name=s.name or s.qualified_name.split("::")[-1],
                    kind=s.kind,
                    signature=s.signature,
                    path=f.path,
                    start_line=s.line_start,
                    end_line=s.line_end,
                    decl_paths=list(s.decl_paths),
                    doc_comment=s.doc[:2000],
                    scip_symbol=s.scip_symbol,
                    indexed_commit=commit,
                )
            )
    tombstones = [
        CodeRepo.tombstone_of("code_symbols", row)
        for key, row in existing_keys.items()
        if key not in new_keys
    ]
    stats.symbols_new += len(sym_rows)
    stats.symbols_tombstoned += len(tombstones)
    if sink is not None:
        sink.symbols.extend(sym_rows + tombstones)
    else:
        await _publish(repo, "code_symbols", sym_rows + tombstones)

    edge_rows = await _diff_edges(
        repo,
        org,
        ns,
        commit,
        f,
        stats,
        existing_edges,
        spans,
        fn_symbol_spans=fn_symbol_spans,
    )
    if sink is not None:
        sink.edges.extend(edge_rows)
    else:
        await _publish(repo, "code_edges", edge_rows)


async def _diff_edges(
    repo: Any,
    org: str,
    ns: str,
    commit: str,
    f: ScipFile,
    stats: ScipApplyStats,
    existing_edges: list[dict[str, Any]] | None,
    spans: list[tuple[str, int, int]] | None,
    fn_symbol_spans: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """The edge rows a file writes — fresh edges, declaration lines dropped —
    plus the tombstones of live edges the delta no longer holds. Reads the
    live edges and chunk spans itself unless the round handed them in."""
    if existing_edges is None:
        existing_edges = await repo.live_edges_for_path(org, ns, f.path, "scip")
    existing_edge_keys: dict[tuple[str, str, str, str, int], dict[str, Any]] = {
        (
            str(r["edge_kind"]),
            str(r["from_symbol_id"]),
            str(r["to_symbol_id"]),
            str(r["path"]),
            int(r["line"]),
        ): r
        for r in existing_edges
    }
    new_edge_keys: set[tuple[str, str, str, str, int]] = set()
    edge_rows = []
    is_declaration_line = None
    if not f.deleted and f.edges:
        is_declaration_line = (
            _declaration_line_test_from_spans(spans, fn_symbol_spans or [])
            if spans is not None
            else await _declaration_line_test(repo, org, ns, f.path)
        )
    if not f.deleted:
        for e in f.edges:
            # Only a CALL on a declaration line is the false edge this test exists for;
            # an `inherits` edge sits on the class line and a `uses_type` from an
            # interface's field on the interface's, by definition
            if (
                e.edge_kind == "calls"
                and is_declaration_line is not None
                and is_declaration_line(e.line)
            ):
                stats.edges_dropped_declarations += 1
                continue
            fid = symbol_id(e.from_qualified_name, e.from_signature)
            tid = symbol_id(e.to_qualified_name, e.to_signature)
            ekey = (str(e.edge_kind), fid, tid, f.path, e.line)
            if ekey in new_edge_keys:
                continue
            new_edge_keys.add(ekey)
            if ekey in existing_edge_keys:
                continue
            edge_rows.append(
                build_row(
                    "code_edges",
                    org=org,
                    namespace=ns,
                    tier="scip",
                    edge_kind=e.edge_kind,
                    to_symbol_id=tid,
                    from_symbol_id=fid,
                    path=f.path,
                    line=e.line,
                    from_qualified_name=e.from_qualified_name,
                    to_qualified_name=e.to_qualified_name,
                    confidence=float(e.confidence),
                    indexed_commit=commit,
                )
            )
    edge_tombstones = [
        CodeRepo.tombstone_of("code_edges", row)
        for ekey, row in existing_edge_keys.items()
        if ekey not in new_edge_keys
    ]
    stats.edges_new += len(edge_rows)
    stats.edges_tombstoned += len(edge_tombstones)
    return edge_rows + edge_tombstones
