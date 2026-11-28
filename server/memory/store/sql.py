"""Statement fragments and the read-your-writes switch shared by the reads
and writes of the package: the served-value expression, the full-row projection
over loom.memory FINAL, the sequential-consistency suffix, and the identity-key
tuple type.
"""

from __future__ import annotations

import sys
import unicodedata
from typing import Any

# The value memory.get serves: the native JSON column when populated, else the stored text.
SERVED_VALUE = "if(length(toString(value_json)) > 2, toString(value_json), value)"
# The file projection also names its display column `value`. Read the stored column here.
_PROCEDURE_BODY = SERVED_VALUE.replace(", value)", ", memory.value)")


# Preserve short scalar values for the classifier without transferring arbitrary JSON.
# not a setting: the classifier reads scalars only, a wider field is substituted
PROCEDURE_FIELD_MAX_BYTES = 128
# not a setting: mirrors Python's own int() digit limit
PROCEDURE_TOTAL_MAX_DIGITS = sys.get_int_max_str_digits()

# Normalize the characters Python accepts in integer strings before checking the
# ASCII grammar. ASCII control characters accepted by str.isspace() can fail int().
_DECIMAL_DIGITS = tuple(ch for ch in map(chr, range(128, sys.maxunicode + 1)) if ch.isdecimal())
_INTEGER_WHITESPACE = "\t\n\v\f\r" + "".join(
    ch for ch in map(chr, range(128, 0x3001)) if ch.isspace()
)
PROCEDURE_INTEGER_SOURCE_HEX = ("".join(_DECIMAL_DIGITS) + _INTEGER_WHITESPACE).encode().hex()
PROCEDURE_INTEGER_TARGET = "".join(
    str(unicodedata.decimal(ch)) for ch in _DECIMAL_DIGITS
) + " " * len(_INTEGER_WHITESPACE)


def _procedure_field(path: str, oversized: str) -> str:
    raw = f"JSONExtractRaw({_PROCEDURE_BODY}, {path})"
    integer = (
        f"trimBoth(translateUTF8(JSONExtractString({raw}), "
        f"unhex('{PROCEDURE_INTEGER_SOURCE_HEX}'), '{PROCEDURE_INTEGER_TARGET}'))"
    )
    substitute = oversized.format(raw=raw, string=integer)
    return (
        f"if(length({raw}) > {PROCEDURE_FIELD_MAX_BYTES}, {substitute}, "
        f"ifNull(nullIf({raw}, ''), 'null'))"
    )


_PROCEDURE_TEMPLATE = _procedure_field(
    "'template'", "if(toFloat64OrNull({raw}) = 0, 'false', 'true')"
)
_PROCEDURE_TOTAL = _procedure_field(
    "'run_stats', 'total'",
    f"""if(isNotNull(toFloat64OrNull({{raw}})),
        if(toFloat64OrNull({{raw}}) >= 1, '1', '0'),
        arrayMap(digits -> if(
            match(digits, '^[+-]?[0-9]+(_[0-9]+)*$')
                AND ({PROCEDURE_TOTAL_MAX_DIGITS} = 0 OR
                    length(replaceRegexpAll(digits, '[+_-]', '')) <= {PROCEDURE_TOTAL_MAX_DIGITS}),
            if(NOT startsWith(digits, '-') AND match(digits, '[1-9]'), '1', '0'),
            '"invalid"'), [{{string}}])[1])""",
)
_PROCEDURE_TAGS = _procedure_field("'tags'", "'true'")

