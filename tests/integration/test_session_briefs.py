"""Session briefs against a live server.

One line of work across two sessions and a teammate: the first session's
hook saves the events of a compaction window (deterministic builder, so the
test needs no model key), the second session's save revises the same brief,
the agent overwrites the status with sections it wrote, a teammate with read
access loads the brief and an earlier revision, a viewer cannot write one,
and a delete hides it and every revision. Needs LOOM_BASE_URL (skips
otherwise).
"""

from __future__ import annotations

import uuid

import httpx
import pytest


def _hdr(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _call(c: httpx.AsyncClient, tok: str, op: str, body: dict, *, expect: int = 200) -> dict:
    r = await c.post(f"/v1/{op}", json=body, headers=_hdr(tok))
    assert r.status_code == expect, (op, r.status_code, r.text[:400])
    return r.json()


_EVENTS = [
    {"kind": "user", "text": "Add session briefs. Never merge during a deploy hold."},
    {"kind": "read", "path": "server/memory/app/working_memory.py"},
    {"kind": "edit", "path": "server/memory/store/session_briefs.py"},
    {"kind": "command", "text": "Run the unit tests — $ pytest tests/unit -q"},
    {"kind": "error", "text": "Bash: AssertionError: revision 2 != 1"},
    {"kind": "edit", "path": "server/memory/app/session_brief.py"},
    {"kind": "assistant", "text": "Fixed the revision count; PR #2120 is open."},
]


@pytest.mark.asyncio
async def test_a_brief_carries_one_line_of_work_across_sessions(
    control_plane_url: str, mint_test_token
) -> None:
    org = f"brief-{uuid.uuid4().hex[:8]}"
    ns = "coding"
    me = mint_test_token(org=org, namespaces=[ns], roles=["writer"], subject="alice")
    teammate = mint_test_token(org=org, namespaces=[ns], roles=["viewer"], subject="bob")
    outsider = mint_test_token(org=org, namespaces=["elsewhere"], roles=["writer"], subject="eve")
    base = {"org": org, "namespace": ns, "agent": "claude-code"}

    owner = mint_test_token(org=org, namespaces=[ns], roles=["admin"], subject="alice")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        # Revision 1: an automatic save follows the Record conversations switch, which a
        # fresh namespace has off (fail closed) — nothing is stored.
        off = await _call(
            c,
            me,
            "session.brief_save",
            {
                **base,
                "session_id": "s0",
                "project": "loom",
                "branch": "feat/briefs",
                "trigger": "precompact",
                "events": _EVENTS,
                "use_llm": False,
            },
        )
        assert not off["changed"] and off["reason"] == "recording_off"
        await _call(
            c, owner, "conversation.set_logging", {"org": org, "namespace": ns, "enabled": True}
        )
        first = await _call(
            c,
            me,
            "session.brief_save",
            {
                **base,
                "session_id": "s1",
                "project": "loom",
                "branch": "feat/briefs",
                "head_sha": "19d01dc28",
                "trigger": "precompact",
                "events": _EVENTS,
                "event_cursor": "120",
                "use_llm": False,
            },
        )
        assert first["changed"] and first["revision"] == 1
        assert first["brief_key"] == "auto/loom/feat-briefs"
        assert first["author"] == "deterministic"
        assert "Goal:\nAdd session briefs." in first["rendered"]
        assert "not a source of facts" in first["rendered"]

        retry = await _call(
            c,
            me,
            "session.brief_save",
            {
                **base,
                "session_id": "s1",
                "project": "loom",
                "branch": "feat/briefs",
                "events": _EVENTS,
                "event_cursor": "120",
                "use_llm": False,
            },
        )
        assert not retry["changed"] and retry["reason"] == "already-saved"

        second = await _call(
            c,
            me,
            "session.brief_save",
            {
                **base,
                "session_id": "s2",
                "project": "loom",
                "branch": "feat/briefs",
                "trigger": "session_end",
                "use_llm": False,
                "event_cursor": "40",
                "events": [{"kind": "assistant", "text": "Bench is next."}],
            },
        )
        assert second["revision"] == 2 and second["stats"]["events_total"] == 8

        mine = await _call(
            c,
            me,
            "session.brief_save",
            {
                **base,
                "brief_key": "auto/loom/feat-briefs",
                "trigger": "model",
                "sections": {
                    "status": "Handler done; bench next.",
                    "decisions": [
                        {
                            "what": "events come from the client",
                            "why": "no coding turns on the server",
                        }
                    ],
                },
            },
        )
        assert mine["revision"] == 3 and mine["author"] == "model"

        got = await _call(
            c,
            teammate,
            "session.brief_get",
            {
                **base,
                "brief_key": "auto/loom/feat-briefs",
                "include_sections": True,
            },
        )
        assert got["found"] and got["revision"] == 3 and got["revisions"] == 3
        assert got["sections"]["status"] == "Handler done; bench next."
        assert got["sections"]["goal"].startswith("Add session briefs.")
        assert got["source_sessions"] == ["s1", "s2"]
        first_rev = await _call(
            c,
            teammate,
            "session.brief_get",
            {
                **base,
                "brief_key": "auto/loom/feat-briefs",
                "revision": 1,
            },
        )
        assert first_rev["found"] and first_rev["revision"] == 1
        by_project = await _call(c, teammate, "session.brief_get", {**base, "project": "loom"})
        assert by_project["brief_key"] == "auto/loom/feat-briefs"
        listed = await _call(c, teammate, "session.brief_list", {**base, "project": "loom"})
        assert [b["brief_key"] for b in listed["briefs"]] == ["auto/loom/feat-briefs"]

        await _call(
            c,
            teammate,
            "session.brief_save",
            {
                **base,
                "brief_key": "x",
                "sections": {"goal": "no"},
            },
            expect=403,
        )
        await _call(
            c,
            outsider,
            "session.brief_get",
            {
                **{**base, "namespace": ns},
                "brief_key": "auto/loom/feat-briefs",
            },
            expect=403,
        )

        gone = await _call(
            c,
            me,
            "session.brief_delete",
            {
                **base,
                "brief_key": "auto/loom/feat-briefs",
            },
        )
        assert gone["deleted"]
        after = await _call(
            c,
            teammate,
            "session.brief_get",
            {
                **base,
                "brief_key": "auto/loom/feat-briefs",
                "revision": 1,
            },
        )
        assert not after["found"] and after["deleted"]
        empty = await _call(c, teammate, "session.brief_list", {**base})
        assert empty["briefs"] == []
