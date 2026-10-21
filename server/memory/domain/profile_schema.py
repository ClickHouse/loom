"""The shape an operator gives each user's structured profile in a namespace.

A profile schema is a small JSON Schema: an object whose `properties` each
name a type and say in a `description` what the property holds (the model
that fills the profile reads it). Types and constraints are the typed-record
ones: a property is a string, number, integer or boolean, or an array of
one of them, with `enum`, `pattern`, `minLength` / `maxLength` and
`minimum` / `maximum` checked by `record_constraints`. Every property is
present in a built profile, null when the user's memories do not settle it,
so the schema takes no `required`.

Pure: `parse` checks a schema at `profile_schema.set`, `conform` checks a
model's answer against it before the profile is written, `project` reshapes
a stored profile to the schema as it now is.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from server.core.errors import ValidationError
from server.memory.domain import record_constraints
from server.memory.domain.record_values import FIELD_RE, coerce_record_value

MAX_PROPERTIES = 64  # not a setting: wire bound, matches memory_schema.register's field cap
MAX_SCHEMA_BYTES = 16384  # not a setting: wire bound on profile_schema.set's input
MAX_DESCRIPTION_CHARS = 500  # not a setting: schema bound, each description is prompt text
MAX_ENUM_VALUES = 64  # not a setting: schema bound, each value is prompt text
# An array property holds at most this many items unless it says fewer.
MAX_ITEMS = 50  # not a setting: schema bound on a stored profile's arrays
# A full answer holds at most this many values (a scalar property is one, an array its
# maxItems).
MAX_ANSWER_VALUES = 256  # not a setting: schema bound on the model's answer
# The worker's output cap is an estimate of a full answer's tokens (`answer_tokens`):
# each key and value pays FRAMING_TOKENS for its quotes, colon, separator and
# whitespace; a string runs to its maxLength or its longest enum value at
# CHARS_PER_TOKEN, else to STRING_TOKENS; a number or boolean to SCALAR_TOKENS.
FRAMING_TOKENS = 4  # not a setting: JSON punctuation around one key or value
CHARS_PER_TOKEN = 2  # not a setting: a low estimate, so text in any script fits
# A string with no maxLength is a short phrase; a longer value declares its maxLength.
STRING_TOKENS = 48  # not a setting: the answer's shape, part of the schema contract
SCALAR_TOKENS = 12  # not a setting: the longest JSON number is about 24 characters
# A full answer's estimate stays within what a model writes in one answer. Any schema
# within MAX_PROPERTIES and MAX_ANSWER_VALUES that sets no maxLength is under it; a
# large maxLength can pass it.
MAX_ANSWER_TOKENS = 16384  # not a setting: schema bound on the model's answer
SCALAR_TYPES = ("string", "number", "integer", "boolean")
MAX_SAFE_INTEGER = 2**53 - 1  # not a setting: JSON's exact-integer range

_SCHEMA_KEYS = {"type", "properties", "title", "description", "additionalProperties"}
# The keywords of an array's items, and with `description` of a scalar property. `title`
# is accepted wherever JSON Schema puts it and ignored: the model reads `description`.
_ITEM_KEYS = {"type", "title", "enum", "pattern", "minLength", "maxLength", "minimum", "maximum"}
_SCALAR_KEYS = _ITEM_KEYS | {"description"}
_ARRAY_KEYS = {"type", "title", "description", "items", "maxItems"}
# JSON Schema keyword -> the typed-record constraint it is checked as.
_CONSTRAINT_KEYS = {
    "pattern": "pattern",
    "minLength": "min_length",
    "maxLength": "max_length",
    "minimum": "min_value",
    "maximum": "max_value",
}


@dataclass(frozen=True)
class ProfileProperty:
    name: str
    type: str  # one of SCALAR_TYPES, or "array" of `item_type`
    description: str
    item_type: str = ""
    enum: tuple[str, ...] = ()
    constraints: tuple[tuple[str, Any], ...] = ()
    max_items: int = MAX_ITEMS

    @property
    def scalar_type(self) -> str:
        return self.item_type or self.type


def answer_values(props: list[ProfileProperty]) -> int:
    """How many values a full answer to these properties holds."""
    return sum(p.max_items if p.type == "array" else 1 for p in props)


def _value_tokens(prop: ProfileProperty) -> int:
    """The tokens one value of the property may take, framing included."""
    if prop.scalar_type != "string":
        return SCALAR_TOKENS + FRAMING_TOKENS
    max_length: int | None = dict(prop.constraints).get("max_length")
    if prop.enum:
        max_length = max(len(v) for v in prop.enum)
    if max_length is None:
        return STRING_TOKENS + FRAMING_TOKENS
    return math.ceil(max_length / CHARS_PER_TOKEN) + FRAMING_TOKENS


def answer_tokens(props: list[ProfileProperty]) -> int:
    """An estimate of the most tokens a full answer to these properties takes: each
    property's key, and its value or maxItems values."""
    total = 0
    for p in props:
        key = math.ceil(len(p.name) / CHARS_PER_TOKEN) + FRAMING_TOKENS
        count = p.max_items if p.type == "array" else 1
        total += key + count * _value_tokens(p)
    return total


