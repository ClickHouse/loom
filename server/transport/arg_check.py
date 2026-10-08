"""An argument the op does not know is named, never dropped.

Every op's input model ignores keys it has no field for, over HTTP and over
MCP alike, so a misnamed argument would vanish with a 200: `task.complete`
with ``result: {...}`` (the field is ``outcome``) would answer ``done`` and
store an empty outcome. This module names such keys so the transports can say so:

- ``ignored_args``: the top-level keys the op's model has no field (or alias)
  for, minus the keys the transport itself reads (consumer, traffic class,
  explain, the idempotency key);
- ``did_you_mean``: for each, the closest field name when one is close.

An op that changes state (any op that is not ``Operation.read_only``: WRITER
or above, audited, or marked ``writes``) refuses a key that is a close match
for one of its fields (`refusal`): the caller meant that field, and the write
it would have made is not the one it asked for. Every other unknown key is
served and reported — a client a release behind the server keeps working.
"""

from __future__ import annotations

import difflib
from functools import lru_cache
from typing import Any

from pydantic import AliasChoices, AliasPath, BaseModel

# Keys the transports read off the raw arguments themselves (consumer,
# traffic class, explain for the MCP lean shape, and the idempotency key MCP
# pops before validation). Never "ignored".
TRANSPORT_KEYS: frozenset[str] = frozenset(
    {"consumer", "traffic_class", "explain", "_idempotency_key", "idempotency_key"}
)
# The identity tuple (and the run a call joins). The SDKs and the /me client
# send it on every call whatever the op takes, so naming them on each answer
# would be noise, not a lost write.
IDENTITY_KEYS: frozenset[str] = frozenset(
    {"org", "namespace", "agent", "user_id", "session_id", "run_id"}
)

# difflib ratio at or above which an unknown key is read as a misspelling of
# a field ("namepsace" → "namespace", "tag" → "tags", "memory_keys" →
# "memory_key"). Below it the key is reported without a suggestion.
CLOSE_MATCH_CUTOFF = 0.8


def _alias_names(alias: Any) -> list[str]:
    if isinstance(alias, str):
        return [alias]
    if isinstance(alias, AliasChoices):
        out: list[str] = []
        for choice in alias.choices:
            out.extend(_alias_names(choice))
        return out
    if isinstance(alias, AliasPath):
        first = alias.path[0] if alias.path else None
        return [first] if isinstance(first, str) else []
    return []


@lru_cache(maxsize=2048)
def known_fields(model: type[BaseModel]) -> frozenset[str] | None:
    """Every key `model` reads at the top level — field names and their
    aliases — or None when the model keeps extras itself (``extra='allow'``)
    or refuses them (``extra='forbid'``), where nothing is silently lost."""
    if model.model_config.get("extra") in ("allow", "forbid"):
        return None
    names: set[str] = set()
    for name, info in model.model_fields.items():
        names.add(name)
        names.update(_alias_names(info.alias))
        names.update(_alias_names(info.validation_alias))
    return frozenset(names)


def unknown_args(model: type[BaseModel], args: Any) -> list[str]:
    """The top-level keys of `args` that `model` would silently drop."""
    if not isinstance(args, dict) or not args:
        return []
    known = known_fields(model)
    if known is None:
        return []
    skip = TRANSPORT_KEYS | IDENTITY_KEYS
    return sorted(k for k in args if isinstance(k, str) and k not in known and k not in skip)


# Synonyms models send for a field the op has (seen in session transcripts
# and the `ignored_args` log). A synonym only counts where the op has the
# target field.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "query": ("text",),  # tracker.similar
    "text": ("addition",),  # working_memory.append
    "limit": ("top_k",),  # tracker.similar
    "key_prefix": ("prefix",),  # memory.list
}
# Prefixes a model adds to a field's name that the field itself leaves off
# ("memory_keys" for ApplyTarget.keys, "key_prefix" for prefix).
_STEM_PREFIXES: tuple[str, ...] = ("memory_", "block_", "key_")


def _stem(name: str) -> str:
    for p in _STEM_PREFIXES:
        if name.startswith(p) and len(name) > len(p):
            name = name[len(p) :]
            break
    return name[:-1] if name.endswith("s") and len(name) > 3 else name


def _tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in name.split("_") if t)


