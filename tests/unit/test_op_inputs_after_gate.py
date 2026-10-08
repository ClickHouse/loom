"""No code.* / qa.* read input carries a model-level
"one of A or B" rule.

The transport parses the body (`server/transport/http.py`,
`op.input_model.model_validate`) before `check_authorized` runs, so a
`@model_validator` that raises answers a 422 naming the op's argument shape
to a principal the namespace gate is about to refuse. The rule belongs in
the handler, right after `resolve_code_identity` (code.*) or `_bound_input`
(qa.*), as `server.core.errors.ValidationError`.

A leakage suite whose payload builder fills every field from a hints table
never fires such a rule; this test is the server-less gate over EVERY registered op: an argument-less construction may
fail per field (a missing required field, a too-short list) but never with a
model-level error (`loc == ()`), which is what a "one of" rule produces.
"""

from __future__ import annotations

import typing
from typing import Any

import pydantic
import pytest

from server.operations.registry import ALL_OPERATIONS

# writes / feedback whose argument rules are theirs to keep (the leakage
# suite skips the same set; the writer gate is covered by test_grants)
_WRITES = {
    "code.refresh",
    "code.scip_ingest",
    "code.reindex",
    "code.calibrate",
    "code.outcome",
    "code.repo.add",
    "code.repo.add_branch",
    "code.repo.remove",
    "tracker.source.add",
    "tracker.source.check",
    "tracker.source.rotate",
    "tracker.source.remove",
}
_IDENTITY = {"org": "o", "namespace": "n", "agent": "a", "consumer": "t"}


def _read_ops() -> list[Any]:
    return [
        op
        for op in ALL_OPERATIONS
        if op.name.startswith(("code.", "qa.")) and op.name not in _WRITES
    ]


def _identity_only(model: type) -> dict[str, Any]:
    """Identity plus the fields the model itself requires, filled by type —
    never a subject: the shape a refused principal with empty args sends."""
    out: dict[str, Any] = {}
    for fname, field in model.model_fields.items():
        if fname in _IDENTITY:
            out[fname] = _IDENTITY[fname]
            continue
        if not field.is_required():
            continue
        ann, origin, args = (
            field.annotation,
            typing.get_origin(field.annotation),
            typing.get_args(field.annotation),
        )
        if ann is str:
            out[fname] = "x"
        elif ann is int:
            out[fname] = 1
        elif ann is bool:
            out[fname] = False
        elif origin in (list, tuple) or ann is list:
            out[fname] = []
        elif origin is dict or ann is dict:
            out[fname] = {}
        elif origin is typing.Literal and args:
            out[fname] = args[0]
    return out


@pytest.mark.parametrize("op", _read_ops(), ids=lambda op: op.name)
def test_no_read_input_refuses_the_argument_less_call_at_model_level(op: Any) -> None:
    try:
        op.input_model(**_identity_only(op.input_model))
    except pydantic.ValidationError as e:
        model_level = [err for err in e.errors() if tuple(err.get("loc") or ()) == ()]
        assert not model_level, (
            f"{op.name}: a model-level rule answers before the namespace gate — move it into "
            f"the handler as server.core.errors.ValidationError: "
            f"{[m['msg'] for m in model_level]}"
        )


def test_the_six_moved_rules_are_the_handlers() -> None:
    """The rules live in handlers, by op — each handler raises ValidationError on
    the argument-less call (their own suites prove it); here the models
    accept it, so the op reaches the gate first."""
    from server.code.api.models_conventions import CodeConventionsInput
    from server.code.api.models_history import CodeBisectCandidatesInput, CodeSymbolHistoryInput
    from server.code.api.models_testing import CodeCoverageClaimInput
    from server.tracker.api.models import TrackerAnswerInput, TrackerSimilarInput

    for model in (
        CodeSymbolHistoryInput,
        CodeBisectCandidatesInput,
        CodeCoverageClaimInput,
        CodeConventionsInput,
    ):
        model(org="o", namespace="n")
    for model in (TrackerAnswerInput, TrackerSimilarInput):
        model(org="o", namespace="n")
