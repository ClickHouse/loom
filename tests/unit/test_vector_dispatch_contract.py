"""First-party vector SQL builders and dispatches have an explicit classification.

The repository inventory runs only in the dedicated CI test body. Collection and the
six recognizer controls never read repository source or import server modules.
This source contract cannot prove arbitrary runtime SQL, optimizer work, native
execution, or ranking equality. Caller-supplied analyst SQL keeps its separate gate.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from pathlib import Path

# Match function spellings only in executable AST string literals, never comments
# or docstrings. This is test-only source discovery, never runtime SQL admission.
_DISTANCE = re.compile(
    r"\b(?:cosineDistance|L1Distance|L2Distance|L2SquaredDistance|LinfDistance|dotProduct)\b", re.I
)
_SINKS = {
    "query",
    "query_all",
    "query_one",
    "query_rows",
    "query_columns",
    "raw_bytes",
    "raw_stream",
    "candidates",
    "_rows",
    "_chunk_query",
    "_call_retrying_empty_block",
}

# A row is (distance-literal count, vector_work arguments, query contexts,
# references to classified SQL fragments). Repeated SQL distance expressions do
# not infer a weight: the independently reviewed marker expressions below do.
# The final scalar peer-row fetch, lookup metadata fetch and key-match fetch are
# deliberately recorded with None. Builder-only rows have no dispatch context.
_CLASSIFIED = {
    "server/code/store/index/answers.py:AnswersMixin.nearest_answers": (
        0,
        (("org", "ns", "1"),),
        (("query_all", ("org", "ns", "1")),),
        (("server/code/store/index/answers.py:_candidate_sql", 1),),
    ),
    "server/code/store/index/answers.py:_candidate_sql": (1, (), (), ()),
    "server/code/store/index/files.py:FilesMixin.best_chunks_for_paths": (
        1,
        (("org", "ns", "1"),),
        (("query_all", ("org", "ns", "1")),),
        (),
    ),
    "server/code/store/index/files.py:FilesMixin.track_path_chunks": (
        1,
        (("org", "ns", "1 if query_vec else 0"),),
        (("query_all", ("org", "ns", "1 if query_vec else 0")),),
        (),
    ),
    "server/code/store/index/search.py:SearchMixin.exact_rerank_by_ids._ranked": (
        1,
        (("org", "ns", "1"),),
        (("query_all", ("org", "ns", "1")),),
        (),
    ),
    "server/code/store/index/search.py:SearchMixin.vector_search_cards": (
        1,
        (("org", "ns", "1"),),
        (("query_all", ("org", "ns", "1")),),
        (),
    ),
    "server/code/store/index/search.py:SearchMixin.vector_search_chunks": (
        1,
        (("org", "ns", "1"),),
        (("query_all", ("org", "ns", "1")),),
        (),
    ),
    "server/code/store/index/search.py:SearchMixin.vector_shortlist_ids": (
        1,
        (("org", "ns", "1"),),
        (("query_all", ("org", "ns", "1")),),
        (),
    ),
    "server/core/ch.py:CHClient._query_attempts": (0, (("''", "()", "0"),), (), ()),
    "server/core/ch.py:CHClient.connect": (0, (("''", "()", "0"),), (), ()),
    "server/core/ch.py:CHClient._raw_stream_query": (0, (("''", "()", "0"),), (), ()),
    "server/core/ch.py:CHClient.raw_stream": (0, (("''", "()", "0"),), (), ()),
    "server/identity/app/namespace_catalog.py:handle_namespace_search_categories": (
        1,
        (("input.org", "input.namespace", "1"),),
        (("query_all", ("input.org", "input.namespace", "1")),),
        (),
    ),
    "server/knowledge/app/conversation_search.py:vector_search": (
        2,
        (("q.org", "q.namespace", "1"),),
        (("query_all", ("q.org", "q.namespace", "1")),),
        (),
    ),
    "server/knowledge/app/search.py:hybrid_search": (
        1,
        (("q.org", "q.namespaces", "1"),),
        (("query_all", ("q.org", "q.namespaces", "1")),),
        (),
    ),
    "server/knowledge/app/search.py:keys_search": (
        1,
        (("q.org", "q.namespaces", "1"),),
        (("query_all", ("q.org", "q.namespaces", "1")),),
        (),
    ),
    "server/knowledge/app/search.py:lexical_search": (
        1,
        (("q.org", "q.namespaces", "1"),),
        (("query_all", ("q.org", "q.namespaces", "1")),),
        (),
    ),
    "server/knowledge/app/source_search.py:collect_leg": (
        0,
        (("query.org", "mirrors", "1"),),
        (("candidates", ("query.org", "mirrors", "1")),),
        (("server/knowledge/store/source_candidates.py:statement", 1),),
    ),
    "server/knowledge/store/source_candidates.py:statement": (1, (), (), ()),
    "server/memory/app/conflicts.py:_foreign_pinned_rows": (
        0,
        (("str(params['org'])", "str(params['ns'])", "1"),),
        (("query_all", ("str(params['org'])", "str(params['ns'])", "1")),),
        (("server/memory/store/conflict_candidates.py:PINNED_PEERS_SQL", 1),),
    ),
    "server/memory/app/conflicts.py:_peer_rows_joined": (
        1,
        (("str(params['org'])", "str(params['ns'])", "1"),),
        (("query_all", ("str(params['org'])", "str(params['ns'])", "1")),),
        (),
    ),
    "server/memory/app/conflicts.py:candidate_rows_many._chunk": (
        0,
        (),
        (),
        (
            ("server/memory/store/conflict_candidates.py:candidate_sql_many", 1),
            ("server/memory/store/conflict_candidates.py:pinned_sql_many", 1),
        ),
    ),
    "server/memory/app/conflicts.py:candidate_rows_many._chunk._read": (
        0,
        (("org", "namespace", "len(idxs)"),),
        (("query_all", ("org", "namespace", "len(idxs)")),),
        (),
    ),
    "server/memory/app/conflicts.py:check_against_shared": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/memory/app/conflicts.py:nearest_peer_rows": (
        0,
        (("str(params['org'])", "str(params['ns'])", "1"),),
        (("query_all", ("str(params['org'])", "str(params['ns'])", "1")), ("query_all", None)),
        (("server/memory/store/conflict_candidates.py:_peer_candidates_sql", 1),),
    ),
    "server/memory/app/profile_search.py:PROFILE_VECTORS_SQL": (1, (), (), ()),
    "server/memory/app/profile_search.py:_similarities": (
        0,
        (("org", "tuple({str(r['namespace']) for r in rows})", "1"),),
        (("query_all", ("org", "tuple({str(r['namespace']) for r in rows})", "1")),),
        (("server/memory/app/profile_search.py:PROFILE_VECTORS_SQL", 1),),
    ),
    "server/memory/app/retrieval/chunks.py:_chunk_anchor_windows": (
        0,
        (("chunk_params['org']", "chunk_params['ns']", "1"),),
        (("_chunk_query", ("chunk_params['org']", "chunk_params['ns']", "1")),),
        (("server/memory/app/retrieval/chunks.py:_chunk_neighbor_sql", 1),),
    ),
    "server/memory/app/retrieval/chunks.py:_chunk_candidates_sql": (1, (), (), ()),
    "server/memory/app/retrieval/chunks.py:_chunk_neighbor_sql": (1, (), (), ()),
    "server/memory/app/retrieval/chunks.py:_chunk_stage2_one_trip_sql": (1, (), (), ()),
    "server/memory/app/retrieval/chunks.py:_search_memory_chunks": (
        0,
        (("q.org", "q.namespace", "1"), ("q.org", "q.namespace", "int(neighbors)")),
        (
            ("_chunk_query", ("q.org", "q.namespace", "1")),
            ("_chunk_query", ("q.org", "q.namespace", "int(neighbors)")),
        ),
        (
            ("server/memory/app/retrieval/chunks.py:_chunk_candidates_sql", 1),
            ("server/memory/app/retrieval/chunks.py:_chunk_stage2_one_trip_sql", 1),
        ),
    ),
    "server/memory/app/retrieval/derived.py:_resolve_operand_event_date": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/memory/app/retrieval/derived.py:fetch_current_state_observation": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/memory/app/retrieval/fact_rank.py:_bm25_hybrid_rank_score": (1, (), (), ()),
    "server/memory/app/retrieval/fact_rank.py:_fact_mode_rank_score": (
        0,
        (),
        (),
        (("server/memory/app/retrieval/fact_rank.py:_bm25_hybrid_rank_score", 1),),
    ),
    "server/memory/app/retrieval/fact_sql.py:_fact_distance_expr": (2, (), (), ()),
    "server/memory/app/retrieval/fact_sql.py:_fact_leg_sql": (
        2,
        (),
        (),
        (("server/memory/app/retrieval/fact_sql.py:_fact_distance_expr", 1),),
    ),
    "server/memory/app/retrieval/pool.py:_recency_backfill": (
        1,
        (("q.org", "q.namespace", "1"),),
        (("query_all", ("q.org", "q.namespace", "1")),),
        (),
    ),
    "server/memory/app/retrieval/retry.py:_query_all_retrying_empty_block": (
        0,
        (("str(params['org'])", "str(params['ns'])", "weight"),),
        (
            ("_call_retrying_empty_block", ("str(params['org'])", "str(params['ns'])", "weight")),
            ("query_all", ("str(params['org'])", "str(params['ns'])", "weight")),
        ),
        (),
    ),
    "server/memory/app/retrieval/search.py:vector_search": (
        0,
        (),
        (),
        (("server/memory/app/retrieval/fact_sql.py:_fact_leg_sql", 1),),
    ),
    "server/memory/app/retrieval/search.py:vector_search_trace": (
        1,
        (("q.org", "q.namespace", "1"),),
        (("query_all", ("q.org", "q.namespace", "1")),),
        (),
    ),
    "server/memory/app/retrieval/side_legs.py:_fetch_unmatched_candidates": (
        1,
        (("q.org", "q.namespace", "1"),),
        (("query_all", ("q.org", "q.namespace", "1")),),
        (),
    ),
    "server/memory/app/retrieval/side_legs.py:_lexical_recall_fallback": (
        1,
        (("q.org", "q.namespace", "1"),),
        (
            ("_call_retrying_empty_block", ("q.org", "q.namespace", "1")),
            ("query_all", ("q.org", "q.namespace", "1")),
        ),
        (),
    ),
    "server/memory/app/retrieval/summaries.py:fetch_entity_summaries": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/memory/app/retrieval/summaries.py:fetch_session_summaries": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/memory/app/search/anchor_pool.py:_run_anchor_pool._anchor_rows_for_ns": (
        0,
        (("input.org", "ns", "1"),),
        (("query_all", ("input.org", "ns", "1")),),
        (("server/memory/app/search/sql.py:anchor_pool_sql", 1),),
    ),
    "server/memory/app/search/ann_entity_boosts.py:_ann_entity_boosts._one_entity": (
        2,
        (("org", "namespaces", "2"),),
        (("query_all", ("org", "namespaces", "2")),),
        (),
    ),
    "server/memory/app/search/digest_descent.py:_run_digest_descent._rows": (
        0,
        (("input.org", "ns", "1"),),
        (("query_all", ("input.org", "ns", "1")),),
        (("server/memory/app/search/sql.py:digest_descent_sql", 1),),
    ),
    "server/memory/app/search/key_match.py:_run_key_match_pool._rows_for_ns": (
        0,
        (("input.org", "ns", "0 if component_only else 1"),),
        (("query_all", None), ("query_all", ("input.org", "ns", "0 if component_only else 1"))),
        (("server/memory/app/search/sql.py:key_match_sql", 1),),
    ),
    "server/memory/app/search/sql.py:anchor_pool_sql": (2, (), (), ()),
    "server/memory/app/search/sql.py:digest_descent_sql": (2, (), (), ()),
    "server/memory/app/search/sql.py:key_match_sql": (2, (), (), ()),
    "server/memory/app/session_outcomes.py:_NEAREST_POSITIVE_SQL": (1, (), (), ()),
    "server/memory/app/session_outcomes.py:_contrast_pair": (
        0,
        (("str(neg['org'])", "str(neg['namespace'])", "1"),),
        (("query_one", ("str(neg['org'])", "str(neg['namespace'])", "1")),),
        (("server/memory/app/session_outcomes.py:_NEAREST_POSITIVE_SQL", 1),),
    ),
    "server/memory/app/value_updates.py:_peers_joined": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/memory/app/value_updates.py:_peers_many": (
        0,
        (("org", "namespace", "len(items)"),),
        (("query_all", ("org", "namespace", "len(items)")),),
        (("server/memory/app/value_updates.py:_peers_sql_many", 1),),
    ),
    "server/memory/app/value_updates.py:_peers_sql_many": (1, (), (), ()),
    "server/memory/store/conflict_candidates.py:PINNED_PEERS_SQL": (1, (), (), ()),
    "server/memory/store/conflict_candidates.py:_peer_candidates_sql": (1, (), (), ()),
    "server/memory/store/conflict_candidates.py:candidate_sql_many": (1, (), (), ()),
    "server/memory/store/conflict_candidates.py:pinned_sql_many": (1, (), (), ()),
    "server/memory/store/conflict_resolver.py:_ANCHORED_SQL": (
        0,
        (),
        (),
        (("server/memory/store/conflict_resolver.py:_LIVE_VECTORS", 2),),
    ),
    "server/memory/store/conflict_resolver.py:_LIVE_VECTORS": (1, (), (), ()),
    "server/memory/store/conflict_resolver.py:_nearest_sql": (
        0,
        (),
        (),
        (("server/memory/store/conflict_resolver.py:_LIVE_VECTORS", 1),),
    ),
    "server/memory/store/conflict_resolver.py:cluster_peers": (
        0,
        (
            ("str(row['org'])", "str(row['namespace'])", "1"),
            ("str(row['org'])", "str(row['namespace'])", "2"),
        ),
        (
            ("query_all", ("str(row['org'])", "str(row['namespace'])", "1")),
            ("query_all", ("str(row['org'])", "str(row['namespace'])", "2")),
            ("query_all", None),
        ),
        (
            ("server/memory/store/conflict_resolver.py:_ANCHORED_SQL", 1),
            ("server/memory/store/conflict_resolver.py:_nearest_sql", 1),
        ),
    ),
    "server/memory/store/lookups.py:find_by_identifier": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")), ("query_all", None)),
        (),
    ),
    "server/memory/store/lookups.py:find_chunks_by_identifier": (
        1,
        (("org", "namespace", "1 if qv and order != 'recency' else 0"),),
        (("query_all", ("org", "namespace", "1 if qv and order != 'recency' else 0")),),
        (),
    ),
    "server/memory/store/lookups.py:find_chunks_by_session_date": (
        1,
        (("org", "namespace", "1 if qv else 0"),),
        (("query_all", ("org", "namespace", "1 if qv else 0")),),
        (),
    ),
    "server/tracker/store/embed.py:TrackerEmbedRepo.nearest": (
        1,
        (("org", "namespace", "1"),),
        (("query_all", ("org", "namespace", "1")),),
        (),
    ),
    "server/tracker/store/source/base.py:SourceBase._rows": (
        0,
        (("self.org", "self.namespace", "1 if vector else 0"),),
        (("query_rows", ("self.org", "self.namespace", "1 if vector else 0")),),
        (),
    ),
    "server/tracker/store/source/similarity.py:SimilarReads.similar_vector": (
        1,
        (),
        (("_rows", None),),
        (),
    ),
}

_BUILDERS = {
    "server/code/store/index/answers.py:_candidate_sql",
    "server/knowledge/store/source_candidates.py:statement",
    "server/memory/app/profile_search.py:PROFILE_VECTORS_SQL",
    "server/memory/app/retrieval/chunks.py:_chunk_candidates_sql",
    "server/memory/app/retrieval/chunks.py:_chunk_neighbor_sql",
    "server/memory/app/retrieval/chunks.py:_chunk_stage2_one_trip_sql",
    "server/memory/app/retrieval/fact_rank.py:_bm25_hybrid_rank_score",
    "server/memory/app/retrieval/fact_sql.py:_fact_distance_expr",
    "server/memory/app/retrieval/fact_sql.py:_fact_leg_sql",
    "server/memory/app/search/sql.py:anchor_pool_sql",
    "server/memory/app/search/sql.py:digest_descent_sql",
    "server/memory/app/search/sql.py:key_match_sql",
    "server/memory/app/session_outcomes.py:_NEAREST_POSITIVE_SQL",
    "server/memory/app/value_updates.py:_peers_sql_many",
    "server/memory/store/conflict_candidates.py:PINNED_PEERS_SQL",
    "server/memory/store/conflict_candidates.py:_peer_candidates_sql",
    "server/memory/store/conflict_candidates.py:candidate_sql_many",
    "server/memory/store/conflict_candidates.py:pinned_sql_many",
    "server/memory/store/conflict_resolver.py:_ANCHORED_SQL",
    "server/memory/store/conflict_resolver.py:_LIVE_VECTORS",
    "server/memory/store/conflict_resolver.py:_nearest_sql",
}


# Reviewed AST shapes of SQL producers only. A changed producer is reclassified
# with its dispatch weight evidence; these are not file/head/runtime guard pins.
_PRODUCER_SHAPES = {
    "server/code/store/index/answers.py:_candidate_sql": "cb7e26315031c8b75b101894e9892b1eab9a30e9eb1764de388a5b175ff7cb78",
    "server/knowledge/store/source_candidates.py:statement": "ca17d3458e5510b6a84c5fed87d29cc19059427acf663cbc3b1ac3a433269074",
    "server/memory/app/profile_search.py:PROFILE_VECTORS_SQL": "c7340eba552c3737748ab15fe9142b423a080d8824a2ca9808d91d87ac86a3f0",
    "server/memory/app/retrieval/chunks.py:_chunk_candidates_sql": "49fc7bd583581d70b0abb749ce6ea6991ec5a02de8bc07042f573ec01b5fcc16",
    "server/memory/app/retrieval/chunks.py:_chunk_neighbor_sql": "93e2052b713ee7af7db0491f2ee5f20422d5ec0fae55d070419e08c4301d8c77",
    "server/memory/app/retrieval/chunks.py:_chunk_stage2_one_trip_sql": "031697e9e97e8d455c4cfdc993a1425f6a6e1536dc1aeb75f23da4e255409736",
    "server/memory/app/retrieval/fact_rank.py:_bm25_hybrid_rank_score": "11cf2389dfea336dd908f3920a84d9fbd490f645a3b762417458ce81a372c207",
    "server/memory/app/retrieval/fact_sql.py:_fact_distance_expr": "43ac98d7a19379f9cf3135d6b4392b2b90f51a41ef0987baa97c5fe540d9efeb",
    "server/memory/app/retrieval/fact_sql.py:_fact_leg_sql": "116155d18336a59aac48e52bd18730b9c22299db1b5137e200b79074c294d535",
    "server/memory/app/search/sql.py:anchor_pool_sql": "36a63191e5c0706ddd50fe7d62cae2254c211beac62489b24edb1671bb016d82",
    "server/memory/app/search/sql.py:digest_descent_sql": "2ff4e7cfd2958f75cb233fd1e4c78e54374f379564a6d2fb55cdd2eb0eebf13d",
    "server/memory/app/search/sql.py:key_match_sql": "c6264d8d4b9a1c7c1c5120a01b10528442e1ec8cf7dd0e4587f331a63dbdbf02",
    "server/memory/app/session_outcomes.py:_NEAREST_POSITIVE_SQL": "643e1247cf3e1d2866ed8fa67e1f75d1f35a6064afbe791bbe965a1039e0c3e4",
    "server/memory/app/value_updates.py:_peers_sql_many": "e5363c90fe391dc78eaf22662bfbacc0c34091f74173f447df5889eb52a11adb",
    "server/memory/store/conflict_candidates.py:PINNED_PEERS_SQL": "a6bd82afd8cdc9296411f148141f474f530bdde4f77cd4cbf0b6bad4252083b0",
    "server/memory/store/conflict_candidates.py:_peer_candidates_sql": "de9d6da049bc73c8863d466aa1f6319a3911b05998c39975c052653699d8e14c",
    "server/memory/store/conflict_candidates.py:candidate_sql_many": "e9a0840b791ecbf3134fb856f187acd0b905d73b5ddaca790502e4169dc50b39",
    "server/memory/store/conflict_candidates.py:pinned_sql_many": "c57de290301feef0053752f6a24de6a2c333da33059f8fc716a59909be17fed4",
    "server/memory/store/conflict_resolver.py:_ANCHORED_SQL": "943c9482ad51a170a2bd1fab47a5bd9285bae9b66cb2bf41790bd43ff53e76be",
    "server/memory/store/conflict_resolver.py:_LIVE_VECTORS": "192a524488636780e2012d69a6ba4f4431c75fb3542fd0147b040a61988dcdf5",
    "server/memory/store/conflict_resolver.py:_nearest_sql": "3bba90c6c2f8ddafe6a9481852825a69ce9546b9f25987071c075cb59f898f4f",
}


def _name(node: ast.AST) -> str:
    return (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else ""
    )


def _marker(call: ast.Call) -> tuple[str, ...]:
    parameters = ("org", "namespaces", "weight")
    if len(call.args) > 3 or any(isinstance(arg, ast.Starred) for arg in call.args):
        raise ValueError("vector_work needs explicit org, namespaces and optional weight")
    bound = dict(zip(parameters, (ast.unparse(arg) for arg in call.args), strict=False))
    for keyword in call.keywords:
        if keyword.arg not in parameters or keyword.arg in bound:
            raise ValueError("vector_work has an unresolved or duplicate argument")
        bound[keyword.arg] = ast.unparse(keyword.value)
    if not {"org", "namespaces"} <= bound.keys():
        raise ValueError("vector_work is missing org or namespaces")
    return (bound["org"], bound["namespaces"], bound.get("weight", "1"))


def _producer_shape(node: ast.AST) -> str:
    """Reviewed SQL-producing expressions/bodies, independent of Python formatting.

    A body fingerprint also covers repeated local aliases or callee composition;
    reference counts alone cannot distinguish those shapes. This applies only to
    SQL producers, not whole files or row-returning consumers. SQL string contents
    remain significant. Function annotations and the leading docstring do not.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        node = ast.Module(body=body, type_ignores=[])
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


