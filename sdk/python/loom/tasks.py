"""Task inbox client.

One inbox per agent, and the inbox is its queue. A bot posts work into an
inbox it may not be able to read (``create``), the inbox's trust policy
decides whether the task waits in triage, and the owner pulls work under a
lease with a fence token (``pull``) and finishes it with exactly one of
``complete`` / ``fail`` / ``wont_do`` / ``defer`` / ``block``. Senders learn
outcomes by pulling ``updates`` (a cursor over the events of tasks they
posted). Agents push their writes and pull their work; nothing here is
pushed to an agent.

Every call is made under the agent's own ``(org, namespace, agent)``; the
namespace is the one the token authorizes (a poster's own, an owner's
inbox namespace), and ``inbox`` names the queue.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent

MAX_WAIT_SECONDS = 30


def _iso(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    return dt if isinstance(dt, str) else dt.isoformat()


class TasksClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def _base(self, *, agent_id: str | None = None) -> dict[str, Any]:
        return {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "agent": agent_id or self._agent.agent,
        }

    async def _post(
        self, op: str, payload: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        body = {k: v for k, v in payload.items() if v is not None}
        out: dict[str, Any] = await self._agent.transport.post(
            f"/v1/{op}", body, timeout_s=timeout_s
        )
        return out

    # ---------------------------------------------------------- inboxes

    async def register_inbox(
        self,
        inbox: str,
        *,
        owner_agent: str | None = None,
        description: str = "",
        posters: builtins.list[str] | None = None,
        accept_disclosure: builtins.list[str] | None = None,
        default_lease_seconds: int = 1800,
        max_attempts: int = 3,
        max_defers: int = 5,
        triage_escalate_after_seconds: int = 14_400,
        webhook_url: str = "",
        webhook_secret_ref: str = "",
        if_match_version: int | None = None,
    ) -> dict[str, Any]:
        """Admin side: register (or re-register) an inbox owned by this
        namespace. ``posters`` may name subjects or agents that may post
        without holding the namespace."""
        payload = self._base()
        payload.update(
            {
                "inbox": inbox,
                "owner_agent": owner_agent or self._agent.agent,
                "description": description,
                "posters": posters or [],
                "default_lease_seconds": default_lease_seconds,
                "max_attempts": max_attempts,
                "max_defers": max_defers,
                "triage_escalate_after_seconds": triage_escalate_after_seconds,
                "webhook_url": webhook_url,
                "webhook_secret_ref": webhook_secret_ref,
                "if_match_version": if_match_version,
            }
        )
        if accept_disclosure is not None:
            payload["accept_disclosure"] = accept_disclosure
        return await self._post("inbox.register", payload)

    async def list_inboxes(
        self, *, mine_everywhere: bool = False, limit: int = 200
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update({"mine_everywhere": mine_everywhere, "limit": limit})
        return await self._post("inbox.list", payload)

    async def principals(self, inbox: str, *, query: str = "", limit: int = 200) -> dict[str, Any]:
        """Admin side: the identities loom already knows in the org (people by
        their login subject, bots by ``sa:`` name), with what they hold, when
        they were last seen, whether they can already post into ``inbox`` and
        whether they are listed — the poster picker's source."""
        payload = self._base()
        payload.update({"inbox": inbox, "query": query, "limit": limit})
        return await self._post("inbox.principals", payload)

    async def set_policy(
        self,
        inbox: str,
        *,
        poster: str,
        mode: str,
        kind: str = "*",
        require_evidence: builtins.list[str] | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        """Trust per poster × kind: ``deny`` / ``review`` / ``auto`` — the mode the
        owner sets; a ``review`` task waits in triage until the inbox's agent
        or a person accepts it. ``require_evidence`` names brief fields that
        must be present."""
        payload = self._base()
        payload.update(
            {
                "inbox": inbox,
                "poster": poster,
                "kind": kind,
                "mode": mode,
                "require_evidence": require_evidence or [],
                "remove": remove,
            }
        )
        return await self._post("inbox.policy_set", payload)

    async def list_policies(self, inbox: str) -> dict[str, Any]:
        payload = self._base()
        payload["inbox"] = inbox
        return await self._post("inbox.policy_list", payload)

    # ---------------------------------------------------------- posting

    async def create(
        self,
        inbox: str,
        *,
        kind: str,
        title: str,
        brief: dict[str, Any] | None = None,
        priority: int = 3,
        disclosure: str = "internal",
        not_before: datetime | str | None = None,
        deadline: datetime | str | None = None,
        origin: str = "",
        parent_task_id: str | None = None,
        idempotency_key: str = "",
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Post a task into ``inbox`` (another agent's, or this agent's own
        for a follow-up: ``not_before`` hides it from pull until then).
        Returns ``{"task": …, "created": bool, "seq": int}``; the task's
        ``status`` is ``todo`` or ``triage`` by the inbox's policy."""
        payload = self._base(agent_id=agent_id)
        payload.update(
            {
                "inbox": inbox,
                "kind": kind,
                "title": title,
                "brief": brief or {},
                "priority": priority,
                "disclosure": disclosure,
                "not_before": _iso(not_before),
                "deadline": _iso(deadline),
                "origin": origin,
                "parent_task_id": parent_task_id,
                "idempotency_key": idempotency_key,
            }
        )
        return await self._post("task.create", payload)

    async def accept(
        self,
        task_id: str,
        *,
        priority: int | None = None,
        constraints: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update(
            {"task_id": task_id, "priority": priority, "constraints": constraints, "note": note}
        )
        return await self._post("task.accept", payload)

    # ---------------------------------------------------------- the owner's loop

    async def pull(
        self,
        inbox: str,
        *,
        kinds: builtins.list[str] | None = None,
        limit: int = 1,
        wait_seconds: int = 0,
        lease_seconds: int | None = None,
        status: str = "todo",
    ) -> dict[str, Any]:
        """Claim up to ``limit`` tasks (``status="todo"``) or read review
        items unclaimed (``status="triage"``). ``wait_seconds`` (≤ 30) holds
        the call until something arrives; each claimed task carries the
        ``fence`` every later owner write must present."""
        payload = self._base()
        payload.update(
            {
                "inbox": inbox,
                "kinds": kinds or [],
                "limit": limit,
                "wait_seconds": min(int(wait_seconds), MAX_WAIT_SECONDS),
                "lease_seconds": lease_seconds,
                "status": status,
            }
        )
        return await self._post("task.pull", payload, timeout_s=MAX_WAIT_SECONDS + 15)

    async def stream(
        self,
        inbox: str,
        *,
        kinds: builtins.list[str] | None = None,
        lease_seconds: int | None = None,
        wait_seconds: int = MAX_WAIT_SECONDS,
    ) -> AsyncIterator[dict[str, Any]]:
        """Long-poll ``pull`` for ever and yield one claimed task at a time —
        the body of a daemon's inbox loop. The caller finishes each task
        with ``complete`` / ``fail`` / ``wont_do`` / ``defer`` / ``block`` and
        heartbeats long work."""
        while True:
            out = await self.pull(
                inbox,
                kinds=kinds,
                limit=1,
                wait_seconds=wait_seconds,
                lease_seconds=lease_seconds,
            )
            for task in out.get("tasks", []):
                yield task

    async def claim(self, task_id: str, *, lease_seconds: int | None = None) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "lease_seconds": lease_seconds})
        return await self._post("task.claim", payload)

    async def heartbeat(
        self, task_id: str, *, fence: int, lease_seconds: int | None = None
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "fence": fence, "lease_seconds": lease_seconds})
        return await self._post("task.heartbeat", payload)

    async def progress(self, task_id: str, *, fence: int, note: str) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "fence": fence, "note": note})
        return await self._post("task.progress", payload)

    async def complete(
        self, task_id: str, *, fence: int, outcome: dict[str, Any] | None = None, note: str = ""
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "fence": fence, "outcome": outcome or {}, "note": note})
        return await self._post("task.complete", payload)

    async def fail(
        self, task_id: str, *, fence: int, reason: str, outcome: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update(
            {"task_id": task_id, "fence": fence, "reason": reason, "outcome": outcome or {}}
        )
        return await self._post("task.fail", payload)

    async def defer(
        self,
        task_id: str,
        *,
        reason: str,
        fence: int = 0,
        resume_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update(
            {"task_id": task_id, "fence": fence, "reason": reason, "resume_at": _iso(resume_at)}
        )
        return await self._post("task.defer", payload)

    async def block(
        self, task_id: str, *, reason: str, fence: int = 0, blocked_on: str = "human"
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update(
            {"task_id": task_id, "fence": fence, "reason": reason, "blocked_on": blocked_on}
        )
        return await self._post("task.block", payload)

    async def resume(self, task_id: str, *, note: str = "") -> dict[str, Any]:
        """A deferred task back to ``todo`` now, ahead of its ``resume_at``."""
        payload = self._base()
        payload.update({"task_id": task_id, "note": note})
        return await self._post("task.resume", payload)

    async def unblock(self, task_id: str, *, note: str = "") -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "note": note})
        return await self._post("task.unblock", payload)

    async def wont_do(
        self, task_id: str, *, reason: str, fence: int | None = None
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "reason": reason, "fence": fence})
        return await self._post("task.wont_do", payload)

    async def cancel(
        self, task_id: str, *, reason: str, fence: int | None = None
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "reason": reason, "fence": fence})
        return await self._post("task.cancel", payload)

    async def reopen(self, task_id: str, *, reason: str) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "reason": reason})
        return await self._post("task.reopen", payload)

    async def update(
        self,
        task_id: str,
        *,
        if_match_version: int,
        title: str | None = None,
        brief: dict[str, Any] | None = None,
        priority: int | None = None,
        deadline: datetime | str | None = None,
        not_before: datetime | str | None = None,
        clear_deadline: bool = False,
        clear_not_before: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update(
            {
                "task_id": task_id,
                "if_match_version": if_match_version,
                "title": title,
                "brief": brief,
                "priority": priority,
                "deadline": _iso(deadline),
                "not_before": _iso(not_before),
                "clear_deadline": clear_deadline,
                "clear_not_before": clear_not_before,
                "note": note,
            }
        )
        return await self._post("task.update", payload)

    async def reassign(self, task_id: str, *, to_inbox: str, reason: str) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "to_inbox": to_inbox, "reason": reason})
        return await self._post("task.reassign", payload)

    # ---------------------------------------------------------- reads

    async def get(self, task_id: str) -> dict[str, Any] | None:
        payload = self._base()
        payload["task_id"] = task_id
        out = await self._post("task.get", payload)
        task: dict[str, Any] | None = out.get("task") if out.get("found") else None
        return task

    async def list(
        self,
        *,
        inbox: str | None = None,
        posted_by_me: bool = False,
        statuses: builtins.list[str] | None = None,
        kinds: builtins.list[str] | None = None,
        owner: str | None = None,
        limit: int = 100,
        fields: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._base()
        payload.update(
            {
                "inbox": inbox,
                "posted_by_me": posted_by_me,
                "statuses": statuses or [],
                "kinds": kinds or [],
                "owner": owner,
                "limit": limit,
                "fields": fields or [],
            }
        )
        return await self._post("task.list", payload)

    async def counts(self, *, inbox: str | None = None) -> dict[str, Any]:
        payload = self._base()
        payload["inbox"] = inbox
        return await self._post("task.counts", payload)

    async def history(self, task_id: str, *, limit: int = 200) -> dict[str, Any]:
        payload = self._base()
        payload.update({"task_id": task_id, "limit": limit})
        return await self._post("task.history", payload)

    async def updates(
        self,
        *,
        since_seq: int = 0,
        inbox: str | None = None,
        limit: int = 200,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        """The sender's cursor: events on tasks this agent posted (or, with
        ``inbox``, every event in an inbox it owns) after ``since_seq``.
        Store ``next_seq`` and pass it back; never advance it on failure."""
        payload = self._base()
        payload.update(
            {
                "since_seq": since_seq,
                "inbox": inbox,
                "limit": limit,
                "wait_seconds": min(int(wait_seconds), MAX_WAIT_SECONDS),
            }
        )
        return await self._post("task.updates", payload, timeout_s=MAX_WAIT_SECONDS + 15)
