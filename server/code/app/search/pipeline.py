"""`search`: the routes, the legs, fusion, calibration, ranking, the window and
the page, in that order."""

from __future__ import annotations

from typing import Any

from server.code.app.search.calibrate import _calibrate
from server.code.app.search.fusion import _fuse
from server.code.app.search.gather import _gather_legs
from server.code.app.search.history import _serve_history_anchors
from server.code.app.search.rank import _rank_units
from server.code.app.search.routes import _route_before_legs
from server.code.app.search.serve import _abstain_no_hits, _explain_score, _serve_page
from server.code.app.search.state import CodeSearchResult, _model_name
from server.code.app.search.vector import _resolve_vector_arm
from server.code.app.search.window import _order_window
from server.code.app.search_legs import resolve_deadline_ms
from server.code.domain import deadline as code_deadline
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo


async def search(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    query: str,
    top_k: int = 5,
    token_budget: int = 3000,
    path_prefix: str = "",
    # the caller's seen-set: dropped from the fused list after fusion and
    # before diversification (pushing NOT IN into the legs saves nothing)
    exclude_unit_ids: list[str] | None = None,
    user_recent_dirs: list[str] | None = None,
    rerank_endpoint: str = "",
    embedder: Any = None,
    abstain_threshold: float = 0.30,
    noise_floor: float = 0.22,
    extra_terms: list[str] | None = None,
    bodies: str = "auto",
    min_fan_in: int = 0,
    sort_by: str = "score",
    scope: SearchScope | None = None,
    pattern_max_files: int | None = None,
    kind_prior: str | None = None,
    legs: str | None = None,
    track_path: str = "",
    pr_leg_before: Any = None,
    embedding_model: str = "",
    vector_model: str = "",
    vector_shortlist: int | None = None,
    llm_legs: str | None = None,
    deadline_ms: int | None = None,
    # The risk annotation's path reads, started on the ranked head
    risk_prefetch: Any = None,
    # The request's budget, started by the handler; else one from `deadline_ms`
    budget: code_deadline.Budget | None = None,
) -> CodeSearchResult:
    # The label the legs read: the embedder's model name, or a request override so a
    # bench can read a twin label written beside the live one under the same (org,
    # namespace) — a disjoint ORDER BY range, so neither arm reads the other's granules.
    model = embedding_model or _model_name(embedder)
    result = CodeSearchResult(
        budget=budget or code_deadline.start(resolve_deadline_ms(deadline_ms)),
    )
    # A request-named second vector model. Its embedder is resolved here (allow-listed
    # by the deployment) and used by the vector leg only; every other read keeps the
    # served model's label.
    vec_embedder, shortlist_n, default_vm = _resolve_vector_arm(
        vector_model,
        vector_shortlist,
        embedder,
        scope,
    )
    work = {"lexical_queries": 0, "vector_queries": 0, "symbol_queries": 0, "embedding_calls": 0}
    result.retrieval_work = work
    st = await _route_before_legs(
        repo,
        org,
        ns,
        query=query,
        model=model,
        scope=scope,
        result=result,
        work=work,
        legs=legs,
        extra_terms=extra_terms,
        path_prefix=path_prefix,
        pattern_max_files=pattern_max_files,
        abstain_threshold=abstain_threshold,
        exclude_unit_ids=exclude_unit_ids,
        top_k=top_k,
        token_budget=token_budget,
        bodies=bodies,
    )
    if st is None:
        return result
    # ---- route 2: hybrid ----------------------------------------------------
    await _gather_legs(
        repo,
        org,
        ns,
        st,
        query=query,
        embedder=embedder,
        vec_embedder=vec_embedder,
        shortlist_n=shortlist_n,
        run_lexical=st.run_lexical,
        run_vector=st.run_vector,
        path_prefix=path_prefix,
        extra_terms=extra_terms,
        pr_leg_before=pr_leg_before,
        llm_legs=llm_legs,
    )
    fused, fusion_kind = _fuse(st)
    if not fused and st.history_units:
        _serve_history_anchors(st, top_k=top_k, token_budget=token_budget, bodies=bodies)
        return result
    if not fused:
        await _abstain_no_hits(
            repo,
            org,
            ns,
            st,
            abstain_threshold=abstain_threshold,
            track_path=track_path,
            embedder=embedder,
        )
        return result

    legs_agree = _calibrate(
        st,
        fused,
        run_vector=st.run_vector,
        noise_floor=noise_floor,
        abstain_threshold=abstain_threshold,
    )
    ranked = await _rank_units(
        repo,
        org,
        ns,
        st,
        fused,
        query=query,
        top_k=top_k,
        min_fan_in=min_fan_in,
        sort_by=sort_by,
        kind_prior=kind_prior,
        risk_prefetch=risk_prefetch,
        rerank_endpoint=rerank_endpoint,
        user_recent_dirs=user_recent_dirs,
    )
    ranked = await _order_window(
        repo,
        org,
        ns,
        st,
        ranked,
        query=query,
        top_k=top_k,
        path_prefix=path_prefix,
        sort_by=sort_by,
        min_fan_in=min_fan_in,
        exclude_unit_ids=exclude_unit_ids,
    )
    section_tokens = await _serve_page(
        repo,
        org,
        ns,
        st,
        ranked,
        top_k=top_k,
        token_budget=token_budget,
        bodies=bodies,
        track_path=track_path,
        embedder=embedder,
    )
    _explain_score(
        st,
        fusion_kind=fusion_kind,
        legs_mode=st.legs_mode,
        legs_agree=legs_agree,
        section_tokens=section_tokens,
        vector_model=vector_model,
        default_vm=default_vm,
        vec_embedder=vec_embedder,
        shortlist_n=shortlist_n,
    )
    return result
