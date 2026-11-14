"""Reading one place in a file: code.outline, code.enclosing, code.trace
(stack trace to definitions) and code.examples.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from server.code.api.models_common import (
    _CodeIdentity,
    _drop_empty_lessons,
    _LessonsOut,
    _ResultLessons,
)
from server.code.domain.units import CodeUnit

# ============================================================
# code.outline (one file's declarations with line spans)
# ============================================================


# A SQL test's declarations are its statement records
OUTLINE_DEFAULT_KINDS: tuple[str, ...] = (
    "function",
    "method",
    "class",
    "struct",
    "fixture",
    "statement",
    # A workflow's jobs, a Makefile's targets, a Dockerfile's stages
    "job",
    "target",
    "stage",
    # sqlc named queries and Liquibase changesets
    "query",
    "changeset",
    # The tables and views a SQL file creates
    "table",
    "view",
    # dbt models and macros
    "model",
    "macro",
)


class CodeOutlineInput(_CodeIdentity, _ResultLessons):
    """ "What is in this file?" — the question a whole-file read answers at
    the price of the whole file. The outline answers it from the symbol
    table: every declaration in source order with its line span, so the
    follow-up is a targeted read (offset/limit) or one code.symbol, never
    the file. This is the redirect target the plugin's read shunt names."""

    # repo-relative path ("src/Foo.cpp"); a leading ./ is tolerated
    path: str = Field(min_length=1, max_length=1000)
    # Declaration kinds to list (`function`, `class`, `job`, …); the default covers every kind
    # the symbol table records for code, SQL tests, build files and data models.
    kinds: list[str] = Field(default_factory=lambda: list(OUTLINE_DEFAULT_KINDS), max_length=16)
    # Whole-row budgeted: an entry ships complete (signature included) or
    # not at all; the first elided entry names its own code.symbol call.
    token_budget: int = Field(default=1500, ge=200, le=12000)


