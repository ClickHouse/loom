"""Wire models for `namespace.inventory`: what one namespace holds, by plane, and who
appears in it, with each population named for what it is."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

InventoryPlane = Literal["memories", "records", "documents", "runs", "integrations"]
ObservedPopulation = Literal["agents", "authors", "subjects"]
ReadScope = Literal["namespace", "own_user", "own_session"]


class NamespaceInventoryInput(BaseModel):
    """What one namespace holds: live counts and newest write per plane (memories,
    records, documents, runs, integrations), the registered member count, and the
    agents, authors and subjects observed on live memory rows. A section the caller
    cannot read, or that failed, says so with `complete: false` and a reason."""

    org: str
    namespace: str
    # `own_user` / `own_session` bound every count to `user_id` (and `session_id`); a
    # token's own bound narrows further, never wider.
    read_scope: ReadScope = "namespace"
    # The user `own_user` / `own_session` count; ignored under `namespace`.
    user_id: str | None = None
    # The session `own_session` counts; ignored under the other scopes.
    session_id: str | None = None
    # At most this many entries in each observed population (most rows first).
    population_limit: int = Field(default=20, ge=0, le=200)


class InventoryScope(BaseModel):
    """The boundary every count below was read under."""

    org: str
    namespace: str
    read_scope: ReadScope
    # The user (and session) the counts are bound to; None reads the whole namespace.
    user_id: str | None
    session_id: str | None


class InventorySection(BaseModel):
    plane: InventoryPlane
    # Live items of the plane inside the scope; None when the section did not run.
    count: int | None
    # Newest write the section saw; None when it holds nothing or did not run.
    latest_at: datetime | None
    # False when the count is missing: `reason` says why.
    complete: bool
    reason: str = ""


class InventoryIdentity(BaseModel):
    name: str
    # Live memory rows in scope that carry this identity.
    rows: int


class InventoryPopulation(BaseModel):
    """One population. `members` are the people holding an active grant; `agents`, `authors` and
    `subjects` are identities observed on live memory rows, never a directory."""

    population: Literal["members"] | ObservedPopulation
    # Distinct identities in the population; None when it did not run.
    total: int | None
    # The largest by row count (members: none listed here, page them with `grants.list`).
    top: list[InventoryIdentity] = Field(default_factory=list)
    complete: bool
    reason: str = ""


class InventoryCapability(BaseModel):
    name: Literal["llm", "embedder"]
    enabled: bool


class NamespaceInventoryOutput(BaseModel):
    # The boundary every count was read under.
    scope: InventoryScope
    # One per plane, in a fixed order: memories, records, documents, runs, integrations.
    sections: list[InventorySection]
    # Members, then the agents, authors and subjects observed on live memory rows.
    populations: list[InventoryPopulation]
    # Whether a real model and a real embedder are configured on this deployment.
    capabilities: list[InventoryCapability]
    # When the reads started.
    computed_at: datetime
