"""Typed schema-mode extraction: one record under a caller's schema,
through object detection, field detection and per-field validated values with
a local retry for each failing field.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TypeGuard

from server.llm.client import LLMResult, get_llm_client
from server.llm.json_text import strip_code_fence
from server.memory.app.extract.client import _merge_llm_results
from server.memory.app.extract.models import ExtractedRecord, ExtractionError, RecordValue
from server.memory.app.extract.prompts import _build_extraction_user_prompt
from server.memory.domain.extraction_prompts import with_strict_examples_preamble
from server.observability.genai import staged

log = logging.getLogger("server.memory.app.extract")


# Supported schema type names. The set is small on purpose: more types
# means more LLM prompt complexity and more validator code without
# closing real use cases. Date/datetime are passed through as strings
# (ISO 8601); callers wanting time math should parse in their own code.
_SCHEMA_TYPES = {"string", "number", "integer", "boolean"}


def _is_enum_spec(spec: object) -> TypeGuard[list[str]]:
    """A schema field's spec can be either a type string ("string",
    "number", …) or a list of allowed string values (enum). Enum
    constrains the LLM's output to a fixed vocabulary — the only
    reliable way to prevent it from coining new categories.
    """
    return isinstance(spec, list) and all(isinstance(v, str) for v in spec)


def _validate_schema(schema: Mapping[str, object]) -> None:
    """Catch caller-side typos in the schema BEFORE paying for an LLM
    call. Operator-grade error: 400, not 502."""
    if not schema:
        raise ExtractionError("record_schema must declare at least one field")
    for field_name, spec in schema.items():
        if not isinstance(field_name, str) or not field_name.strip():
            raise ExtractionError(
                f"record_schema field names must be non-empty strings; got {field_name!r}",
            )
        if _is_enum_spec(spec):
            if len(spec) == 0:
                raise ExtractionError(
                    f"record_schema enum field {field_name!r} must have at least one allowed value",
                )
            continue
        if not isinstance(spec, str) or spec not in _SCHEMA_TYPES:
            raise ExtractionError(
                f"record_schema field {field_name!r} has unsupported "
                f"spec {spec!r}; supported: {sorted(_SCHEMA_TYPES)} or "
                "a non-empty list of allowed enum string values",
            )


def _coerce_field(value: object, type_name: str) -> str | int | float | bool:
    """LLMs return everything as either string or number; coerce to the
    declared type. Raises ValueError on un-coercible input."""
    if value is None:
        raise ValueError("null value")
    if type_name == "string":
        return str(value)
    if type_name == "integer":
        if isinstance(value, bool):
            raise ValueError("bool is not integer")
        if not isinstance(value, (int, float, str)):
            raise ValueError(f"cannot coerce {type(value).__name__} to integer")
        return int(value)
    if type_name == "number":
        if isinstance(value, bool):
            raise ValueError("bool is not number")
        if not isinstance(value, (int, float, str)):
            raise ValueError(f"cannot coerce {type(value).__name__} to number")
        return float(value)
    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"true", "yes", "1"}:
                return True
            if v in {"false", "no", "0"}:
                return False
        raise ValueError(f"not coercible to boolean: {value!r}")
    raise ValueError(f"unknown type {type_name!r}")


def _build_typed_prompt(schema: Mapping[str, object]) -> str:
    """Schema-specific system prompt. The LLM sees the exact field names
    + types it must fill so the response shape is predictable. Enum
    fields list their allowed values so the LLM picks one — without
    this, "category" gets variants like "coffee" / "Coffee" / "food"
    and aggregate counts fragment."""
    field_lines: list[str] = []
    for name, spec in schema.items():
        if _is_enum_spec(spec):
            allowed = ", ".join(f'"{v}"' for v in spec)
            field_lines.append(
                f'  - "{name}": one of [{allowed}] (case-sensitive; pick the best fit)'
            )
        else:
            field_lines.append(f'  - "{name}": {spec}')
    fields_doc = "\n".join(field_lines)
    # With ≥2 fields, ask for relations too so the typed record joins
    # graph search (entity_triples).
    relations_block = ""
    if len(schema) >= 2:
        relations_block = (
            '\nAlso include "relations": a JSON array of triples '
            '{"s": subject, "v": verb, "o": object} capturing the verb '
            "that connects two of the named things in the record. "
            "Examples: a record about a user's job emits "
            '{"s":"alice","v":"works_at","o":"clickhouse"}. Emit '
            "AT LEAST one triple when ≥2 named entities are present. "
            "Verb: 1-3 lowercase words, underscore-joined "
            "(works_at, lives_in, prefers, owns). Empty array when "
            "no clear inter-entity relationship can be extracted.\n"
        )
    return (
        "Goal: extract one structured record from messages and return "
        "strict JSON.\n\n"
        'Required output: a JSON object with exactly one key, "record", '
        "whose value is an object with these fields:\n"
        f"{fields_doc}\n\n"
        'Also include "entities": 0-6 short lowercase entity strings '
        "describing the record (proper nouns, project names, "
        "technologies, named things).\n"
        f"{relations_block}\n"
        "Rules:\n"
        "- Every declared field MUST appear in the record.\n"
        "- For enum fields, pick EXACTLY ONE of the listed values — never "
        "  coin a new one. If nothing fits, pick the closest.\n"
        "- If a non-enum field's value is unknown from the messages, use "
        'the type\'s natural empty: "" for string, 0 for integer/number, '
        "false for boolean. Don't guess.\n"
        "- Do not include extra fields outside the schema.\n"
        "- No commentary, no markdown — only the JSON object."
    )


def _schema_field_doc(schema: Mapping[str, object]) -> str:
    lines: list[str] = []
    for name, spec in schema.items():
        if _is_enum_spec(spec):
            allowed = ", ".join(str(v) for v in spec)
            lines.append(f"- {name}: enum({allowed})")
        else:
            lines.append(f"- {name}: {spec}")
    return "\n".join(lines)


def _json_object_from_text(text: str) -> dict[str, object]:
    candidate = strip_code_fence(text)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ExtractionError(f"no JSON object in LLM response: {text[:200]!r}")
    try:
        obj = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as e:
        raise ExtractionError(f"invalid JSON object in LLM response: {e}") from e
    if not isinstance(obj, dict):
        raise ExtractionError("LLM response JSON was not an object")
    return obj


# Per-field local-retry budget. k=2 = one initial value-extraction pass
# plus up to one single-field retry per failed field.
_FIELD_RETRY_K = 2


def _field_empty(spec: object) -> RecordValue:
    """The value written for a field the extractor GAVE UP on (unvalidatable
    after _FIELD_RETRY_K attempts): always None, the explicit unknown marker.

    It does not mirror the prompt's natural-empty rule ("" / 0 / False) or,
    for an ENUM field, `spec[0]` — a plausible real category. Those would be
    fabrications: the inline memory_records projection stores 0 / False /
    spec[0] with value_state='value', so `memory.record.query` would match
    records the model never actually extracted. None projects to
    value_state='unknown', which positive predicates never match. The
    field stays PRESENT in the record (every declared field must appear)
    and is listed in ExtractedRecord.stubbed_fields.
    """
    del spec
    return None


def _coerce_one_field(field_name: str, raw_value: object, spec: object) -> RecordValue:
    """Validate + coerce a single field value against its spec. Raises
    ExtractionError on any deviation — the per-field validation gate.
    Enum membership and type coercion are checked here, per field."""
    if _is_enum_spec(spec):
        value_str = str(raw_value)
        if value_str not in spec:
            raise ExtractionError(
                f"field {field_name!r} got {value_str!r} but must be one of {spec!r}",
            )
        return value_str
    try:
        return _coerce_field(raw_value, str(spec))
    except (ValueError, TypeError) as e:
        raise ExtractionError(
            f"field {field_name!r} could not coerce to {spec}: {e}",
        ) from e


def _record_obj_from_text(text: str) -> dict[str, object]:
    """Pull the {'record': {...}} object out of an LLM response, returning
    just the inner record dict. Empty dict when absent/malformed — callers
    treat a missing field as a per-field miss, not a record-level failure."""
    try:
        payload = _json_object_from_text(text)
    except ExtractionError:
        return {}
    raw_record = payload.get("record")
    return raw_record if isinstance(raw_record, dict) else {}


def _interpret_present(val: object) -> bool | None:
    """Interpret a Stage-1 detector ``present`` value as an explicit bool.

    Returns ``True``/``False`` for a clear affirmative/negative (a JSON bool
    or a truthy/falsey string some models emit), or ``None`` when the answer
    is missing or unrecognised. The caller treats ``None`` as "not
    confirmed" — it must NOT proceed to extract a record on a non-affirmative.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


