"""code.symbol, code.callers / code.callees and code.impact: the definition
lookup and the call graph around it, with the edge and candidate-site wire rows.
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
    _drop_unset_overlay,
    _drop_unset_resolved_by,
    _drop_unset_symbol_context,
    _lead_namespaces,
    _PrPostImage,
    _trim_namespaces,
)
from server.code.domain.units import CodeUnit
from server.operations.base import MCP_HIDDEN

# ============================================================
# code.symbol
# ============================================================


class CodeSymbolInput(_CodeIdentity):
    # bare, qualified, or partially qualified name — or `path:line`
    # ("src/Foo.cpp:123"), which resolves to the innermost function
    # containing that line: a grep hit or review comment becomes the whole
    # function in one call.
    name: str = ""
    # An index symbol id (a definition's `unit_id`, an edge's `*_symbol_id`); when set it
    # wins over `name` and resolves exactly that definition.
    symbol_id: str = ""
    # Read each definition's body; a 200+ line class ships its skeleton plus member
    # outline instead. False = signature, doc and pointer only.
    include_body: bool = True
    # Attach `recent_prs` (merged PRs over the definitions' paths) to the first definition.
    include_history: bool = True
    # Attach `examples` (call sites that use the symbol) to the first definition.
    include_examples: bool = True
    # Attach `lessons` to the first definition: the memories linked to the symbol from
    # `lesson_namespaces` (over MCP the caller's memory home by default) and the
    # repository's team namespace, each with its `code_state` (`fresh`, `moved`, or
    # `changed` when this branch's body differs from the one the memory was written
    # against).
    include_lessons: bool = True
    # The memory namespaces whose linked memories `lessons` reads besides the repository's
    # team rules (over MCP the caller's memory home by default); one the caller cannot
    # read is skipped.
    lesson_namespaces: list[str] = Field(default_factory=list, max_length=4)
    # Estimated-token budget for the answer; definitions past it are served elided
    # (signature, doc, pointer), and the `uses_brief` block is paid out of it first.
    token_budget: int = Field(default=2500, ge=200, le=12000)
    # Definitions carry `fan_in` when the weekly stats are enabled; min_fan_in drops the
    # rest, sort_by='fan_in' puts the most-called definition of an overloaded/ambiguous
    # name first.
    min_fan_in: int = Field(default=0, ge=0)
    # `score` keeps the resolver's order; `fan_in` puts the most-called definition first.
    sort_by: Literal["score", "fan_in"] = "score"
    # `uses=true` returns the definition AND the lines that mention its bare name — what
    # a separate code.grep on the name would return, read beside the definition from the
    # same chunk index — minus the definition's own span(s). A qualified name greps its
    # bare tail. `uses_limit` caps the list (the chunk over-fetch is 3× the cap, the
    # same shape code.grep uses); `uses_truncated` says when the cap held. `uses_brief:
    # true` makes a lookup that resolves to ONE definition carry a compact `uses`
    # block: {count, files, top: [{path, line, caller, tier, confidence}] (<= 8),
    # truncated, epistemic}, built from the live call edges into it and the declined
    # sites named like it, budgeted before the bodies (`uses_tokens`). A hub (fan-in at
    # or above `code_symbol_uses_hub_fan_in`, or a capped read) is {count_floor,
    # skipped: "hub"}. `uses: true` returns the full list and takes precedence.
    uses: bool | None = None
    # At most this many `uses` lines (1–200) when `uses` is true.
    uses_limit: int = Field(default=50, ge=1, le=200)
    # Serve the compact `uses` block when the lookup resolves to one definition;
    # ignored when `uses` is true.
    uses_brief: bool = False
    # When several definitions match, rank them by the change at hand — the files in
    # `near_paths` (<= 20) or the merged PR's file list for `pr_number` from the ledger:
    # same file, same directory, co-change in code_pr_history, then path distance.
    # Ranking only; every definition stays in the list; `picked_by` names the key that
    # decided. Neither set: no ranking by context and no `picked_by` on the wire.
    near_paths: list[str] = Field(default_factory=list, max_length=20)
    # A merged PR whose file list (from the PR ledger) ranks several matching
    # definitions; 0 = no PR ranking.
    pr_number: int = Field(default=0, ge=0)
    # Presence on several namespaces: `presence: true` runs the resolve once per entry
    # of `namespaces` (at most 8; `namespace`, when set and absent, leads the list;
    # unset list = the addressed namespace alone), concurrently under
    # `code_search_fanout_timeout_ms`, every entry authorized at VIEWER first (one
    # refusal refuses the request). The answer is `presence`: per namespace present /
    # absent / unknown with the definition's path and span, plus `summary`;
    # `definitions` is empty. Bodies and enrichment are not read. The
    # release-containment question ("is X on 26.4?") in one call instead of one per
    # branch. `namespaces` without `presence` is refused: the rank interleave is
    # code.search's shape, not a definition lookup's.
    namespaces: list[str] = Field(default_factory=list, max_length=8)
    # Answer per namespace in `namespaces` whether the definition is there (the
    # `presence` / `summary` blocks) instead of returning definitions.
    presence: bool = False
    # The neighbours graph block for the resolved definitions themselves —
    # k callers, callees, tests and citing documents per definition, in the
    # same call. None = off.
    neighbours: bool | None = None
    # How many callers, callees, tests and citing documents (each) the `neighbours`
    # block lists per definition.
    neighbour_k: int = Field(default=5, ge=1, le=20)

    @field_validator("namespaces")
    @classmethod
    def _namespaces_trimmed_and_unique(cls, v: list[str]) -> list[str]:
        return _trim_namespaces(v)

    @model_validator(mode="after")
    def _presence_owns_the_list(self) -> CodeSymbolInput:
        if self.namespaces and not self.presence:
            raise ValueError(
                "namespaces on code.symbol requires presence: true — a ranked merge "
                "across namespaces is code.search's shape"
            )
        self.namespaces = _lead_namespaces(self.namespace, self.namespaces)
        return self


class CodeSymbolOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_empty_presence(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # presence / summary (and the other optional blocks) ride only when
        # their mode fired, so a plain lookup's payload carries none of them
        return _drop_unfired_symbol_blocks(
            _drop_unset_symbol_context(_drop_empty_presence_keys(handler(self)))
        )

    # The resolved definitions (at most 10), fitted to `token_budget`; empty when
    # nothing resolved (see `candidates`) or on a presence answer.
    definitions: list[CodeUnit]
    # True when the served definitions carry more than one distinct name/signature pair.
    ambiguous: bool = False
    # For a class / interface / enum definition, its type hierarchy from the `inherits`
    # edges of both tiers, folded by qualified name: {"supertypes": [...], "subtypes":
    # [...]} of {symbol, kind, via (extends | implements), tier, confidence, path,
    # line}. Absent for functions and when the seed has no hierarchy edge.
    hierarchy: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # When nothing resolved: cheap fuzzy candidates (names + paths only).
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    # Estimated tokens of the answer: definitions, uses, cited documents, neighbours
    # and hierarchy blocks together.
    tokens_returned: int = 0
    # Id of this call's trace row; pass it to `code.outcome`. Empty when no trace
    # row was written (a miss or a presence answer).
    trace_run_id: str = ""
    # uses=true: {path, line, text, scope, scope_start_line, scope_end_line, kind} per
    # line mentioning the symbol's bare name, path/line ordered, the definition span(s)
    # excluded. `kind` is a lexical label of the LINE — `code`, `comment`, `include` —
    # not of the use. Empty and false when the flag is off. With `uses_brief` (or the
    # deployment default) and one definition, the compact block {count, files, top,
    # truncated, epistemic} or {count_floor, skipped: "hub", epistemic}; `uses_tokens`
    # is what it cost out of token_budget (0 and off the wire when no block was built).
    uses: list[dict[str, Any]] | dict[str, Any] = Field(default_factory=list)
    # True when the `uses` list stopped at `uses_limit`.
    uses_truncated: bool = False
    # Estimated tokens the compact `uses` block cost out of `token_budget`.
    uses_tokens: int = 0
    # Which context key ranked the definitions when `near_paths` / `pr_number` was given
    # — same_file | same_dir | co_change | path_distance | none. "" and off the wire
    # when no context was given.
    picked_by: str = ""
    # `anonymous_namespace` | `tail` when an over-qualified name bound through
    # symbol-form tolerance; "" and off the wire otherwise.
    resolved_by: str = ""
    # The document sections that cite the definition (the Markdown lane's `reads` edges)
    # — section qualified name, path, line; at most five distinct sections, first by
    # confidence.
    documented_in: list[dict[str, Any]] = Field(default_factory=list)
    # The neighbours graph block on the definitions (one entry per definition
    # anchor); empty unless `neighbours` ran.
    neighbours: list[dict[str, Any]] = Field(default_factory=list)
    # How many definitions the `neighbours` block was built for; 0 when it is off.
    anchors: int = 0
    # Estimated tokens the `neighbours` block cost (counted in `tokens_returned`).
    neighbour_tokens: int = 0
    # presence=true: one row per requested namespace, in request order — {namespace,
    # verdict: present | absent | unknown, path, start_line, end_line, tier,
    # indexed_commit, index_status, ms, reason, definitions}. `absent` only when the
    # namespace answered with an index behind it (a git_head watermark); `unknown` names
    # why in `reason` (deadline | error:<code> | no_index). `summary` = {present: [...],
    # absent: [...], unknown: [...]}. Both off the wire otherwise.
    presence: list[dict[str, Any]] = Field(default_factory=list)
    # Presence summary: {present, absent, unknown} lists of namespaces; also filled
    # from the entry's `depends_on` namespaces when the name is not defined here.
    summary: dict[str, list[str]] = Field(default_factory=dict)


# ============================================================
# code.callers / code.callees
# ============================================================


class CodeCallersInput(_CodeIdentity, _PrPostImage):
    # Bare, qualified or partly qualified name of the seed symbol, or `path:line` for the
    # innermost function containing that line.
    name: str = ""
    # An index symbol id for the seed; wins over `name`.
    symbol_id: str = ""
    # How many hops to walk out from the seed (1–3); 1 = direct callers / callees.
    depth: int = Field(default=1, ge=1, le=3)
    # At most this many edges across all hops (1–500); `truncated` says when it held.
    max_nodes: int = Field(default=200, ge=1, le=500)
    # Drop parser-tier edges below this confidence. Grades: scip 1.0 (compiler
    # resolved); treesitter 1.0 same-file unique definition, 0.8 method of the
    # enclosing class, 0.6 unique match in the repo-wide name table — the grade
    # where a bare `parse`/`check` binds to an unrelated same-named function.
    # 0 (default) keeps every edge; 0.7 keeps compiler + same-file + class
    # edges only. Every returned edge carries its `confidence` regardless.
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0, json_schema_extra=MCP_HIDDEN)
    # Widen the answer from "what calls it" to "what MAY call it". Callers: the call
    # sites the fast-tier resolver declined that are named like the seed come back as
    # `candidate_sites` — a block apart from `edges`, never merged into it — and, for a
    # virtual method the compiler tier knows, callers of the base declaration and of
    # every override are walked too, each edge tagged `via_override`. Callees: the
    # seed's own declined call sites. Off: neither block is on the wire.
    include_ambiguous: bool | None = None
    # Unset = on; candidates below
    # `candidate_min_confidence` (default the deployment floor, 0.2) are
    # counted in the block's note and not returned. 0 shows every site.
    candidate_min_confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, json_schema_extra=MCP_HIDDEN
    )
    # An OPEN pull request whose head the mirror holds (`refs/pull/N/head`). Its
    # post-image is parsed and merged over the index for this read: a function the PR
    # adds resolves as a seed, and the call sites the PR itself introduces are edges
    # (tier `overlay`). The answer says `overlay: true` and names the head; 0 = the
    # index only.
    pr_number: int = Field(default=0, ge=0)
    # The edge kinds the walk follows — a subset of calls | inherits | overrides |
    # fixture_use | reads | writes. Empty (default) follows them all. `["inherits"]` on
    # a class or interface seed answers "what implements or extends this" in one call,
    # without the constructor calls and method calls that share the seed.
    edge_kinds: list[str] = Field(default_factory=list)


class CodeEdgeWire(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_empty_via(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # `via_override` is on the wire only when dispatch put the edge there;
        # `via` only when the caller is a registration function
        data: dict[str, Any] = handler(self)
        if not data.get("via_override"):
            data.pop("via_override", None)
        if not data.get("via"):
            data.pop("via", None)
        return data

    edge_kind: str
    from_symbol: str
    to_symbol: str
    from_symbol_id: str
    to_symbol_id: str
    path: str
    line: int
    tier: str  # scip = compiler-exact; treesitter = name-resolved
    confidence: float
    # Qualified name of the dispatch seed this edge was reached through — the
    # base virtual an override seed binds to, or an override of a base seed —
    # when `include_ambiguous` walked it. "" for a direct edge.
    via_override: str = ""
    # "registration" when the caller is a registration function
    # (`REGISTER_FUNCTION(X)`, `registerAggregateFunctionX`): the class is
    # reached through a registry that constructs it and drives its interface,
    # not through a call site. "" and off the wire otherwise.
    via: str = ""


class CodeCandidateSiteWire(BaseModel):
    """One declined call site that MAY call (callers) or MAY be called by
    (callees) the seed. Not an edge: the resolver did not bind it."""

    path: str
    line: int
    # the enclosing function of the site (callers) / the callee name as
    # written at the site (callees)
    caller: str = ""
    callee_text: str = ""
    # ambiguous_overload | virtual_dispatch | unresolved_name
    reason: str
    # 1/|candidates| floored at 0.1 for an ambiguous site; 0.5 for an
    # unresolved name (the index had no definition of the name when the file
    # was indexed — the seed being the one definition now says something
    # about the table, not about the call)
    confidence: float
    disposition: str
    # how many definitions of the bare name the resolver saw at the site
    candidates: int = 0


class CodeCandidateGroupWire(BaseModel):
    # receiver_scope: the site was written `Scope::name(...)`, key is the
    # written scope; file: an unqualified / member call, key is the path
    # (callers). callee: grouped by the callee name (callees).
    kind: str
    key: str
    n: int
    confidence: float  # max over the group's sites
    sites: list[CodeCandidateSiteWire]


class CodeCandidateSitesWire(BaseModel):
    """The `include_ambiguous` block of code.callers / code.callees. Always `epistemic:
    lower_bound`: these are sites the resolver could not bind, filtered to the seed's
    language and to the qualification the call was written with, minus the sites `edges`
    already hold. `total` counts what passed the filters before `max_nodes` cut the list
    (`truncated`); `read_capped` says the keyed read itself stopped at its row cap, so
    `total` is a floor too."""

    epistemic: str = "lower_bound"
    total: int = 0
    returned: int = 0
    truncated: bool = False
    read_capped: bool = False
    by_reason: dict[str, int] = Field(default_factory=dict)
    groups: list[CodeCandidateGroupWire] = Field(default_factory=list)
    note: str = ""


class CodeCallersOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_unrequested_candidates(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        # without `include_ambiguous` the candidate_sites key is off the wire
        data: dict[str, Any] = handler(self)
        if data.get("candidate_sites") is None:
            data.pop("candidate_sites", None)
        return _drop_unset_overlay(_drop_unset_resolved_by(data))

    # The call-graph edges found, hop by hop; the far end (`from_*` for callers, `to_*`
    # for callees) is the answer. Empty when the seed is ambiguous or nothing was recorded.
    edges: list[CodeEdgeWire]
    # Qualified names of the definitions the walk started from; empty when refused as
    # ambiguous.
    seed_symbols: list[str]
    # On the wire only when `pr_number` was given. `overlay` true = the PR's post-image
    # was merged over the index (edges of tier `overlay` are the PR's own call sites; a
    # seed the PR adds has no index row); `overlay_head_sha` the head parsed;
    # `overlay_added` how many functions the PR adds in its parsed files; `overlay_note`
    # why the overlay is off (`head_not_in_mirror` | `no_merge_base` | `no_repo_entry`)
    # or which changed files a live one left out.
    overlay: bool = False
    # The PR head commit whose post-image was parsed; "" when there is no overlay.
    overlay_head_sha: str = ""
    # How many functions the PR adds in its parsed files.
    overlay_added: int = 0
    # Why the overlay is off: `head_not_in_mirror`, `no_merge_base` or `no_repo_entry`;
    # when it is on, "" or the changed files it left out unparsed.
    overlay_note: str = ""
    # How an over-qualified name bound when the exact form missed —
    # `anonymous_namespace` (`(anonymous namespace)::` segments stripped) or `tail` (a
    # namespace the definition does not carry dropped; one definition fit), or
    # `registration` when a SQL-level name (`sumMap`, `arrayFold::executeImpl`) bound to
    # the implementing class's methods through its registration statement. "" and off
    # the wire for an exact hit.
    resolved_by: str = ""
    # A bare short name that resolves to many unrelated definitions (setColumns
    # on 60 classes) is not walked: `ambiguous` is set, `candidates` lists the
    # qualified names to pick from, and edges is empty. Qualify the name.
    ambiguous: bool = False
    # With `ambiguous`: up to 20 qualified names to pick from.
    candidates: list[str] = Field(default_factory=list)
    # Virtual dispatch: when the seed is an override, calls in the code go to
    # the BASE declaration, so its callers are included and listed here as
    # {"symbol", "via": "overrides" | "inherits" | "virtual_by_name"}. Empty
    # when the seed is not an override or no base could be resolved. With
    # `include_ambiguous` a base seed's overrides join the list as
    # {"symbol", "via": "overridden_by"}.
    dispatch_seeds: list[dict[str, str]] = Field(default_factory=list)
    # Plain-text notes on how the walk ran: dispatch or member folding, the ambiguity
    # refusal, declined call sites, stale compiler-tier files; "" when none apply.
    note: str = ""
    # True when the walk stopped short: `max_nodes` held or a hop's edge read hit its cap.
    truncated: bool = False
    # Epistemic contract: edge counts are FLOORS, never totals. Both tiers
    # miss dynamic dispatch (function pointers, virtual calls bound at
    # runtime), and the treesitter tier drops ambiguous cross-file callers
    # by design. Zero edges means "none recorded", never "none exists".
    counts_floor: bool = True
    # HOW LOW the floor may be (callers direction): call sites in the fast
    # tier's files whose callee is NAMED like the seed but which the resolver
    # declined to bind — `unresolved` (no definition of that name in the
    # index) or `ambiguous` (several same-named definitions, none pinned).
    # Each is a site that MAY call this symbol and is not in `edges`. 0 when
    # none were declined, or when the seed sits in scip-covered files (the
    # compiler tier binds exactly).
    unresolved_calls: int = 0
    # Declined call sites named like the seed with several same-named definitions,
    # none pinned (see `unresolved_calls`).
    ambiguous_calls: int = 0
    # Epistemic envelope. `epistemic` is "exact" when the compiler tier
    # covers every seed file, no call site named like a seed was declined,
    # and no override seed lacks its base — the edges ARE the callers
    # (static dispatch only). Otherwise "lower_bound", and `causes` says
    # why, from {unresolved_calls, ambiguous_calls, dispatch_seeds_missing,
    # treesitter_only_files, external_calls, ctor_edges_absent (a
    # constructor seed with no edge in an index that predates constructor
    # calls through factories), scip_stale_files (a compiler-tier edge
    # served from a file changed since that tier last ran), truncated (the
    # walk stopped short of the graph)}.
    epistemic: str = "lower_bound"
    # Why `epistemic` is `lower_bound`, from the set above; empty when it is `exact`.
    causes: list[str] = Field(default_factory=list)
    # Distinct symbols reached per hop BEFORE any slicing: {1: n, 2: m}.
    # The true size of the radius when `truncated` says the rows are a page.
    by_hop_counts: dict[int, int] = Field(default_factory=dict)
    # Declined call sites as ranked candidates, on request only (`include_ambiguous`).
    # None (and absent from the wire) otherwise. Never merged into `edges`; its own
    # `epistemic` is always lower_bound while the edges block keeps its own label above.
    candidate_sites: CodeCandidateSitesWire | None = None
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""


# ============================================================
# code.impact
# ============================================================


class CodeImpactInput(_CodeIdentity, _PrPostImage):
    """Symbol-shaped (name/symbol_id) or diff-shaped (files/diff) blast
    radius. Diff-shaped is the CI/pre-commit question — "I changed these
    files: what feels it, which tests reach it, and what usually changes
    together that this change left untouched?" Exactly one shape per call."""

    # Symbol shape: the seed's bare, qualified or partly qualified name, or `path:line`.
    name: str = ""
    # Symbol shape: an index symbol id for the seed; wins over `name`.
    symbol_id: str = ""
    # How many caller hops the radius walks from the seeds (1–2).
    depth: int = Field(default=2, ge=1, le=2)
    # Diff shape: repo-relative changed paths, and/or a unified diff whose
    # hunk line ranges narrow the touched set from whole files to the
    # symbols actually edited.
    files: list[str] = Field(default_factory=list, max_length=50)
    # A unified diff (at most 200,000 chars); its hunk ranges pick the edited functions
    # as seeds.
    diff: str = Field(default="", max_length=200_000)
    # Left out of `owners`: the change's own author(s) are not its reviewers.
    # GitHub logins as recorded on merged PRs.
    exclude_authors: list[str] = Field(default_factory=list, max_length=10)
    # Hub-safe ergonomics. `impacted_symbols` is sliced PER HOP —
    # `per_hop_limit` rows from each hop starting at `offset` within the hop
    # — so a hub's hop-1 fan-out cannot starve hop 2 of the answer. `by_hop_counts` carries the
    # true per-hop totals and `truncated` says whether this page is all of
    # them. `summary_only` drops the impacted rows and keeps counts, risk,
    # tests, co-change and owners: the call a hook makes to decide whether
    # to expand at all.
    summary_only: bool = False
    # At most this many `impacted_symbols` rows from each hop (1–500).
    per_hop_limit: int = Field(default=200, ge=1, le=500)
    # Rows to skip within each hop before `per_hop_limit` applies; pages a hub's radius.
    offset: int = Field(default=0, ge=0, le=100_000)
    # An OPEN pull request whose head the mirror holds. With no `files` / `diff` the
    # PR's own range (merge-base .. head) is the diff; either way its post-image is
    # parsed and merged over the index: the functions the PR ADDS become seeds
    # (`touched_symbols` rows with `via: "added"`, tier `overlay`) and the call sites
    # inside the PR count as hop-1 callers. `overlay: true` on the answer; 0 = the index
    # only.
    pr_number: int = Field(default=0, ge=0)


