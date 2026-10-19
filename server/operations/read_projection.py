"""Field projection on the memory point reads.

`memory.get`, `memory.bulk_get` and `memory.list` take `fields: [...]`, a
whitelist of output field names. The handler resolves it against the output
model once (an unknown name is a 422, never a silent drop), stores the set
on the response model's private `_projection`, and the model's wrap
serializer keeps only those keys plus the ones every reader relies on
(`found` / `unchanged` on a get row, `memory_key` and `sensitivity` on a list
entry). Unset, `_projection` is None and the payload is byte-identical to what
every existing client sees.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer

from server.core.errors import ValidationError

# The keys a projected payload always carries, per response model.
GET_ALWAYS: frozenset[str] = frozenset({"found", "unchanged"})
# `sensitivity` comes with every list entry rather than by name: a reader that asked for it
# by name would get a 422 from a replica still on a build without the field.
LIST_ENTRY_ALWAYS: frozenset[str] = frozenset({"memory_key", "sensitivity"})


def resolve(
    model_cls: Any, fields: list[str] | None, *, always: frozenset[str]
) -> frozenset[str] | None:
    """The projection for `fields`: None when the caller asked for nothing
    (every field); otherwise the requested names plus `always`.
    Raises ValidationError (422) on a name the model does not have."""
    if not fields:
        return None
    known = set(model_cls.model_fields)
    unknown = sorted({str(f) for f in fields} - known)
    if unknown:
        raise ValidationError(
            f"fields: unknown output field(s) {unknown}",
            unknown_fields=unknown,
            known_fields=sorted(known),
        )
    return frozenset(str(f) for f in fields) | always


def project(data: dict[str, Any], keep: frozenset[str] | None) -> dict[str, Any]:
    """`data` with only the `keep` keys; `data` itself when keep is None."""
    if keep is None:
        return data
    return {k: v for k, v in data.items() if k in keep}


class ProjectedRow(BaseModel):
    """A list row that serializes only the fields a
    caller asked for. The handler resolves `fields` once with `resolve` and
    stamps the set on `_projection`; unset, the row is byte-identical to an
    unprojected one. Shared by the memory get/list rows and the knowledge
    list row."""

    _projection: frozenset[str] | None = None

    @model_serializer(mode="wrap")
    def _project(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return project(handler(self), self._projection)


def project_rows(
    rows: list[Any], model_cls: Any, fields: list[str] | None, *, always: frozenset[str]
) -> list[Any]:
    """Resolve `fields` once against `model_cls` and
    stamp the projection on every row (each a `ProjectedRow`). Unset fields
    leave the rows untouched — the page is byte-identical."""
    keep = resolve(model_cls, fields, always=always)
    if keep is not None:
        for r in rows:
            r._projection = keep
    return rows
