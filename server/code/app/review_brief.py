"""code.review_brief handler: a point read of the brief the ingest step built for a merged
PR; on a miss, build it from the ledger and the mirror, store it, serve it. A PR the ledger
never saw is built from the merge commit the mirror's first-parent chain names for it
(`source: built_from_mirror`, `ledger: absent`), and the on-demand store is the caller's
`store` flag — never for a measurement consumer."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal

from server.code.api.models_conventions import CodeSettingFactsInput
from server.code.api.models_history import CodeBlameRangeInput, CodeSymbolHistoryInput
from server.code.api.models_review import CodeReviewBriefOutput
from server.code.api.models_symbol import CodeImpactInput
from server.code.api.models_testing import CodeCoverageClaimInput, CodeTestHomeInput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index import pr_overlay as po
from server.code.app.index import review_brief as rb
from server.code.app.index import review_brief_build, review_brief_sources
from server.code.app.index.mirrors import history_dir
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.domain.index.blame_range import sha_text
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.memory.app.recall_credit import is_measurement_label
from server.settings import settings

logger = logging.getLogger(__name__)

Source = Literal["precomputed", "built", "built_from_mirror", "missing"]
Ledger = Literal["", "present", "absent"]


async def call_code_op(op: str, body: dict[str, Any]) -> dict[str, Any]:
    """The brief builder's op seam: a code.* op handler invoked in-process as the consumer
    `review_brief` (its trace rows are attributable and filterable)."""
    from server.code.app.blame import handle_code_blame_range
    from server.code.app.coverage import handle_code_coverage_claim, handle_code_test_home
    from server.code.app.settings import handle_code_setting_facts
    from server.code.app.symbol import handle_code_impact
    from server.code.app.symbol_history import handle_code_symbol_history

    table: dict[str, tuple[Any, Any]] = {
        "code.impact": (CodeImpactInput, handle_code_impact),
        "code.coverage_claim": (CodeCoverageClaimInput, handle_code_coverage_claim),
        "code.test_home": (CodeTestHomeInput, handle_code_test_home),
        "code.setting_facts": (CodeSettingFactsInput, handle_code_setting_facts),
        "code.symbol_history": (CodeSymbolHistoryInput, handle_code_symbol_history),
        "code.blame_range": (CodeBlameRangeInput, handle_code_blame_range),
    }
    model, handler = table[op]
    out = await handler(model(**{**body, "consumer": "review_brief"}))
    return out.model_dump() if hasattr(out, "model_dump") else dict(out)


@po.reads_post_image
async def handle_code_review_brief(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    pr_number = int(input.pr_number or 0)
    merge_sha = str(input.merge_sha or "").strip().lower()
    is_open = bool(getattr(input, "open", False))
    if is_open and not pr_number:
        raise ValidationError("pr_number is required with open: true")
    if not pr_number and not merge_sha:
        raise ValidationError("pr_number or merge_sha is required")
    # The store is the caller's, and a measurement label (`eval-*` and the bench sentinels)
    # never fills the cache it measures.
    store = bool(input.store) and not is_measurement_label(input.consumer)

    # An open PR is keyed by its head — the point read asks for (pr_number, head); a head
    # the mirror lacks is `missing` with a note.
    overlay_note = ""
    head_sha = ""
    if is_open:
        merge_sha = ""
        with code_span("review_brief.open_head") as sp:
            img = po.posted(pr_number)
            if img is not None:
                head_sha = img.head_sha or f"{po.POSTED_PREFIX}{img.digest()[:12]}"
                # What the caller sent is its own: the brief is served, never stored
                # for other readers.
                store = False
            else:
                head_sha, overlay_note = await _open_head(org, ns, pr_number)
            span_attrs(sp, results=int(bool(head_sha)))

    payload, source = await _payload_for(
        repo,
        org,
        ns,
        pr_number=pr_number,
        merge_sha=merge_sha,
        is_open=is_open,
        head_sha=head_sha,
        store=store,
    )
    provenance = dict((payload or {}).get("provenance") or {})

    tier = str(input.tier or rb.DEFAULT_TIER)
    sections: dict[str, dict[str, Any]] = {}
    tokens_used, budget = 0, rb.TIER_BUDGET_TOKENS.get(tier, rb.TIER_BUDGET_TOKENS[rb.DEFAULT_TIER])
    decisions: dict[str, bool] = {}
    if payload is not None:
        served = payload
        if rb.RATIONALE in rb.tier_sections(tier, list(input.sections)):
            # Read now and served beside the stored sections, never written into them.
            with code_span("review_brief.rationale") as sp:
                rationale = await _rationale_section(
                    org, ns, payload, pr_number=pr_number, is_open=is_open
                )
                span_attrs(sp, results=int(rationale["rows_total"]))
            stored = dict(payload.get("sections") or {})
            served = {**payload, "sections": {**stored, rb.RATIONALE: rationale}}
        sections, tokens_used, budget = rb.serve(
            served,
            tier=tier,
            requested=list(input.sections),
            budget=int(input.token_budget or 0),
        )
        decisions = dict(payload.get("decisions") or rb.decisions_of(payload.get("sections") or {}))
        pr_number = int(payload.get("pr_number") or pr_number)
        merge_sha = "" if is_open else str(payload.get("merge_sha") or merge_sha)
    overlay_fields: dict[str, Any] = {}
    if is_open:
        overlay_fields = {
            "overlay": bool(payload and payload.get("overlay")),
            "overlay_head_sha": str((payload or {}).get("head_sha") or head_sha or ""),
            "overlay_added": _overlay_added(payload),
            "overlay_note": overlay_note if payload is None else "",
        }
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.review_brief",
        query=f"pr:{pr_number}" if pr_number else f"sha:{merge_sha[:12]}",
        route=f"brief_{source}",
        returned_ids=[f"pr:{pr_number}"] if payload is not None else [],
        returned_paths=[],
        returned_scores=[],
        top_k=1,
        tokens_returned=tokens_used,
        abstain=payload is None,
        retrieval_risk="low" if payload is not None else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict="" if payload is not None else "no_brief",
    )
    set_code_span_attributes(
        route=f"brief_{source}",
        tier=tier,
        results=len(sections),
        tokens_returned=tokens_used,
        abstain=payload is None,
        consumer=input.consumer,
    )
    return CodeReviewBriefOutput(
        pr_number=pr_number,
        merge_sha=merge_sha,
        source=source,
        tier=tier,
        built_at=str((payload or {}).get("built_at") or ""),
        indexed_commit=str((payload or {}).get("indexed_commit") or ""),
        build_ms=int((payload or {}).get("build_ms") or 0),
        tokens_used=tokens_used,
        token_budget=budget,
        decisions=decisions,
        sections=sections,
        ledger=_ledger_of(source, provenance),
        provenance=provenance,
        stored=bool(
            payload is not None
            and source != "precomputed"
            and store
            and payload.get("stored", True)
        ),
        trace_run_id=trace_run_id,
        **overlay_fields,
    )


def _ledger_of(source: Source, provenance: dict[str, Any]) -> Ledger:
    """What the ledger said about this PR: `present` when this call built
    from its row, `absent` when the brief (this build or a stored one) came
    from the mirror because the ledger had none, "" when it was not consulted
    (the ingest built the row at merge)."""
    if source == "built":
        return "present"
    if (
        source == "built_from_mirror"
        or provenance.get("ledger") == review_brief_sources.LEDGER_ABSENT
    ):
        return "absent"
    return ""


async def _payload_for(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    pr_number: int,
    merge_sha: str,
    is_open: bool,
    head_sha: str,
    store: bool,
) -> tuple[dict[str, Any] | None, Source]:
    """(payload, source): the point read — keyed (pr_number, merge_sha), or
    (pr_number, head) for an open PR — then the build on a miss."""
    with code_span("review_brief.point_read") as sp:
        try:
            if is_open and not head_sha:
                row = None
            else:
                row = await repo.review_brief(
                    org, ns, pr_number=pr_number, merge_sha=head_sha if is_open else merge_sha
                )
        except Exception as e:  # noqa: BLE001 — a failed read is a miss; the build answers
            logger.warning("code.review_brief: point read failed (treated as a miss): %s", e)
            row = None
        span_attrs(sp, results=int(row is not None))
    payload: dict[str, Any] | None = None
    if row is not None:
        try:
            payload = json.loads(str(row.get("payload") or "{}"))
        except ValueError:
            payload = None
        if payload is not None and is_open and not payload.get("overlay"):
            payload = None  # a merged brief under this number is not the open one
    if payload is not None:
        return payload, "precomputed"
    with code_span("review_brief.build") as sp:
        if is_open:
            payload = (
                await _build_open(
                    repo,
                    org,
                    ns,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    store=store,
                )
                if head_sha
                else None
            )
            source: Source = "built" if payload is not None else "missing"
        else:
            payload, source = await _build_on_miss(
                repo,
                org,
                ns,
                pr_number=pr_number,
                merge_sha=merge_sha,
                store=store,
            )
        span_attrs(sp, results=int(payload is not None))
    return payload, source


async def _rationale_section(
    org: str, ns: str, payload: dict[str, Any], *, pr_number: int, is_open: bool
) -> dict[str, Any]:
    """The decisions and constraints the PR's files touch, from the repository's trunk
    code namespace as they are now, with the token check against the PR's diff (read only
    when a row has a name to check). Source paths are read first, at most
    `code_review_brief_rationale_paths` of them; rows with something to check are kept
    ahead of the `code_review_brief_rationale_rows` cut."""
    from server.code.app.memory_links import trunk_namespace
    from server.memory.app.code_plane_reads import decisions_for_paths

    most = settings.code_review_brief_rationale_paths
    rows = ((payload.get("sections") or {}).get("paths") or {}).get("rows") or []
    paths = [str(r["path"]) for r in sorted(rows, key=lambda r: r.get("class") != "src")]
    try:
        read = await decisions_for_paths(
            org,
            trunk_namespace(org, ns) or ns,
            paths[:most],
            limit=settings.code_review_brief_rationale_rows,
            first=rb.has_check,
        )
        diff: str | None = ""
        cut = False
        found = read.served
        if any(rb.checked_names(d["text"], constraint="constraint" in d["tags"]) for d in found):
            diff, cut = await _pr_diff(org, ns, payload, pr_number=pr_number, is_open=is_open)
    except Exception as e:  # noqa: BLE001 — the stored sections serve without this one
        logger.warning("code.review_brief: rationale read failed: %s", e)
        return rb.failed_section("decisions_for_paths", e)
    return rb.rationale_section(
        found,
        diff,
        diff_cut=cut,
        paths_left=max(len(paths) - most, 0),
        left_out=read.left_out,
        paths_full=read.paths_full,
        links_skipped=read.links_skipped,
        refused=read.refused,
        # the stored list may hold fewer paths than the PR changed once it is this long
        paths_maybe_cut=len(rows) >= rb.IMPACT_SEED_FILES,
        budget_tokens=settings.code_review_brief_rationale_tokens,
    )


async def _pr_diff(
    org: str, ns: str, payload: dict[str, Any], *, pr_number: int, is_open: bool
) -> tuple[str | None, bool]:
    """(diff, cut) of the briefed PR, cut at MAX_DIFF_CHARS: the diff the caller posted,
    else the diff of the open head against its merge-base or of the merge commit against
    its first parent, from a mirror already on this host (a read here never starts a
    clone); (None, False) when it cannot be read here."""
    from server.code.app.index import gitrepo
    from server.code.app.index.mirrors import present_dir
    from server.code.domain.index.repos import repo_for_namespace

    img = po.posted(pr_number) if is_open else None
    if img is not None:
        if not img.diff:
            return None, False
        return img.diff[: rb.MAX_DIFF_CHARS], len(img.diff) > rb.MAX_DIFF_CHARS
    if payload.get("overlay"):
        base, head = str(payload.get("base_sha") or ""), str(payload.get("head_sha") or "")
    else:
        head = str(payload.get("merge_sha") or "")
        base = f"{head}^1" if head else ""
    try:
        git_dir = await present_dir(repo_for_namespace(org, ns))
        if not (git_dir and base and head):
            return None, False
        return await gitrepo.run_bounded(
            git_dir, "diff", "--no-renames", base, head, max_bytes=rb.MAX_DIFF_CHARS
        )
    except Exception as e:  # noqa: BLE001 — a diff that cannot be read leaves the check unknown
        logger.info("code.review_brief: diff for the rationale check of PR %s: %s", pr_number, e)
        return None, False


def _overlay_added(payload: dict[str, Any] | None) -> int:
    """How many functions the PR adds: the `via: added` rows of the symbols section."""
    if payload and "overlay_added" in payload:
        return int(payload["overlay_added"] or 0)
    rows = ((payload or {}).get("sections") or {}).get("symbols", {}).get("rows") or []
    return sum(1 for r in rows if str(r.get("via") or "") == "added")


async def _open_head(org: str, ns: str, pr_number: int) -> tuple[str, str]:
    """(head sha, reason) for an open PR: the mirror's `refs/pull/N/head` tip, or '' with why."""
    from server.code.app.index import gitrepo
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        return "", po.NO_REPO
    git_dir, gap = await history_dir(cfg)
    if gap:
        return "", gap
    try:
        head = await gitrepo.pull_head_sha(git_dir, pr_number)
    except Exception as e:  # noqa: BLE001 — a mirror that cannot answer is a missing head
        logger.info("code.review_brief: head of open PR %s unknown: %s", pr_number, e)
        head = ""
    return head, ("" if head else po.HEAD_NOT_IN_MIRROR)


