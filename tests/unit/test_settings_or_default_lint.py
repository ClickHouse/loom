"""`settings.<field> or <literal>` must not hide a declared default.

A reader that writes `settings.x or 4000` restates the field's default where
nobody deleting or changing the field looks, and turns an explicit 0 / "" in
the environment into that copy. Flagged, for a read of a known settings field:

- a falsy literal of the field's own type (`or 0`, `or ""`): a no-op, the
  field is typed and never None;
- the literal equal to the field's declared default;
- any literal after a field whose value is guaranteed truthy (a Literal of
  non-empty strings, or a bound ge>=1 / gt>=0), where the fallback is dead.

A literal that means something else (`or None`, a display placeholder like
`or "unknown"`) is fine. To express "0 = the default", bound the field at load
or give the 0 its own branch with a comment.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

from server.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
FIELDS = Settings.model_fields


def _base_type(ann: object) -> type | None:
    if ann in (int, float, str, bool):
        return ann  # type: ignore[return-value]
    if typing.get_origin(ann) is typing.Literal and all(
        isinstance(a, str) for a in typing.get_args(ann)
    ):
        return str
    return None


def _guaranteed(name: str) -> bool:
    fld = FIELDS[name]
    ann = fld.annotation
    if typing.get_origin(ann) is typing.Literal:
        return all(isinstance(a, str) and a for a in typing.get_args(ann))
    for m in fld.metadata:
        ge, gt = getattr(m, "ge", None), getattr(m, "gt", None)
        if (ge is not None and ge >= 1) or (gt is not None and gt >= 0):
            return True
    return False


def _flagged(name: str, lit: object) -> str | None:
    if lit is None:
        return None
    fld = FIELDS[name]
    if _guaranteed(name):
        return "the field is never empty/zero: the fallback is dead"
    if not lit and type(lit) is _base_type(fld.annotation):
        return "a falsy literal of the field's own type is a no-op"
    if lit == fld.default and type(lit) is type(fld.default):
        return "restates the declared default"
    return None


def _violations() -> list[str]:
    out: list[str] = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("server/settings/"):
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        aliases = {"settings"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "server.settings":
                aliases |= {a.asname or a.name for a in node.names if a.name == "settings"}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
                continue
            first, second = node.values[0], node.values[1]
            if not (
                isinstance(first, ast.Attribute)
                and isinstance(first.value, ast.Name)
                and first.value.id in aliases
                and first.attr in FIELDS
                and isinstance(second, ast.Constant)
            ):
                continue
            why = _flagged(first.attr, second.value)
            if why:
                out.append(f"{rel}:{node.lineno} {first.attr} or {second.value!r}: {why}")
    return out


def test_no_reader_restates_or_masks_a_settings_default() -> None:
    assert _violations() == []


def test_the_rule_flags_what_it_says() -> None:
    assert _flagged("ratelimit_shared_slots", 16384)  # bounded: dead
    assert _flagged("ch_timeout_read_s", 0.0)  # no-op
    assert _flagged("code_org", "")  # no-op
    assert _flagged("memory_search_default_mode", "semantic")  # restates the default
    assert _flagged("session_brief_llm_model", None) is None
    assert _flagged("build_commit", "unknown") is None
