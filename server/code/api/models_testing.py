"""The test plane: code.test_gate (tests to run, and the touched definitions measured
unexecuted or linked to no test), code.test_home, code.tests_for and code.coverage_claim.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer

from server.code.api.models_common import _CodeIdentity, _drop_unset_resolved_by

# ============================================================
# code.test_gate (pre-PR obligations: tests to run + what no test is shown to run)
# ============================================================


class CodeTestGateInput(_CodeIdentity):
    """The pre-PR question in one call: for THIS change, which tests must run, and
    which touched definitions no test is shown to run. Diff-shaped like code.impact
    (files and/or a unified diff); the answer is the tests, two lists of touched
    definitions and one boolean, `obligations`, that a Stop hook or CI step can gate on.
    Tests come back in EVIDENCE order — a changed test file, then tests a coverage run
    measured executing a touched definition, then tests a call chain links to one, then
    tests that call other definitions of the edited files, then the mined test map —
    each row saying why it is there and how to run it (or `run_unknown` when the harness
    is not derivable from the path; never a guessed command)."""

    # Changed file paths; a test file here is itself a test to run. Unioned with the
    # diff's paths; files and/or `diff` must be given.
    files: list[str] = Field(default_factory=list, max_length=50)
    # A unified diff against the revision the index holds (the working tree against HEAD, a
    # pull request against its merge base). Each changed line touches the innermost
    # function or class it sits in; a line in neither touches nothing. A file with no
    # hunks, as from `files` alone, touches every function in it.
    diff: str = Field(default="", max_length=200_000)
    # Reverse-edge hops to look for tests through. Two is code.impact's
    # ceiling; three reaches integration tests that sit behind a fixture.
    depth: int = Field(default=2, ge=1, le=3)
    # At most this many rows in each of `tests`, `untested` and `unlinked` (at most 200).
    limit: int = Field(default=30, ge=1, le=200)


class CodeTestHomeInput(_CodeIdentity):
    """Where does a new test case belong? A dedup fingerprint —
    the affected file stems, the settings the test uses, the engine class,
    the symbols it exercises — against the indexed tests tree. Answers
    `add_case_to` with the existing stateless or integration file that
    already covers the area (the maintainers' most repeated ask), or
    `new_file_justified`, or `unknown` when the positive control failed.
    Also the next free stateless test number."""

    # Source file stems the change affects (a path or extension is stripped); matched in
    # test content and against indexed source paths. Malformed entries are dropped.
    file_stems: list[str] = Field(default_factory=list, max_length=10)
    # Setting names the test uses (lower snake_case); others are dropped.
    settings: list[str] = Field(default_factory=list, max_length=10)
    # The engine class (`MergeTree`; parameters stripped), matched as `ENGINE = <name>`.
    engine: str = Field(default="", max_length=60)
    # Symbols the test exercises; the part after the last `::` is what is matched.
    symbols: list[str] = Field(default_factory=list, max_length=10)
    # At most this many `candidates` (at most 50).
    limit: int = Field(default=10, ge=1, le=50)


class CodeTestHomeOutput(BaseModel):
    # add_case_to | new_file_justified | unknown
    verdict: str = "unknown"
    # The top candidate's path when the verdict is add_case_to; '' otherwise.
    home: str = ""
    # {path, matched: ["setting:x", "engine:MergeTree", …], map_confidence, score,
    #  reference: the candidate's indexed expectation file or null}
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    # The fingerprint as normalized, `kind:value` per part (stem, setting, engine, symbol).
    parts: list[str] = Field(default_factory=list)
    # Indexed source files whose stem is one of `file_stems` (first 20).
    src_paths: list[str] = Field(default_factory=list)
    # The highest stateless test number in the index plus one; 0 when none was found.
    next_free_stateless_number: int = 0
    # {symbol, ok}: the positive control this call ran; ok=False ⇒ unknown
    control: dict[str, Any] = Field(default_factory=dict)
    # Id of this call's row in `loom.code_search_trace`.
    trace_run_id: str = ""


class CodeCoverageClaimInput(_CodeIdentity):
    """Is this already tested? One subject — a symbol name (a C++ symbol or a SQL-level
    function name such as `sumMap`), a source path or a setting name — and the tests
    that reach it, each row with its evidence `tier`: `exact_scoped` (a test names the
    qualified symbol or the SQL name the class registers), `pr_cooccurrence` (the test
    file changed with the source in a PR), `short_name_grep` (a test contains the bare
    method name), `file_reference` (a test names another function the same file
    registers), `path_convention`, and `executed` above them all (an imported per-test
    coverage run measured the test executing the subject's lines). `strongest_tier`
    summarises; `search_terms` says which literal was grepped at which tier; `measured`
    says whether the run executed the subject at all. Verdict `covered_by` needs an
    executed, exact_scoped or pr_cooccurrence row; `weak_evidence` when only weaker tiers
    were found (`uncovered` when the run measured the subject unexecuted);
    `uncovered` when nothing was — the last two only when the call's positive control
    found the corpus measurable, otherwise `unknown`. Both test trees (0_stateless,
    1_stateful) and tests/integration are searched."""

    # The subject as a symbol name (qualified or bare) or a SQL-level function name.
    symbol: str = Field(default="", max_length=300)
    # The subject as a repository-relative source path.
    path: str = Field(default="", max_length=1000)
    # The subject as a setting name.
    setting: str = Field(default="", max_length=120)
    # At most this many `tests` rows (at most 100).
    limit: int = Field(default=20, ge=1, le=100)
    # "give exactly one of symbol, path, setting" is the handler's check,
    # raised after the namespace gate


class CodeCoverageClaimOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _drop_unset_resolved_by(handler(self))

    # The subject as given (the one of symbol, path, setting that was set).
    subject: str = ""
    # symbol | path | setting
    subject_kind: str = ""
    # covered_by | weak_evidence | uncovered | unknown
    verdict: str = "unknown"
    # {test_path, methods: [...], tier, confidence, evidence}
    tests: list[dict[str, Any]] = Field(default_factory=list)
    # Tests found before `limit` (the larger of the rows and the grep's match count).
    tests_total: int = 0
    # The source files the subject lives in: the path itself, the files defining the
    # resolved symbol (first 20), or [] for a setting.
    src_paths: list[str] = Field(default_factory=list)
    # exact when a test names the subject or the include graph reaches it;
    # lower_bound when only mined co-occurrence or a path convention says so
    epistemic: str = "lower_bound"
    # The strongest evidence tier among the rows (executed | exact_scoped |
    # pr_cooccurrence | short_name_grep | file_reference | path_convention), '' when no
    # test was found.
    strongest_tier: str = ""
    # The literals the grep ran, each with the tier a hit counts as — the SQL names the
    # class registers and the qualified form are `exact_scoped`, the bare method name
    # `short_name_grep`.
    search_terms: dict[str, str] = Field(default_factory=dict)
    # `registration` when the subject was a SQL-level name bound to its implementing
    # class through the registration statement; '' and off the wire otherwise.
    resolved_by: str = ""
    # {symbol, ok[, via]}: the positive control this call ran; ok=False ⇒ unknown.
    # `via: unit_tests` when a test unit reaching the subject stood in for it.
    control: dict[str, Any] = Field(default_factory=dict)
    # Imported execution evidence, {} when none exists: measured, executed, paths,
    # hits, commit (empty for mixed commits), commits, index_commit, run_current,
    # files (per-path commit, indexed_commit, symbol_commits, freshness), freshness,
    # unverified_paths, unmeasured_paths. Only verified source and symbol-span identities
    # contribute hits; nonexecution also requires a run at the current index watermark.
    # Freshness is verified | partial | unverified.
    measured: dict[str, Any] = Field(default_factory=dict)
    # Id of this call's row in `loom.code_search_trace`.
    trace_run_id: str = ""


class CodeTestGateOutput(BaseModel):
    # {test_path, why, evidence, run, run_unknown} per test file, strongest evidence first.
    # `why` is the kind of evidence: `changed_test` (the file is in the diff), `coverage`
    # (a coverage run measured it executing a touched definition; `links_to`),
    # `static_call` (a call chain links its tests to a touched definition: `hop` of the
    # nearest, `callers` how many of its tests do, `links_to` naming up to ten of the
    # definitions, `confidence` 0.9 at one hop, 0.8 at two and 0.6 at three,
    # `via` `constructor` when the test calls the class of a touched constructor,
    # `enclosing` when it calls the function a touched nested function is defined in or
    # `fixture` when it reaches through a fixture), `file_neighbor` (it calls `callees`
    # other definitions of the edited files and says nothing about the touched ones),
    # `test_map` (mined history about the file: `src_path`, `confidence`). Within a kind
    # the nearest hop is first, then the file linked to more of the touched definitions,
    # then the one with more tests or callees.
    tests: list[dict[str, Any]] = Field(default_factory=list)
    # Touched source definitions a coverage run measured unexecuted, at a commit the index
    # verified: {qualified_name, path, start_line, commit}. Empty without such a run.
    untested: list[dict[str, Any]] = Field(default_factory=list)
    # Touched source definitions no coverage run measured and no call chain links a test
    # to: {qualified_name, path, start_line}. Not proof that no test runs them: tests reach
    # code through fixtures, string dispatch and entry points the graph has no edge for. A
    # `file_neighbor` or `test_map` row never takes a definition off this list.
    unlinked: list[dict[str, Any]] = Field(default_factory=list)
    # The change as the gate saw it.
    changed_files: list[str] = Field(default_factory=list)
    # The source definitions the change touches, {qualified_name, path, start_line, tier},
    # by path and line (first 200).
    touched_symbols: list[dict[str, Any]] = Field(default_factory=list)
    # True while `tests` or `untested` is non-empty: run the tests, then rely on green;
    # write a test for (or explain) the untested rows. `unlinked` alone asks for nothing.
    obligations: bool = False
    # Rows of `tests` before `limit`.
    tests_total: int = 0
    # Rows of `untested` before `limit`.
    untested_total: int = 0
    # Rows of `unlinked` before `limit`.
    unlinked_total: int = 0
    # True when `limit` cut `tests` short of `tests_total`.
    tests_capped: bool = False
    # True when `limit` cut `untested` short of `untested_total`.
    untested_capped: bool = False
    # True when `limit` cut `unlinked` short of `unlinked_total`.
    unlinked_capped: bool = False
    # Same epistemic contract as code.callers: the graph is a floor, so a test missing
    # from `tests` is one the index cannot see.
    counts_floor: bool = True
    # `measured` when a verified coverage run settled every touched definition and every
    # changed line sat in one; otherwise `lower_bound` with `causes`. The gate never
    # answers `exact`: a static graph cannot show that no test runs a definition.
    epistemic: str = "lower_bound"
    # Why `epistemic` is lower_bound, from code.callers' causes (unresolved_calls,
    # ambiguous_calls, treesitter_only_files, external_calls, truncated: a hop read hit its
    # row limit) and two of its own: `unmapped_changes` (a changed line of an indexed file
    # sits in no definition) and `no_coverage` (no verified coverage run measured a touched
    # definition). Empty when `epistemic` is measured, or when the change touches no
    # indexed source definition.
    causes: list[str] = Field(default_factory=list)
    # Distinct symbols reached per hop BEFORE any slicing: {1: n, 2: m}.
    # The true size of the radius when `truncated` says the rows are a page.
    by_hop_counts: dict[int, int] = Field(default_factory=dict)
    # Id of this call's row in `loom.code_search_trace`.
    trace_run_id: str = ""


# ============================================================
# code.tests_for
# ============================================================


class CodeTestsForInput(_CodeIdentity):
    """Which stateless tests carry EVERY named facet: the tests that `SET` a setting,
    call a function, create an `ENGINE`, read a `system.*` table, use a `FORMAT`, carry
    a `-- Tags:` tag, or expect an error name in a `{ serverError X }` hint — from the
    statement records of `.sql` tests (`loom.code_test_facets`), one keyed read, no
    chunk scan. Facets AND together; `same_statement` on a row says they co-occur on one
    statement ("expects NUMBER_OF_ARGUMENTS_DOESNT_MATCH from a call to arrayJoin" wants
    that). Kinds: `setting`, `function`, `engine`, `type`, `system_table`, `format`,
    `error_code`, `tag`, `statement_kind`. Names are as written in the test
    (`quantileExactWeightedInterpolated`, `ReplicatedMergeTree`, `BAD_ARGUMENTS`); a
    hint's numeric code is its own name (`62`). SQL inside TypeScript, Python, Go and
    shell files adds `dialect`, `table_read`, `table_write`; the file facts `http_route`
    (`GET /users/:id`, at the handler) and `env_var` (`DATABASE_URL`, at the read) cover
    TypeScript / JavaScript / Python."""

    # {kind, name} per facet (kinds above; an unknown kind or an empty name is refused);
    # duplicates are dropped.
    facets: list[dict[str, str]] = Field(min_length=1, max_length=24)
    # At most this many `tests` rows (at most 500).
    limit: int = Field(default=50, ge=1, le=500)
    # `closest: true` answers "the existing test most like this one" instead of "the
    # tests carrying every facet": candidates share at least one facet and rank by a
    # weighted per-kind Jaccard over their whole facet sets; each row carries
    # `similarity`, `shared` (what the candidate has of the ask) and `missing` (what the
    # ask has that it lacks — the diff a new case would add). Up to 24 facets may be
    # named.
    closest: bool = False


class CodeTestsForOutput(BaseModel):
    # {path, ordinals, lines, confidence, same_statement}, keyword-bound
    # matches (confidence 1.0) before call-shaped ones (0.8), then by path;
    # with `closest`: {path, similarity, shared, missing, extra_facets} by
    # similarity descending.
    tests: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0  # matching tests before `limit`
    # True when `total` exceeds the rows returned.
    truncated: bool = False
    facets: list[dict[str, str]] = Field(default_factory=list)  # as resolved
    # '' when rows were returned; `no_match` when the namespace holds facet
    # rows of every asked kind but none carry these names (an answer);
    # `not_covered` when some asked kind has no row in the namespace at all
    # (its lanes never wrote that kind — a TypeScript tree asked for a
    # `setting`, or a tree indexed before the kind existed; grep is the
    # fallback).
    verdict: str = ""
    # True only for `not_covered` (the index cannot answer; grep instead).
    abstain: bool = False
    uncovered_kinds: list[str] = Field(default_factory=list)  # the kinds behind not_covered
    # Provenance + freshness: branch, commit, indexed_at, lag_commits (int, -1
    # unknown), stale (bool: lag > LOOM_CODE_STALE_AFTER_COMMITS), status.
    as_of: dict[str, Any] = Field(default_factory=dict)
    # Estimated tokens of the returned paths (about 4 characters per token).
    tokens_returned: int = 0
    # Server time spent on the call, in milliseconds.
    took_ms: int = 0
    # Id of this call's row in `loom.code_search_trace`.
    trace_run_id: str = ""