def _unique(cands: list[str]) -> str | None:
    return cands[0] if len(cands) == 1 else None


def closest_field(key: str, fields: list[str]) -> str | None:
    """The field `key` most likely meant, or None. In order: a close spelling
    (difflib), a known synonym, the same stem once a model prefix
    and a plural are dropped (memory_keys → keys), and one field whose name
    words contain the key's or are contained in them (block → block_key,
    key_prefix → prefix). The last two count only when one field matches."""
    match = difflib.get_close_matches(key, fields, n=1, cutoff=CLOSE_MATCH_CUTOFF)
    if match:
        return match[0]
    for target in SYNONYMS.get(key, ()):
        if target in fields:
            return target
    stem = _stem(key)
    hit = _unique([f for f in fields if _stem(f) == stem])
    if hit:
        return hit
    kt = _tokens(key)
    return _unique([f for f in fields if (kt < _tokens(f) or _tokens(f) < kt)])


def did_you_mean(model: type[BaseModel], keys: list[str]) -> dict[str, str]:
    """Closest field name for each key that has one (`closest_field`)."""
    fields = sorted(model.model_fields)
    out: dict[str, str] = {}
    for k in keys:
        match = closest_field(k, fields)
        if match:
            out[k] = match
    return out


def ignored_block(op: Any, args: Any) -> dict[str, Any]:
    """``{ignored_args, did_you_mean?}`` for a response, or ``{}``."""
    keys = unknown_args(op.input_model, args)
    if not keys:
        return {}
    block: dict[str, Any] = {"ignored_args": keys}
    guesses = did_you_mean(op.input_model, keys)
    if guesses:
        block["did_you_mean"] = guesses
    return block


def refusal(op: Any, block: dict[str, Any]) -> dict[str, Any] | None:
    """The 422 detail for a write whose unknown key is a close match for a
    field it has, else None (the call is served and the block reported)."""
    guesses = block.get("did_you_mean") or {}
    if not guesses or op.read_only:
        return None
    pairs = ", ".join(f"{k!r} → {v!r}" for k, v in sorted(guesses.items()))
    return {
        "code": "UNKNOWN_ARGUMENT",
        "message": (
            f"{op.name} has no argument {', '.join(repr(k) for k in sorted(guesses))}; "
            f"did you mean {pairs}? Nothing was written."
        ),
        "ignored_args": block["ignored_args"],
        "did_you_mean": guesses,
    }


def attach(result: Any, block: dict[str, Any]) -> Any:
    """`result` (a dumped dict) with the block merged in; a non-dict result
    is returned as is."""
    if not block or not isinstance(result, dict):
        return result
    return {**result, **block}


class ArgumentRefused:
    """A write's close-match unknown key, raised by the transports as a 422."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self._detail = detail

    def error_detail(self) -> dict[str, Any]:
        return dict(self._detail)


def named_argument_error(op: Any, args: Any) -> Any:
    """The 422 a call must answer before validation, or None: a write's
    misspelled argument."""
    refused = refusal(op, ignored_block(op, args))
    return ArgumentRefused(refused) if refused is not None else None


_NOTED: set[tuple[str, str]] = set()


def _note(op_name: str, keys: list[str]) -> None:
    """One INFO line per (op, key) per process: the operator sees which
    clients lose which fields without a line per call."""
    import logging

    fresh = [k for k in keys if (op_name, k) not in _NOTED]
    if not fresh:
        return
    if len(_NOTED) < 4096:
        _NOTED.update((op_name, k) for k in fresh)
    logging.getLogger("server.transport.arg_check").info(
        "ignored_args %s %s", op_name, ",".join(fresh)
    )


def response_body(op: Any, args: Any, result: Any) -> Any:
    """The dumped result a transport returns, with `ignored_args` /
    `did_you_mean` when the call carried keys the op does not read."""
    from server.transport.epoch_nulls import scrub

    body = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    body = scrub(op.output_model, body)  # a zero date is "never"
    block = ignored_block(op, args)
    if block:
        _note(op.name, block["ignored_args"])
    return attach(body, block)


def header_value(op: Any, args: Any) -> str:
    """`X-Loom-Ignored-Args` for an HTTP answer ("" when none)."""
    return ",".join(unknown_args(op.input_model, args))
