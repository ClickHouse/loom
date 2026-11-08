"""Statements over loom.code_answers, code.ask's answer cache: the nearest live answer
entries of a question, the exact question's cached abstain, an entry by id, and the
file hashes an entry is verified against.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase


def _candidate_sql() -> str:
    from server.core.vectors.search_settings import vector_search_settings

    # question_embedding carries the same codec as code_chunks.embedding; the settings
    # fragment turns the scan codes-first. No key filter on purpose: a close entry under
    # another key is reported as key_mismatch, not as a plain miss.
    return """
        SELECT org, namespace, question_id, question, embedding_model, answer,
               citations, cited_paths, cited_hashes, llm_model,
               retrieval_confidence, indexed_commit, cache_key, prompt_version,
               entry_kind, abstain_reason, retrieval_paths, retrieval_hashes,
               cost_usd, latency_ms, reused_from, memory_namespace, memory_keys,
               created_at,
               dateDiff('second', created_at, now()) AS age_s,
               cosineDistance(question_embedding, {q:Array(Float32)}) AS distance
        FROM loom.code_answers FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND embedding_model = {model:String}
          AND tombstoned_at IS NULL AND answer != '' AND entry_kind = 'answer'
        ORDER BY distance ASC
        LIMIT {k:UInt32}
        """ + vector_search_settings(table="loom.code_answers", column="question_embedding")


class AnswersMixin(CodeRepoBase):
    """code.ask's answer cache."""

    async def nearest_answers(
        self, org: str, ns: str, model: str, qvec: list[float], *, k: int
    ) -> list[dict[str, Any]]:
        """The `k` live answer entries of (org, ns, model) nearest to `qvec`, nearest
        first, with their `distance` and `age_s`."""
        rows = await self._ch.query_all(
            _candidate_sql(), {"org": org, "ns": ns, "model": model, "q": qvec, "k": k}
        )
        return list(rows or [])

    async def negative_answer(
        self, org: str, ns: str, model: str, question_id: str
    ) -> dict[str, Any] | None:
        """The live cached abstain under `question_id` (question, reason, commit, age)."""
        row: dict[str, Any] | None = await self._ch.query_one(
            """
            SELECT question, abstain_reason, indexed_commit,
                   dateDiff('second', created_at, now()) AS age_s
            FROM loom.code_answers FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND embedding_model = {model:String} AND question_id = {qid:UUID}
              AND tombstoned_at IS NULL AND entry_kind = 'negative'
            LIMIT 1
            """,
            {"org": org, "ns": ns, "model": model, "qid": question_id},
        )
        return row

    async def live_answer_entries(
        self, org: str, ns: str, question_id: str
    ) -> list[dict[str, Any]]:
        """The live rows under `question_id`, every column but the vector (a tombstone
        mirrors the row)."""
        rows = await self._ch.query_all(
            """
            SELECT * EXCEPT (question_embedding) FROM loom.code_answers FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND question_id = {qid:UUID} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "qid": question_id},
        )
        return list(rows or [])

    async def file_hash_rows(self, org: str, ns: str, paths: list[str]) -> list[dict[str, Any]]:
        """(`path`, `content_hash`) of the live `code_files` rows of `paths` (at most
        100), the stored hash as the table returns it."""
        rows = await self._ch.query_all(
            """
            SELECT path, content_hash FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "paths": paths[:100]},
        )
        return list(rows or [])
