"""The coded vector layout as the default for a fresh install.

Existing tables move onto `Array(BFloat16) CODEC(Quantized('rabitq', dim, 0))`
with `scripts/ops/vector_codec_rebuild.py`; a fresh install would otherwise
create them as `CODEC(NONE)` (the schema declares `Array(Float32)` or
bf16), because the codec cannot be added by ALTER and a server
before 26.8 does not know it. The migration runner therefore rewrites, at
apply time and only when this user can have the codec on (`CodecSettings`):

- in a `CREATE TABLE` of a coded table, each vector column's declaration to
  `Array(BFloat16)[ DEFAULT …] CODEC(Quantized('rabitq', <dim>, 0)[, LZ4])`,
  without the `, LZ4` where `allow_suspicious_codecs` cannot be on;
- any `ALTER TABLE <coded table> MODIFY COLUMN <vector column> …` that
  restates the type to a no-op, since the column already has its final shape
  and ClickHouse rejects a restatement of a coded column (code 524).

The migration files themselves stay byte-identical (their checksums are
recorded), and an install on a server without the codec gets the plain layout.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Gates the Quantized codec on ClickHouse 26.8+.
CODEC_SETTING = "enable_quantized_codec"
# Lets a general codec follow Quantized in one CODEC(...) (`entity_vectors`' LZ4).
CHAIN_SETTING = "allow_suspicious_codecs"
# Gated the codec on 26.7; only the provider rebuild still accepts it.
EXPERIMENTAL_CODEC_SETTING = "allow_experimental_codecs"

# Read before a coded statement is sent: a setting goes only where this user may turn it on.
CODEC_SETTINGS_SQL = (
    "SELECT name, value, readonly, min, max, disallowed_values FROM system.settings "
    f"WHERE name IN ('{CODEC_SETTING}', '{CHAIN_SETTING}', '{EXPERIMENTAL_CODEC_SETTING}')"
)
_COLUMNS = ("name", "value", "readonly", "min", "max", "disallowed_values")
_ON = ("1", "true")


def _number(bound: Any) -> float:
    word = str(bound).strip().lower()
    return 1.0 if word == "true" else 0.0 if word == "false" else float(word)


def _allows_one(low: Any, high: Any, disallowed: Iterable[Any] | None) -> bool:
    """Whether a setting's constraints let it be set to 1: `min` <= 1 <= `max`, and 1
    not among `disallowed_values`. A bound that is not a number counts as refusing."""
    try:
        if (low is not None and _number(low) > 1) or (high is not None and _number(high) < 1):
            return False
    except ValueError:
        return False
    return not any(str(value).strip().lower() in _ON for value in disallowed or ())


@dataclass(frozen=True)
class CodecSettings:
    """The codec settings `system.settings` lists for this user (`CODEC_SETTINGS_SQL`),
    as name -> (on, settable). Each one is:

    - settable: this user may set it to 1 (`readonly = 0`, 1 within `min` / `max`, 1 not
      in `disallowed_values`); sent with the statement that needs it;
    - held at 1: on already, and never sent (it needs no change);
    - held at 0, or absent: it cannot be on. Without `enable_quantized_codec` vector
      tables are created plain; without `allow_suspicious_codecs` a general codec
      chained after Quantized is dropped (`statement`).

    `readonly` is 1 only under a `<readonly/>` (`<const/>`) constraint; `<max>0</max>`
    or `<disallowed>1</disallowed>` holds a setting the same way with `readonly = 0`.
    ClickHouse Cloud holds both read-only: `enable_quantized_codec` stays at its default
    0 until the operator enables it for the service, `allow_suspicious_codecs` at 0."""

    states: Mapping[str, tuple[bool, bool]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows: Iterable[Sequence[Any] | Mapping[str, Any]]) -> CodecSettings:
        """From `CODEC_SETTINGS_SQL`'s rows, as tuples or as dicts."""
        states: dict[str, tuple[bool, bool]] = {}
        for row in rows:
            name, value, readonly, low, high, disallowed = (
                tuple(row[column] for column in _COLUMNS) if isinstance(row, Mapping) else row
            )
            on = str(value).strip().lower() in _ON
            settable = not int(readonly or 0) and _allows_one(low, high, disallowed)
            states[str(name)] = (on, settable)
        return cls(states)

    def listed(self, name: str) -> bool:
        return name in self.states

    def settable(self, name: str) -> bool:
        """This user may set `name` to 1."""
        state = self.states.get(name)
        return state is not None and state[1]

    def usable(self, name: str) -> bool:
        """Settable, or held at 1."""
        state = self.states.get(name)
        return state is not None and (state[1] or state[0])

    @property
    def codec(self) -> bool:
        """A coded column can be created here."""
        return self.usable(CODEC_SETTING)

    @property
    def chain(self) -> bool:
        """A general codec can follow Quantized."""
        return self.usable(CHAIN_SETTING)

    def statement(self, stmt: str) -> tuple[str, dict[str, int]]:
        """`stmt` as this server can run it, and the settings to send with it: the
        general codec after Quantized is dropped where `chain` is false."""
        if not self.chain:
            stmt = drop_chains(stmt)
        return stmt, statement_settings(stmt, self)


