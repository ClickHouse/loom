"""namespace.tune — drafts of the changes to the settings that create a namespace's
memories and cut and extract its documents, from what its rows, its documents and its
audit show.

Three settings shape the memories a namespace gets: the extraction rules (an org
admin's namespace policy, or the instructions a request sends), the registered
categories, and the guide its writers read. Each proposal names one of them, its
current value, the value it would set, the rows that support it and the rows it must
keep working for, the agents whose future memories it shapes, and the ordinary write
that would apply it with the guard that write has. Nothing is applied, and every draft
is built here from fixed templates: a tag or key prefix from the rows is used only when
it is a plain name (`_NAME`), so stored text can never choose a target, an op or an
argument other than the name it is. No model is called.

The document drafts (the chunk size and stride, an extractor route per format) come from
`server.knowledge.app.tune`, which cuts a sample of the documents again in process.

Each section is guarded: one that cannot be read lands in `skipped` and drafts nothing.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from functools import partial
from typing import Any

from server.auth.sharing import is_personal
from server.core.datetimes import naive_utc_now
from server.identity.api.models_findings import FindingEvidence
from server.identity.api.models_namespace_catalog import (
    NamespaceAuditFinding,
    NamespaceAuditInput,
    NamespaceListCategoriesInput,
)
from server.identity.api.models_namespace_guide import (
    GUIDE_MAX_CHARS,
    NamespaceGuideGetInput,
    NamespaceGuideGetOutput,
)
from server.identity.api.models_namespace_tune import (
    NamespaceTuneInput,
    NamespaceTuneOutput,
    TuneApply,
    TuneEvidence,
    TuneProposal,
)
from server.identity.app.namespace_audit import handle_namespace_audit
from server.identity.app.namespace_catalog import handle_namespace_list_categories
from server.identity.app.namespace_guide import handle_namespace_guide_get
from server.identity.app.request_scope import request_scope
from server.identity.store import namespace_tune as store
from server.knowledge.app.tune import DocumentDrafts, document_drafts
from server.memory.app import extraction_policy
from server.memory.app.finding_evidence import IDENTITY, revisions_for
from server.memory.app.pipeline import OFFERED_CATEGORIES
from server.memory.app.search import pg_validation

log = logging.getLogger(__name__)

# A tag or key prefix the drafts may name: a plain identifier, nothing a reader could
# take for an instruction. The store reads filter by this pattern in SQL; the drafts
# check it again with `fullmatch`, a second guard that does not rest on the SQL regex
# engine's reading of it. A `<prefix>:` tag names where a row came from (`source:`,
# `speaker:`, `agent:`), not what it is about, so it is never a topic.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Agents named as affected, most rows first; `affected_agents_total` counts them all.
MAX_AFFECTED_AGENTS = 20  # not a setting: bounds one response
# Key prefixes and tags a drafted guide lists, each.
MAX_GUIDE_ENTRIES = 12  # not a setting: the draft's length, which a person edits anyway
# Plain-named tags and key prefixes on at least `min_rows` rows, each, read to draft from.
MAX_CANDIDATES = 500  # not a setting: bounds one read; a namespace with more has no layout

# The extraction rule each writer-side audit finding asks for. Only these findings draft
# a rule: the deployment's own state (`system_state`) and read patterns are not the
# extractor's to fix.
_FINDING_RULES: dict[str, str] = {
    "double_encoded_values": (
        "Store each value as plain text or one JSON object, never a JSON string that holds JSON."
    ),
    "json_prefix_excerpts": "Open each value with the claim in plain words, not a JSON prefix.",
    "oversized_values": (
        "Keep a memory to one claim of a few sentences; a document belongs in knowledge.upsert."
    ),
    "episodic_blobs": (
        "Keep an episode to a short summary; a run's full output belongs in context.offload."
    ),
    "coordination_as_memory": (
        "Keep leases, locks and status flags in working memory, not in memory."
    ),
    "hand_rolled_index_rows": (
        "Do not write rows that list other keys; the key layout is the index."
    ),
}

_HISTORICAL_EXTRACTION = (
    "Rows already stored keep their shape. Reworking them is the loom-optimize "
    "procedure (memory.move per row, keeping a redirect), a separate action with its own "
    "approval."
)


def _proposal_id(target: str, proposed: str) -> str:
    return hashlib.sha256(f"{target}\n{proposed}".encode()).hexdigest()[:16]


class _Tune:
    def __init__(self, inp: NamespaceTuneInput) -> None:
        self.inp = inp
        # A narrowed token is refused before the handler, so this is the whole namespace;
        # every read of memory rows still takes it.
        self.scope = request_scope()
        self.skipped: list[str] = []

    async def guarded[T](self, section: str, read: Callable[[], Awaitable[T]], default: T) -> T:
        """One section's read; a failure lands in `skipped` and the default stands."""
        try:
            return await read()
        except Exception as exc:  # noqa: BLE001 — a read-only draft; one section must not sink the rest
            log.warning("namespace.tune %s skipped: %s", section, exc)
            self.skipped.append(f"{section}: {type(exc).__name__}")
            return default

    async def cite(self, section: str, rows: Iterable[dict[str, Any]]) -> list[FindingEvidence]:
        """The live revision of each row named by identity, as the reports cite rows."""
        samples = [{c: r[c] for c in IDENTITY} for r in rows]
        if not samples:
            return []
        i = self.inp
        revisions = await self.guarded(
            section,
            lambda: revisions_for(i.org, i.namespace, samples, scope=self.scope),
            dict[Any, dict[str, Any]](),
        )
        out = []
        for s in samples:
            rev = revisions.get(tuple(s[c] for c in IDENTITY))
            if rev is not None:
                out.append(FindingEvidence(**{k: rev[k] for k in FindingEvidence.model_fields}))
        return out


