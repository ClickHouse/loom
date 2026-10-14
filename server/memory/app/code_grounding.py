"""Code/GitHub grounding plane for memory.ask — the one built-in grounding
provider.

`memory.ask` (memory.app.ask) is domain-agnostic: it retrieves and
synthesizes. This module is the pluggable layer that reconciles an answer's
grounding pool against an authoritative external source of record — here, loom's
own code-intelligence plane over the same repo (symbols, files, PRs). It is the
only place that knows about PRs / commits / the code index, so the memory core
stays generic and other sources of record (issue trackers, DB schemas, docs)
can be added as sibling grounders behind the same `apply_code_grounding`-shaped
entry point.

Every step is gated by an explicit code-namespace BINDING (`_resolve_code_target`
— never guesses a repo) and by its own setting, and every step is additive /
fail-open, so a deployment with no bound code index is a complete no-op.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from server.code.domain.anchor import local_name
from server.memory.store.code_links import LinkRow

logger = logging.getLogger(__name__)

# The consumer tag every code-plane read this module makes carries: memory.ask's
# grounding hop, distinguishable in
# code_search_trace from a direct code.search by the same client.
GROUNDING_CONSUMER = "memory.ask"


def _resolve_code_target(search_input: Any, settings: Any) -> tuple[str, str]:
    """(org, namespace) of the code index enrichment should read, or ("","")
    to skip. A deployment may index many repos AND many branches as separate
    code namespaces (code-myrepo @ main vs code-myrepo-2-1 @ 2.1),
    and one agent may span repos — so a bare symbol name is ambiguous across
    them. We therefore NEVER fall back to the deployment's default code
    namespace here; the target must be explicit:
      1. a per-request override on the memory.ask input (`code_namespace` /
         `code_org`) — the caller knows which repo/branch it is working on;
      2. else the operator's configured binding for this deployment.
    Neither set -> ("","") -> caller skips enrichment (fail closed, no guess)."""
    ns = (
        getattr(search_input, "code_namespace", "") or settings.memory_ask_code_namespace or ""
    ).strip()
    if not ns:
        return ("", "")
    org = (
        getattr(search_input, "code_org", "")
        or settings.memory_ask_code_org
        or settings.code_org
        or getattr(search_input, "org", "")
        or ""
    ).strip()
    return (org, ns)


async def _authorized_code_target(search_input: Any, settings: Any) -> tuple[str, str]:
    """`_resolve_code_target`, then the current principal's right to read it. The
    grounding hop runs inside a memory.ask the transport
    authorized against the MEMORY namespace only; the code namespace it
    reads must pass the same gate for the same principal — a restricted code
    index bound by the operator or named by the request is otherwise readable
    through any memory namespace. Unauthorized → ("", "") and every leg is a
    no-op, never a 403 on the ask. A restricted target that passes leaves one
    audit row (the transport's row names the memory namespace, not this one).
    """
    code_org, code_ns = _resolve_code_target(search_input, settings)
    if not code_ns:
        return ("", "")
    try:
        from server.auth.authz import row_in_scope
        from server.operations.base import Role

        if not await row_in_scope(code_org, code_ns, Role.VIEWER):
            _warn_once(
                "code grounding skipped (the caller may not read the code target): %s",
                PermissionError(f"{code_org}/{code_ns}"),
            )
            return ("", "")
    except Exception as e:  # noqa: BLE001 — fail closed: no target
        logger.debug("code grounding authorization failed: %s", e)
        return ("", "")
    try:
        from server.auth.context import current_auth
        from server.identity.app.restricted_access import note_restricted_access

        await note_restricted_access(
            current_auth(),
            op="memory.ask.grounding",
            org=code_org,
            namespace=code_ns,
            transport="grounding",
        )
    except Exception:  # noqa: BLE001 — the ledger never blocks the hop
        pass
    return (code_org, code_ns)


def _hop_symbols(query: str, pool: list[Any], cap: int, *, rows: int = 12) -> list[str]:
    """Symbol-shaped identifiers (an uppercase letter or `::`) the top `rows`
    pool excerpts name and the question does not, first-seen order, original
    case, at most `cap`. Pure."""
    if cap <= 0 or not pool:
        return []
    from server.code.domain import idents

    asked = idents.embedded_idents(query or "")
    out: list[str] = []
    seen: set[str] = set()
    for h in pool[:rows]:
        text = str(getattr(h, "content_excerpt", "") or "")
        for tok in idents.EMBEDDED_IDENT_RE.findall(text):
            t = str(tok).lstrip("~")
            if len(t) < 5 or not (any(c.isupper() for c in t) or "::" in t):
                continue
            k = t.lower()
            if k in seen or k in asked or any(comp in asked for comp in k.split("::")):
                continue
            seen.add(k)
            out.append(t)
            if len(out) >= cap:
                return out
    return out


async def _inject_code_context(
    query: str,
    search_input: Any,
    settings: Any,
    pool: list[Any] | None = None,
    *,
    code_target: tuple[str, str] | None = None,
) -> list[Any]:
    """Grounding facts from loom's code-intelligence plane for a code-entity
    query. Two gates: (1) the query must name a code-shaped identifier (reuse
    the code plane's own NL identifier detector so plain English never triggers
    it, and a people/process query is a no-op); (2) an explicit code target
    must resolve (see _resolve_code_target — we do NOT guess a repo/branch).
    When both hold, run code.search against that EXPLICIT (org, namespace) and
    render the top units as `[code index @ <branch>] path:line — symbol …`
    grounding hits, carrying the authoritative source location + signature +
    recent PRs the memory corpus usually lacks. Fail-open to []."""
    from server.code.domain import idents

    _hop_cap = int(settings.memory_ask_code_hop_symbols)
    hop_syms = _hop_symbols(query or "", list(pool or []), _hop_cap)
    if not query or (not idents.embedded_idents(query) and not hop_syms):
        return []
    code_org, code_ns = code_target or await _authorized_code_target(search_input, settings)
    if not code_ns:
        return []  # no explicit repo/branch bound — never guess which index
    named = sorted(idents.embedded_idents(query))
    try:
        from server.code.api.models_search import CodeSearchInput
        from server.code.api.models_symbol import CodeSymbolInput
        from server.code.app.search.handler import handle_code_search
        from server.code.app.symbol import handle_code_symbol
        from server.memory.api.models_search import _MemorySearchHit
    except Exception as e:  # noqa: BLE001 — code plane absent: skip
        logger.debug("code-context imports failed: %s", e)
        return []

    def _render(u: Any, src_tag: str) -> Any | None:
        path = getattr(u, "path", "") or ""
        if not path:
            return None
        start = int(getattr(u, "start_line", 0) or 0)
        loc = f"{path}:{start}" if start else path
        sym = getattr(u, "symbol", "") or ""
        sig = getattr(u, "signature", "") or ""
        desc = (getattr(u, "description", "") or "").strip()
        content = f"[{src_tag}] {loc}"
        if sym:
            content += f" — {sym}"
        if sig:
            content += f" {sig}"
        if desc:
            content += f". {desc}"
        pr_bits = [
            f"#{p.get('pr_number')} {str(p.get('title', '')).strip()}".strip()
            for p in (getattr(u, "recent_prs", None) or [])[:3]
            if isinstance(p, dict) and p.get("pr_number")
        ]
        if pr_bits:
            content += ". Recent PRs: " + "; ".join(pr_bits)
        try:
            return _MemorySearchHit(
                memory_key=f"code:{code_ns}:{loc}",
                memory_type="code",
                content_excerpt=content,
                distance=0.0,
                similarity=1.0,
                rank_score=1.0,
                valid_at="",
                source="code_index",
            )
        except Exception:  # noqa: BLE001 — grounding is advisory: the hit serves without it
            return None

    # Op-routing: prefer code.symbol (ONE authoritative definition) for the
    # exact-symbol identifiers the query names — that is the precise answer to
    # "where is X", and it avoids the dilution of code.search's fuzzy top-3
    # (a tangential unit crowding the pool). code.search is the
    # fallback / fills the remaining slots for concept-shaped queries.
    hits: list[Any] = []
    seen_loc: set[str] = set()
    branch = code_ns
    exact = [i for i in named if any(c.isupper() for c in i) or "::" in i][:2]

    # The symbol lookups and the search are independent reads of the same index
    # (at most two exact symbols fill two of three slots, so the search always
    # runs anyway), so they run concurrently; awaited serially, the grounding
    # step is ~35 ClickHouse round trips, 1.8 s of a 3.3 s pre-synthesis path.
    # The hop's reads carry their own consumer tag, so the plane's largest
    # caller is attributable in code_search_trace and its abstain rate does not
    # blur the plane's precision floor.
    async def _symbol(sym: str) -> Any | None:
        try:
            return await handle_code_symbol(
                CodeSymbolInput(
                    org=code_org,
                    namespace=code_ns,
                    name=sym,
                    include_body=False,
                    include_history=False,
                    include_examples=False,
                    include_lessons=False,
                    token_budget=400,
                    consumer=GROUNDING_CONSUMER,
                )
            )
        except Exception:  # noqa: BLE001 — additive
            return None

    async def _search() -> Any | None:
        try:
            return await handle_code_search(
                CodeSearchInput(
                    org=code_org,
                    namespace=code_ns,
                    query=query[:2000],
                    top_k=3,
                    token_budget=1400,
                    bodies="none",
                    consumer=GROUNDING_CONSUMER,
                )
            )
        except Exception as e:  # noqa: BLE001 — additive, never fatal
            _warn_once("code-context search fallback failed: %s", e)
            return None

    # Hop symbols ride the same concurrent round, one
    # authoritative definition each, appended after the question's rows.
    gathered = await asyncio.gather(
        *(_symbol(sym) for sym in exact), *(_symbol(sym) for sym in hop_syms), _search()
    )
    symbol_outs, out = list(gathered[: len(exact)]), gathered[-1]
    hop_outs = list(gathered[len(exact) : len(exact) + len(hop_syms)])
    # The exact definitions first, the search fills.
    for so in symbol_outs:
        defs = getattr(so, "definitions", None) or [] if so is not None else []
        if len(defs) != 1:  # 0 = unknown; >1 = homonym, don't guess — let search decide
            continue
        h = _render(defs[0], f"code index @ {branch}")
        if h is not None and h.memory_key not in seen_loc:
            seen_loc.add(h.memory_key)
            hits.append(h)

    if len(hits) < 3 and out is not None and not getattr(out, "abstain", False):
        as_of = getattr(out, "as_of", None) or {}
        branch = str(as_of.get("branch") or as_of.get("repo_branch") or code_ns)
        for u in getattr(out, "results", None) or []:
            if len(hits) >= 3:
                break
            h = _render(u, f"code index @ {branch}")
            # Every rendered, unseen unit is appended, not just the last one.
            if h is not None and h.memory_key not in seen_loc:
                seen_loc.add(h.memory_key)
                hits.append(h)
    for so in hop_outs:
        defs = getattr(so, "definitions", None) or [] if so is not None else []
        if len(defs) != 1:
            continue
        h = _render(defs[0], f"code index @ {branch} (hop)")
        if h is not None and h.memory_key not in seen_loc:
            seen_loc.add(h.memory_key)
            hits.append(h)
    return hits[: 3 + len(hop_syms)]


_PR_NUM_RE = re.compile(r"(?:\bPR\b|\bissue\b|\bpull request\b|#)\s*#?(\d{4,6})\b", re.I)


def _linked_issue_numbers(row: dict[str, Any], cap: int = 6) -> list[int]:
    """Issue numbers a PR-history row links; tolerant of the
    column arriving as ints, strings or None."""
    out: list[int] = []
    for x in list(row.get("linked_issues") or [])[:cap]:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return out


def describe_pr(num: int, row: dict[str, Any], as_of_date: Any = None) -> str:
    """One-line record of a merged PR from `code_pr_history` for a record
    answer: author, merged state as of the anchor, title,
    linked issues. Empty when the row has nothing to say."""
    author = str(row.get("author") or "").strip()
    title = str(row.get("title") or "").strip()[:160]
    merged = row.get("merged_at")
    md = merged.date() if (merged is not None and hasattr(merged, "date")) else None
    parts = [f"PR #{num}"]
    if author:
        parts.append(f"authored by @{author}")
    if md is not None and as_of_date is not None and md > as_of_date:
        parts.append(f"was NOT merged yet as of {as_of_date} (merged {md})")
    elif md is not None:
        parts.append(f"was merged on {md}")
    if title:
        parts.append(f"— {title}")
    linked = _linked_issue_numbers(row)
    if linked:
        plural = "s " if len(linked) > 1 else " "
        parts.append("It links issue" + plural + ", ".join(f"#{i}" for i in linked) + ".")
    return " ".join(parts) if len(parts) > 1 else ""


def _referenced_pr_numbers(query: str, memories: list[Any], cap: int = 12) -> list[int]:
    """PR/issue numbers the query or the grounding memories reference (4-6
    digits — the PR-number range, not years)."""
    text = (
        (query or "") + " " + " ".join((getattr(m, "content_excerpt", "") or "") for m in memories)
    )
    seen: dict[int, None] = {}
    for m in _PR_NUM_RE.finditer(text):
        seen.setdefault(int(m.group(1)), None)
        if len(seen) >= cap:
            break
    return list(seen)


def describe_item(num: int, row: dict[str, Any], as_of_date: Any = None) -> str:
    """One-line authoritative sentence for a loom.code_gh_items row, state
    resolved AS OF `as_of_date` when given (None = now): kind, opener, open
    date, and open / closed (reason) / merged. Empty when the row is
    unusable or the item did not exist yet at the anchor (the temporal gate
    owns that case)."""

    def _d(v: Any) -> Any:
        return v.date() if (v is not None and hasattr(v, "date")) else None

    opened = _d(row.get("opened_at"))
    closed, merged = _d(row.get("closed_at")), _d(row.get("merged_at"))
    if opened is None or (as_of_date is not None and opened > as_of_date):
        return ""
    is_pr = bool(row.get("is_pr"))
    parts = [f"[github] #{num} is a PR" if is_pr else f"[github] #{num} is an issue (not a PR)"]
    author = str(row.get("author") or "").strip()
    parts.append(f"opened by @{author} on {opened}" if author else f"opened on {opened}")
    at = as_of_date
    if merged is not None and (at is None or merged <= at):
        parts.append(f"merged {merged}")
    elif closed is not None and (at is None or closed <= at):
        reason = str(row.get("state_reason") or "").strip()
        tail = (f" ({reason})" if reason else "") + (" without merging" if is_pr else "")
        parts.append(f"closed {closed}{tail}")
    else:
        parts.append(f"still open as of {at}" if at is not None else "open")
    title = str(row.get("title") or "").strip()[:160]
    if title:
        parts.append(f"— {title}")
    return " ".join(parts)


async def _inject_pr_facts(
    query: str,
    memories: list[Any],
    search_input: Any,
    settings: Any,
    *,
    code_target: tuple[str, str] | None = None,
) -> list[Any]:
    """Ground the answer's PR claims against loom's AUTHORITATIVE PR plane.

    A hard-false class in memory.ask answers is PR/issue facts —
    author, merged-state, PR-vs-issue kind — which synthesis takes verbatim from
    free-text memories that can be stale or wrong. loom uniquely also holds
    `code_pr_history` (real author / merged_at / title per merged PR over the
    same repo). For each PR the query or grounding memories reference, inject
    that authoritative record as a high-trust `[code index]` grounding fact, so
    synthesis prefers the real author/state over a memory's guess. As-of aware:
    a PR merged AFTER the anchor is reported as "not merged yet as of <anchor>".
    Additive, fail-open, gated by the same explicit code binding as enrichment;
    a miss (issue / unmerged / out-of-window) simply injects nothing.
    """
    numbers = _referenced_pr_numbers(query, memories)
    if not numbers:
        return []
    code_org, code_ns = code_target or await _authorized_code_target(search_input, settings)
    if not code_ns:
        return []  # no explicit repo/branch bound — never guess
    try:
        from server.code.app.reads import code_index
        from server.memory.api.models_search import _MemorySearchHit

        repo = code_index()
        facts = await repo.prs_by_number(code_org, code_ns, numbers)
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        _warn_once("pr-fact grounding failed: %s", e)
        return []
    as_of = getattr(search_input, "as_of", None)
    as_of_date = as_of.date() if (as_of is not None and hasattr(as_of, "date")) else None
    hits: list[Any] = []
    # Numbers the merged-PR plane does not know — issues, open or abandoned
    # PRs — come from loom.code_gh_items: kind, opener and
    # the state AS OF the anchor, which is what a memory most often gets
    # wrong ("PR #N" for an issue; "merged" for a PR still open then).
    rest = [n for n in numbers if n not in facts]
    items: dict[int, dict[str, Any]] = {}
    if rest:
        try:
            items = await repo.items_by_number(code_org, code_ns, rest)
        except Exception as e:  # noqa: BLE001 — no gh-items rows: PR-plane facts only
            _warn_once("gh-items grounding lookup failed: %s", e)
    for num, row in items.items():
        content = describe_item(num, row, as_of_date)
        if not content:
            continue
        try:
            hits.append(
                _MemorySearchHit(
                    memory_key=f"gh_item:{code_ns}:{num}",
                    memory_type="code",
                    content_excerpt=content,
                    distance=0.0,
                    similarity=1.0,
                    rank_score=1.0,
                    valid_at="",
                    source="code_index",
                )
            )
        except Exception:  # noqa: BLE001 — one malformed item is skipped
            continue
    for num, row in facts.items():
        author = str(row.get("author") or "").strip()
        title = str(row.get("title") or "").strip()[:160]
        merged = row.get("merged_at")
        md = merged.date() if (merged is not None and hasattr(merged, "date")) else None
        parts = [f"[code index] PR #{num}"]
        if author:
            parts.append(f"authored by @{author}")
        if md is not None and as_of_date is not None and md > as_of_date:
            parts.append(f"NOT merged yet as of {as_of_date} (merged {md})")
        elif md is not None:
            parts.append(f"merged {md}")
        if title:
            parts.append(f"— {title}")
        linked = _linked_issue_numbers(row)
        if linked:
            # Render the record's linked_issues: without them "which issue does
            # it link to" answers "the memories do not record" with the list in
            # hand.
            plural = "s " if len(linked) > 1 else " "
            parts.append("links issue" + plural + ", ".join(f"#{i}" for i in linked))
        content = " ".join(parts)
        try:
            hits.append(
                _MemorySearchHit(
                    memory_key=f"code_pr:{code_ns}:{num}",
                    memory_type="code",
                    content_excerpt=content,
                    distance=0.0,
                    similarity=1.0,
                    rank_score=1.0,
                    valid_at="",
                    source="code_index",
                )
            )
        except Exception:  # noqa: BLE001 — one malformed fact is skipped
            continue
    return hits[:8]


async def _gate_future_prs(
    memories: list[Any],
    search_input: Any,
    settings: Any,
    *,
    code_target: tuple[str, str] | None = None,
) -> list[Any]:
    """Drop grounding memories that reference a PR which did NOT EXIST as of the
    query's `as_of` anchor — the dominant residual hard-false class.

    Retrieval already excludes memories DATED after
    the anchor. But a bulk-ingested memory can carry a valid_at <= anchor while
    its TEXT names a PR opened after that date (the ingest under-dated it), so
    the date filter can't see it and synthesis then cites a PR that did not yet
    exist. This gate resolves each referenced PR's GitHub open date from loom's
    own code_pr_history (opened_at) and drops any memory citing a PR with
    opened_at > as_of. No-op without as_of or a bound code index; additive/fail-open. A PR
    whose open date is unknown is never gated (conservative).
    """
    as_of = getattr(search_input, "as_of", None)
    if as_of is None or not memories:
        return memories
    as_of_dt = as_of if hasattr(as_of, "date") else None
    if as_of_dt is None:
        return memories
    numbers = _referenced_pr_numbers("", memories, cap=64)
    if not numbers:
        return memories
    code_org, code_ns = code_target or await _authorized_code_target(search_input, settings)
    if not code_ns:
        return memories
    try:
        from server.code.app.reads import code_index

        repo = code_index()
        facts = await repo.prs_by_number(code_org, code_ns, numbers)
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.debug("pr-temporal gate lookup failed: %s", e)
        return memories
    # PRs whose open date is known AND strictly after the anchor = didn't exist.
    future: set[int] = set()
    known: set[int] = set()
    for num, row in facts.items():
        opened = row.get("opened_at")
        od = opened if hasattr(opened, "date") else None
        if od is not None and od.year > 1970:
            known.add(int(num))
            if od > as_of_dt:
                future.add(int(num))
    # The PR plane knows merged PRs only. Issues and open / abandoned PRs —
    # the numbers agents cite most — live in loom.code_gh_items; resolve the
    # rest there. Absent table or row: never gated.
    rest = [n for n in numbers if n not in known]
    if rest:
        try:
            items = await repo.items_by_number(code_org, code_ns, rest)
        except Exception as e:  # noqa: BLE001 — absent table or row: never gated
            logger.debug("gh-items temporal lookup failed: %s", e)
            items = {}
        for num, row in items.items():
            opened = row.get("opened_at")
            od = opened if hasattr(opened, "date") else None
            if od is not None and od.year > 1970 and od > as_of_dt:
                future.add(int(num))
    if not future:
        return memories
    kept: list[Any] = []
    dropped = 0
    for m in memories:
        refs = set(_referenced_pr_numbers("", [m], cap=16))
        if refs & future:
            dropped += 1
            continue
        kept.append(m)
    if dropped:
        logger.debug("pr-temporal gate dropped %d memories citing future PRs", dropped)
    return kept


async def _annotate_stale_code_memories(memories: list[Any], search_input: Any) -> Any | None:
    """One grounding note for the strongest memories whose linked code has moved,
    changed or gone since they were written, read from their code links
    (`server.memory.app.code_links`); synthesis then prefers the current code. None
    when no such memory is in the pool. Fail-open."""
    from server.memory.api.models_search import _MemorySearchHit
    from server.memory.app.code_link_reads import hit_memories, readable_namespaces

    pool = [m for m in memories[:8] if str(getattr(m, "memory_type", "")) != "code"]
    # the search read most hits' links: read only the rest's
    by_hit: list[list[LinkRow] | None] = [getattr(m, "_code_links", None) for m in pool]
    unread = [i for i, v in enumerate(by_hit) if v is None]
    if unread:
        try:
            heads = await hit_memories(search_input, [pool[i] for i in unread])
            read: list[list[LinkRow]] = [(m or {}).get("links", []) for m in heads]
            cns = {a["code_namespace"] for v in read for a in v}
            seen = await readable_namespaces(str(search_input.org), cns)
        except Exception as e:  # noqa: BLE001 — grounding is advisory
            _warn_once("code staleness: links unreadable: %s", e)
            return None
        for i, v in zip(unread, read, strict=True):
            by_hit[i] = [a for a in v if a["code_namespace"] in seen]
    links = [a for v in by_hit for a in v or []]
    notes: list[str] = []
    for a in links:
        what, key = a["qualified_name"] or a["path"], f"memory [{a['memory_key']}]"
        if a["state"] == "missing":
            notes.append(f"{key} is about {what}, which the code index no longer has")
        elif a["state"] == "changed":
            notes.append(f"{key} is about {what}, which changed after it was written")
        elif a["state"] == "unresolved":
            notes.append(f"{key} is about {what}, which was renamed or removed: find it again")
        elif a["state"] == "moved" and (moved := _moved_note(a, what)):
            notes.append(f"{key} {moved}")
    if not notes:
        return None
    body = (
        "[code index — STALENESS] These memories describe code that has since moved or "
        "changed; prefer the current code:\n- " + "\n- ".join(notes[:8])
    )
    return _MemorySearchHit(
        memory_key="code:staleness",
        memory_type="code",
        content_excerpt=body,
        distance=0.0,
        similarity=1.0,
        rank_score=1.0,
        valid_at="",
        source="code_index",
    )


def _moved_note(a: LinkRow, what: str) -> str:
    """What a moved link's memory has wrong against where it was written (its anchor):
    the name (inside its module), the file, or both; '' when it names neither."""
    old_name, old_path = a["anchored_name"], a["anchored_path"]
    new_name, new_path = a["qualified_name"], a["path"]
    renamed = bool(old_name) and local_name(old_name, old_path or new_path) != local_name(
        new_name, new_path
    )
    parts = [f"names {old_name}; it is now {new_name}"] if renamed else []
    if old_path and old_path != new_path:
        parts.append(f"places {what} in {old_path}; it now lives in {new_path}")
    return ", and ".join(parts)


_WARNED: set[str] = set()


def _warn_once(msg: str, exc: BaseException) -> None:
    """A fail-open code-plane read that raises is a deployment fault (a
    missing column, a disabled gate), not noise: say so once per process at
    WARNING and keep the debug detail for the rest, so a read that fails on
    every ask cannot hide behind a debug line."""
    key = msg.split(":")[0]
    if key in _WARNED:
        logger.debug(msg, exc)
        return
    _WARNED.add(key)
    logger.warning(msg + " (further occurrences logged at debug)", exc)


async def apply_code_grounding(
    query: str, search_input: Any, synth_pool: list[Any], settings: Any
) -> list[Any]:
    """Reconcile the synthesis pool against the code plane, in a fixed order:
      1. PR-temporal gate — drop memories citing a PR opened after `as_of`;
      2. code enrichment  — append authoritative file:line / symbol facts;
      3. PR-fact grounding — prepend authoritative PR author/merge records;
      4. staleness oracle  — prepend a note for memories whose linked code moved,
         changed or is gone.
    Each step keeps its own setting gate and is a no-op without a bound code
    index, so this is a complete no-op for a non-code deployment.
    """

    # The four steps run as two concurrent rounds. The gate
    # reads only the pool and the enrichment only the query, so they run
    # together; PR-fact grounding reads the pool the first round produced
    # (a code hit's "Recent PRs" are referenced PRs), and the staleness note
    # reads the same pool — it does not wait for the PR facts, which are
    # code-typed rows it skips anyway, so the PR facts do not displace
    # real memories out of the eight the staleness pass examines.
    async def _noop_gate() -> list[Any]:
        return synth_pool

    async def _noop_hits() -> list[Any]:
        return []

    async def _noop_note() -> Any | None:
        return None

    # The target is resolved and authorized ONCE for the
    # current principal; an unauthorized or unbound target makes all four
    # steps no-ops.
    target = await _authorized_code_target(search_input, settings)
    if not target[1]:
        return synth_pool

    gated, code_hits = await asyncio.gather(
        _gate_future_prs(synth_pool, search_input, settings, code_target=target)
        if settings.memory_ask_pr_temporal_gate
        else _noop_gate(),
        _inject_code_context(query, search_input, settings, pool=synth_pool, code_target=target)
        if settings.memory_ask_code_enrichment
        else _noop_hits(),
    )
    if len(gated) != len(synth_pool):
        synth_pool = gated
    if code_hits:
        synth_pool = synth_pool + code_hits
    pr_hits, stale_note = await asyncio.gather(
        _inject_pr_facts(query, synth_pool, search_input, settings, code_target=target)
        if settings.memory_ask_pr_grounding
        else _noop_hits(),
        _annotate_stale_code_memories(synth_pool, search_input)
        if settings.memory_ask_code_staleness
        else _noop_note(),
    )
    if pr_hits:
        synth_pool = pr_hits + synth_pool
    if stale_note is not None:
        synth_pool = [stale_note] + synth_pool
    return synth_pool
