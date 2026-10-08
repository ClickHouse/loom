"""The hypothetical-answer leg: a model writes the passage the index would hold
for the answer, and the served chunks nearest to it vote as a capped list."""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.app.search.state import _POOL
from server.code.app.spans import code_span, span_attrs
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


_HYDE_SYSTEM = (
    "You are a core developer of the codebase indexed as `{namespace}`. Given a question about how "
    "it works, write the passage a retrieval index would hold for the code that implements the "
    "answer: 3-6 lines - the exact class and function names as they appear in the source, the file "
    "they live in if you know it, and one or two sentences of doc-comment describing the mechanism "
    "in the code's own terms. Plain text, no markdown, no explanation to the reader. "
    "If unsure of a name, give your best guess."
)


def _hyde_config(work: dict[str, Any], *, prose: bool) -> str:
    """The model that writes the hypothetical answer for this question's kind, or ""
    (off)."""
    from server.code.domain.index.question_kind import kind_allowed

    model = str(settings.code_search_hyde_model).strip()
    if (
        not model
        or not prose
        or not kind_allowed(
            str(work.get("question_kind") or "other"),
            settings.code_search_hyde_kinds,
        )
    ):
        return ""
    return model


async def _hyde_leg_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    query: str,
    hyde_model: str,
    embedder: Any,
    path_prefix: str,
    scope: SearchScope | None,
    work: dict[str, Any],
) -> list[dict[str, Any]]:
    """The hypothetical answer's vector list — a model writes the passage the index
    would hold for the answer, the served embedder embeds it, and the served label's
    chunks nearest to it are returned for the fusion as a capped list. Empty model or no
    embedder = off; any failure is no vote, never a failed search."""
    if not hyde_model or embedder is None:
        return []
    work["hyde_queries"] = work.get("hyde_queries", 0) + 1
    with code_span("search.hyde_leg") as sp:
        try:
            from server.llm.cache import wrap_scoped
            from server.llm.client import get_llm_client_for_model
            from server.observability.genai import llm_stage

            t0 = time.perf_counter()
            # Behind the exact-prompt cache inside code.ask
            llm = wrap_scoped(get_llm_client_for_model(hyde_model))
            with llm_stage("code_hyde"):
                res = await llm.complete(
                    _HYDE_SYSTEM.format(namespace=ns),
                    query,
                    max_tokens=int(settings.code_search_hyde_max_tokens),
                )
            passage = " ".join(str(getattr(res, "text", "") or "").split())
            work["hyde_chars"] = len(passage)
            if len(passage) < 20:
                return []
            vec = await embedder.embed(passage)
            rows = await repo.vector_search_chunks(
                org,
                ns,
                model,
                vec,
                path_prefix=path_prefix,
                pool=_POOL,
                scope=scope,
            )
            work["hyde_ms"] = int((time.perf_counter() - t0) * 1000)
            span_attrs(sp, results=len(rows))
            return rows
        except Exception as e:  # noqa: BLE001 — a vote, never a gate
            logger.warning("code.search hyde leg failed: %s", e)
            work["hyde_leg_failed"] = 1
            return []