class CodeImpactOutput(BaseModel):
    @model_serializer(mode="wrap")
    def _drop_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _drop_unset_overlay(_drop_unset_resolved_by(handler(self)))

    # Each row {qualified_name, path, hop, tier}; a row whose function is a
    # registration function (`REGISTER_FUNCTION(X)`, `registerStorageFoo`)
    # carries `via: "registration"` — the seed is reached through a
    # registry; the key is absent otherwise.
    impacted_symbols: list[dict[str, Any]]
    # Tests the test map ties to the seed and impacted paths:
    # {test_path, src_path, confidence}.
    impacted_tests: list[dict[str, Any]]
    # Files changed in the same merged PRs as the seed paths, most often first
    # ({path, prs}, at most 20): the last 10 PRs for the symbol shape, 50 for the diff shape.
    co_change_files: list[dict[str, Any]]
    # impacted/50 + tests/40, capped at 1. Kept for its consumers; as a
    # predictor of a revert or fix PR it is weak (within noise in a
    # calibration replay).
    blast_radius_score: float = 0.0
    # Diff shape only: the symbols the change touches (hunk-overlap when a
    # diff was given, else every function in the files), and the files that
    # historically co-change with the touched set but are absent from it —
    # the "did you forget to update X?" list.
    touched_symbols: list[dict[str, Any]] = Field(default_factory=list)
    # Diff shape: co-change files absent from the change ({path, prs, of}, at most 5,
    # support at least 4 PRs and 15%); empty for the symbol shape.
    co_change_missing: list[dict[str, Any]] = Field(default_factory=list)
    # Who to ask: {author, share, prs, reviews, last_merged_at} from merged
    # PRs over the seed paths — the PRs a person AUTHORED (`prs`) and the
    # ones they REVIEWED (`reviews`, inline comments), recency-decayed
    # (120-day half-life), share floor, at most 3, bots and
    # `exclude_authors` removed; `via` is "area" for those rows. When fewer
    # than three clear the floor, the list is topped up with the
    # namespace's busiest reviewers of the last 180 days, `via`
    # "namespace", share 0 (bench/owners_precision.py measures the list).
    # See code/domain/index/owners.py.
    owners: list[dict[str, Any]] = Field(default_factory=list)
    # Merged PRs over the seed paths that were REVERTS ({pr_number, title,
    # merged_at}, newest first, <=5): the one kind of history that says what
    # not to do, invisible to symbol lookups (the code is back) and to
    # co-change (same files as the original). Empty = none in the PR rows
    # considered (a floor, like every count here).
    reverts: list[dict[str, Any]] = Field(default_factory=list)
    # Same epistemic contract as code.callers: radius counts are floors.
    counts_floor: bool = True
    # Symbol shape: call sites named like the seed(s) that the fast tier
    # declined to bind — the size of the floor. See CodeCallersOutput.
    unresolved_calls: int = 0
    # Declined call sites with several same-named definitions (see `unresolved_calls`).
    ambiguous_calls: int = 0
    # Epistemic envelope. `epistemic` is "exact" when the compiler tier
    # covers every seed file, no call site named like a seed was declined,
    # and no override seed lacks its base — the edges ARE the callers
    # (static dispatch only). Otherwise "lower_bound", and `causes` says
    # why, from {unresolved_calls, ambiguous_calls, dispatch_seeds_missing,
    # treesitter_only_files, external_calls, ctor_edges_absent (a
    # constructor seed with no edge in an index that predates constructor
    # calls through factories), truncated (the walk or its page stopped short
    # of the graph)}.
    epistemic: str = "lower_bound"
    # Why `epistemic` is `lower_bound`, from the set above; empty when it is `exact`.
    causes: list[str] = Field(default_factory=list)
    # Distinct symbols reached per hop BEFORE any slicing: {1: n, 2: m}.
    # The true size of the radius when `truncated` says the rows are a page.
    by_hop_counts: dict[int, int] = Field(default_factory=dict)
    # True when `impacted_symbols` is a page of the radius (per_hop_limit /
    # offset sliced it, or the per-hop edge read hit its cap).
    truncated: bool = False
    # `anonymous_namespace` | `tail` when the symbol shape's name bound through
    # symbol-form tolerance; "" and off the wire otherwise.
    resolved_by: str = ""
    # Why the radius is empty, '' when it is not. Answers:
    # `no_indexed_paths` (the change names no path the symbol table holds
    # functions for — test-only / data-file diffs; the graph is untouched),
    # `no_seeds_in_hunks` (indexed source, but every hunk falls outside every
    # function), `no_static_callers` (the touched functions resolved and
    # nothing in either tier calls, inherits or overrides them — treat it as
    # the answer; `epistemic`/`causes` say whether it is exact or a floor).
    # Abstains (`abstain: true`): `not_covered` (a source path the index does
    # not hold), `no_match` (the diff parsed to no path — or a request that
    # named no symbol, no files and no diff — a verdict rather than a 404).
    verdict: str = ""
    # Diff shape: every path the request named, with its class — `test` | `doc` |
    # `config` | `unindexed` (outside the indexed prefixes, or a source path the index
    # holds no file row for) | `src` — so the verdict is actionable without re-deriving
    # it: `no_indexed_paths` over paths that are all `test` is a test-only change (skip
    # the gap analyst), and that verdict is `epistemic: exact` whenever no path is
    # `src`. Empty for the symbol shape.
    paths_seen: list[dict[str, str]] = Field(default_factory=list)
    # On the wire only when `pr_number` was given — see CodeCallersOutput.
    # `overlay_added` counts the functions the PR adds in its parsed post-image files;
    # each is a `touched_symbols` seed.
    overlay: bool = False
    # The PR head commit whose post-image was parsed; "" when there is no overlay.
    overlay_head_sha: str = ""
    # How many functions the PR adds in its parsed files, each a `touched_symbols` seed.
    overlay_added: int = 0
    # Why the overlay is off: `head_not_in_mirror`, `no_merge_base` or `no_repo_entry`;
    # when it is on, "" or the changed files it left out unparsed.
    overlay_note: str = ""
    # Id of this call's trace row; pass it to `code.outcome`.
    trace_run_id: str = ""
