"""Exact lookups over the indexed corpus: code.grep (literal / regex) and
code.files (indexed files by name).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from server.code.api.models_common import (
    _CodeIdentity,
    _drop_empty_presence_keys,
    _drop_unfired_symbol_blocks,
    _lead_namespaces,
    _trim_namespaces,
)
from server.operations.base import MCP_HIDDEN

# ============================================================
# code.grep (exact literal / regex over the indexed corpus)
# ============================================================


class CodeGrepInput(_CodeIdentity):
    """Exact-match lookup over the indexed corpus. Unlike shell grep, the
    answer arrives WITH what raw grep can't attach: the
    enclosing symbol per hit, one indexed round-trip, and a hit list that
    distinguishes code from the repo's 14K .reference fixtures (unindexed
    by design). Literal by default; regex=True switches to RE2."""

    # The text to find: a literal substring, or an RE2 expression with `regex`.
    pattern: str = Field(min_length=2, max_length=1000)
    # True reads `pattern` as RE2; an invalid expression is a 422.
    regex: bool = False
    # False matches `pattern` regardless of letter case.
    case_sensitive: bool = True
    # Only paths under this repo-relative prefix; '' = the whole index. A prefix outside
    # the indexed ones answers `not_covered`.
    path_prefix: str = ""
    # Continuation (matches mode): the `next_cursor` of an earlier response to this
    # pattern; the next page of matching lines is one point read of what the first call
    # extracted.
    cursor: str = Field(default="", max_length=1024)
    # The caller's seen-set. Grep serves no unit ids, so these are PATHS
    # (what its trace records as returned_ids): lines in them are skipped.
    exclude_unit_ids: list[str] = Field(
        default_factory=list, max_length=200, json_schema_extra=MCP_HIDDEN
    )
    # matches: up to `limit` matching LINES with text + enclosing scope. files: up to
    # `limit` matching FILES (path, matching chunk count, first line) and no line text —
    # the coverage question ("which files mention X?") that a 20-line cap cannot answer.
    # presence: which of `namespaces` contain the pattern — one row per namespace with
    # the verdict, `files_total` and the first matching file's path/line, no line text;
    # `summary` groups the verdicts. Unset `namespaces` = the addressed namespace alone.
    # message: `pattern` is a RENDERED error or log message ("Cannot convert string 'x'
    # to type DateTime64(3)"); the answer is the statement whose format template printed
    # it — `verdict` raised_at | no_template_match, `template` (the message with `{}`
    # where arguments stood), `error_code` (the ErrorCodes name when the text carried
    # `Code: N`), and matches ranked by how much of the message each statement's own
    # template covers. Lines are throw / LOG_ / onError statements only; a pattern no
    # template matches (a code line, not a rendered message) is run once more as a plain
    # matches grep and says so in `fallback`.
    mode: Literal["matches", "files", "presence", "message"] = "matches"
    # Default depends on mode: 20 lines, or the 500-file ceiling — a files
    # answer capped at 20 reads like a complete coverage answer to a caller
    # that did not know to send a limit. matches mode is clamped to 200 lines.
    limit: int | None = Field(default=None, ge=1, le=500)
    # File filters, rg `-g` / `-t` style: keep paths matching any glob, drop paths
    # matching any exclude, restrict to a language's extension set. Each list at most 8.
    path_globs: list[str] = Field(default_factory=list, max_length=8)
    # Drop paths matching any of these rg-style globs.
    exclude_globs: list[str] = Field(default_factory=list, max_length=8)
    # Keep only files with these languages' extensions (rg `-t` names: cpp, py, ts, go, …);
    # an unknown name is a 422 that lists the known ones.
    langs: list[str] = Field(default_factory=list, max_length=8)
    # Several namespaces, mode="presence" only: at most 8, trimmed, duplicates removed;
    # `namespace`, when set and absent, leads the list. Every entry is authorized at
    # VIEWER before anything runs.
    namespaces: list[str] = Field(default_factory=list, max_length=8)
    # The neighbours graph block in the SAME call, matches mode only: the
    # hits' enclosing functions become anchors (at most 20, first
    # appearance) and each carries up to `neighbour_k` callers, callees and
    # mapped tests at edge confidence >= 0.8 — what `code.enclosing` with
    # neighbours=true would answer as a second call. None = off.
    neighbours: bool | None = None
    # With `neighbours`: at most this many callers and callees per anchor (1..20).
    neighbour_k: int = Field(default=5, ge=1, le=20)
    # The request's wall-clock budget in ms, from arrival (values under 200 read as 200).
    # The scan stops at what is left and returns the hits it read so far, with `partial:
    # true` and `partial_reason`, instead of timing out. None or 0 = no budget.
    deadline_ms: int | None = Field(default=None, ge=0, le=60000)

    @field_validator("namespaces")
    @classmethod
    def _namespaces_trimmed_and_unique(cls, v: list[str]) -> list[str]:
        return _trim_namespaces(v)

    @model_validator(mode="after")
    def _presence_owns_the_list(self) -> CodeGrepInput:
        if self.namespaces and self.mode != "presence":
            raise ValueError(
                "namespaces on code.grep requires mode: 'presence' — matches and files "
                "are read from one namespace"
            )
        if self.mode == "presence" and self.cursor:
            raise ValueError("cursor cannot be combined with mode: 'presence'")
        if self.neighbours and self.mode != "matches":
            raise ValueError(
                "neighbours on code.grep requires mode: 'matches' — the block hangs off "
                "the hits' enclosing functions"
            )
        self.namespaces = _lead_namespaces(self.namespace, self.namespaces)
        return self

    @property
    def effective_limit(self) -> int:
        if self.mode == "files":
            return self.limit or 500
        if self.mode == "presence":
            return 1
        return min(self.limit or 20, 200)


class CodeGrepOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_unfired_mode_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # the presence block rides only when it fired, the neighbours block
        # only when it ran, and the message fields only with a verdict
        data = _drop_empty_presence_keys(handler(self))
        data = _drop_unfired_symbol_blocks(data)
        if not data.get("verdict"):
            for k in ("verdict", "template", "error_code", "coverage"):
                data.pop(k, None)
        for k in ("partial", "partial_reason", "fallback"):
            if not data.get(k):
                data.pop(k, None)
        return data

    # {path, line, text, scope, scope_start_line, scope_end_line} per matching
    # LINE, path/line ordered. scope_* is the span of the enclosing unit named
    # by `scope` (0/0 when the line sits in no unit) — enough to decide whether
    # to fetch the body, or to skip it when that unit is already on screen.
    matches: list[dict[str, Any]]
    # Continuation: cursor for the next page of matching lines, "" when the extracted
    # set is exhausted. has_more mirrors it.
    next_cursor: str = ""
    # True when `next_cursor` is set.
    has_more: bool = False
    # mode=files: {path, chunks, first_line} per matching file, path ordered.
    files: list[dict[str, Any]] = Field(default_factory=list)
    files_total: int = 0  # distinct matching files before `limit` (mode=files)
    # Indexed chunks read that contain the pattern (matches mode over-fetches up to 200).
    chunks_matched: int
    # True when more lines or files match than were returned, or the chunk read hit its cap.
    truncated: bool = False
    # Chunk coverage is not byte-complete for a file: absence here cannot
    # prove absence in the repo (same floor contract as code.verify).
    coverage_floor: bool = True
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
    # True when path_prefix lies outside the indexed prefixes (hits are then
    # empty by construction, not by absence). `note` says which prefixes exist.
    # Also: "pattern read as a regex; pass regex=true to make this explicit"
    # when a regex-shaped literal matched nothing and was run once as a regex
    # (route +regex_fallback).
    not_covered: bool = False
    # The neighbours graph block: one entry per anchor — symbol_id, qualified_name,
    # path, module, callers, callees, tests, epistemic, causes; empty unless
    # `neighbours` ran.
    neighbours: list[dict[str, Any]] = Field(default_factory=list)
    anchors: int = 0  # distinct enclosing functions the block covers
    anchors_capped: bool = False  # more resolved functions than the 20 cap
    # Tokens of the neighbours block alone; 0 when it did not run.
    neighbour_tokens: int = 0
    # A caveat on the answer: the pattern was re-read as a regex or matched literally, the
    # prefix is outside the index, or the embedding coverage is low; '' when none.
    note: str = ""
    # mode=message only (dropped from the wire otherwise): raised_at |
    # no_template_match; the message's `{}` template; the ErrorCodes name for
    # a `Code: N` prefix ("" when unmapped); the best statement's coverage
    # (matched message characters / message characters). Each match then also
    # carries `template` (the statement's own) and `coverage`.
    verdict: str = ""
    # mode=message: the message with `{}` where arguments stood.
    template: str = ""
    # mode=message: the ErrorCodes name for a `Code: N` prefix; '' when unmapped.
    error_code: str = ""
    # mode=message: the best statement's matched share of the message characters (0..1).
    coverage: float = 0.0
    # mode=presence: one row per requested namespace, in request order — {namespace,
    # verdict: present | absent | unknown, path, start_line, end_line, tier: "",
    # files_total, indexed_commit, index_status, ms, reason}. `absent` only when the
    # namespace answered with an index behind it; `unknown` names why in `reason`
    # (deadline | error:<code> | no_index | not_covered). `summary` = {present, absent,
    # unknown} lists. Both off the wire otherwise.
    presence: list[dict[str, Any]] = Field(default_factory=list)
    # mode=presence: {present, absent, unknown}, each the namespaces with that verdict.
    summary: dict[str, list[str]] = Field(default_factory=dict)
    # Id of this call's trace row ('' on a not_covered answer); pass it to `code.outcome`.
    trace_run_id: str = ""
    # `deadline_ms` stopped the scan (or skipped a later read): the hits are those found
    # in the budget and `partial_reason` says what was cut. Off the wire otherwise.
    partial: bool = False
    # What the deadline cut, as `deadline N ms: <stage>; <stage>`.
    partial_reason: str = ""
    # mode=message whose pattern matched no statement template: "literal" when the same
    # pattern was then run as a plain matches grep and `matches` are its lines (the
    # verdict fields stay the message read's). Off the wire otherwise.
    fallback: Literal["", "literal"] = ""


# ============================================================
# code.files (indexed files by name)
# ============================================================


class CodeFilesInput(_CodeIdentity):
    """Which indexed files have this NAME — an rg-style glob
    (`tests/queries/0_stateless/05138_*`, `**/*Parquet*.cpp`), a basename stem
    (`05138_with_fill` matches the stem exactly or as its leading prefix:
    05138_with_fill_step_below_precision.sql), or a path prefix — from the file table,
    no chunk read. grep matches CONTENT, not paths: "does test 05138 exist, and where"
    is this call. Returns path, lang, size, lines and the commit each file was indexed
    at."""

    # An rg-style glob over the repo-relative path (`**/*Parquet*.cpp`); '' = no glob filter.
    glob: str = Field(default="", max_length=200)
    # A basename without extension, matched exactly or as its leading prefix; exact matches
    # sort first. '' = no stem filter.
    stem: str = Field(default="", max_length=200)
    # Only paths under this prefix; '' = no prefix filter. Filters combine; at least one is
    # required.
    path_prefix: str = Field(default="", max_length=500)
    # At most this many files (1..500).
    limit: int = Field(default=50, ge=1, le=500)


class CodeFilesOutput(BaseModel):
    # {path, lang, size_bytes, loc, indexed_commit}; exact stem matches first, then path
    # order. A stateless TEST row also carries `reference`: the path of its indexed
    # expectation file (`<stem>.reference` / `.reference.j2`) or null; the fixtures are
    # listed as rows of their own (lang `reference`).
    files: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0  # matching files before `limit`
    # True when `total` is larger than the files returned.
    truncated: bool = False
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
    # True when `path_prefix` lies outside the indexed prefixes; `files` is then empty.
    not_covered: bool = False
    # With `not_covered`: which prefixes the index holds; '' otherwise.
    note: str = ""
    # Id of this call's trace row ('' on a not_covered answer); pass it to `code.outcome`.
    trace_run_id: str = ""
