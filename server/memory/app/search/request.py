"""Settling a memory.search request before retrieval: recipe and namespace
defaults, role tags, read scope, the top_k ceiling and the large-k body and
explanation tiers."""

from __future__ import annotations

from typing import Any

from server.core.naming import named
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.budget import SearchBudget, _ensure_search_budget
from server.memory.app.search.query_plan import (
    _apply_default_search_mode,
    _apply_search_recipe,
    _field_was_set,
    _sanitize_agent_query,
)


def _apply_role_tags(input: MemorySearchInput) -> MemorySearchInput:
    """`role` becomes a SOFT tag `role:<role>`: rows a same-role agent wrote — a reflection from
    a run that declared the role, a memory tagged by hand — earn the tag-overlap boost and
    nothing is filtered out. Hard tag filters (tags without tags_soft) are the caller's own
    scope and are left alone; a role never narrows a search."""
    role = str(input.role or "").strip().lower()
    if not role:
        return input
    tag = f"role:{role}"
    tags = list(input.tags or [])
    if not tags:
        return input.model_copy(update={"tags": [tag], "tags_soft": True})
    if input.tags_soft and tag not in tags:
        return input.model_copy(update={"tags": [*tags, tag]})
    return input


async def _apply_search_defaults(input: Any) -> tuple[Any, bool]:
    """Fill the fields the caller left unset, in precedence order: the recipe (a caller
    choice), then the namespace's `retrieval_defaults` (read from the addressed namespace,
    TTL-cached, fails open to none), then the deployment's `memory_search_default_mode`. Also
    returns whether the caller pinned `hyde`, read before any fill."""
    from server.memory.app import namespace_retrieval_defaults as _ns_defaults

    caller_hyde_explicit = _field_was_set(input, "hyde")
    input = _apply_search_recipe(input)
    input = _ns_defaults.apply(
        input, await _ns_defaults.resolve(str(input.org or ""), str(input.namespace or ""))
    )
    return _apply_default_search_mode(input), caller_hyde_explicit


_EXPLAIN_FIELDS: tuple[str, ...] = ("explanation", "score_components")


def _excerpt_bodies_k() -> int:
    """The top_k above which an unpinned request serves the
    excerpt body tier and no per-hit explanation. 0 = never."""
    from server.settings import settings as _s

    return int(_s.memory_search_excerpt_bodies_k)


def _above_excerpt_k(input: Any) -> bool:
    k_max = _excerpt_bodies_k()
    return k_max > 0 and int(getattr(input, "top_k", 0) or 0) > k_max


def _apply_large_k_body_default(input: Any) -> Any:
    """A request above `memory_search_excerpt_bodies_k` that
    pinned no `bodies` reads the excerpt tier — keys, types and scores are
    the same, the clip is memory_search_excerpt_chars long, and a decision or
    constraint keeps its stored clip (`_apply_bodies`). A caller that
    pinned any tier (full included), a recipe that set one, and every
    in-process reader that passes `bodies` are untouched; so is every k at
    or below the threshold. Applied once: the copy marks `bodies` as set."""
    if _field_was_set(input, "bodies") or not _above_excerpt_k(input):
        return input
    return input.model_copy(update={"bodies": "excerpt"})


def _apply_k_defaults(input: Any) -> tuple[Any, int]:
    """The role ceiling on top_k and then, on the k actually served, the large-k body tier."""
    input, ceiling = _apply_top_k_ceiling(input)
    return _apply_large_k_body_default(input), ceiling


def apply_explain_default(hits: list[Any], input: Any) -> None:
    """Above the excerpt k, `explanation` and
    `score_components` leave the wire unless the caller pinned
    `explain: true`; `explain: false` drops them at any k. In-process readers
    keep the attributes — only serialization elides them."""
    explain = getattr(input, "explain", None)
    if explain is True or (explain is None and not _above_excerpt_k(input)):
        return
    for h in hits:
        try:
            h._elide = frozenset(_EXPLAIN_FIELDS)
        except Exception:  # noqa: BLE001 — a foreign hit type keeps its payload
            continue


