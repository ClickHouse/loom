"""Inbox trust follows the token's principal, not the caller-set
agent label. A foreign poster naming the owner's agent lands in triage; the
owner (the subject that registered the inbox, or a service account it minted)
and the subjects an owner policy names are trusted; a label-keyed row can
restrict but never grant ``auto``; an unauthenticated stack keeps the label."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from server.tasks.api import models_task as wire
from server.tasks.app import common, inboxes, transitions
from tests.unit.test_task_inbox import ORG, FakeRepo, _caller, _register, patch_handlers

OWNER = "owner@acme.com"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fr = FakeRepo()
    fr.creators: dict[str, str] = {"sa:owner-bot-1a2b": OWNER, "sa:public-bot": "someone-else"}

    async def service_account_creator(org: str, sa_subject: str) -> str:
        return fr.creators.get(sa_subject, "")

    fr.service_account_creator = service_account_creator  # type: ignore[attr-defined]
    patch_handlers(monkeypatch, "repo", fr)
    monkeypatch.setattr("server.tasks.domain.cursors.LAG_SECONDS", -1)
    return fr


def _as(monkeypatch: pytest.MonkeyPatch, subject: str, *, human: bool = False) -> None:
    """The caller's verified token: `subject`, a service account unless `human`."""
    patch_handlers(monkeypatch, "_token_subject", lambda: subject)
    patch_handlers(monkeypatch, "_caller_subject", lambda fallback="": subject or fallback)
    patch_handlers(monkeypatch, "_caller_is_human", lambda: human)


async def _owned_inbox(fake: FakeRepo, created_by: str = f"human:{OWNER}") -> None:
    await _register(fake)
    row = fake.inboxes[(ORG, "private-bot")]
    row["created_by"] = created_by


async def _post(agent: str, **over: Any) -> Any:
    body: dict[str, Any] = {
        **_caller("public", agent),
        "inbox": "private-bot",
        "kind": "security_finding",
        "title": "UB in Parser",
        "brief": {"objective": "file it privately", "inputs": {"finding_id": "f-1"}},
        "disclosure": "confidential",
    }
    body.update(over)
    return await transitions.handle_task_create(wire.TaskCreateInput(**body))


async def _no_write(org: str, namespace: str, *, write: bool, hides_answer=True) -> bool:
    return False


@pytest.mark.asyncio
async def test_a_foreign_poster_naming_the_owner_agent_lands_in_triage(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake)
    patch_handlers(
        monkeypatch, "_in_scope", _no_write
    )  # a listed poster outside the owner namespace
    _as(monkeypatch, "sa:public-bot")
    out = await _post("private-bot")  # the owner's agent label, spoofed
    assert out.task.status == "triage"


@pytest.mark.asyncio
async def test_the_owner_and_a_service_account_it_minted_are_trusted_from_anywhere(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake)
    fake.inboxes[(ORG, "private-bot")]["posters"] = [OWNER, "sa:owner-bot-1a2b", "sa:unknown-bot"]
    patch_handlers(monkeypatch, "_in_scope", _no_write)  # posting from outside the owner namespace
    _as(monkeypatch, OWNER)
    assert (await _post("any-label")).task.status == "todo"
    _as(monkeypatch, "sa:owner-bot-1a2b")
    assert (await _post("any-label", title="follow-up")).task.status == "todo"
    _as(
        monkeypatch, "sa:unknown-bot"
    )  # listed, minted by nobody Loom knows, naming the owner agent
    assert (await _post("private-bot", title="spoof")).task.status == "triage"