@staged("extract_typed")
async def extract_typed_record_iterative(
    messages: list[dict[str, object]],
    schema: Mapping[str, object],
    *,
    max_tokens: int = 1024,
    observation_date: str | None = None,
) -> tuple[ExtractedRecord, LLMResult]:
    """Gated, per-field schema-aware extraction.

    Three gated stages — object-detect -> field-detect -> field-value —
    each with a validation gate. The field-value stage validates EVERY
    field independently; any field that fails its gate gets a LOCAL
    single-field retry (up to ``_FIELD_RETRY_K`` total attempts) whose
    prompt FREEZES the already-validated fields as fixed conditioning
    context and narrows scope to the one failing field.

    Stage gates:
      * object-detect ``present: false`` -> raise (no record to extract).
      * field-detect parse failure -> ONE detection retry, then proceed
        with an empty detected set (a soft signal, never fatal).
      * field-value: per-field gate + local retry; a field that still
        fails after its retries falls back to the schema's empty/unknown
        value so the record as a whole still completes.

    The OUTER op stays fail-open: a parseable record is always returned
    once the object is present. ExtractionError (-> 502 at the handler)
    only on TOTAL failure — schema-invalid, empty messages, or
    object-not-present.
    """

    _validate_schema(schema)
    if not messages:
        raise ExtractionError("messages must contain at least one entry")

    client = get_llm_client()
    source = _build_extraction_user_prompt(messages, observation_date, None)
    field_doc = _schema_field_doc(schema)

    # --- Stage 1: object detection (gate: present -> proceed) ---
    object_result = await client.complete(
        system=with_strict_examples_preamble(
            "You decide whether the source messages contain an instance "
            "of the requested memory record. Return only JSON: "
            '{"present": true|false}.',
        ),
        user=f"Schema fields:\n{field_doc}\n\nSource messages:\n{source}",
        max_tokens=128,
    )
    usage = object_result  # every call's four token counts, merged as they come
    present_obj = _json_object_from_text(object_result.text)
    present_val = _interpret_present(present_obj.get("present"))
    if present_val is None:
        # Detector parsed but didn't clearly answer present: true|false
        # (missing key or non-bool). Retry ONCE with a strict addendum,
        # mirroring the Stage-2 parse-failure retry. A single malformed
        # reply must not 502 a record that is actually present — but we
        # must NOT silently proceed to extract a record we have no
        # affirmative evidence for.
        retry_present = await client.complete(
            system=with_strict_examples_preamble(
                "You decide whether the source messages contain an instance "
                "of the requested memory record. Return ONLY JSON: "
                '{"present": true} or {"present": false}. No other keys.',
            ),
            user=f"Schema fields:\n{field_doc}\n\nSource messages:\n{source}",
            max_tokens=128,
        )
        usage = _merge_llm_results(usage, retry_present)
        present_val = _interpret_present(_json_object_from_text(retry_present.text).get("present"))
    if present_val is not True:
        # Explicit false OR still-ambiguous after retry -> no record.
        raise ExtractionError("source messages do not contain the requested record object")

    # --- Stage 2: field detection (gate: parse fail -> retry once) ---
    field_system = with_strict_examples_preamble(
        "You decide which schema fields are CLEARLY mentioned or "
        "supported by the source messages. Return only JSON: "
        '{"fields": ["field_name", ...]}.',
    )
    field_user = f"Schema fields:\n{field_doc}\n\nSource messages:\n{source}"
    field_result = await client.complete(system=field_system, user=field_user, max_tokens=256)
    usage = _merge_llm_results(usage, field_result)
    detected_fields = _parse_detected_fields(field_result.text, schema)
    if detected_fields is None:
        # Parse failure: do NOT silently fall through to empty — retry the
        # detection once, then accept an empty set as a soft signal.
        retry_detect = await client.complete(
            system=field_system + "\n\nVALIDATION FAILED: your previous answer was not valid "
            'JSON. Return ONLY {"fields": [...]} with field names from the '
            "schema.",
            user=field_user,
            max_tokens=256,
        )
        usage = _merge_llm_results(usage, retry_detect)
        detected_fields = _parse_detected_fields(retry_detect.text, schema) or set()

    detection_note = (
        "Detected supported fields: "
        + (", ".join(sorted(detected_fields)) if detected_fields else "none")
        + ". For fields not detected, use the schema's unknown/empty value rule."
    )
    value_system = with_strict_examples_preamble(
        _build_typed_prompt(schema)
        + "\n\n"
        + "You extract values for specific fields from source evidence. "
        + detection_note,
    )

    # --- Stage 3: field-value extraction with per-field gate ---
    value_result = await client.complete(system=value_system, user=source, max_tokens=max_tokens)
    usage = _merge_llm_results(usage, value_result)
    raw_record = _record_obj_from_text(value_result.text)

    validated: dict[str, RecordValue] = {}
    failed: list[str] = []
    for field_name, spec in schema.items():
        if field_name in raw_record:
            try:
                validated[field_name] = _coerce_one_field(field_name, raw_record[field_name], spec)
                continue
            except ExtractionError:
                pass
        failed.append(field_name)

    # Local single-field retry: one field at a time, validated fields
    # frozen as fixed conditioning context. k=2 total attempts/field
    # (initial pass above counted as attempt 1).
    stubbed: list[str] = []
    for field_name in failed:
        spec = schema[field_name]
        ok = False
        for _attempt in range(_FIELD_RETRY_K - 1):
            retry_result = await client.complete(
                system=value_system,
                user=_build_single_field_user_prompt(field_name, spec, validated, source),
                max_tokens=max_tokens,
            )
            usage = _merge_llm_results(usage, retry_result)
            retry_record = _record_obj_from_text(retry_result.text)
            if field_name in retry_record:
                try:
                    validated[field_name] = _coerce_one_field(
                        field_name, retry_record[field_name], spec
                    )
                    ok = True
                    break
                except ExtractionError:
                    continue
        if not ok:
            # Field could not be validated after its retries. Write the
            # explicit unknown marker (None) rather than failing the whole
            # record — keeps the OUTER op fail-open — and never a fabricated
            # value. Recorded in stubbed_fields so the caller can tell.
            validated[field_name] = _field_empty(spec)
            stubbed.append(field_name)
    if stubbed:
        log.warning(
            "typed extraction: %d/%d field(s) unvalidatable after %d attempts, "
            "written as unknown (None): %s",
            len(stubbed),
            len(schema),
            _FIELD_RETRY_K,
            ", ".join(stubbed),
        )

    # Entities + relations come from the (best) value-stage response.
    entities, relations = _entities_relations_from_text(value_result.text)
    record = ExtractedRecord(
        record=validated,
        entities=entities,
        relations=relations,
        stubbed_fields=stubbed,
    )
    return record, usage  # its text is the last value-stage reply


