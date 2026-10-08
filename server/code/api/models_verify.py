"""Checking a claim against the index: code.verify, code.verify_citations and
code.path_guards.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from server.code.api.models_common import _CodeIdentity, _PrPostImage

# ============================================================
# code.verify (claim -> verdict with evidence)
# ============================================================


class CodeVerifyInput(_CodeIdentity):
    """Verify one closed-form claim against the index and return a
    three-valued verdict. The epistemic rule: the call graph and chunk store
    are FLOORS (name-matched edges, incomplete chunking, no dynamic
    dispatch), so an absence can support `not_established`, never `refuted`
    — only positive evidence refutes or confirms.

    kinds and their subject/object:
      calls    subject=caller symbol, object=callee symbol — does subject
               (transitively, bounded by depth) call object?
      unused   subject=symbol — does nothing call it? (never confirmable;
               callers found -> refuted, none found -> not_established)
      defines  subject=path, object=symbol — does the file define it?
      contains subject=path, object=literal — does the file contain it?
      reaches  subject=symbol, object=symbol or path prefix — does any
               bounded call chain from subject land there?
    """

    # The claim's shape: calls | unused | defines | contains | reaches (see above).
    kind: Literal["calls", "unused", "defines", "contains", "reaches"]
    # What the claim is about: a symbol name, or a repo-relative path for defines/contains.
    subject: str = Field(min_length=1, max_length=500)
    # The claim's other end: a symbol, a literal (contains) or a path prefix (reaches); '' for
    # unused.
    object: str = Field(default="", max_length=500)
    # For calls and reaches: the most call hops the walk follows (1..6).
    depth: int = Field(default=4, ge=1, le=6)


class CodeVerifyOutput(BaseModel):
    # A subject or object that resolves no symbol is a RESOURCE_NOT_FOUND refusal, not a
    # verdict; its detail carries `role` (subject | object) and `did_you_mean`, up to 5
    # of code.symbol's near misses {qualified_name, path, kind, tier, start_line, end_line}.
    verdict: Literal["confirmed", "refuted", "not_established"]
    # Machine-readable cause, stable for bots to branch on. Empty on confirmed.
    #   no_edge_no_mention                 refuted: no edge in either tier and the object's
    #                                      bare name is absent from the subject's definition
    #   no_edge_name_mentioned             not_established: name present, no resolved edge
    #                                      (macro / template instantiation / lambda / dispatch)
    #   no_edge_subject_not_compiler_indexed  not_established: subject has no scip row
    #   no_edge_body_incomplete            not_established: subject's definition text is not
    #                                      fully indexed, so absence of the name proves nothing
    #   no_edge_subject_unqualified        not_established: subject given as a bare name; it
    #                                      may have resolved to a homonym — qualify it first
    #   depth_exhausted                    not_established: no chain within `depth` (reaches,
    #                                      or calls without an object to test lexically) and
    #                                      the walk stopped at its bounds (`floors.exhausted`)
    #   no_path_in_graph                   not_established: as above, but the walk reached
    #                                      every callee the index holds before its bounds
    #   ctor_edges_absent                  not_established: the subject (unused) or object
    #                                      (calls) is a constructor and the tier answering
    #                                      for it predates constructor calls through
    #                                      factories; `floors.epistemic`
    #                                      is "lower_bound" with `floors.causes`
    #   ambiguous / unused_* / defines_* / contains_*  other kinds, see handler
    reason: str = ""
    # A human-readable account of how the verdict was reached.
    explanation: str
    # Positive rows behind the verdict: edges (a call chain, in order),
    # symbol rows, or content excerpts — shape depends on kind.
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # Why refutation was unavailable when the verdict is not_established:
    # what was searched, how far, and which blind spots apply.
    floors: dict[str, Any] = Field(default_factory=dict)
    # The index's own verdict behind a `not_established` claim ('' otherwise).
    # `no_static_callers`: the subject resolved and the bounded walk found no edge in
    # either tier — the graph's answer, with `reason` and `floors` saying how far it can
    # be trusted; not an abstain. `not_covered`: the subject file is not in the index;
    # `no_match`: it is, but its symbol table or chunk set cannot refute (both abstain).
    # Named apart from `verdict`, which stays the three-valued claim verdict.
    index_verdict: str = ""
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.verify_citations
# ============================================================


class CodeVerifyCitationsInput(_CodeIdentity, _PrPostImage):
    """Every citation a draft carries, resolved against the index before
    the draft is posted somewhere public and irreversible. Citations are
    extracted by shape, never by a model: `path:line`, `path:start-end`,
    `[path:start-end](…/blob/<sha>/path#L1-L9)`, a bare path, `#N` / `PR N`
    / `issue N` / an item URL, and a qualified name `Ns::Name` /
    `Class::method`. Numbers inside fenced code blocks are not citations
    (a repro's `#1` is not an issue); paths, locations and names are.

    Per citation: `resolved` (the target exists and, for a line, sits in a
    known unit), `unresolved` (no such path / number / name in the index),
    `stale` (the path exists but the line is beyond the file or outside
    every unit, or — with `at_commit` — the file changed since that commit),
    `ambiguous` (several definitions). Path, number and name checks are
    exact; "the line is inside a unit" is a lower bound (chunk and symbol
    coverage are floors), and every citation says which.

    `at_commit`: a commit the draft was written against. When the bare
    mirror knows it, the file leg adds — read-only, at most 20 `git show`
    calls and 5 s per request — whether each cited path existed at that
    commit with that many lines and whether it changed between the commit
    and the index head. `base_namespace`: where the PR/issue ledger and the
    master index live when the addressed namespace is a release branch
    (defaults to the mirror's master entry when one is configured).

    A bare `Filename.ext:NNN` (no directory) resolves through the file table, a bare
    function name (`` `sumMap` ``, `readRow()`) through the symbol table, then the class a
    factory registration names, then the quoted registration string in the mirror.
    `pr_number`: the PR the draft reviews — its merge commit (ledger, else the mirror's
    first-parent log) is the default `at_commit`, and every path, location and resolved
    name says `in_pr_diff: true | false | unknown` against the merge commit's hunks;
    `pr_diff` carries the counts."""

    # The draft to check: a review comment, PR body or answer, as plain text or Markdown.
    text: str = Field(min_length=1, max_length=200_000)
    # The commit the draft was written against; '' = check against the index head only
    # (or the PR's merge commit with `pr_number`).
    at_commit: str = Field(default="", max_length=64)
    # The reviewed PR; 0 = no PR-diff leg
    pr_number: int = Field(default=0, ge=0, le=100_000_000)
    # Namespace read after the addressed one for citations it did not resolve; '' = the
    # mirror's default-branch entry when the addressed one is a release branch.
    base_namespace: str = Field(default="", max_length=128)
    # citations verified per call, first-appearance order; the rest are
    # counted in `truncated`
    limit: int = Field(default=200, ge=1, le=500)


class CodeVerifyCitationsOutput(BaseModel):
    # all_resolved | has_unresolved | has_stale | no_citations. Unresolved
    # outranks stale; `ambiguous` counts as resolved (the name exists).
    verdict: str
    # total, resolved, unresolved, stale, ambiguous, kind_<kind> counts
    counts: dict[str, int] = Field(default_factory=dict)
    # one per distinct citation, first-appearance order: {kind, text,
    # verdict, reason, epistemic, occurrences, path?, start_line?, end_line?,
    # number?, name?, rev?, form?, in_pr_diff?, in_code_block?, target?,
    # candidates?}. form: basename | bare_name; in_pr_diff:
    # true | false | unknown, only with `pr_number`.
    # reasons: path_not_indexed | not_covered | line_beyond_eof |
    # line_outside_units | number_unknown | name_unknown |
    # several_definitions | file_changed_since_commit |
    # path_absent_at_commit | line_beyond_eof_at_commit | at_commit_capped |
    # partial_path | qualifier_mismatch | registration_class |
    # registration_string
    citations: list[dict[str, Any]] = Field(default_factory=list)
    # names not verified because their outermost scope is an external
    # library (std::, boost::, …): {kind, text, reason}
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    # True when the draft held more distinct citations than `limit`; the rest are unchecked.
    truncated: bool = False
    # per check: path / number / name / commit -> exact; line -> lower_bound
    epistemic: dict[str, str] = Field(default_factory=dict)
    # {requested, sha, known, calls, capped, ms, error} — the mirror leg
    at_commit: dict[str, Any] = Field(default_factory=dict)
    # only with `pr_number`: {pr_number, merge_sha, source:
    # ledger | mirror_log | "", files_in_diff, in_diff, off_diff, unknown,
    # calls, ms, error: "" | pr_unknown | log_failed | diff_failed |
    # hunks_failed}
    pr_diff: dict[str, Any] = Field(default_factory=dict)
    # The namespaces read, in order: the addressed one, then the base one if any.
    namespaces: list[str] = Field(default_factory=list)
    # Server-side wall time of the call, in milliseconds.
    took_ms: int = 0
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS).
    as_of: dict[str, Any] = Field(default_factory=dict)
    # How far the checks can be trusted, plus why a leg was cut (unknown commit, capped
    # mirror leg, PR not found, truncated citations).
    note: str = ""
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.path_guards
# ============================================================


class CodePathGuardsInput(_CodeIdentity):
    """The conditions that stand on the call path from `from_symbol` to
    `to_symbol`: the verify(reaches) chain, and for each hop the guards the
    call site sits under inside its caller — `if`/`else`/`switch`-case/
    ternary/loop conditions and the early returns and throws textually
    before it — with whether any of them names `mentions`.

    Syntactic and intraprocedural, so the answer is always a lower bound:
    a check inside a helper, an alias of the identifier, a precondition the
    caller's caller asserts, are all invisible. `guarded_by` is a lead to
    read at path:line; `no_guard_named` means no guard on the found path
    names it, never "unchecked"."""

    # The symbol the call path starts from.
    from_symbol: str = Field(min_length=1, max_length=500)
    # a symbol name, or a path prefix (contains '/') like verify(reaches)
    to_symbol: str = Field(min_length=1, max_length=500)
    # the identifier the question asks about ('' lists the guards only)
    mentions: str = Field(default="", max_length=200)
    # The most call hops the walk follows (1..4).
    depth: int = Field(default=4, ge=1, le=4)


class CodePathGuardsOutput(BaseModel):
    #   guarded_by      a guard on some hop names `mentions`
    #   no_guard_named  the path was found, no guard on it names `mentions`
    #   path_found      the path was found and `mentions` was empty (listing)
    #   path_not_found  the bounded walk emptied its frontier without a hit
    #   unknown         the walk hit its depth or node cap with paths open
    #                   (depth_exhausted), a hop has no call-site line
    #                   (hop_without_site), or the namespace holds no guard
    #                   rows at all (guards_not_indexed — the guard stage has
    #                   not reached it; a known guarded call is the positive control)
    verdict: Literal["guarded_by", "no_guard_named", "path_found", "path_not_found", "unknown"]
    # Machine-readable causes behind `unknown`, and the standing floors of a
    # syntactic read otherwise (empty on guarded_by/no_guard_named/path_found
    # only when nothing further limited the answer).
    causes: list[str] = Field(default_factory=list)
    # Always `lower_bound`: guards are read syntactically inside each caller only.
    epistemic: Literal["lower_bound"] = "lower_bound"
    # A human-readable account of how the verdict was reached.
    explanation: str = ""
    # One entry per hop, seed first. A hop is the (caller, callee) PAIR and
    # every call site of it stands on the path, so: {caller, callee, path,
    # line, tier, confidence, guards, mentions_hit} describe the chain's own
    # site and the pair as a whole (mentions_hit = any site), and `sites`
    # lists each site {path, line, guards: [{kind, text}], mentions_hit};
    # `sites_named` counts the sites whose guards name `mentions`.
    hops: list[dict[str, Any]] = Field(default_factory=list)
    # The hops whose guards name `mentions`, as "path:line" — the places to read
    named_at: list[str] = Field(default_factory=list)
    # Symbols the bounded call walk visited.
    visited_symbols: int = 0
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
