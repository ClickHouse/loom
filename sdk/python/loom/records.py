"""Typed memory records — the `memory.record.*` and `memory_schema.*` ops
behind `agent.memory.record` and `agent.memory.schema`.

    await agent.memory.schema.register(
        "customer", fields=[{"name": "account_id", "identity": True},
                            {"name": "tier", "enum_values": ["free", "team"]}])
    out = await agent.memory.record.write(
        "customer", {"account_id": "acc-7", "tier": "team", "seats": 12})
    rows = await agent.memory.record.query("customer", filters={"account_id": "acc-7"})

A record's fields carry an explicit state (`value` / `unknown` / `rejected` /
`absent`) so a read can ask "which customers have no tier yet" without
re-parsing prose. `write` takes the shorthand `{name: scalar}` — `None`
becomes `state="unknown"` — or the full `{name: {"value", "state",
"evidence_id"}}` form per field; both may be mixed. A write to a key that
already holds a record supersedes it field by field (`fold` on the answer),
and a real disagreement lands in `conflicts`.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent

_FIELD_KEYS = frozenset({"value", "state", "evidence_id"})


def normalize_record_fields(fields: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The wire shape of `fields`: a mapping whose keys are all in
    {value, state, evidence_id} passes through; any other value is a
    plain scalar wrapped as `{"value": v}`; `None` is `{"state": "unknown"}`
    (a field the caller looked for and did not find, not a blank)."""
    out: dict[str, dict[str, Any]] = {}
    for name, raw in fields.items():
        if isinstance(raw, Mapping) and raw and set(raw) <= _FIELD_KEYS:
            out[str(name)] = dict(raw)
        elif raw is None:
            out[str(name)] = {"state": "unknown"}
        else:
            out[str(name)] = {"value": raw}
    return out


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


class MemoryRecordClient:
    """`agent.memory.record`: write, query, history and conflicts of typed
    records. The namespace defaults to the agent's; pass `namespace=` to
    address another the token can read."""

    _loom_nested_client = True  # SyncAgent wraps this object's coroutines too

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _scope(self, namespace: str | None) -> dict[str, Any]:
        return {"org": self._agent.org, "namespace": namespace or self._agent.namespace}

    async def write(
        self,
        schema_name: str,
        fields: Mapping[str, Any],
        *,
        memory_key: str = "",
        memory_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        valid_at: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Write one record of `schema_name`. `memory_key` is the logical key
        a later write supersedes; omitted, the schema's identity fields derive
        one (`key_source="identity"`), or the record is anonymous. `memory_id`
        cites the free-form memory the record was read from. Returns
        `record_id`, `field_count`, `memory_key`, `key_source`, `fold`,
        `superseded_record_id` and `conflicts`."""
        body: dict[str, Any] = self._scope(namespace)
        body.update(
            {
                "schema_name": schema_name,
                "memory_key": memory_key,
                "agent": agent_id or self._agent.agent,
                "user_id": user_id or "-",
                "session_id": session_id or "-",
                "fields": normalize_record_fields(fields),
            }
        )
        if memory_id:
            body["memory_id"] = memory_id
        if valid_at is not None:
            body["valid_at"] = _iso(valid_at)
        if valid_to is not None:
            body["valid_to"] = _iso(valid_to)
        return await self._agent.transport.post("/v1/memory.record.write", body)

    async def query(
        self,
        schema_name: str,
        *,
        filters: Mapping[str, str] | None = None,
        predicates: Sequence[Mapping[str, Any]] | None = None,
        as_of: datetime | str | None = None,
        limit: int = 100,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """The active records of `schema_name`: `filters` is exact string
        equality per field; `predicates` are typed comparisons
        (`{"field", "op": eq|ne|gt|gte|lt|lte, "number"|"boolean"|"string"}`)
        or state questions (`{"field", "value_state": unknown|absent|…}`);
        `as_of` reads the records valid at that instant. Returns `rows`
        (each with `record`, `field_states`, `record_id`, `memory_key`) and
        `truncated`."""
        body: dict[str, Any] = self._scope(namespace)
        body.update({"schema_name": schema_name, "limit": limit})
        if filters:
            body["filters"] = {str(k): str(v) for k, v in filters.items()}
        if predicates:
            body["predicates"] = [dict(p) for p in predicates]
        if as_of is not None:
            body["as_of"] = _iso(as_of)
        return await self._agent.transport.post("/v1/memory.record.query", body)

    async def history(
        self, record_id: str, *, limit: int = 50, namespace: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Every version of the logical key `record_id` belongs to, newest
        first — the supersession chain a `write` folded."""
        body: dict[str, Any] = self._scope(namespace)
        body.update({"record_id": record_id, "limit": limit})
        r = await self._agent.transport.post("/v1/memory.record.history", body)
        return list(r.get("rows", []))

    async def conflicts(
        self,
        *,
        schema_name: str = "",
        memory_key: str = "",
        open_only: bool = True,
        limit: int = 100,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Field-level disagreements a superseding write recorded: which
        field, both values, both record ids and both citing memories.
        `open_only=False` includes the ones a later write resolved."""
        body: dict[str, Any] = self._scope(namespace)
        body.update(
            {
                "schema_name": schema_name,
                "memory_key": memory_key,
                "open_only": open_only,
                "limit": limit,
            }
        )
        return await self._agent.transport.post("/v1/memory.record.conflicts", body)


class MemorySchemaClient:
    """`agent.memory.schema`: the record contracts `memory.record.write`
    validates against. `register` needs the admin role on the namespace;
    `list` any reader. Proposals (`memory_schema.propose` / `approve`) stay
    reachable through `agent.call`."""

    _loom_nested_client = True

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _scope(self, namespace: str | None) -> dict[str, Any]:
        return {"org": self._agent.org, "namespace": namespace or self._agent.namespace}

    async def register(
        self,
        name: str,
        fields: Sequence[Mapping[str, Any]],
        *,
        description: str = "",
        status: str = "active",
        created_by: str = "",
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Register (or re-register) schema `name`. Each field is
        `{"name", "type": string|number|integer|boolean, "required",
        "identity", "enum_values", "pattern", "min_length", "max_length",
        "min_value", "max_value", "description"}`; identity fields derive a
        record's key when the writer names none. Returns `schema_id`,
        `name`, `field_count`, `status`."""
        body: dict[str, Any] = self._scope(namespace)
        body.update(
            {
                "name": name,
                "description": description,
                "fields": [dict(f) for f in fields],
                "created_by": created_by or self._agent.agent,
                "status": status,
            }
        )
        return await self._agent.transport.post("/v1/memory_schema.register", body)

    async def list(
        self, *, include_proposed: bool = False, limit: int = 100, namespace: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """The namespace's schemas with their fields; `include_proposed`
        adds the ones awaiting approval."""
        body: dict[str, Any] = self._scope(namespace)
        body.update({"include_proposed": include_proposed, "limit": limit})
        r = await self._agent.transport.post("/v1/memory_schema.list", body)
        return list(r.get("schemas", []))