# A server that lists none of them: no codec, nothing sent.
NO_CODEC = CodecSettings()


# table -> (vector columns, general codec chained after the codes or None)
CODED_TABLES: dict[str, tuple[tuple[str, ...], str | None]] = {
    "memory_embeddings": (("embedding", "memory_key_embedding"), None),
    "memory_chunks": (("embedding",), None),
    "code_chunks": (("embedding",), None),
    # repeated vectors: LZ4 compresses them ~4x; dropped where `CodecSettings.chain` is false
    "entity_vectors": (("entity_embedding",), "LZ4"),
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


_WIDTH = re.compile(r"Quantized\([^,]*,\s*(\d+)")


def codec_width(codec: str) -> int | None:
    """The vector width a column's `compression_codec` fixes; None when not coded."""
    found = _WIDTH.search(codec)
    return int(found.group(1)) if found else None


def creates_coded_table(sql: str) -> bool:
    """Whether `sql` creates a table whose vector columns get the codec."""
    return any(m.group(1) in CODED_TABLES for m in _CREATE_RE.finditer(sql))


def codable(dim: int) -> bool:
    """Whether the codec takes `dim`: RaBitQ packs one bit per dimension into whole bytes."""
    return dim > 0 and dim % 8 == 0


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


def statement_settings(stmt: str, codec: CodecSettings) -> dict[str, int]:
    """Per-statement settings a coded CREATE needs on ClickHouse >= 26.8, each
    only where `codec` says this user may set it to 1: a held one is on already,
    or it cannot be turned on (code 452).

    The server asks for `allow_suspicious_codecs` only when a general codec is
    chained behind the codes, `CODEC(Quantized(...), <general>)`; a bare
    Quantized column needs `enable_quantized_codec` alone."""
    if "Quantized(" not in stmt:
        return {}
    wanted = [CODEC_SETTING, *([CHAIN_SETTING] if _CHAINED_RE.search(stmt) else [])]
    return {name: 1 for name in wanted if codec.settable(name)}


def drop_chains(stmt: str) -> str:
    """`stmt` with each general codec after Quantized removed:
    `CODEC(Quantized('rabitq', 1024, 0), LZ4)` becomes `CODEC(Quantized('rabitq', 1024, 0))`.
    Only the compression of the stored codes changes; without
    `allow_suspicious_codecs` the server refuses the chain (code 36)."""
    return _CHAIN_RE.sub(r"\1)", stmt)


_CHAINED_RE = re.compile(r"CODEC\(\s*Quantized\([^)]*\)\s*,", re.I)
# The codes, then the chain up to CODEC's closing parenthesis (one level of nesting: ZSTD(3)).
_CHAIN_RE = re.compile(r"(CODEC\(\s*Quantized\([^)]*\))\s*,(?:[^()]|\([^()]*\))*\)", re.I)
