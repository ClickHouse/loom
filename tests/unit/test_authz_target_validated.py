"""No op's input model rewrites the `org` or `namespace` a request names.

Both transports authorize the validated input's `org` / `namespace`, which
the handler reads, while the ledger row records the raw body's. They are
the same pair only while validation keeps both fields verbatim, so every
registry op's input model is held to that: no alias, no field validator on
either field, no constraint that transforms the value, and no model
validator that assigns either one.
"""

from __future__ import annotations

import inspect
import re
import textwrap

import pytest
from pydantic import BaseModel

from server.operations.registry import ALL_OPERATIONS

_FIELDS = ("org", "namespace")
# A value-changing constraint (a length bound only refuses, never rewrites).
_TRANSFORMS = ("strip_whitespace", "to_lower", "to_upper")
_ASSIGNS = re.compile(
    r"""(self\.(org|namespace)\s*=[^=])|(\[\s*["'](org|namespace)["']\s*\]\s*=[^=])"""
    r"""|(\.setdefault\(\s*["'](org|namespace)["'])|(\.pop\(\s*["'](org|namespace)["'])"""
)


def _models() -> list[tuple[str, type[BaseModel]]]:
    out = []
    for op in ALL_OPERATIONS:
        if any(f in op.input_model.model_fields for f in _FIELDS):
            out.append((op.name, op.input_model))
    return out


MODELS = _models()


def test_the_registry_has_identity_bearing_ops() -> None:
    assert len(MODELS) > 200


@pytest.mark.parametrize(("name", "model"), MODELS, ids=[n for n, _ in MODELS])
def test_org_and_namespace_are_read_verbatim(name: str, model: type[BaseModel]) -> None:
    fields = model.model_fields
    for f in _FIELDS:
        if f not in fields:
            continue
        info = fields[f]
        assert info.alias is None and info.validation_alias is None, f"{name}.{f} is aliased"
        for meta in info.metadata:
            assert not any(getattr(meta, t, False) for t in _TRANSFORMS), f"{name}.{f}: {meta}"
    decorators = model.__pydantic_decorators__
    for dec_name, dec in decorators.field_validators.items():
        touched = set(dec.info.fields) & set(_FIELDS)
        assert not touched, f"{name}: field validator {dec_name} runs on {touched}"
    for dec_name, dec in decorators.model_validators.items():
        src = textwrap.dedent(inspect.getsource(dec.func))
        assert not _ASSIGNS.search(src), f"{name}: model validator {dec_name} sets org/namespace"


def test_the_scan_sees_an_assignment() -> None:
    assert _ASSIGNS.search("        self.namespace = 'x'\n")
    assert _ASSIGNS.search("data['org'] = 'y'")
    assert not _ASSIGNS.search("if self.namespace == 'x':")
