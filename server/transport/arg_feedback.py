"""A call with a wrong argument says how to make it right.

A bare MCP `-32602 "Input validation failed"` with the field errors in
`error.data` does not help: Claude Code never shows `error.data` to the
model, which then reads Loom's source or guesses field names for a few turns.

`feedback(op, arguments, errors)` is the one compact block the error message
carries instead: each field error (loc, type, msg), every unknown key with the
field it most likely meant (nested models too: `target.memory_keys` →
`target.keys`), the op's required and optional args, and one example call —
an argument template for missing fields or a validated correction for
misspelled keys.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from server.core.errors import ValidationError as LoomValidationError
from server.transport.arg_check import IDENTITY_KEYS, TRANSPORT_KEYS, did_you_mean, unknown_args

_MAX_ERRORS = 4
_MAX_OPTIONAL = 14
_MAX_EXAMPLE_CHARS = 320


def _hidden(info: Any) -> bool:
    extra = info.json_schema_extra
    return isinstance(extra, dict) and bool(extra.get("x-mcp-hidden"))


def _args(model: type[BaseModel], *, required: bool) -> list[str]:
    return [
        n
        for n, f in model.model_fields.items()
        if f.is_required() == required and n not in IDENTITY_KEYS and not _hidden(f)
    ]


def required_args(model: type[BaseModel]) -> list[str]:
    return _args(model, required=True)


def optional_args(model: type[BaseModel]) -> list[str]:
    return _args(model, required=False)


def _nested_model(model: type[BaseModel], field: str) -> type[BaseModel] | None:
    info = model.model_fields.get(field)
    ann = getattr(info, "annotation", None)
    return ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None


def shaped(model: type[BaseModel], field: str) -> str:
    """`field`, with a nested model's own args in braces
    (`target{keys|filter|search}`), so a help row names the nested shape
    instead of leaving the caller to find it in Loom's source."""
    sub = _nested_model(model, field)
    if sub is None:
        return field
    inner = [n for n, f in sub.model_fields.items() if not _hidden(f)]
    return f"{field}{{{'|'.join(inner)}}}" if inner else field


def guesses(model: type[BaseModel], arguments: Any) -> dict[str, str | None]:
    """`{unknown key path: field it meant or None}` at the top level and one
    level into each nested-model argument."""
    out: dict[str, str | None] = {}
    if not isinstance(arguments, dict):
        return out
    keys = unknown_args(model, arguments)
    near = did_you_mean(model, keys)
    out.update({k: near.get(k) for k in keys})
    for field, value in arguments.items():
        sub = _nested_model(model, field) if isinstance(field, str) else None
        if sub is None or not isinstance(value, dict):
            continue
        sub_keys = unknown_args(sub, value)
        sub_near = did_you_mean(sub, sub_keys)
        out.update(
            {
                f"{field}.{k}": (f"{field}.{sub_near[k]}" if k in sub_near else None)
                for k in sub_keys
            }
        )
    return out


def _placeholder(model: type[BaseModel], field: str) -> Any:
    ann = str(getattr(model.model_fields[field], "annotation", ""))
    if _nested_model(model, field) is not None:
        return {}
    if "list" in ann:
        return ["…"]
    if "int" in ann or "float" in ann:
        return 1
    if "bool" in ann:
        return True
    return "…"


def _renamed(
    value: dict[str, Any], near: dict[str, str | None], prefix: str
) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = key
        address = f"{prefix}{key}"
        if address in near:
            target = near[address]
            if target is None:
                return None
            scope, _, name = target.rpartition(".")
            if scope != prefix.removesuffix("."):
                return None
        if name in out:
            return None
        out[name] = item
    return out


def example(
    model: type[BaseModel],
    arguments: dict[str, Any],
    near: dict[str, str | None],
    *,
    fill_missing: bool = True,
) -> dict[str, Any] | None:
    """Keep supplied values while correcting names and filling missing slots."""
    skip = IDENTITY_KEYS | TRANSPORT_KEYS
    if any(target in skip for target in near.values()):
        return None
    call = _renamed({k: v for k, v in arguments.items() if k not in skip}, near, "")
    if call is None:
        return None
    for field, value in list(call.items()):
        if isinstance(value, dict) and _nested_model(model, field) is not None:
            renamed = _renamed(value, near, f"{field}.")
            if renamed is None:
                return None
            call[field] = renamed
    if fill_missing:
        for field in required_args(model):
            call.setdefault(field, _placeholder(model, field))
    return call


