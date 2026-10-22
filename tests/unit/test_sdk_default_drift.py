"""A value the SDK's domain clients put on the wire by default equals the
server Input model's default for that field, so an unset SDK kwarg never
overrides what the server would choose (a route-aware budget, a deployment
setting). Each client method is called with only its required arguments
against a recording transport; a server field without a default is skipped
(there is nothing to drift from)."""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
from types import SimpleNamespace
from typing import Any

from loom import (
    audit,
    briefs,
    calibration,
    code,
    cost,
    erasure,
    grants,
    namespace,
    policy,
    records,
    tasks,
    tracker,
)
from pydantic_core import PydanticUndefined

from server.operations import registry as so

CLIENTS = [
    code.CodeClient,
    tasks.TasksClient,
    tracker.TrackerClient,
    namespace.NamespaceClient,
    records.MemoryRecordClient,
    records.MemorySchemaClient,
    briefs.BriefsClient,
    policy.PolicyClient,
    grants.GrantsClient,
    erasure.ErasureClient,
    cost.CostClient,
    calibration.CalibrationClient,
    audit.AuditClient,
]

IDENTITY = {"org", "namespace", "agent", "user_id", "session_id", "run_id"}

# (op, field) -> why the SDK sends something other than the server default.
ALLOWED = {
    ("memory_schema.register", "created_by"): "stamps the calling agent's name",
}

OPS = {op.name: op for op in so.ALL_OPERATIONS}


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, payload: dict[str, Any], *a: Any, **k: Any) -> Any:
        self.calls.append((path, dict(payload)))
        return {
            "found": True,
            "rows": [],
            "truncated": False,
            "repos": [],
            "schemas": [],
            "briefs": [],
            "task": {},
        }


def _value_for(p: inspect.Parameter) -> Any:
    ann = str(p.annotation).replace(" ", "")
    if ann.startswith("date"):
        return dt.date(2026, 1, 1)
    if ann.startswith("datetime"):
        return dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    if ann.startswith("bool"):
        return True
    if ann.startswith("int"):
        return 7
    if ann.startswith("float"):
        return 0.5
    if ann.startswith(("Sequence[Mapping", "list[dict")):
        return [{"name": "f"}]
    if ann.startswith(("list", "Sequence")):
        return ["x"]
    if ann.startswith(("dict", "Mapping")):
        return {"f": "v"}
    return "x"


def _server_default(field: Any) -> Any:
    if field.default is not PydanticUndefined:
        return field.default
    return field.default_factory() if field.default_factory else PydanticUndefined


def _drift() -> set[tuple[str, str, Any, Any]]:
    found: set[tuple[str, str, Any, Any]] = set()

    async def run() -> None:
        for cls in CLIENTS:
            for name, fn in inspect.getmembers(cls, inspect.iscoroutinefunction):
                if name.startswith("_"):
                    continue
                params = list(inspect.signature(fn).parameters.values())[1:]
                required = {
                    p.name: _value_for(p) for p in params if p.default is inspect.Parameter.empty
                }
                rec = _Recorder()
                client = cls(SimpleNamespace(org="o", namespace="ns", agent="bot", transport=rec))
                await fn(client, **required)
                for path, body in rec.calls:
                    model = OPS[path.removeprefix("/v1/")].input_model
                    for key, sent in body.items():
                        field = model.model_fields.get(key)
                        if key in IDENTITY or key in required or field is None:
                            continue
                        default = _server_default(field)
                        if default is PydanticUndefined or sent == default:
                            continue
                        if sent in ([], {}, "") and default in (None, [], {}, ""):
                            continue
                        found.add((path.removeprefix("/v1/"), key, sent, default))

    asyncio.run(run())
    return found


def test_sdk_defaults_match_the_server_models() -> None:
    drift = _drift()
    unexpected = sorted(d for d in drift if d[:2] not in ALLOWED)
    assert not unexpected, (
        f"SDK default differs from the server's (op, field, sdk, server): {unexpected}"
    )
    stale = sorted(set(ALLOWED) - {d[:2] for d in drift})
    assert not stale, f"ALLOWED entries that no longer drift: {stale}"