async def _caller_policy(run: _Tune, out: NamespaceTuneOutput) -> str | None:
    """The policy in force, shown to an org admin; None with the reason otherwise."""
    read = await extraction_policy.for_caller(run.inp.org, run.inp.namespace)
    out.extraction_policy, out.extraction_policy_unavailable = read.policy, read.unavailable
    if read.skipped:
        run.skipped.append(f"settings.extraction_policy: {read.skipped}")
    return read.policy


def _round_robin(lists: list[list[FindingEvidence]], limit: int) -> list[FindingEvidence]:
    """Up to `limit` rows taking one from each list in turn, so every finding is cited."""
    out: list[FindingEvidence] = []
    seen: set[tuple[str, str, str, str]] = set()
    for depth in range(max((len(x) for x in lists), default=0)):
        for x in lists:
            if depth < len(x):
                ref = _ref(x[depth])
                # A row two checks flag is cited once.
                if ref not in seen:
                    seen.add(ref)
                    out.append(x[depth])
    return out[:limit]


def _ref(e: FindingEvidence) -> tuple[str, str, str, str]:
    return (e.agent, e.user_id, e.session_id, e.memory_key)


async def _extraction_proposal(
    run: _Tune, policy: str | None, findings: list[NamespaceAuditFinding]
) -> TuneProposal | None:
    """One draft of extraction instructions for the writer-side findings the audit raised,
    when memory.set_from_messages' extraction writes into the namespace.

    The target is the per-request instructions, never the operator policy: a policy in
    force overrides every other instruction and turns off extraction's recovery passes,
    so a style rule there would cost facts the namespace keeps today. Instructions reach
    only the rows extraction writes (source trace `pipeline:set_from_messages`), so with
    none of those nothing is drafted; the audit's counts cover every writer."""
    i = run.inp
    fired = [
        f
        for f in findings
        if f.code in _FINDING_RULES and f.count > 0 and f.severity in ("warning", "critical")
    ]
    # A rule whose exact text the policy in force already contains is not drafted again.
    held = policy or ""
    drafted = [f for f in fired if _FINDING_RULES[f.code] not in held]
    if not drafted:
        return None
    writers = await run.guarded(
        "extraction.writers",
        partial(
            store.extraction_writers,
            i.org,
            i.namespace,
            scope=run.scope,
            limit=MAX_AFFECTED_AGENTS,
        ),
        None,
    )
    if writers is None:
        return None
    if not writers:
        # Extraction writes nothing here: the instructions would reach nothing.
        run.skipped.append(
            "extraction: memory.set_from_messages writes no row here, so no rule is drafted for "
            + ", ".join(f.code for f in drafted)
        )
        return None
    cited = {_ref(e) for f in drafted for e in f.evidence}
    from_extraction = await run.guarded(
        "extraction.cited",
        partial(store.written_by_extraction, i.org, i.namespace, sorted(cited), scope=run.scope),
        None,
    )
    if from_extraction is None:
        return None
    extracted = {
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))
        for r in from_extraction
    }
    # A rule rests on rows extraction wrote: a finding none of whose cited rows came from
    # it drafts nothing, said in `skipped` so the absence is not read as clean.
    by_finding = {f.code: [e for e in f.evidence if _ref(e) in extracted] for f in drafted}
    # A finding the audit cited no live row of (its evidence read failed, or the rows went
    # away before it) is judged on nothing: no draft, said apart from the unreached ones.
    uncited = [f for f in drafted if not f.evidence]
    if uncited:
        run.skipped.append(
            "extraction: the audit cited no live row of " + ", ".join(f.code for f in uncited)
        )
    unreached = [f for f in drafted if f.evidence and not by_finding[f.code]]
    if unreached:
        run.skipped.append(
            "extraction: no cited row of "
            + ", ".join(f.code for f in unreached)
            + " was written by extraction (judged on the audit's cited rows)"
        )
    drafted = [f for f in drafted if by_finding[f.code]]
    if not drafted:
        return None
    rule_text = "\n".join(f"- {_FINDING_RULES[f.code]}" for f in drafted)
    counts = "; ".join(
        f"{f.code}: {f.count} of {f.population} rows"
        if f.population is not None
        else f"{f.code}: {f.count} rows"
        for f in drafted
    )
    extraction_rows = int(writers[0]["total_rows"])
    # A population the audit could not read gives no bound; with none read, no count.
    populations = [f.population for f in drafted if f.population is not None]
    supporting = _round_robin([by_finding[f.code] for f in drafted], i.sample_limit)
    from_it = len({_ref(e) for f in drafted for e in by_finding[f.code]})
    return TuneProposal(
        id=_proposal_id("extraction_instructions.request", rule_text),
        target="extraction_instructions.request",
        scope=f"each memory.set_from_messages request into {i.org}/{i.namespace} that sends them",
        title="Send extraction instructions for the shapes the audit found",
        rationale=(
            f"The audit flagged {counts} (counts over every writer). Extraction wrote "
            f"{extraction_rows} of the live rows here; {from_it} of the audit's cited rows "
            "came from it. No user, agent or session setting stores instructions, so a "
            "writer sends them per request."
        ),
        current="",
        proposed=rule_text,
        evidence=TuneEvidence(
            finding_codes=[f.code for f in drafted],
            supporting_count=sum(f.count for f in drafted),
            supporting=supporting,
            # Of the smallest population any of these checks read, the rows none of them
            # flagged: at least that population less every flagged row.
            counter_count=(
                max(0, min(populations) - sum(f.count for f in drafted)) if populations else None
            ),
        ),
        # The writers the instructions reach: the agents whose rows extraction wrote.
        affected_agents=[str(r["agent"]) for r in writers],
        affected_agents_total=int(writers[0]["total"]),
        historical=_HISTORICAL_EXTRACTION,
        apply=TuneApply(
            op="memory.set_from_messages",
            args={"extraction_instructions": rule_text},
            args_complete=False,
            required_role="writer",
            guard="None needed: the instructions apply only to the request that carries them.",
        ),
        limits=(
            "Each writer has to send them; a request without them extracts as before. The "
            "flagged counts include rows written by hand (memory.set), which never pass "
            "through extraction; a row counts as extraction's by its source trace. They "
            "are not drafted as the operator extraction policy: a policy overrides every "
            "other instruction and turns off the recovery passes that keep preferences, "
            "costs, ages and corrections."
        ),
    )