def _apply_top_k_ceiling(input: Any) -> tuple[Any, int]:
    """Cap top_k by the caller's role on the namespace.

    Scoped tokens carry a per-namespace role; unscoped tokens the highest of
    their roles. Viewer and writer each have a configurable ceiling (0 = none);
    admin and above are uncapped, and so is a request with no token (dev
    mode). Returns the input (copied when lowered) and the ceiling applied,
    0 when none."""
    from server.auth.context import current_auth
    from server.operations.base import Role
    from server.settings import settings

    auth = current_auth()
    if auth is None:
        return input, 0
    role = auth.effective_role(input.org, input.namespace)
    if role is None:
        role = max(auth.roles) if auth.roles else None
    if role is None or role >= Role.ADMIN:
        return input, 0
    if role >= Role.WRITER:
        ceiling = int(settings.memory_search_top_k_ceiling_writer)
    else:
        ceiling = int(settings.memory_search_top_k_ceiling_viewer)
    if ceiling <= 0 or int(getattr(input, "top_k", 0) or 0) <= ceiling:
        return input, 0
    return input.model_copy(update={"top_k": ceiling}), ceiling


async def _resolve_search_input(
    input: MemorySearchInput,
) -> tuple[MemorySearchInput, bool, int, SearchBudget]:
    """Phase 1 of memory.search: settle the request.

    Applies the search defaults, the fast-budget pins, query sanitising, role
    tags, the agent / namespace_scope / read_scope / top_k defaults, and
    attaches the cascade-wide search budget. Returns the settled input, whether
    the caller asked for HyDE explicitly, the top_k ceiling, and the budget.
    """
    input, caller_hyde_explicit = await _apply_search_defaults(input)
    if getattr(input, "retrieval_budget", "standard") == "fast" and not getattr(
        input, "recipe", ""
    ):
        input = input.model_copy(
            update={
                "search_mode": "semantic",
                "hyde": False,
                "include_top_n_unmatched": 0,
            }
        )
    sanitized_query = _sanitize_agent_query(input.query)
    if sanitized_query != input.query:
        input = input.model_copy(update={"query": sanitized_query})
    input = _apply_role_tags(input)

    # Membership is the read boundary: the default agent "*" reads ALL agents
    # in the accessible namespaces (the WHERE builders treat "*"/"-" as "drop
    # the per-agent filter"). Normalize an empty agent to "*" too. An explicit
    # agent still narrows to that author.
    if not input.agent:
        input = input.model_copy(update={"agent": "*"})
    # An unset namespace_scope takes the deployment default; downstream code compares the
    # resolved value only.
    if input.namespace_scope is None:
        from server.settings import settings as _scope_settings

        input = input.model_copy(
            update={"namespace_scope": _scope_settings.memory_search_default_namespace_scope}
        )

    # Read scope: resolve the request's read_scope against the token's claim and turn it into
    # the user/session predicates every leg's WHERE carries. Runs before the fan-out so the
    # recursive passes (the iterative retry, the read-your-writes rereads) inherit the narrowed
    # input.
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import pin_read_filters

    input, _ = pin_read_filters(input, current_auth())
    input = input.model_copy(update={"run_id": named(input.run_id)})
    input, _top_k_ceiling = _apply_k_defaults(input)

    # One budget object bounds the whole recursive fallback cascade; minted
    # on the top-level call and re-attached so recursive calls share the
    # same mutable instance (survives model_copy as a private attr).
    search_budget = _ensure_search_budget(input)
    if getattr(input, "_search_budget", None) is not search_budget:
        input = input.model_copy(update={"_search_budget": search_budget})
    return input, caller_hyde_explicit, _top_k_ceiling, search_budget
