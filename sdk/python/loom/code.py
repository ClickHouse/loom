"""Code intelligence client — retrieval over a shared, per-merge-fresh
index of a source repository (the loom code plane).

    hits = await agent.code.search("how are quotas enforced?")
    for u in hits["results"]:
        print(u["path"], u["symbol"], u["score"])
    if hits["abstain"]:
        print("no strong match — grep the working tree instead")

    d = await agent.code.symbol("IColumn::insert")
    callers = await agent.code.callers("MergeTreeData::renameTempPartAndAdd")
    pack = await agent.code.context_pack(pr_number=54321)

org/namespace default server-side to the deployment's code namespace, so
a bot with any read grant on it needs no extra configuration. Report what
you did with the results via `outcome(...)` — it feeds ranking quality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class CodeClient:
    """`callers` / `callees`: `min_confidence` drops parser-tier edges below
    the grade (scip is always 1.0; treesitter 1.0 same-file, 0.8 enclosing
    class, 0.6 repo-wide name match). Edges arrive compiler tier first, then
    by confidence, each carrying its own `confidence`. `include_ambiguous`
    adds the declined call sites as a `candidate_sites` block apart from the
    edges; unset follows the deployment default.

    `search`, `outline`, `callers`, `callees` and `impact` answer with `lessons`: the
    team's memories about the answer's top files, and `lessons_left_out`, how many
    more rows about those files `memory.for_file` holds (left out by the cap, a cut, or
    because a newer row read for the answer supersedes them). `include_lessons=False`
    skips that read."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        token_budget: int | None = None,
        path_prefix: str = "",
        bodies: str | None = None,
        annotate: bool = True,
        pattern: str = "",
        regex: bool = False,
        case_sensitive: bool = False,
        path_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        langs: list[str] | None = None,
        namespaces: list[str] | None = None,
        include_lessons: bool = True,
    ) -> dict[str, Any]:
        """Hybrid code search (symbol-shaped queries answer exactly from
        the symbol table). Read three fields on every answer: `abstain`
        (an answer, not a failure — `abstain_code == "not_covered"` means the
        path is outside the index, so read the file yourself),
        `retrieval_risk`, and `as_of.stale` (index trails master by more
        than the configured commits — treat spans as approximate). Keep
        `trace_run_id` and call `outcome(...)`. `token_budget` / `bodies`
        unset take the server's defaults (a route-aware budget, the
        deployment's bodies setting); `bodies="none"` returns
        spans and signatures only (cheapest); `annotate=False` drops the
        churn/amp/tested risk annotations. `pattern` (literal, or RE2 with
        `regex=True`) constrains BOTH legs to chunks containing it — one call
        instead of grep-then-search; `abstain_code == "pattern_too_broad"`
        carries `matched_files` and asks you to narrow. `path_globs` /
        `exclude_globs` (rg `-g`) and `langs` (rg `-t`) scope by file.
        `namespaces=[...]` (up to 8) searches several indexed repositories
        at once — ranks interleaved in that order under one budget, each
        hit tagged `namespace`, `fanout` disclosing per-namespace timing;
        no `next_cursor` under fan-out. `lessons` as in the class docstring."""
        body: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "path_prefix": path_prefix,
            "annotate": annotate,
        }
        if token_budget is not None:
            body["token_budget"] = token_budget
        if bodies is not None:
            body["bodies"] = bodies
        if pattern:
            body.update({"pattern": pattern, "regex": regex, "case_sensitive": case_sensitive})
        if path_globs:
            body["path_globs"] = list(path_globs)
        if exclude_globs:
            body["exclude_globs"] = list(exclude_globs)
        if langs:
            body["langs"] = list(langs)
        if namespaces:
            body["namespaces"] = list(namespaces)
        if not include_lessons:
            body["include_lessons"] = False
        return await self._agent.transport.post("/v1/code.search", body)

    async def grep(
        self,
        pattern: str,
        *,
        regex: bool = False,
        path_prefix: str = "",
        limit: int | None = None,
        mode: str = "matches",
        path_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        langs: list[str] | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        """Exact literal (or RE2 regex) match over indexed chunks; every hit
        carries its enclosing symbol (`scope`) and that unit's span
        (`scope_start_line` / `scope_end_line`, 0 outside any unit), so a
        caller can skip `enclosing()` for a hit inside a function it has
        already shown. `mode="files"` answers "which files
        mention X" (no line text) — use it for coverage questions. `limit`
        defaults per mode: 20 lines, or the 500-file ceiling, so a files
        answer is complete unless `truncated` says otherwise. `not_covered:
        true` means the path_prefix lies outside the indexed prefixes.
        `mode="presence"` with `namespaces=[...]` (up to 8)
        answers "which of these indexes contain the pattern" in one call:
        `presence` has one row per namespace — verdict present / absent /
        unknown, `files_total`, the first matching file — and `summary`
        groups the verdicts.
        `mode="message"` takes a RENDERED error/log message and returns the
        statement whose template printed it (`verdict` raised_at |
        no_template_match, `template`, `error_code`). A regex-shaped literal
        that matches nothing is run once as a regex; `note` says so."""
        body: dict[str, Any] = {
            "pattern": pattern,
            "regex": regex,
            "path_prefix": path_prefix,
            "mode": mode,
        }
        if namespaces:
            body["namespaces"] = list(namespaces)
        if limit is not None:
            body["limit"] = limit
        if path_globs:
            body["path_globs"] = list(path_globs)
        if exclude_globs:
            body["exclude_globs"] = list(exclude_globs)
        if langs:
            body["langs"] = list(langs)
        return await self._agent.transport.post("/v1/code.grep", body)

    async def files(
        self,
        *,
        glob: str = "",
        stem: str = "",
        path_prefix: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Indexed files by NAME: an rg-style glob, a
        basename stem (exact or leading prefix: `05138_with_fill` finds
        05138_with_fill_step_below_precision.sql) or a path prefix, from
        the file table — `files` [{path, lang, size_bytes, loc,
        indexed_commit, reference (a test's indexed expected-output file or
        None)}], exact stem matches first, `total` before
        `limit`. grep matches content, not paths; this answers "does test
        NNNNN exist, and where"."""
        body: dict[str, Any] = {
            "glob": glob,
            "stem": stem,
            "path_prefix": path_prefix,
            "limit": limit,
        }
        return await self._agent.transport.post("/v1/code.files", body)

    async def enclosing(
        self,
        locations: list[str],
        *,
        include_body: bool = False,
        token_budget: int = 4000,
        neighbours: bool | None = None,
        neighbour_k: int = 5,
    ) -> dict[str, Any]:
        """Batched `path:line` -> enclosing function (<=200 per call), input
        order preserved; `symbol` is "" for lines outside any indexed
        function. This is what the grep shim calls to annotate real grep
        output, and the primitive for review comments that cite path:line.
        `include_body=True` ships each enclosing function's whole body
        (fetched once per distinct function, filled until `token_budget`;
        the rest carry a `next` code.symbol escalation) — a grep hit
        becomes the function it sits in, in one call. `neighbours=True`
        adds `neighbours`: per distinct enclosing function (<=20) up to
        `neighbour_k` callers, callees and mapped tests, its module and the
        exact / lower_bound envelope — evidence beside the hits, never a
        rank. None follows the deployment default (off)."""
        body: dict[str, Any] = {
            "locations": locations,
            "include_body": include_body,
            "token_budget": token_budget,
            "neighbour_k": neighbour_k,
        }
        if neighbours is not None:
            body["neighbours"] = neighbours
        return await self._agent.transport.post("/v1/code.enclosing", body)

    async def outline(
        self,
        path: str,
        *,
        kinds: list[str] | None = None,
        token_budget: int = 1500,
        include_lessons: bool = True,
    ) -> dict[str, Any]:
        """One file's declarations with line spans, in source order
        (functions, methods, classes, structs, fixtures by default) — the
        answer to "what is in this file?" without reading it. Whole-row
        budgeted: an entry is complete or absent, and `next` is the
        code.symbol call for the first entry that did not fit.
        `indexed: false` means the path has no live symbol rows. `lessons` as
        in the class docstring."""
        body: dict[str, Any] = {"path": path, "token_budget": token_budget}
        if kinds is not None:
            body["kinds"] = kinds
        if not include_lessons:
            body["include_lessons"] = False
        return await self._agent.transport.post("/v1/code.outline", body)

    async def symbol(
        self,
        name: str = "",
        *,
        symbol_id: str = "",
        include_body: bool = True,
        include_history: bool = True,
        include_examples: bool = True,
        include_lessons: bool = True,
        token_budget: int = 2500,
        uses: bool | None = None,
        uses_limit: int = 50,
        uses_brief: bool = False,
        near_paths: list[str] | None = None,
        pr_number: int = 0,
        namespaces: list[str] | None = None,
        presence: bool = False,
    ) -> dict[str, Any]:
        """Exact definition lookup by (qualified) name — or by `path:line`
        ("src/Foo.cpp:123"), which returns the innermost function containing
        that line — compiler-grade where available, enriched with recent
        PRs, tested usage examples, and lessons from the memory plane.

        `uses_brief=True` makes a lookup that resolves to
        one definition carry a compact `uses` block: `count`, `files`, the
        top call sites `top: [{path, line, caller, tier, confidence}]`,
        `truncated`, `epistemic` — or `{count_floor, skipped: "hub"}` for a
        name the repo calls from everywhere; `uses_tokens` is what it cost
        out of `token_budget`. `uses=True` asks instead for
        the full list: the lines mentioning the symbol's bare name with their
        enclosing scope — what a code.grep on the name would return — minus
        the definition itself, capped at `uses_limit` (`uses_truncated`
        says when the cap held). `uses=False` drops a deployment-default
        block.

        `near_paths` (up to 20 files you are looking at) or `pr_number` (a
        merged PR in the ledger) ranks several matching definitions by the
        change at hand — same file, same directory, co-change history, path
        distance; every definition stays in the list and
        `picked_by` names the key that decided (`none` when nothing did).
        Sent only when given.

        `presence=True` with `namespaces=[...]` (up to 8)
        answers "on which of these indexes does the symbol exist" in one
        call: `presence` has one row per namespace — verdict present /
        absent / unknown with the definition's path, span and tier — and
        `summary` groups the verdicts; `definitions` is empty. Unknown
        (deadline, error, no index) is never folded into absent."""
        body: dict[str, Any] = {
            "name": name,
            "symbol_id": symbol_id,
            "include_body": include_body,
            "include_history": include_history,
            "include_examples": include_examples,
            "include_lessons": include_lessons,
            "token_budget": token_budget,
        }
        if uses is not None:
            body["uses"] = bool(uses)
            if uses:
                body["uses_limit"] = uses_limit
        if uses_brief:
            body["uses_brief"] = True
        if near_paths:
            body["near_paths"] = list(near_paths)
        if pr_number:
            body["pr_number"] = int(pr_number)
        if presence or namespaces:
            body["presence"] = True
            body["namespaces"] = list(namespaces or [])
        return await self._agent.transport.post("/v1/code.symbol", body)

    async def callers(
        self,
        name: str = "",
        *,
        symbol_id: str = "",
        depth: int = 1,
        max_nodes: int = 200,
        min_confidence: float = 0.0,
        include_ambiguous: bool | None = None,
        candidate_min_confidence: float | None = None,
        pr_number: int = 0,
        include_lessons: bool = True,
    ) -> dict[str, Any]:
        """Confidence, `include_ambiguous` and `lessons` as in the class docstring;
        `include_ambiguous` here also walks a virtual method's overrides
        (edges tagged `via_override`). By default candidates below the
        deployment's confidence floor are counted, not returned;
        `candidate_min_confidence=0` shows every declined site. An edge
        carrying `via: "registration"` comes from a registration function —
        the factory-registered class's static caller; `resolved_by` on the
        payload says when an over-qualified name (`(anonymous namespace)::`,
        an extra namespace) bound tolerantly. `pr_number` merges an OPEN
        PR's post-image over the index: a function the PR adds resolves as
        the seed and its call sites inside the PR are edges of tier
        `overlay`."""
        body: dict[str, Any] = {
            "name": name,
            "symbol_id": symbol_id,
            "depth": depth,
            "max_nodes": max_nodes,
            "min_confidence": min_confidence,
        }
        if include_ambiguous is not None:
            body["include_ambiguous"] = bool(include_ambiguous)
        if candidate_min_confidence is not None:
            body["candidate_min_confidence"] = float(candidate_min_confidence)
        if pr_number:
            body["pr_number"] = int(pr_number)
        if not include_lessons:
            body["include_lessons"] = False
        return await self._agent.transport.post("/v1/code.callers", body)

    async def callees(
        self,
        name: str = "",
        *,
        symbol_id: str = "",
        depth: int = 1,
        max_nodes: int = 200,
        min_confidence: float = 0.0,
        include_ambiguous: bool | None = None,
        candidate_min_confidence: float | None = None,
        include_lessons: bool = True,
    ) -> dict[str, Any]:
        """Confidence, `include_ambiguous` and `lessons` as in the class docstring."""
        body: dict[str, Any] = {
            "name": name,
            "symbol_id": symbol_id,
            "depth": depth,
            "max_nodes": max_nodes,
            "min_confidence": min_confidence,
        }
        if include_ambiguous is not None:
            body["include_ambiguous"] = bool(include_ambiguous)
        if candidate_min_confidence is not None:
            body["candidate_min_confidence"] = float(candidate_min_confidence)
        if not include_lessons:
            body["include_lessons"] = False
        return await self._agent.transport.post("/v1/code.callees", body)

    async def impact(
        self,
        name: str = "",
        *,
        symbol_id: str = "",
        depth: int = 2,
        files: list[str] | None = None,
        diff: str = "",
        exclude_authors: list[str] | None = None,
        pr_number: int = 0,
        include_lessons: bool = True,
    ) -> dict[str, Any]:
        """Blast radius, in either shape.

        Symbol shape (`name` / `symbol_id`): reverse edges + mapped tests +
        co-change files for one symbol.

        Diff shape (`files` and/or `diff`, the review-bot question): the
        symbols the hunks overlap, the tests that reach them, and
        `co_change_missing` — files that changed together with these in ≥2
        past PRs but are absent from this change. Pass the unified diff for
        hunk-level precision; files alone means every function in them.

        Both shapes return `owners`: recency-weighted PR authors over the seed
        paths (who to ask), with `exclude_authors` — normally the change's own
        author — removed before shares are computed.

        `pr_number`: an OPEN PR whose head the mirror holds.
        With no `files` / `diff` its own range is the diff; its post-image is
        merged over the index so the functions it adds are seeds (`via:
        "added"`) and its own call sites count. `overlay: true` on the answer.
        `lessons` as in the class docstring."""
        body: dict[str, Any] = {
            "name": name,
            "symbol_id": symbol_id,
            "depth": depth,
        }
        if files:
            body["files"] = files
        if diff:
            body["diff"] = diff
        if exclude_authors:
            body["exclude_authors"] = list(exclude_authors)
        if pr_number:
            body["pr_number"] = int(pr_number)
        if not include_lessons:
            body["include_lessons"] = False
        return await self._agent.transport.post("/v1/code.impact", body)

    async def test_gate(
        self,
        *,
        files: list[str] | None = None,
        diff: str = "",
        depth: int = 2,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Pre-PR obligations for a change: `tests` to run in evidence order
        (each with `why`, and `run` or `run_unknown`), `untested` touched
        symbols no test reaches, and `obligations` — true while either list
        is non-empty. Pass the unified diff for hunk-level touched symbols;
        files alone means every function in them."""
        body: dict[str, Any] = {"depth": depth, "limit": limit}
        if files:
            body["files"] = files
        if diff:
            body["diff"] = diff
        return await self._agent.transport.post("/v1/code.test_gate", body)

    async def trace(
        self,
        trace: str,
        *,
        max_frames: int = 15,
        token_budget: int = 3000,
    ) -> dict[str, Any]:
        """Stack trace → each frame resolved to its definition, order kept."""
        return await self._agent.transport.post(
            "/v1/code.trace",
            {
                "trace": trace,
                "max_frames": max_frames,
                "token_budget": token_budget,
            },
        )

    async def verify(
        self,
        kind: str,
        subject: str,
        *,
        obj: str = "",
        depth: int = 4,
    ) -> dict[str, Any]:
        """Three-valued check of one closed-form claim. `kind` is one of
        calls | unused | defines | contains | reaches; `verdict` is
        confirmed | refuted | not_established — never "refuted" from an
        absence alone (the graph is a floor)."""
        return await self._agent.transport.post(
            "/v1/code.verify",
            {"kind": kind, "subject": subject, "object": obj, "depth": depth},
        )

    async def verify_citations(
        self,
        text: str,
        *,
        at_commit: str = "",
        base_namespace: str = "",
        pr_number: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Every citation in a draft — `path:line`, `path:start-end`,
        blob links, bare paths, `Filename.ext:NNN`, `#N` / `PR N` /
        `issue N`, `Ns::Name`, a backticked function name — resolved
        against the index: per citation
        resolved | unresolved | stale | ambiguous with the target; top-level
        `verdict` all_resolved | has_unresolved | has_stale and `counts`.
        `at_commit` adds a bounded read-only mirror check (20 git calls /
        5 s). `pr_number` makes the PR's merge commit the default
        `at_commit` and adds `in_pr_diff` per citation plus a `pr_diff`
        summary. Run it before posting anywhere irreversible."""
        body: dict[str, Any] = {"text": text, "limit": limit}
        if at_commit:
            body["at_commit"] = at_commit
        if base_namespace:
            body["base_namespace"] = base_namespace
        if pr_number:
            body["pr_number"] = pr_number
        return await self._agent.transport.post("/v1/code.verify_citations", body)

    async def context_pack(
        self,
        *,
        pr_number: int = 0,
        token_budget: int = 2500,
        sections: list[str] | None = None,
    ) -> dict[str, Any]:
        """Precomputed briefing for a merged PR (or a repo orientation map
        when pr_number is 0). Call at the START of a PR-review task.
        `sections` keeps only the named blocks of a precomputed pack —
        symbols, co_change, tests, prs, files — e.g. ["tests", "prs"] for a
        hypothesis-forming agent that should not be handed a lead list."""
        return await self._agent.transport.post(
            "/v1/code.context_pack",
            {
                "pr_number": pr_number,
                "token_budget": token_budget,
                "sections": sections or [],
            },
        )

    async def review_brief(
        self,
        pr_number: int = 0,
        *,
        merge_sha: str = "",
        tier: str = "standard",
        sections: list[str] | None = None,
        token_budget: int = 0,
        store: bool = True,
        open: bool = False,
    ) -> dict[str, Any]:
        """A merged PR's review brief: the point read of the
        sectioned facts the ingest built at merge — paths, symbols, tests,
        settings, history, blame, qa, risk (`jit_risk` and
        `author_revert_prior`, priors for ranking, never a gate) — each with
        a verdict, plus `decisions` (test_only, symbol_with_regressions,
        uncovered_symbol, randomized_setting, deleted_lines_blamed).
        `tier` trivial | standard | full selects the sections and the token
        budget; a PR never briefed is built now — from the ledger's row, or
        from the merge commit the mirror names when the ledger predates the PR
        (`source: built_from_mirror`, `ledger: absent`). `store=False`
        serves such a build without caching it (a measurement consumer never
        caches).
        `open=True` briefs an OPEN PR's head on the mirror
        against its merge-base, the functions it adds seeded through the
        post-image overlay; `overlay` / `overlay_head_sha` on the answer."""
        body: dict[str, Any] = {"pr_number": pr_number, "tier": tier, "sections": sections or []}
        for key, value in (
            ("merge_sha", merge_sha),
            ("token_budget", token_budget),
        ):
            if value:
                body[key] = value
        if not store:
            body["store"] = False
        if open:
            body["open"] = True
        return await self._agent.transport.post("/v1/code.review_brief", body)

    async def examples(
        self, name: str = "", *, symbol_id: str = "", top_k: int = 3
    ) -> dict[str, Any]:
        return await self._agent.transport.post(
            "/v1/code.examples",
            {"name": name, "symbol_id": symbol_id, "top_k": top_k},
        )

    async def history(
        self,
        name: str = "",
        *,
        symbol_id: str = "",
        path: str = "",
        limit: int = 5,
        reviews_per_pr: int | None = None,
    ) -> dict[str, Any]:
        """The merged PRs that last touched a symbol or path; each row carries
        up to `reviews_per_pr` (server default 3, max 10, 0 = none) human
        review comments on that file and `review_count`."""
        body: dict[str, Any] = {"name": name, "symbol_id": symbol_id, "path": path, "limit": limit}
        if reviews_per_pr is not None:
            body["reviews_per_pr"] = reviews_per_pr
        return await self._agent.transport.post("/v1/code.history", body)

    async def pr_check(
        self,
        title: str = "",
        *,
        body: str = "",
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
        issues: list[int] | None = None,
        pr_number: int = 0,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Before opening a pull request: does a PR like this draft already
        exist? `verdict` is `duplicate_of` (an open PR is
        doing this, or a merged one did — check main), `related` or `none`;
        items carry state, score and evidence lines. Heuristic, no GitHub
        call; a title alone never reads as a duplicate."""
        return await self._agent.transport.post(
            "/v1/code.pr_check",
            {
                "title": title,
                "body": body,
                "paths": list(paths or []),
                "symbols": list(symbols or []),
                "issues": list(issues or []),
                "pr_number": pr_number,
                "top_k": top_k,
            },
        )

    async def blame_range(
        self,
        path: str,
        start_line: int,
        end_line: int,
        *,
        from_version: str = "",
        to_version: str = "",
        from_sha: str = "",
        to_sha: str = "",
        limit: int = 10,
        stage: str = "",
        mentions: str = "",
        reviews_per_pr: int | None = None,
    ) -> dict[str, Any]:
        """Which merged PRs changed `path[start_line:end_line]` inside a
        release window. `stage` is `introduced` (the
        server's default: backports, reverts and whitespace / rename-only
        touches demoted, PRs that name the enclosing function or the
        behaviour in `mentions` promoted, a re-land of a reverted change
        `reland_of` with the reverted PR kept undemoted) or
        `last_touched` (the blame order);
        `last_touched` in the response always carries the blame order.
        `verdict` is candidates / introduced_unknown / no_candidates /
        unknown; `epistemic` is `exact` when git blame ranked the answer,
        `lower_bound` when only the file-level PR ledger did — read `note`.
        Each candidate carries `commits` / `lines_in_range` and up to
        `reviews_per_pr` (server default 3, 0 = none) human review comments
        on a line inside the range, plus `review_count`."""
        body: dict[str, Any] = {
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "limit": limit,
        }
        if reviews_per_pr is not None:
            body["reviews_per_pr"] = reviews_per_pr
        for key, value in (
            ("from_version", from_version),
            ("to_version", to_version),
            ("from_sha", from_sha),
            ("to_sha", to_sha),
            ("stage", stage),
            ("mentions", mentions),
        ):
            if value:
                body[key] = value
        return await self._agent.transport.post("/v1/code.blame_range", body)

    async def first_build(
        self,
        *,
        sha: str = "",
        pr_number: int = 0,
    ) -> dict[str, Any]:
        """Which CI build first carried `sha` or the merge of `pr_number`.
        `verdict` is built / not_yet_built / unknown (read
        `causes`); `epistemic` is `exact` when a run names the sha or the
        PR's own head builds answer, `lower_bound` when the earliest checked
        base descends from it; `first_build` carries the run's version,
        time, PR and base; `note` says what the QA view does not show."""
        body: dict[str, Any] = {}
        if sha:
            body["sha"] = sha
        if pr_number:
            body["pr_number"] = pr_number
        return await self._agent.transport.post("/v1/code.first_build", body)

    async def backport_gap(
        self,
        pr_number: int = 0,
        *,
        issue_number: int = 0,
        branches: list[str] | None = None,
        max_symbols: int = 8,
        exact: bool = False,
        history: bool = False,
    ) -> dict[str, Any]:
        """Which release branches still carry the bug `pr_number` fixed (or
        the fix of `issue_number`), and the PR to cherry-pick there.
        `branches` names release-branch namespaces or versions
        (`"26.4"`); unset means every configured release branch, newest
        first. `verdict` is gap / pending / covered / not_affected / unknown;
        `branches` in the response carries one row each, gaps first, with
        `cherry_pick: {pr_number, merge_sha}` on a gap and `evidence`.
        `exact=True` tests the fix's own lines on every
        branch: the removed lines still there with the added lines absent
        make `gap` exact (basis `hunk`); the added lines present with the
        removed lines gone and no backport recorded make `not_affected`
        exact; each row's `presence.patch` carries the mode (exact |
        symbol_only), the line counts and the token-bag overlaps.
        `history=True` (implies exact) reads the branch's own
        tree and history in the mirror for the rows the lines leave
        undecided — every line at the tip under the same rules, then `git
        log -S` for what the tip lacks or brought; `presence.history`
        discloses the counts and probed lines."""
        body: dict[str, Any] = {"max_symbols": max_symbols}
        if pr_number:
            body["pr_number"] = pr_number
        if issue_number:
            body["issue_number"] = issue_number
        if branches:
            body["branches"] = list(branches)
        if exact:
            body["exact"] = True
        if history:
            body["history"] = True
        return await self._agent.transport.post("/v1/code.backport_gap", body)

    async def backport_gaps(
        self,
        branch: str = "",
        *,
        wanted_only: bool = True,
        days: int = 90,
        limit: int = 500,
    ) -> dict[str, Any]:
        """The set-wide backport report: which merged fixes
        labelled for backport does release branch `branch` (a namespace or a
        version such as `"26.6"`; unset = every configured release branch)
        still owe? Rows from loom.code_backport_gaps, written nightly by the
        `code_backport_gaps` worker, gaps first with `cherry_pick`; `counts`
        per verdict, `counts_by_branch`, `computed` (the table's stamps) and
        `stale`. `wanted_only=False` includes unlabelled fixes the pass
        computed."""
        body: dict[str, Any] = {"wanted_only": wanted_only, "days": days, "limit": limit}
        if branch:
            body["branch"] = branch
        return await self._agent.transport.post("/v1/code.backport_gaps", body)

    async def conventions(
        self,
        pr_number: int = 0,
        *,
        head_sha: str = "",
        files: list[str] | None = None,
        diff: str = "",
        rules: list[str] | None = None,
    ) -> dict[str, Any]:
        """The repository's convention checks as facts over one PR's diff:
        each rule with its verdict (violated | satisfied |
        not_applicable | unknown | unusual), evidence rows (path, line,
        text) and epistemic label. Give `pr_number` (merged), a `head_sha`
        the mirror knows, or `files` + `diff` for an open PR."""
        body: dict[str, Any] = {}
        if pr_number:
            body["pr_number"] = pr_number
        for key, value in (
            ("head_sha", head_sha),
            ("files", files or []),
            ("diff", diff),
            ("rules", rules or []),
        ):
            if value:
                body[key] = value
        return await self._agent.transport.post("/v1/code.conventions", body)

    async def map(self, *, token_budget: int = 1000, module_prefix: str = "") -> dict[str, Any]:
        return await self._agent.transport.post(
            "/v1/code.map",
            {"token_budget": token_budget, "module_prefix": module_prefix},
        )

    async def outcome(
        self,
        trace_run_id: str,
        action: str,
        *,
        targets: list[str] | None = None,
        source: str = "",
        tokens_saved: int = 0,
        reason: str = "",
    ) -> dict[str, Any]:
        """Report what happened with a retrieval: opened_file |
        used_in_answer | fell_back_grep | ignored. Free per-query
        relevance labels (loom.code_search_outcome). `shunt_hit` /
        `shunt_pass` (trace_run_id may be "") record a bulk-read shunt
        decision instead; `tokens_saved` is the bytes/4 estimate for hits.
        `reason` says WHY a fell_back_grep / ignored happened, one of
        abstained | empty | too_slow | wrong_shape | not_indexed | stale |
        preference (anything else records as unattributed)."""
        body: dict[str, Any] = {
            "trace_run_id": trace_run_id,
            "action": action,
            "targets": targets or [],
            "source": source,
        }
        if tokens_saved:
            body["tokens_saved"] = int(tokens_saved)
        if reason:
            body["reason"] = reason
        return await self._agent.transport.post("/v1/code.outcome", body)

    async def index_status(
        self,
        *,
        org: str = "",
        namespace: str = "",
        commit: str = "",
        paths: list[str] | None = None,
        brief: bool | None = None,
    ) -> dict[str, Any]:
        """Watermarks, lag vs the fetched branch tip, counts, workers, the
        last CI code.refresh breadcrumb, and the weekly tables' state — the
        symbol fan-in table (`symbol_stats_rows`, `symbol_stats_at`) and the
        precomputed risk table (`unit_risk_rows`, `unit_risk_at`); 0 / "" =
        never written — for one repo entry. Empty org/namespace = the
        deployment's default code plane. `commit` (a merge SHA) adds
        `commit_coverage`: `ready` once the fast tier and every compiler tier
        have reached that commit; `paths` (the PR's files)
        adds `paths_changed_overlap`, those a later merge changed."""
        body: dict[str, Any] = {"org": org, "namespace": namespace}
        if commit:
            body["commit"] = commit
        if paths:
            body["paths"] = list(paths)
        if brief is not None:
            body["brief"] = bool(brief)  # default: compact when `commit` is set
        return await self._agent.transport.post("/v1/code.index_status", body)

    async def coverage_watch(
        self, commit: str, inbox: str, *, org: str = "", namespace: str = ""
    ) -> dict[str, Any]:
        """Ask for one `code.coverage.ready` event on task inbox `inbox` once
        every index tier covers `commit` (full SHA, or an abbreviation the
        mirror resolves) — instead of polling index_status(commit=). Needs
        writer on the inbox's owner namespace. An already-covered commit
        answers `ready=true` and the event is posted at once."""
        return await self._agent.transport.post(
            "/v1/code.coverage.watch",
            {"org": org, "namespace": namespace, "commit": commit, "inbox": inbox},
        )

    async def coverage_unwatch(
        self, commit: str, inbox: str, *, org: str = "", namespace: str = ""
    ) -> dict[str, Any]:
        """Drop a coverage watch; `removed=false` when none was live."""
        return await self._agent.transport.post(
            "/v1/code.coverage.unwatch",
            {"org": org, "namespace": namespace, "commit": commit, "inbox": inbox},
        )

    async def coverage(self, *, org: str = "", namespace: str = "") -> dict[str, Any]:
        """What is indexed per language/tier and WHEN each part was last
        written, plus PR-history range. Check the scip column before
        trusting callers/impact for a language: compiler symbols mean an
        exact call graph, tree-sitter-only means heuristic."""
        return await self._agent.transport.post(
            "/v1/code.coverage", {"org": org, "namespace": namespace}
        )

    async def repos(self, *, org: str = "") -> list[dict[str, Any]]:
        """Every configured repo@branch entry the token can read (one code
        namespace each). Unreadable entries are omitted, not 403'd."""
        r = await self._agent.transport.post("/v1/code.repos", {"org": org})
        return list(r.get("repos") or [])

    async def context_packs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        pr_number: int = 0,
        org: str = "",
        namespace: str = "",
    ) -> dict[str, Any]:
        """Newest-first catalogue of precomputed per-PR packs ({rows, total}).
        For one PR's pack text use context_pack()."""
        return await self._agent.transport.post(
            "/v1/code.context_packs",
            {
                "org": org,
                "namespace": namespace,
                "limit": limit,
                "offset": offset,
                "pr_number": pr_number,
            },
        )