@pytest.mark.asyncio
async def test_a_writer_bot_on_the_owner_namespace_posting_as_the_owner_agent_is_trusted(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bot deployment: an admin registered the inbox, the bot (a writer on
    the owner namespace) files its own follow-ups as the owner agent."""
    await _owned_inbox(fake)
    patch_handlers(monkeypatch, "_in_scope", lambda *a, **k: _true())
    _as(monkeypatch, "sa:private-bot")
    assert (await _post("private-bot")).task.status == "todo"
    assert (await _post("other-label", title="t2")).task.status == "triage"


@pytest.mark.asyncio
async def test_a_listed_label_admits_no_tokened_caller(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake)
    fake.inboxes[(ORG, "private-bot")]["posters"] = ["helper-label"]
    patch_handlers(monkeypatch, "_in_scope", _no_write)
    _as(monkeypatch, "sa:stranger")
    with pytest.raises(HTTPException) as e:
        await _post("helper-label")
    # refused; the stranger cannot read the owner namespace either, so the answer is the
    # unregistered-inbox one rather than a 403 that confirms the inbox
    assert e.value.status_code == 404 and e.value.detail["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_human_writer_on_the_owner_namespace_stays_trusted(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake)

    async def _writer(org: str, namespace: str, *, write: bool, hides_answer=True) -> bool:
        return namespace == "private"

    patch_handlers(monkeypatch, "_in_scope", _writer)
    _as(monkeypatch, "alice@acme.com", human=True)
    assert (await _post("alice")).task.status == "todo"


@pytest.mark.asyncio
async def test_a_policy_on_the_subject_grants_and_a_label_row_only_restricts(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake)
    fake.inboxes[(ORG, "private-bot")]["posters"] = ["sa:trusted-bot", "sa:public-bot"]
    patch_handlers(monkeypatch, "_in_scope", lambda *a, **k: _true())
    _as(monkeypatch, OWNER, human=True)
    for poster, mode in (
        ("sa:trusted-bot", "auto"),
        ("helper-label", "auto"),
        ("bad-label", "deny"),
    ):
        await inboxes.handle_inbox_policy_set(
            wire.InboxPolicySetInput(
                **_caller("private", "admin"),
                inbox="private-bot",
                poster=poster,
                kind="*",
                mode=mode,
            )
        )
    patch_handlers(monkeypatch, "_in_scope", _no_write)  # foreign posters from here on
    _as(monkeypatch, "sa:trusted-bot")
    assert (await _post("x")).task.status == "todo"
    _as(monkeypatch, "sa:public-bot")
    assert (await _post("helper-label", title="t2")).task.status == "triage"
    with pytest.raises(HTTPException) as e:
        await _post("bad-label", title="t3")
    assert e.value.detail["code"] == "TASK_POSTER_DENIED"
    # the owner's own writer bot keeps the label row's auto
    patch_handlers(monkeypatch, "_in_scope", lambda *a, **k: _true())
    _as(monkeypatch, "sa:private-bot")
    assert (await _post("helper-label", title="t4")).task.status == "todo"


async def _true() -> bool:
    return True


@pytest.mark.asyncio
async def test_self_accept_is_still_refused_for_a_trusted_owner(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake)
    patch_handlers(monkeypatch, "_in_scope", _no_write)
    _as(monkeypatch, "sa:public-bot")
    t = (await _post("public-bot")).task
    assert t.status == "triage"
    patch_handlers(monkeypatch, "_in_scope", lambda *a, **k: _true())
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(**_caller("private", "x"), task_id=t.task_id)
        )
    assert e.value.detail["code"] == "TASK_SELF_ACCEPT"


@pytest.mark.asyncio
async def test_no_token_keeps_the_label_rule_and_an_ownerless_inbox_trusts_only_writers(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _owned_inbox(fake, created_by="")
    patch_handlers(monkeypatch, "_in_scope", _no_write)
    _as(monkeypatch, "sa:public-bot")
    assert (await _post("private-bot")).task.status == "triage"
    listed = await inboxes.handle_inbox_list(wire.InboxListInput(**_caller("private", "admin")))
    assert listed.inboxes[0].owner_subject == ""
    _as(monkeypatch, "")  # an unauthenticated stack: the label rule, as before
    out = await _post("private-bot", title="t2", from_subject="sa:public-bot")
    assert out.task.status == "todo"


def test_owner_subject_reads_both_actor_kinds() -> None:
    assert common.owner_subject({"created_by": "human:jdoe"}) == "jdoe"
    assert common.owner_subject({"created_by": "process:sa:x-1"}) == "sa:x-1"
    assert common.owner_subject({"created_by": ""}) == ""


@pytest.mark.asyncio
async def test_inbox_list_everywhere_admits_an_agent_label_only_without_a_token(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tokened caller naming a listed agent label does not see the inbox (its
    posters, webhook, owner namespace); the label admits only an unauthenticated
    caller, the rule posting applies."""
    await _register(fake)
    fake.inboxes[(ORG, "private-bot")]["posters"] = ["label-bot"]
    patch_handlers(monkeypatch, "_in_scope", _no_write)
    listing = wire.InboxListInput(**_caller("public", "label-bot"), mine_everywhere=True)
    _as(monkeypatch, "sa:stranger")
    assert (await inboxes.handle_inbox_list(listing)).inboxes == []
    _as(monkeypatch, "")  # no token: the label is all there is to check
    assert [i.inbox for i in (await inboxes.handle_inbox_list(listing)).inboxes] == ["private-bot"]


@pytest.mark.asyncio
async def test_inbox_list_everywhere_leaves_out_an_inbox_the_caller_may_only_read(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing names the inboxes a post or reassign can reach: read on the owner
    namespace alone does not admit one, write or a poster entry does."""
    await _register(fake)
    fake.inboxes[(ORG, "private-bot")]["posters"] = []
    can_write = False

    async def _reader(org: str, namespace: str, *, write: bool, hides_answer=True) -> bool:
        return namespace == "private" and (not write or can_write)

    patch_handlers(monkeypatch, "_in_scope", _reader)
    _as(monkeypatch, "sa:viewer")
    listing = wire.InboxListInput(**_caller("public", "viewer"), mine_everywhere=True)
    assert (await inboxes.handle_inbox_list(listing)).inboxes == []
    fake.inboxes[(ORG, "private-bot")]["posters"] = ["sa:viewer"]
    assert [i.inbox for i in (await inboxes.handle_inbox_list(listing)).inboxes] == ["private-bot"]
    fake.inboxes[(ORG, "private-bot")]["posters"] = []
    can_write = True
    assert [i.inbox for i in (await inboxes.handle_inbox_list(listing)).inboxes] == ["private-bot"]
