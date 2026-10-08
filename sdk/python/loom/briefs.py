"""Session briefs client.

A session brief is the condensed, resumable state of one line of work: the
goal, where it stands, the decisions taken and why, what was tried and
dropped, the open questions, the next steps, the files and the pointers to
the detail. An agent saves one before its context is dropped and a later
session (the same agent after a restart, a teammate, another client) loads
it to carry the work on::

    await a.briefs.save("billing-migration", sections={
        "goal": "move invoices to the new ledger",
        "status": "schema migrated, backfill half done",
        "decisions": [{"what": "backfill by month", "why": "bounded parts"}],
        "next_steps": ["backfill 2026-08", "switch reads"],
    })
    brief = await a.briefs.get("billing-migration")
    system_prompt = brief["rendered"]

`save(events=...)` hands the server what happened instead (typed events:
``{"kind": "user" | "assistant" | "edit" | "command" | "error" | "read" |
"search" | "tool" | "note", "text": ..., "path": ...}``) and it writes the
brief itself. A brief is working state, not retrieval memory: it is never
embedded or returned by memory.search.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class BriefsClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _base(self, *, session_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": self._agent.agent,
        }
        if session_id:
            body["session_id"] = session_id
        return body

    async def _post(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {k: v for k, v in payload.items() if v is not None}
        out: dict[str, Any] = await self._agent.transport.post(f"/v1/{op}", body)
        return out

    async def save(
        self,
        brief_key: str = "",
        *,
        sections: Mapping[str, Any] | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        project: str = "",
        branch: str = "",
        head_sha: str = "",
        session_id: str | None = None,
        trigger: str = "manual",
        if_revision: int | None = None,
    ) -> dict[str, Any]:
        """Revise (or start) a brief. `sections` you wrote replace the stored
        ones (decisions / dead ends append); `events` are revised into it
        server-side. Returns ``{brief_key, revision, changed, author,
        rendered, stats}``."""
        return await self._post(
            "session.brief_save",
            {
                **self._base(session_id=session_id),
                "brief_key": brief_key or None,
                "sections": dict(sections) if sections is not None else None,
                "events": [dict(e) for e in events] if events is not None else None,
                "project": project or None,
                "branch": branch or None,
                "head_sha": head_sha or None,
                "trigger": trigger,
                "if_revision": if_revision,
            },
        )

    async def get(
        self,
        brief_key: str = "",
        *,
        from_session: str = "",
        project: str = "",
        revision: int = 0,
        include_sections: bool = False,
        full: bool = False,
    ) -> dict[str, Any] | None:
        """The brief (``rendered`` is the text to load), or None when there
        is none — by key, else the newest one a session wrote, else the
        newest for a project. `revision=N` reads an earlier revision; `full`
        appends the client's own last compaction summary, verbatim."""
        out = await self._post(
            "session.brief_get",
            {
                **self._base(),
                "brief_key": brief_key or None,
                "from_session": from_session or None,
                "project": project or None,
                "revision": revision or None,
                "include_sections": include_sections or None,
                "full": full or None,
            },
        )
        return out if out.get("found") else None

    async def list(
        self, *, project: str = "", mine_only: bool = False, limit: int = 10
    ) -> builtins.list[dict[str, Any]]:
        """The namespace's briefs, most recently saved first."""
        out = await self._post(
            "session.brief_list",
            {**self._base(), "project": project or None, "mine_only": mine_only, "limit": limit},
        )
        briefs: builtins.list[dict[str, Any]] = list(out.get("briefs") or [])
        return briefs

    async def delete(self, brief_key: str) -> bool:
        """Forget a brief; True when one was deleted."""
        out = await self._post("session.brief_delete", {**self._base(), "brief_key": brief_key})
        return bool(out.get("deleted"))