def _parse_detected_fields(text: str, schema: Mapping[str, object]) -> set[str] | None:
    """Parse the field-detection stage response. Returns the set of
    detected field names that exist in the schema, or None on parse
    failure (so the caller can distinguish 'none detected' from
    'couldn't read the answer' and retry only the latter). A `fields`
    value that is not a list is unreadable too."""
    try:
        detected_obj = _json_object_from_text(text)
    except ExtractionError:
        return None
    fields = detected_obj.get("fields", [])
    if not isinstance(fields, list):
        return None
    return {str(f) for f in fields if isinstance(f, (str, int)) and str(f) in schema}


def _build_single_field_user_prompt(
    field_name: str,
    spec: object,
    frozen: Mapping[str, RecordValue],
    source: str,
) -> str:
    """Narrowed user prompt for the local single-field retry. Freezes the
    already-validated fields as fixed context and asks only for the one
    failing field. The caller passes the same `value_system`, so the
    response shape is still the {'record': {...}} object the parser reads."""
    if _is_enum_spec(spec):
        type_hint = "one of [" + ", ".join(str(v) for v in spec) + "]"
    else:
        type_hint = str(spec)
    frozen_block = (
        "Already-validated fields (DO NOT change these):\n"
        + json.dumps(frozen, default=str)
        + "\n\n"
        if frozen
        else ""
    )
    return (
        f"{frozen_block}"
        f"Re-extract ONLY the field {field_name!r} (type: {type_hint}). "
        "The previous attempt produced a missing or invalid value for it. "
        'Return the full {"record": {...}} object with every field, but '
        f"correct {field_name!r} so it is present and matches its declared "
        "type. If the source does not state it, use the schema's "
        "empty/unknown value.\n\n"
        f"Source messages:\n{source}"
    )


def _entities_relations_from_text(
    text: str,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Parse the optional entities + relations arrays out of a value-stage
    response with tolerant (fail-soft) JSON handling. Returns empty lists
    when absent — entities/relations are best-effort graph signal, never a
    record-completion requirement."""
    try:
        payload = _json_object_from_text(text)
    except ExtractionError:
        return [], []
    raw_entities = payload.get("entities", [])
    if isinstance(raw_entities, list):
        entities = [
            str(e).strip().lower()
            for e in raw_entities
            if isinstance(e, (str, int)) and str(e).strip()
        ][:6]
        entities = list(dict.fromkeys(entities))
    else:
        entities = []
    raw_relations = payload.get("relations", [])
    relations: list[tuple[str, str, str]] = []
    if isinstance(raw_relations, list):
        for r in raw_relations[:6]:
            if not isinstance(r, dict):
                continue
            s = str(r.get("s", "")).strip().lower()
            v = str(r.get("v", "")).strip().lower().replace(" ", "_")
            o = str(r.get("o", "")).strip().lower()
            if not v or len(v) < 2:
                continue
            if not s and not o:
                continue
            relations.append((s or "?", v, o or "?"))
    return entities, relations