async def _category_proposals(
    run: _Tune,
    names: list[str],
    tags: list[tuple[str, int]],
    agents: list[str],
    total: int | None,
) -> list[TuneProposal]:
    """A category for each much-used tag no category is named after."""
    i = run.inp
    out: list[TuneProposal] = []
    registered = {n.lower() for n in names}
    # One draft per name in any case: `Billing` and `billing` would be two categories.
    chosen: list[tuple[str, int]] = []
    for tag, rows in tags:
        if len(chosen) >= i.max_category_proposals:
            break
        if rows < i.min_rows or not _NAME.fullmatch(tag) or tag.lower() in registered:
            continue
        registered.add(tag.lower())
        chosen.append((tag, rows))
    if not chosen:
        return out
    # Every spelling of the chosen names, however few rows carry it.
    found = await run.guarded(
        "category.spellings",
        partial(
            store.tag_spellings,
            i.org,
            i.namespace,
            [t.lower() for t, _ in chosen],
            scope=run.scope,
        ),
        list[dict[str, Any]](),
    )
    spellings: dict[str, list[tuple[str, int]]] = {}
    for r in found:
        # The second guard: only a plain name reaches the draft's text.
        if _NAME.fullmatch(str(r["tag"])):
            spellings.setdefault(str(r["tag"]).lower(), []).append((str(r["tag"]), int(r["rows"])))
    for tag, rows in chosen:
        tagged = await run.guarded(
            f"category.{tag}",
            partial(
                store.tagged_rows, i.org, i.namespace, tag, scope=run.scope, limit=i.sample_limit
            ),
            list[dict[str, Any]](),
        )
        filed = await run.guarded(
            f"category.{tag}.filed",
            partial(store.tagged_in_other_categories, i.org, i.namespace, tag, scope=run.scope),
            None,
        )
        # A count that could not be read is left out, not stated as 0.
        filed_clause = (
            "" if filed is None else f"; {filed} of them are filed under another category"
        )
        description = f"Rows tagged `{tag}`."
        out.append(
            TuneProposal(
                id=_proposal_id("namespace.category", tag),
                target="namespace.category",
                scope=f"namespace {i.org}/{i.namespace}",
                title=f"Register a category for the tag `{tag}`",
                rationale=(
                    f"{rows} live rows carry the tag `{tag}` and no category is named after "
                    f"it{filed_clause}. A registered category is what "
                    "namespace.search_categories finds, and extraction offers the first "
                    f"{OFFERED_CATEGORIES} by name as tags for new rows."
                ),
                current="",
                proposed=tag,
                evidence=TuneEvidence(
                    supporting_count=rows,
                    supporting=await run.cite(f"category.{tag}.cited", tagged),
                    counter_count=filed,
                ),
                affected_agents=agents,
                affected_agents_total=total,
                historical=(
                    "Registering files no row: rows already tagged join the category only "
                    "through memory.assign_category, a separate action per row."
                ),
                apply=TuneApply(
                    op="namespace.register_category",
                    args={
                        "org": i.org,
                        "namespace": i.namespace,
                        "name": tag,
                        "description": description,
                    },
                    required_role="writer",
                    guard=(
                        "None: registering a name that exists updates that category, so read "
                        "namespace.list_categories again just before applying."
                    ),
                ),
                limits=_spelling_limits(tag, spellings.get(tag.lower(), []))
                + _offer_limits(tag, names),
            )
        )
    return out