class _Inventory(ast.NodeVisitor):
    def __init__(self, path: str, builders: set[str]) -> None:
        self.path, self.builders = path, builders
        self.scope: list[str] = []
        self.context: tuple[str, ...] | None = None
        self.rows: dict[str, list] = {}
        self.imports: dict[tuple[str, str], str] = {}
        self.modules: dict[tuple[str, str], str] = {}
        self.forwarding: dict[tuple[str, str, str], list[str]] = {}
        self.values: dict[str, set[str]] = {}
        self.producers: set[str] = set()
        self.shapes: dict[str, str] = {}

    def _owner(self) -> str:
        return self.path + ":" + ".".join(self.scope)

    def _row(self) -> list:
        return self.rows.setdefault(self._owner(), [0, [], [], Counter()])

    def visit_Expr(self, node: ast.Expr) -> None:
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            self.generic_visit(node)

    def _scope(self, name: str, body: list[ast.stmt]) -> None:
        old_context = self.context
        self.scope.append(name)
        self.context = None
        for child in body:
            self.visit(child)
        self.scope.pop()
        self.context = old_context

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.shapes[self._owner() + ("." if self.scope else "") + node.name] = _producer_shape(node)
        self._scope(node.name, node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.shapes[self._owner() + ("." if self.scope else "") + node.name] = _producer_shape(node)
        self._scope(node.name, node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node.name, node.body)

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.scope and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self.scope.append(node.targets[0].id)
            self.shapes[self._owner()] = _producer_shape(node.value)
            self.visit(node.value)
            if self._sql(node.value):
                self.producers.add(self._owner())
            self.scope.pop()
        else:
            self.generic_visit(node)
            if self._sql(node.value):
                for target in node.targets:
                    self.values.setdefault(self._owner(), set()).update(
                        name.id for name in ast.walk(target) if isinstance(name, ast.Name)
                    )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.scope and isinstance(node.target, ast.Name) and node.value is not None:
            self.scope.append(node.target.id)
            self.shapes[self._owner()] = _producer_shape(node.value)
            self.visit(node.value)
            if self._sql(node.value):
                self.producers.add(self._owner())
            self.scope.pop()
        else:
            self.generic_visit(node)
            if (
                node.value is not None
                and self._sql(node.value)
                and isinstance(node.target, ast.Name)
            ):
                self.values.setdefault(self._owner(), set()).add(node.target.id)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None and self._sql(node.value):
            self.producers.add(self._owner())
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            parent = self.path.removesuffix(".py").split("/")[: -node.level]
            module = ".".join([*parent, *([module] if module else [])])
        for alias in node.names:
            self.imports[(".".join(self.scope), alias.asname or alias.name)] = (
                module.replace(".", "/") + ".py:" + alias.name
            )
            self.modules[(".".join(self.scope), alias.asname or alias.name)] = (
                module + "." + alias.name
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.modules[(".".join(self.scope), alias.asname or alias.name.split(".")[0])] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def _attribute(self, node: ast.Attribute) -> str | None:
        parts = []
        root = node
        while isinstance(root, ast.Attribute):
            parts.insert(0, root.attr)
            root = root.value
        if isinstance(root, ast.Name):
            for length in range(len(self.scope), -1, -1):
                module = self.modules.get((".".join(self.scope[:length]), root.id))
                if module is None:
                    continue
                names = [*module.split("."), *parts]
                for split in range(1, len(names)):
                    target = "/".join(names[:split]) + ".py:" + ".".join(names[split:])
                    if target in self.builders:
                        return target
                break
        return None

    def _binding(self, node: ast.Name) -> str | None:
        for length in range(len(self.scope), -1, -1):
            prefix = ".".join(self.scope[:length])
            target = self.imports.get((prefix, node.id))
            local = self.path + ":" + ".".join([*self.scope[:length], node.id])
            if target in self.builders or local in self.builders:
                return target if target in self.builders else local
        return None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (target := self._attribute(node)) is not None:
            self._row()[3][target] += 1
        else:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and (target := self._binding(node)) is not None:
            self._row()[3][target] += 1

    def _sql(self, node: ast.AST) -> bool:
        """Conservative named SQL outputs, not the row results of query calls.

        Local aliases only grow within their lexical owner. Arbitrary callees do
        not inherit SQL from arguments; known SQL-producing wrappers do, after
        fixed-point discovery. This is not general interprocedural data flow.
        """
        if isinstance(node, ast.Name):
            return (
                node.id in self.values.get(self._owner(), set()) or self._binding(node) is not None
            )
        if isinstance(node, ast.Attribute):
            return self._attribute(node) is not None
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and _DISTANCE.search(node.value) is not None
        if isinstance(node, ast.Call):
            return self._sql(node.func)
        if isinstance(node, ast.Await | ast.FormattedValue | ast.NamedExpr):
            return self._sql(node.value)
        if isinstance(node, ast.Subscript):
            return self._sql(node.value)
        if isinstance(node, ast.BinOp):
            return self._sql(node.left) or self._sql(node.right)
        if isinstance(node, ast.IfExp):
            return self._sql(node.body) or self._sql(node.orelse)
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            return any(self._sql(value) for value in node.elts)
        if isinstance(node, ast.JoinedStr | ast.BoolOp):
            return any(self._sql(value) for value in node.values)
        if isinstance(node, ast.Dict):
            return any(self._sql(value) for value in node.values)
        return False

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _DISTANCE.search(node.value):
            self._row()[0] += 1

    def visit_With(self, node: ast.With) -> None:
        old_context = self.context
        for item in node.items:
            self.visit(item.context_expr)
            if (
                isinstance(item.context_expr, ast.Call)
                and _name(item.context_expr.func) == "vector_work"
            ):
                self.context = _marker(item.context_expr)
        for child in node.body:
            self.visit(child)
        self.context = old_context

    def visit_Call(self, node: ast.Call) -> None:
        name = _name(node.func)
        if name == "vector_work":
            self._row()[1].append(_marker(node))
        # The core transport executes arbitrary passed SQL. Its zero-weight
        # schema/consumer clears are classified, but its driver calls are not
        # first-party SQL builders and must not acquire another reservation.
        if name in _SINKS and self.path != "server/core/ch.py":
            self._row()[2].append((name, self.context))
        for kw in node.keywords:
            if kw.arg in {"vector", "weight"}:
                self.forwarding.setdefault((self._owner(), name, kw.arg), []).append(
                    ast.unparse(kw.value)
                )
        self.generic_visit(node)


def _unclassified(
    sources: dict[str, str],
    classified: dict,
    builders: set[str],
    producer_shapes: dict | None = None,
) -> list[str]:
    trees = {path: ast.parse(source, filename=path) for path, source in sources.items()}
    builders = set(builders)
    while True:
        visitors = []
        for path, tree in trees.items():
            visitor = _Inventory(path, builders)
            visitor.visit(tree)
            visitors.append(visitor)
        expanded = builders | {owner for visitor in visitors for owner in visitor.producers}
        if expanded == builders:
            break
        builders = expanded
    observed = {}
    for visitor in visitors:
        for owner, (literals, markers, queries, refs) in visitor.rows.items():
            if literals or markers or refs:
                observed[owner] = (
                    literals,
                    tuple(markers),
                    tuple(queries),
                    tuple(sorted(refs.items())),
                )
    failures = [
        f"{owner}: expected {classified.get(owner)!r}, found {observed.get(owner)!r}"
        for owner in sorted(classified.keys() | observed.keys())
        if classified.get(owner) != observed.get(owner)
    ]
    if producer_shapes is not None:
        actual_shapes = {
            owner: shape
            for visitor in visitors
            for owner, shape in visitor.shapes.items()
            if owner in builders
        }
        failures.extend(
            f"{owner}: SQL producer shape needs classification"
            for owner in sorted(producer_shapes.keys() | actual_shapes.keys())
            if producer_shapes.get(owner) != actual_shapes.get(owner)
        )
    return failures


def test_connect_metadata_marker_has_a_scalar_classification() -> None:
    path = "server/core/ch.py"
    root = Path(__file__).resolve().parents[2]
    tree = ast.parse((root / path).read_text())
    client = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CHClient"
    )
    client.body = [
        node
        for node in client.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "connect"
    ]
    assert len(client.body) == 1
    owner = path + ":CHClient.connect"
    classified = {name: row for name, row in _CLASSIFIED.items() if name == owner}
    failures = _unclassified({path: ast.unparse(client)}, classified, set())
    assert not failures, "\n".join(failures)


def test_first_party_vector_dispatches_have_a_classification() -> None:
    """Whole source contract: run in CI, not the local small focused cohort."""
    root = Path(__file__).resolve().parents[2]
    # No source walk at import/collection time. A new first-party Python module
    # is included even when it is outside today's literal-hit directories.
    sources = {
        str(path.relative_to(root)): path.read_text()
        for path in sorted((root / "server").rglob("*.py"))
    }
    assert not (failures := _unclassified(sources, _CLASSIFIED, _BUILDERS, _PRODUCER_SHAPES)), (
        "\n".join(failures)
    )
    forwarding = {}
    for path in (
        "server/tracker/store/source/similarity.py",
        "server/memory/app/retrieval/search.py",
    ):
        visitor = _Inventory(path, _BUILDERS)
        visitor.visit(ast.parse(sources[path]))
        forwarding.update(visitor.forwarding)
    assert forwarding[
        ("server/tracker/store/source/similarity.py:SimilarReads.similar_vector", "_rows", "vector")
    ] == ["True"]
    assert forwarding[
        (
            "server/memory/app/retrieval/search.py:_fact_rows_task",
            "_query_all_retrying_empty_block",
            "weight",
        )
    ] == ["weight"]


def test_new_builder_wrapper_and_relative_dispatch_need_classification() -> None:
    sources = {
        "server/new.py": 'def sql():\n    return "SELECT cosineDistance(v, q)"\n',
        "server/wrapper.py": "from server.known import sql\ndef wrap():\n    fragment = sql()\n    return fragment\n",
        "server/final.py": "from server.wrapper import wrap\nasync def read(ch):\n    return await ch.query_all(wrap())\n",
        "server/pkg/relative.py": "from . import known as sql\nasync def read(ch):\n    return await ch.query_all(sql.fragment)\n",
        "server/pkg/nested/relative.py": "from .. import known as sql\nasync def read(ch):\n    return await ch.query_all(sql.fragment)\n",
    }
    failures = _unclassified(sources, {}, {"server/known.py:sql", "server/pkg/known.py:fragment"})
    assert len(failures) == 5
    for owner in (
        "server/new.py:sql",
        "server/wrapper.py:wrap",
        "server/final.py:read",
        "server/pkg/relative.py:read",
        "server/pkg/nested/relative.py:read",
    ):
        assert any(message.startswith(owner + ":") for message in failures), owner
    # A real query's row result must not turn its callers into SQL builders.
    row_sources = {
        "server/rows.py": "from server.known import sql\nasync def read(ch):\n    return await ch.query_all(sql())\n",
        "server/caller.py": "from server.rows import read\nasync def handle(ch):\n    return await read(ch)\n",
    }
    failures = _unclassified(row_sources, {}, {"server/known.py:sql"})
    assert len(failures) == 1
    assert failures[0].startswith("server/rows.py:read:")


def test_changed_dispatch_weight_is_rejected_without_adding_a_read() -> None:
    source = """from server.known import sql
async def read(ch, org, ns):
    with vector_work(org, ns, 1):
        return await ch.query_all(sql())
"""
    expected = {
        "server/caller.py:read": (
            0,
            (("org", "ns", "1"),),
            (("query_all", ("org", "ns", "1")),),
            (("server/known.py:sql", 1),),
        )
    }
    assert _unclassified({"server/caller.py": source}, expected, {"server/known.py:sql"}) == []
    failures = _unclassified(
        {"server/caller.py": source.replace("ns, 1", "ns, 2")}, expected, {"server/known.py:sql"}
    )
    assert len(failures) == 1
    assert "('org', 'ns', '2')" in failures[0]


def test_comments_scalar_analyst_sql_and_classified_conditional_read_are_distinct() -> None:
    source = '''# cosineDistance(embedding, q) is a comment.
def scalar():
    """cosineDistance is mentioned in documentation only."""
    return "SELECT count()"
async def analyst(ch, supplied_sql):
    return await ch.query_all(supplied_sql)
async def lookup(ch, org, ns, vector):
    sql = "SELECT cosineDistance(embedding, q)" if vector else "SELECT key"
    with vector_work(org, ns, 1 if vector else 0):
        return await ch.query_all(sql)
'''
    expected = {
        "server/example.py:lookup": (
            1,
            (("org", "ns", "1 if vector else 0"),),
            (("query_all", ("org", "ns", "1 if vector else 0")),),
            (),
        )
    }
    assert _unclassified({"server/example.py": source}, expected, set()) == []


def test_an_extra_fragment_branch_is_rejected_with_unchanged_dispatch_weight() -> None:
    source = """_BASE = "SELECT cosineDistance(v, q) AS distance FROM vectors"
_ANCHORED = _BASE + " UNION ALL " + _BASE
async def read(ch, org, ns):
    with vector_work(org, ns, 2):
        return await ch.query_all(_ANCHORED)
"""
    base = "server/example.py:_BASE"
    anchored = "server/example.py:_ANCHORED"
    expected = {
        base: (1, (), (), ()),
        anchored: (0, (), (), ((base, 2),)),
        "server/example.py:read": (
            0,
            (("org", "ns", "2"),),
            (("query_all", ("org", "ns", "2")),),
            ((anchored, 1),),
        ),
    }
    assert _unclassified({"server/example.py": source}, expected, {base}) == []
    third = source.replace(
        '_ANCHORED = _BASE + " UNION ALL " + _BASE',
        '_ANCHORED = _BASE + " UNION ALL " + _BASE + " UNION ALL " + _BASE',
    )
    failures = _unclassified({"server/example.py": third}, expected, {base})
    assert len(failures) == 1
    assert failures[0].startswith(anchored + ":")
    assert "_BASE', 3)" in failures[0]

    # Ordinary alias composition has only one direct _BASE reference. Its third
    # branch must still fail while the actual dispatch marker remains weight 2.
    alias_source = '_BASE = "SELECT cosineDistance(v, q) AS distance FROM vectors"\ndef wrap():\n    part = _BASE\n    return part + " UNION ALL " + part\nasync def read(ch, org, ns):\n    with vector_work(org, ns, 2):\n        return await ch.query_all(wrap())\n'
    wrapped = "server/example.py:wrap"
    alias_expected = {
        base: (1, (), (), ()),
        wrapped: (0, (), (), ((base, 1),)),
        "server/example.py:read": (
            0,
            (("org", "ns", "2"),),
            (("query_all", ("org", "ns", "2")),),
            ((wrapped, 1),),
        ),
    }
    alias_shapes = {
        "server/example.py:_BASE": "74b97265f930b608d0ae6e1ea7cb097aed174596182bbecebb8867f6db334ff5",
        "server/example.py:wrap": "a195b6fe77e1a647c15223306fcdd9c4d55f0220eb1303c0b128e7d4dc98c73c",
    }
    assert (
        _unclassified({"server/example.py": alias_source}, alias_expected, {base}, alias_shapes)
        == []
    )
    formatted_alias = alias_source.replace(
        "    part = _BASE", "    # Same SQL producer.\n    part = (_BASE)"
    )
    assert (
        _unclassified({"server/example.py": formatted_alias}, alias_expected, {base}, alias_shapes)
        == []
    )
    third_alias = alias_source.replace(
        'return part + " UNION ALL " + part',
        'return part + " UNION ALL " + part + " UNION ALL " + part',
    )
    failures = _unclassified(
        {"server/example.py": third_alias}, alias_expected, {base}, alias_shapes
    )
    assert failures == [wrapped + ": SQL producer shape needs classification"]


def test_marker_keywords_bind_identity_scope_weight_and_reject_unresolved_arguments() -> None:
    import pytest

    for call in (
        "vector_work(org, ns, 2)",
        "vector_work(org, namespaces=ns, weight=2)",
        "vector_work(org=org, namespaces=ns, weight=2)",
        "vector_work(weight=2, namespaces=ns, org=org)",
    ):
        assert _marker(ast.parse(call, mode="eval").body) == ("org", "ns", "2")
    assert _marker(ast.parse("vector_work(org=org, namespaces=ns)", mode="eval").body) == (
        "org",
        "ns",
        "1",
    )
    for call in (
        "vector_work(*args)",
        "vector_work(**kwargs)",
        "vector_work(org, ns, weight=2, org=other)",
        "vector_work(namespaces=ns)",
        "vector_work(org, ns, 2, 3)",
        "vector_work(org, ns, unknown=2)",
    ):
        with pytest.raises(ValueError):
            _marker(ast.parse(call, mode="eval").body)
    source = "from server.known import sql\nasync def read(ch, org, ns):\n    with vector_work(org=org, namespaces=ns, weight=2):\n        return await ch.query_all(sql())\n"
    expected = {
        "server/caller.py:read": (
            0,
            (("org", "ns", "2"),),
            (("query_all", ("org", "ns", "2")),),
            (("server/known.py:sql", 1),),
        )
    }
    assert _unclassified({"server/caller.py": source}, expected, {"server/known.py:sql"}) == []
    failures = _unclassified(
        {"server/caller.py": source.replace("namespaces=ns", "namespaces=other")},
        expected,
        {"server/known.py:sql"},
    )
    assert len(failures) == 1
    assert "('org', 'other', '2')" in failures[0]


def test_added_unmarked_read_and_nested_context_scope_are_checked_independently() -> None:
    source = """from server.known import sql
async def read(ch, org, ns):
    with vector_work(org, ns):
        await ch.query_all(sql())
        with vector_work(org, ns, 2):
            await ch.query_all(sql())
        await ch.query_all(sql())
        async def child():
            with vector_work(org, ns, 3):
                return await ch.query_all(sql())
"""
    reference = (("server/known.py:sql", 3),)
    expected = {
        "server/caller.py:read": (
            0,
            (("org", "ns", "1"), ("org", "ns", "2")),
            (
                ("query_all", ("org", "ns", "1")),
                ("query_all", ("org", "ns", "2")),
                ("query_all", ("org", "ns", "1")),
            ),
            reference,
        ),
        "server/caller.py:read.child": (
            0,
            (("org", "ns", "3"),),
            (("query_all", ("org", "ns", "3")),),
            (("server/known.py:sql", 1),),
        ),
    }
    assert _unclassified({"server/caller.py": source}, expected, {"server/known.py:sql"}) == []
    failures = _unclassified(
        {"server/caller.py": source + "    await ch.query_all(sql())\n"},
        expected,
        {"server/known.py:sql"},
    )
    assert len(failures) == 1
    assert "('query_all', None)" in failures[0]
    unmarked_child = source.replace(
        "            with vector_work(org, ns, 3):\n                return", "            return"
    )
    failures = _unclassified(
        {"server/caller.py": unmarked_child}, expected, {"server/known.py:sql"}
    )
    assert len(failures) == 1
    assert failures[0].startswith("server/caller.py:read.child:")
    assert "('query_all', None)" in failures[0]
