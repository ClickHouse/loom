"""The coded vector layout as the default for a fresh install.

Existing tables move onto `Array(BFloat16) CODEC(Quantized('rabitq', dim, 0))`
with `scripts/ops/vector_codec_rebuild.py`; a fresh install would otherwise
create them as `CODEC(NONE)` (the schema declares `Array(Float32)` or
bf16), because the codec cannot be added by ALTER and a server
before 26.8 does not know it. The migration runner therefore rewrites, at
apply time and only when the server supports the codec:

- in a `CREATE TABLE` of a coded table, each vector column's declaration to
  `Array(BFloat16)[ DEFAULT …] CODEC(Quantized('rabitq', <dim>, 0)[, LZ4])`;
- any `ALTER TABLE <coded table> MODIFY COLUMN <vector column> …` that
  restates the type to a no-op, since the column already has its final shape
  and ClickHouse rejects a restatement of a coded column (code 524).

The migration files themselves stay byte-identical (their checksums are
recorded), and an install on a server without the codec gets the plain layout.
"""

from __future__ import annotations

import re

# table -> (vector columns, general codec chained after the codes or None)
CODED_TABLES: dict[str, tuple[tuple[str, ...], str | None]] = {
    "memory_embeddings": (("embedding", "memory_key_embedding"), None),
    "memory_chunks": (("embedding",), None),
    "code_chunks": (("embedding",), None),
    "entity_vectors": (("entity_embedding",), "LZ4"),  # repeated vectors: LZ4 compresses them ~4x
    "session_summaries": (("summary_embedding",), None),
    "conversation_embeddings": (("embedding",), None),
    "chunks": (("embedding",), None),
    "entity_summaries": (("summary_embedding",), None),
    "code_answers": (("question_embedding",), None),
    "categories": (("embedding",), None),
    "code_summaries": (("embedding",), None),
    "qa_item_embeddings": (("embedding",), None),
}

_CREATE_RE = re.compile(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(?:loom\.)?`?(\w+)`?", re.I)
_ALTER_RE = re.compile(r"ALTER TABLE\s+(?:loom\.)?`?(\w+)`?", re.I)


def codec_clause(dim: int, chain: str | None) -> str:
    inner = f"Quantized('rabitq', {int(dim)}, 0)" + (f", {chain}" if chain else "")
    return f"CODEC({inner})"


def rewrite_statement(stmt: str, dim: int) -> str | None:
    """The statement to run instead of `stmt` — `stmt` itself when it does
    not touch a coded table's vector column, `None` when it must be skipped."""
    m = _CREATE_RE.search(stmt)
    if m and m.group(1) in CODED_TABLES:
        cols, chain = CODED_TABLES[m.group(1)]
        out = stmt
        for col in cols:
            pat = re.compile(
                rf"`{re.escape(col)}`\s+Array\((?:Float32|BFloat16)\)((?:\s+DEFAULT\s+[^,\n]*?)?)(?:\s+CODEC\([^)]*\))?(?=\s*,|\s*\n|\s*\))"
            )
            replacement = f"`{col}` Array(BFloat16)\\1 {codec_clause(dim, chain)}"
            out = pat.sub(replacement, out, count=1)
        return out
    m = _ALTER_RE.search(stmt)
    if m and m.group(1) in CODED_TABLES:
        cols, _ = CODED_TABLES[m.group(1)]
        for col in cols:
            if re.search(rf"MODIFY COLUMN\s+`?{re.escape(col)}`?\s+Array\(", stmt, re.I):
                return None
    return stmt


def statement_settings(stmt: str) -> dict[str, int]:
    """Per-statement settings a coded CREATE needs on ClickHouse >= 26.8.

    `allow_suspicious_codecs` is READONLY on ClickHouse Cloud (the driver rejects
    the statement outright), and the server only asks for
    it when a general codec is chained behind the codes. So it is added only
    for `CODEC(Quantized(...), <general>)`; a bare Quantized column gets
    `enable_quantized_codec` alone."""
    if "Quantized(" not in stmt:
        return {}
    out = {"enable_quantized_codec": 1}
    if _CHAINED_RE.search(stmt):
        out["allow_suspicious_codecs"] = 1
    return out


_CHAINED_RE = re.compile(r"CODEC\(\s*Quantized\([^)]*\)\s*,", re.I)