def _offer_limits(tag: str, names: list[str]) -> str:
    """What registering `tag` does to the categories extraction offers: the first
    `OFFERED_CATEGORIES` names in byte order, as the pipeline reads them."""
    if len(names) < OFFERED_CATEGORIES:
        return ""
    if tag not in sorted([*names, tag])[:OFFERED_CATEGORIES]:
        return (
            f" Extraction offers the first {OFFERED_CATEGORIES} categories by name, and "
            f"{OFFERED_CATEGORIES} sort before this one, so it would not be offered."
        )
    pushed = sorted(names)[OFFERED_CATEGORIES - 1]
    # A category name is stored unchecked: only a plain one is quoted.
    named = (
        f"`{pushed}`"
        if _NAME.fullmatch(pushed)
        else f"the category at position {OFFERED_CATEGORIES} by name"
    )
    return (
        f" Extraction offers the first {OFFERED_CATEGORIES} categories by name: this one "
        f"would be offered, and {named} no longer would."
    )


def _spelling_limits(tag: str, spellings: list[tuple[str, int]]) -> str:
    """The draft's limits, naming any other spelling of its tag, which new rows filed
    under the category's exact name would stay apart from."""
    text = "A tag used for two meanings would need two categories, not one."
    others = [f"`{t}` ({n} rows)" for t, n in spellings if t != tag]
    if others:
        text += (
            f" Also tagged {', '.join(others)}: extraction tags new rows with the "
            "category's exact name, so rows under another spelling stay apart from it; "
            "pick one spelling and retag the rest."
        )
    return text