PROCEDURE_GUIDANCE_FIELDS = f"""
    if(memory_type = 'procedural' AND JSONType({_PROCEDURE_BODY}) = 'Object',
        concat(
            '{{"quarantined":',
                if(JSONType({_PROCEDURE_BODY}, 'quarantined') = 'Object', '{{}}', 'null'),
            ',"template":', {_PROCEDURE_TEMPLATE},
            ',"run_stats":',
                if(JSONType({_PROCEDURE_BODY}, 'run_stats') = 'Object',
                   concat('{{"total":', {_PROCEDURE_TOTAL}, '}}'),
                   {_procedure_field("'run_stats'", "'true'")}),
            ',"tags":',
                if(JSONType({_PROCEDURE_BODY}, 'tags') IN ('Array', 'Object', 'String'),
                    if(arrayExists(tag -> JSONExtractString(tag) = 'mined',
                                   JSONExtractArrayRaw({_PROCEDURE_BODY}, 'tags'))
                       OR JSONHas({_PROCEDURE_BODY}, 'tags', 'mined')
                       OR position(JSONExtractString({_PROCEDURE_BODY}, 'tags'), 'mined') > 0,
                       '["mined"]', '[]'),
                    {_PROCEDURE_TAGS}),
            '}}'
        ), '') AS procedure_fields
"""


# A high row is reachable by key, never pushed into a code or edit prompt.
FILE_LISTING_VISIBLE = """
    tombstoned_at IS NULL
    AND (expires_at IS NULL OR expires_at > now())
    AND (valid_to IS NULL OR valid_to > now())
    AND sensitivity != 'high'
"""

# Full-row projection over loom.memory FINAL, shared by get_by_key/get_by_id.
# Mirrors every column build_memory_version_row carries forward (single read
# source of truth — keep in sync with the builder in `rows`).
_MEMORY_FINAL_PROJECTION = f"""
        SELECT
            memory_id,
            org, namespace, agent, user_id, session_id,
            memory_key, memory_type,
            {SERVED_VALUE} AS value,
            -- The stored bytes, for version rows: the preferred form
            -- above is a rendering, not what the writer wrote.
            value AS value_text,
            toString(value_json) AS value_json,
            confidence, version, pinned,
            created_at, updated_at, expires_at, valid_at, valid_to, invalid_at,
            status, requires_review, run_id, source_trace,
            tags, entities, recall_count, last_recalled_at, char_limit,
            tombstoned_at,
            redacted_at, redaction_reason,
            conflict_with, duplicate_of, evidence_count,
            cross_promoted_from_memory_id, cross_promoted_from_namespace,
            superseded_by, rejection_reason,
            temporal_anchor, temporal_confidence,
            reinforcement_count, last_reinforced_at, mentioned_at,
            source_resource_id, content_sha256,
            files, concepts,
            verified_by, verified_at, verified_sha256,
            sensitivity, actor, source_class,
            grounding, evidence,
            lock_mode, lock_reason, lock_until, locked_by, locked_at,
            on_expire, expire_reason
        FROM loom.memory FINAL"""


def attested_sql(alias: str = "") -> str:
    """ClickHouse form of `memory.domain.trust.attests`: the row's attestation binds
    its current content (an actor, an attested hash that is set, and equal to
    the row's content hash). `alias` prefixes the columns (`"m."`)."""
    a = alias
    return (
        f"({a}verified_by != '' AND {a}verified_sha256 NOT IN ('', '0')"
        f" AND {a}verified_sha256 = {a}content_sha256)"
    )


# On ClickHouse Cloud (Shared*MergeTree, several replicas) a SELECT that lands
# on a different replica than the INSERT can miss a row written moments ago,
# even with synchronous inserts. `select_sequential_consistency = 1` forces a fresh
# metadata snapshot so the row is visible; it costs latency, so a read path
# pays it only on a MISS, only once, and only where the caller says the row may
# be fresh, or where a write acts on what it reads. A no-op on single-node
# MergeTree.
_SEQUENTIAL = "SETTINGS select_sequential_consistency = 1"


def read_your_writes_enabled() -> bool:
    from server.settings import settings

    return bool(settings.ch_read_your_writes)


def _is_full_row(row: dict[str, Any] | None) -> bool:
    """A client that answers the whole row to a locating read (tests, a
    future combined read) short-circuits the fetch."""
    return row is not None and "value" in row


_IDENTITY_KEY_TUPLE = "Array(Tuple(String, String, String, String, String, String))"