class CodeOutlineOutput(_LessonsOut):
    @model_serializer(mode="wrap")
    def _drop_empty_head(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # only a reference fixture carries a head; other outlines omit it
        data: dict[str, Any] = handler(self)
        if not data.get("head"):
            data.pop("head", None)
            data.pop("head_truncated", None)
        return _drop_empty_lessons(data)

    # The repo-relative path read, normalised from the request.
    path: str
    # source order: {kind, qualified_name, signature, start_line, end_line, tier}
    entries: list[dict[str, Any]]
    total: int  # declarations found for the requested kinds
    # Entries in `entries`.
    returned: int
    # Declarations left out because `token_budget` ran out (`total - returned`).
    elided: int
    # `code.symbol name=<qualified_name>` of the first elided entry ("" when
    # nothing was elided); a larger token_budget re-fetches the whole list.
    next: str = ""
    # False when the path has no live symbol rows: unindexed, renamed, or
    # the index has not caught up — read the file.
    indexed: bool = True
    # Why `entries` is empty, '' when it is not.
    # `no_indexed_paths`: the file type declares nothing the symbol table
    # indexes (.sql / .reference / .xml / .md) or the held file has none of
    # the requested kinds — an answer; read the file if you need its text.
    # `not_covered`: a source-language path the index does not hold (abstain).
    # `no_match`: the file is held but its parse failed (abstain).
    verdict: str = ""
    # Estimated tokens of the entries (and `head`) returned.
    tokens_returned: int = 0
    # Server-side wall time of the call, in milliseconds.
    took_ms: int = 0
    # A reference fixture (lang `reference`) declares nothing; its indexed head (first
    # 4,000 chars) is returned here instead, with `head_truncated` when the file is
    # longer. Absent for every other file.
    head: str = ""
    # True when the fixture is longer than the `head` returned; omitted with `head`.
    head_truncated: bool = False
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.enclosing
# ============================================================


class CodeEnclosingInput(_CodeIdentity):
    """Batched `path:line` -> enclosing function. Built for the grep shim:
    real grep stays exact, then ONE round-trip names the function every hit
    sits in — the "which function is this line in?" question that otherwise
    costs an agent a file read per hit. Also the primitive behind review
    comments that cite `path:line`."""

    # "src/Foo.cpp:123" — repo-relative path, 1-based line.
    locations: list[str] = Field(min_length=1, max_length=200)
    # include_body: ship the enclosing function's whole body with each resolved
    # location, so a grep hit becomes the function it sits in without a follow-up
    # code.symbol or a file read (most index answers are otherwise followed by a read of
    # the same span). Bodies are fetched once per distinct symbol — ten hits in one
    # function cost one body — and fill by first appearance until token_budget is spent;
    # the rest ship elided with the pasteable `next` escalation. Default off: the grep
    # shim only wants the name on every line.
    include_body: bool = False
    # With `include_body`: token budget for the bodies (200..12000); bodies past it ship elided.
    token_budget: int = Field(default=4000, ge=200, le=12000)
    # neighbours: a graph evidence block per distinct enclosing function:
    # up to neighbour_k callers, callees (compiler tier preferred, symbols
    # outside the repo dropped, parser guesses below 0.8 confidence dropped)
    # and mapped tests, the module (top two directory segments) and the
    # epistemic envelope. Presentation only — it never enters a rank. At
    # most 20 anchors per call; None = off.
    neighbours: bool | None = None
    # With `neighbours`: at most this many callers and callees per function (1..20).
    neighbour_k: int = Field(default=5, ge=1, le=20)


class CodeEnclosingOutput(BaseModel):
    # one entry per input location, input order preserved:
    # {path, line, symbol, symbol_id, qualified_name, signature, tier, kind,
    #  start_line, end_line}
    # kind is "function" / "method" / "fixture" for a line inside a function
    # body; "class" / "struct" / "enum" when no function contains the line
    # but a type body does (a member declaration in a header) — then the
    # span is the whole type. symbol == "" when the line is inside neither
    # (file scope, unindexed path, or the index has not caught up).
    # With include_body, resolved entries add {body, body_elided, next}:
    # body is the whole unit ("" when elided for budget), next is the
    # code.symbol call that fetches it.
    results: list[dict[str, Any]]
    # Locations that landed inside a function, type, or document section.
    resolved: int
    paths_indexed: int  # distinct input paths that have any symbol rows
    # Distinct paths among the parseable input locations.
    paths_total: int
    # Server-side wall time of the call, in milliseconds.
    took_ms: int
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
    # include_body accounting: distinct bodies served / withheld for budget
    bodies_returned: int = 0
    # Distinct bodies withheld because `token_budget` ran out.
    bodies_elided: int = 0
    # include_body bodies + the neighbours block, in tokens
    tokens_returned: int = 0
    # neighbours=true: one entry per distinct enclosing function in first-
    # appearance order, at most 20: {symbol_id, qualified_name, path, module,
    # callers: [qualified names], callees: [...], tests: [paths], epistemic,
    # causes}. Rows keep symbol_id, so a consumer joins a hit to its entry.
    # Type fallbacks (class/struct bodies) carry no call edges and get none.
    neighbours: list[dict[str, Any]] = Field(default_factory=list)
    anchors: int = 0  # distinct enclosing functions the block covers
    anchors_capped: bool = False  # more resolved functions than the 20 cap
    # Tokens of the neighbours block alone; 0 when `neighbours` is off.
    neighbour_tokens: int = 0


# ============================================================
# code.trace (stack trace -> definitions)
# ============================================================


class CodeTraceInput(_CodeIdentity):
    """Paste a stack trace / backtrace; get each frame resolved to its
    definition in one call — the incident-workflow entry point. Frames the
    index can't resolve are returned unresolved, never dropped: the frame
    ORDER is the diagnostic artifact."""

    # The stack trace or backtrace text; qualified `a::b` names and C/C++ `dir/file.cpp:N`
    # locations are read as frames (`std::`, `boost::`, `absl::`, `__*` frames skipped).
    trace: str = Field(min_length=1, max_length=100_000)
    # Resolve at most this many distinct frames, in trace order (1..40).
    max_frames: int = Field(default=15, ge=1, le=40)
    # Token budget for `definitions` (200..12000); units past it are dropped.
    token_budget: int = Field(default=3000, ge=200, le=12000)


class CodeTraceOutput(BaseModel):
    # One entry per recognized frame, trace order: {frame, qualified_name,
    # path, line, symbol_id, resolved}.
    frames: list[dict[str, Any]]
    # The resolved definitions, whole units within `token_budget`.
    definitions: list[CodeUnit]
    # Estimated tokens of `definitions`.
    tokens_returned: int = 0
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.examples
# ============================================================


class CodeExamplesInput(_CodeIdentity):
    # The symbol: a qualified name or a `path:line` inside it; ignored when `symbol_id` is set.
    name: str = ""
    # The symbol's id from an earlier code.* answer; wins over `name`.
    symbol_id: str = ""
    # At most this many call sites (1..10).
    top_k: int = Field(default=3, ge=1, le=10)


class CodeExamplesOutput(BaseModel):
    # Call sites, test-covered callers first: {caller, path, line, tier, test_covered, body},
    # body = the smallest chunk around the line (up to 4,000 chars, '' when none).
    examples: list[dict[str, Any]]
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