def _names_prefix(content: str, prefix: str) -> bool:
    """Whether the guide names `prefix/` as a path of its own, not inside a longer name or
    below another segment (`team/billing/` does not name `billing/`)."""
    return re.search(rf"(?<![A-Za-z0-9_.:/-]){re.escape(prefix)}/", content) is not None


def _guide_lines(prefixes: list[tuple[str, int]], tags: list[tuple[str, int]]) -> list[str]:
    lines = ["## Key layout", ""]
    lines += [f"- `{p}/…`: {n} rows" for p, n in prefixes]
    if tags:
        lines += ["", "## Tags", ""]
        lines += [f"- `{t}`: {n} rows" for t, n in tags]
    return lines


def _guide_proposal(
    run: _Tune,
    guide: NamespaceGuideGetOutput,
    prefixes: list[tuple[str, int]],
    tags: list[tuple[str, int]],
    agents: list[str],
    total: int | None,
) -> TuneProposal | None:
    """A guide naming the key layout and tags, or the prefixes the guide leaves out."""
    i = run.inp
    named = [(p, n) for p, n in prefixes if _NAME.fullmatch(p) and n >= i.min_rows]
    revision = guide.revision
    content = guide.content if guide.found else ""
    if content:
        # The prefixes the guide leaves out, capped after they are found: a guide that
        # names the most-used ones may still leave out the next.
        missing = [(p, n) for p, n in named if not _names_prefix(content, p)]
        listed = missing[:MAX_GUIDE_ENTRIES]
        if not listed:
            return None
        addition = "\n".join(_guide_lines(listed, []))
        proposed = f"{content.rstrip()}\n\n{addition}\n"
        if len(proposed) > GUIDE_MAX_CHARS:
            # namespace.guide_set would refuse it; a person shortens the guide first.
            run.skipped.append(
                "guide: the guide with the missing prefixes is over the guide's limit"
            )
            return None
        title = "Name the key prefixes the guide leaves out"
        why = f"{len(missing)} key prefixes holding rows are not named in the guide"
        if len(missing) > len(listed):
            why += f" (the {len(listed)} most used are drafted)"
    else:
        listed = named[:MAX_GUIDE_ENTRIES]
        if not listed:
            return None
        shown_tags = [(t, n) for t, n in tags if _NAME.fullmatch(t) and n >= i.min_rows]
        body = "\n".join(_guide_lines(listed, shown_tags[:MAX_GUIDE_ENTRIES]))
        proposed = f"# {i.namespace}\n\n{body}\n"
        title = "Write a guide from the key layout and tags"
        why = "the namespace has no guide"
    return TuneProposal(
        id=_proposal_id("namespace.guide", proposed),
        target="namespace.guide",
        scope=f"namespace {i.org}/{i.namespace}",
        title=title,
        rationale=(
            f"{why}. A writer reads the guide before its first write, so it names the "
            "layout new rows should follow; the counts are today's live rows."
        ),
        current=content,
        current_revision=revision,
        proposed=proposed,
        # A guide has no rows to keep working for: it changes no stored row.
        evidence=TuneEvidence(supporting_count=sum(n for _, n in listed), counter_count=None),
        affected_agents=agents,
        affected_agents_total=total,
        historical="A guide changes no stored row; it shapes the writes that read it.",
        apply=TuneApply(
            op="namespace.guide_set",
            args={
                "org": i.org,
                "namespace": i.namespace,
                "content": proposed,
                "expected_revision": revision,
                "note": "Drafted by namespace.tune from the key layout and tags.",
            },
            required_role="writer",
            guard=(
                "expected_revision: a guide written since the draft refuses the write and "
                "answers with its current text to merge into."
            ),
        ),
        limits=(
            "The draft lists what the rows show, not what they mean; a person writes what "
            "each prefix and tag is for."
        ),
    )