def stored_form(schema: dict[str, Any]) -> str:
    """The schema as stored: the admin's own key order, no whitespace."""
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=False)


def schema_sha256(props: list[ProfileProperty]) -> str:
    """A hash of what a profile depends on: the properties, by name, with their
    types, descriptions and constraints. A `title` or the properties' order
    changes no profile, so it changes no hash."""
    shape = sorted((dataclasses.asdict(p) for p in props), key=lambda d: d["name"])
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def _bad(message: str) -> ValidationError:
    return ValidationError(f"profile schema: {message}")


def _scalar_spec(
    where: str, spec: dict[str, Any], allowed: set[str]
) -> tuple[str, tuple[str, ...], tuple[tuple[str, Any], ...]]:
    """(type, enum, constraints) of a scalar property or an array's items."""
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise _bad(f"{where}: unsupported keyword(s) {unknown}")
    kind = spec.get("type")
    if kind not in SCALAR_TYPES:
        raise _bad(f"{where}: type must be one of {list(SCALAR_TYPES)} or array, got {kind!r}")
    enum: tuple[str, ...] = ()
    if "enum" in spec:
        values = spec["enum"]
        if kind != "string":
            raise _bad(f"{where}: enum applies to string properties only")
        if (
            not isinstance(values, list)
            or not values
            or len(values) > MAX_ENUM_VALUES
            or not all(isinstance(v, str) and v for v in values)
        ):
            raise _bad(f"{where}: enum is 1-{MAX_ENUM_VALUES} non-empty strings")
        enum = tuple(values)
    constraints = {_CONSTRAINT_KEYS[k]: spec[k] for k in _CONSTRAINT_KEYS if k in spec}
    reason = record_constraints.validate_shape(where, kind, constraints)
    if reason:
        raise _bad(reason)
    return kind, enum, tuple(sorted(constraints.items()))


