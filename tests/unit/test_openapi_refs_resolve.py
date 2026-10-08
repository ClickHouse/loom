"""Every `$ref` in the served OpenAPI document resolves.

The op routes' request and response schemas are the Pydantic models' JSON
Schemas. Their nested models must live in components/schemas: a
`#/$defs/...` pointer resolves against the document root, where there is no
`$defs`, and ReDoc (`/redoc`) refuses the whole spec.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from server.transport import http


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        own = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return own + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


def _spec() -> dict[str, Any]:
    app = FastAPI()
    http.attach(app)
    return app.openapi()


def test_every_ref_names_a_component_schema() -> None:
    spec = _spec()
    schemas = spec["components"]["schemas"]
    prefix = "#/components/schemas/"
    refs = _refs(spec)
    assert refs, "the op schemas reference no nested model; the test proves nothing"
    bad = sorted({r for r in refs if not r.startswith(prefix) or r[len(prefix) :] not in schemas})
    assert not bad, bad


def test_no_schema_keeps_a_local_defs_block() -> None:
    assert "$defs" not in repr(_spec())


def test_the_spec_is_built_once() -> None:
    app = FastAPI()
    http.attach(app)
    assert app.openapi() is app.openapi()