async def _build_open(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    pr_number: int,
    head_sha: str,
    store: bool,
) -> dict[str, Any] | None:
    """The brief of an open PR's head against its merge-base, title from the items ledger
    when it has the number."""
    from server.code.app.index import gitrepo
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        return None
    img = po.posted(pr_number)
    if img is not None:
        base, files, diff = img.base_sha, po.diff_paths(img.diff) or list(img.files), img.diff
    else:
        git_dir, _gap = await history_dir(cfg)
        branch = f"refs/heads/{cfg.git_branch}"
        base = await gitrepo.merge_base(git_dir, head_sha, branch) if git_dir else ""
        if not base:
            return None
        files, diff = [], None
    title = author = ""
    try:
        item = await repo.gh_pr_head(org, ns, pr_number)
        title = str((item or {}).get("title") or "")
        author = str((item or {}).get("author") or "")  # the author feeds the revert prior
    except Exception as e:  # noqa: BLE001 — the title and author are decoration
        logger.debug("code.review_brief: items row for PR %s skipped: %s", pr_number, e)
    try:
        if img is None:
            files = await gitrepo.diff_name_only(cfg.git_dir, base, head_sha)
        return await review_brief_build.build_for_open(
            repo,
            org,
            ns,
            git_dir=cfg.git_dir,
            pr_number=pr_number,
            title=title,
            head_sha=head_sha,
            base_sha=base,
            changed_paths=files,
            path_prefixes=tuple(cfg.path_prefixes),
            call=call_code_op,
            store=store,
            author=author,
            diff=diff,
        )
    except Exception as e:  # noqa: BLE001 — a miss that cannot build is a miss
        logger.warning("code.review_brief: open build failed for PR %s: %s", pr_number, e)
        return None