def parse(schema: Any) -> list[ProfileProperty]:
    """The schema's properties in declaration order; ValidationError naming
    the first thing wrong with it."""
    if not isinstance(schema, dict):
        raise _bad("must be a JSON object")
    if len(stored_form(schema).encode()) > MAX_SCHEMA_BYTES:
        raise _bad(f"larger than {MAX_SCHEMA_BYTES} bytes")
    unknown = sorted(set(schema) - _SCHEMA_KEYS)
    if unknown:
        raise _bad(f"unsupported keyword(s) {unknown}")
    if schema.get("type") != "object":
        raise _bad('type must be "object"')
    if schema.get("additionalProperties", False) is not False:
        raise _bad("additionalProperties must be false: a profile holds only what it declares")
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        raise _bad("properties must be a non-empty object")
    if len(props) > MAX_PROPERTIES:
        raise _bad(f"at most {MAX_PROPERTIES} properties, got {len(props)}")
    out: list[ProfileProperty] = []
    for name, spec in props.items():
        if not FIELD_RE.match(name):
            raise _bad(f"property name {name!r} must match {FIELD_RE.pattern}")
        if not isinstance(spec, dict):
            raise _bad(f"property {name!r} must be an object")
        description = spec.get("description")
        if not isinstance(description, str) or not description.strip():
            raise _bad(f"property {name!r} needs a description: the model reads it")
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise _bad(f"property {name!r}: description longer than {MAX_DESCRIPTION_CHARS}")
        if spec.get("type") == "array":
            unknown = sorted(set(spec) - _ARRAY_KEYS)
            if unknown:
                raise _bad(f"property {name!r}: unsupported keyword(s) {unknown}")
            items = spec.get("items")
            if not isinstance(items, dict):
                raise _bad(f"property {name!r}: an array declares its items")
            max_items = spec.get("maxItems", MAX_ITEMS)
            if isinstance(max_items, bool) or not isinstance(max_items, int):
                raise _bad(f"property {name!r}: maxItems must be an integer")
            if not 1 <= max_items <= MAX_ITEMS:
                raise _bad(f"property {name!r}: maxItems is 1-{MAX_ITEMS}")
            kind, enum, constraints = _scalar_spec(f"{name}.items", items, _ITEM_KEYS)
            out.append(
                ProfileProperty(
                    name=name,
                    type="array",
                    description=description.strip(),
                    item_type=kind,
                    enum=enum,
                    constraints=constraints,
                    max_items=max_items,
                )
            )
        else:
            kind, enum, constraints = _scalar_spec(name, spec, _SCALAR_KEYS)
            out.append(
                ProfileProperty(
                    name=name,
                    type=kind,
                    description=description.strip(),
                    enum=enum,
                    constraints=constraints,
                )
            )
    if answer_values(out) > MAX_ANSWER_VALUES:
        raise _bad(
            f"a full answer holds {answer_values(out)} values (one per property, maxItems"
            f" per array), at most {MAX_ANSWER_VALUES}: lower maxItems"
        )
    if answer_tokens(out) > MAX_ANSWER_TOKENS:
        raise _bad(
            f"a full answer may take {answer_tokens(out)} tokens, at most"
            f" {MAX_ANSWER_TOKENS}: lower maxLength or maxItems"
        )
    return out


def _scalar(prop: ProfileProperty, value: Any) -> Any:
    """`value` as the property's type, or None when it is not one."""
    if value is None or isinstance(value, dict | list):
        return None
    if prop.scalar_type in ("number", "integer") and not _finite(value, prop.scalar_type):
        return None
    try:
        text, number, flag = coerce_record_value(
            value,
            prop.scalar_type,
            field_name=prop.name,
            enum_values=list(prop.enum),
            constraints=dict(prop.constraints),
        )
    except HTTPException:
        return None
    if prop.scalar_type == "boolean":
        return flag
    if prop.scalar_type == "integer":
        return int(number)
    if prop.scalar_type == "number":
        return number
    return text if text.strip() else None


def _finite(value: Any, kind: str) -> bool:
    """Whether a numeric answer is a finite number (an integer within the exact range)."""
    if isinstance(value, bool):
        return False
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(n) and (kind == "number" or abs(n) <= MAX_SAFE_INTEGER)


def conform(props: list[ProfileProperty], answer: Any) -> tuple[dict[str, Any], int]:
    """The answer as a profile of exactly these properties: each value of the
    property's type, null where the answer has none or one that breaks the
    schema. Returns the profile and how many values were dropped."""
    given = answer if isinstance(answer, dict) else {}
    out: dict[str, Any] = {}
    dropped = 0
    for prop in props:
        raw = given.get(prop.name)
        if prop.type == "array":
            items = raw if isinstance(raw, list) else ([] if raw is None else [raw])
            kept = [v for v in (_scalar(prop, item) for item in items) if v is not None]
            dropped += len(items) - len(kept)
            # Order kept, repeats folded; items past maxItems count as dropped. An
            # empty list says nothing, as null does.
            unique = list(dict.fromkeys(kept))
            dropped += max(0, len(unique) - prop.max_items)
            out[prop.name] = unique[: prop.max_items] or None
        else:
            value = _scalar(prop, raw)
            dropped += int(raw is not None and value is None)
            out[prop.name] = value
    return out, dropped


def project(props: list[ProfileProperty], stored: Any) -> dict[str, Any]:
    """A stored profile in the schema's current shape: a property the schema
    no longer declares is left out, one it added reads null."""
    return conform(props, stored)[0]