async def handle_namespace_tune(input: NamespaceTuneInput) -> NamespaceTuneOutput:
    run = _Tune(input)
    published = pg_validation.published_reads(
        input.org, [input.namespace], lambda reason: run.skipped.append(f"projection: {reason}")
    )
    async with published:
        return await _tune(input, run)


async def _tune(input: NamespaceTuneInput, run: _Tune) -> NamespaceTuneOutput:
    out = NamespaceTuneOutput(org=input.org, namespace=input.namespace, computed_at=naive_utc_now())
    org, ns = input.org, input.namespace

    agent_rows = await run.guarded(
        "agents",
        lambda: store.writing_agents(org, ns, scope=run.scope, limit=MAX_AFFECTED_AGENTS),
        None,
    )
    agents = [str(r["agent"]) for r in agent_rows or []]
    # An unread writer list is served as None, not as no writer.
    agents_total: int | None = None
    if agent_rows is not None:
        agents_total = int(agent_rows[0]["total"]) if agent_rows else 0
    audit = await run.guarded(
        "audit",
        lambda: handle_namespace_audit(
            NamespaceAuditInput(
                org=org,
                namespace=ns,
                window_seconds=input.window_seconds,
                sample_limit=input.sample_limit,
            )
        ),
        None,
    )
    tag_rows = await run.guarded(
        "tags",
        lambda: store.plain_tags(
            org,
            ns,
            scope=run.scope,
            min_rows=input.min_rows,
            name_pattern=_NAME.pattern,
            limit=MAX_CANDIDATES,
        ),
        list[dict[str, Any]](),
    )
    tags = [(str(r["tag"]), int(r["rows"])) for r in tag_rows]
    cats = await run.guarded(
        "categories",
        lambda: handle_namespace_list_categories(
            NamespaceListCategoriesInput(org=org, namespace=ns)
        ),
        None,
    )
    guide = await run.guarded(
        "guide",
        lambda: handle_namespace_guide_get(NamespaceGuideGetInput(org=org, namespace=ns)),
        None,
    )
    prefix_rows = await run.guarded(
        "key_prefixes",
        lambda: store.key_prefixes(
            org,
            ns,
            scope=run.scope,
            min_rows=input.min_rows,
            name_pattern=_NAME.pattern,
            limit=MAX_CANDIDATES,
        ),
        list[dict[str, Any]](),
    )
    prefixes = [(str(r["prefix"]), int(r["rows"])) for r in prefix_rows]

    policy = await _caller_policy(run, out)
    if audit is not None:
        # A check the audit could not run drafts nothing; say so, or it reads as clean.
        run.skipped += [f"audit.{s}" for s in audit.skipped]
        extraction = await _extraction_proposal(run, policy, audit.findings)
        if extraction is not None:
            out.proposals.append(extraction)
    if cats is not None:
        out.categories_registered = len(cats.categories)
        names = [c.name for c in cats.categories]
        out.proposals += await _category_proposals(run, names, tags, agents, agents_total)
    if guide is not None:
        out.guide_revision = int(guide.revision)
    # A personal collection has no guide (namespace.guide_set refuses one), so none is drafted.
    if guide is not None and not is_personal(ns):
        drafted = _guide_proposal(run, guide, prefixes, tags, agents, agents_total)
        if drafted is not None:
            out.proposals.append(drafted)
    documents = await run.guarded(
        "documents",
        lambda: document_drafts(input, run.guarded, run.skipped),
        DocumentDrafts(),
    )
    for p in documents.proposals:
        p.id = _proposal_id(p.target, p.proposed)
    out.proposals += documents.proposals
    out.chunking = documents.chunking
    out.skipped = run.skipped
    return out