def _retry_example(
    model: type[BaseModel],
    arguments: Any,
    near: dict[str, str | None],
    errors: list[dict[str, Any]],
) -> tuple[dict[str, Any], str] | None:
    if not isinstance(arguments, dict):
        return None
    has_correction = any(near.values())
    if not has_correction:
        fillable = {(field,) for field in required_args(model)}
        if not errors or any(
            error.get("type") != "missing" or tuple(error.get("loc") or ()) not in fillable
            for error in errors
        ):
            return None
    call = example(model, arguments, near, fill_missing=not has_correction)
    if call is None:
        return None
    shown = json.dumps(call, ensure_ascii=False, default=str)
    if len(shown) > _MAX_EXAMPLE_CHARS:
        return None
    if has_correction:
        identity = {k: v for k, v in arguments.items() if k in IDENTITY_KEYS}
        try:
            model.model_validate({**identity, **call})
        except ValidationError:
            return None
    return call, shown


def _error_line(e: dict[str, Any]) -> str:
    loc = ".".join(str(p) for p in e.get("loc") or ()) or "arguments"
    msg = str(e.get("msg") or "").removeprefix("Value error, ")
    return f"{loc}: {msg} [{e.get('type') or 'invalid'}]"


def feedback(
    op: Any, arguments: Any, errors: list[dict[str, Any]], *, include_example: bool = True
) -> tuple[str, dict[str, Any]]:
    """`(message, data)` for a call whose arguments failed validation: the
    message is the whole correction in one line-per-part block; `data`
    carries the same parts typed, plus pydantic's `errors`."""
    model = op.input_model
    near = guesses(model, arguments)
    req, opt = required_args(model), optional_args(model)
    retry = _retry_example(model, arguments, near, errors) if include_example else None
    lines = [f"{op.name}: invalid arguments."]
    lines += [_error_line(e) for e in errors[:_MAX_ERRORS]]
    if len(errors) > _MAX_ERRORS:
        lines.append(f"(+{len(errors) - _MAX_ERRORS} more errors)")
    for key, target in near.items():
        lines.append(f"unknown {key!r}" + (f" — did you mean {target!r}?" if target else ""))
    lines.append("required: " + (", ".join(req) or "none"))
    if opt:
        more = f" (+{len(opt) - _MAX_OPTIONAL})" if len(opt) > _MAX_OPTIONAL else ""
        lines.append("optional: " + ", ".join(opt[:_MAX_OPTIONAL]) + more)
    data = {
        # HTTP's code for the same request (a 422 `detail.code`).
        "code": LoomValidationError.code,
        # pydantic's `input` echoes the whole call on a missing field
        "errors": [{k: v for k, v in e.items() if k not in ("input", "url")} for e in errors],
        "op": op.name,
        "required": req,
        "optional": opt,
        "unknown": near,
    }
    if retry is not None:
        call, shown = retry
        lines.append(f"example: {shown}")
        data["example"] = call
    return "\n".join(lines), data


def tool_error(message: str, data: dict[str, Any]) -> dict[str, Any]:
    """The MCP tool result for a call that cannot run as sent: isError, the
    correction as text (what Claude Code shows the model) and the same parts
    as structuredContent. A JSON-RPC error frame reached the model as its
    message alone."""
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
        "structuredContent": data,
    }


def refused_result(op: Any, detail: dict[str, Any]) -> dict[str, Any]:
    """`tool_error` for a refusal made before validation (a write's
    misspelled argument), with the op's args named so the retry needs no
    help call."""
    message = str(detail.get("message") or f"{op.name}: argument refused")
    if detail.get("code") == "UNKNOWN_ARGUMENT":
        req, opt = required_args(op.input_model), optional_args(op.input_model)
        message += "\nrequired: " + (", ".join(req) or "none")
        if opt:
            message += "\noptional: " + ", ".join(opt[:_MAX_OPTIONAL])
    return tool_error(message, {**detail, "op": op.name})