async def _build_on_miss(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    pr_number: int,
    merge_sha: str,
    store: bool,
) -> tuple[dict[str, Any] | None, Source]:
    """The brief for a PR the ingest never briefed (older than the feature,
    or merged while it was off): the ledger's title / file list / merge
    commit and the mirror's diff, through the same builder. Without a
    ledger row (the PR predates the backfill horizon) the merge commit, title and files come
    from the mirror's first-parent chain and the reviewers from the tracker record.
    (None, "missing") when neither names the PR or the namespace has no mirror."""
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        logger.info("code.review_brief: no repo entry for %s/%s", org, ns)
        return None, "missing"
    facts = await repo.pr_merge_facts(org, ns, pr_number=pr_number, merge_sha=merge_sha)
    source: Source = "built"
    provenance: dict[str, Any] | None = None
    if not facts or not sha_text(facts.get("merge_sha")):
        facts = await _mirror_facts(cfg, pr_number=pr_number, merge_sha=merge_sha)
        if not facts:
            return None, "missing"
        source = "built_from_mirror"
        provenance = await review_brief_sources.mirror_provenance(org, ns, facts)
    try:
        payload = await review_brief_build.build_for_merge(
            repo,
            org,
            ns,
            git_dir=cfg.git_dir,
            pr_number=int(facts["pr_number"]),
            title=str(facts.get("title") or ""),
            merge_sha=sha_text(facts.get("merge_sha")),
            changed_paths=[str(f) for f in (facts.get("files") or [])],
            path_prefixes=tuple(cfg.path_prefixes),
            call=call_code_op,
            store=store,
            provenance=provenance,
            author=str(facts.get("author") or ""),
        )
    except Exception as e:  # noqa: BLE001 — a miss that cannot build is a miss
        logger.warning("code.review_brief: on-demand build failed for PR %s: %s", pr_number, e)
        return None, "missing"
    return payload, source


async def _mirror_facts(cfg: Any, *, pr_number: int, merge_sha: str) -> dict[str, Any] | None:
    """The merge commit the mirror names for the PR (or the PR the merge
    commit names); None on a miss, a git failure (a miss, logged) or no mirror here."""
    git_dir, gap = await history_dir(cfg)
    if gap:
        logger.info("code.review_brief: mirror lookup for PR %s skipped: %s", pr_number, gap)
        return None
    try:
        if pr_number:
            return await review_brief_sources.mirror_merge_facts(git_dir, cfg.git_branch, pr_number)
        return await review_brief_sources.mirror_commit_facts(git_dir, merge_sha)
    except Exception as e:  # noqa: BLE001 — the mirror not answering is a miss
        logger.info(
            "code.review_brief: mirror lookup for PR %s / %s failed: %s",
            pr_number,
            merge_sha[:12],
            e,
        )
        return None
