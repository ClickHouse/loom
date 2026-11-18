"""Private diagnostics retain admitted identity without exposing their work bag."""

from __future__ import annotations

import contextvars
from contextlib import nullcontext

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core import request_work
from server.core.diagnostics import private_work
from server.operations.base import Role
from server.transport._common import ledger_identity


def principal():
    return AuthInfo(
        subject="reader@example.test",
        org="org-a",
        namespaces=("team-a",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        read_user_id="reader-user",
    )


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("authenticated", [False, True])
def test_admitted_ledger_identity_survives_diagnostic_suppression(private, explicit, authenticated):
    def exercise():
        set_current_auth(principal() if authenticated else None)
        request_work.begin().admitted = ("org-a", "team-a")
        body = {"agent": "reader", "user_id": "body-user", "session_id": "session-a"}
        if explicit:
            body.update(org=" org-a ", namespace=" team-a ")
        with private_work() if private else nullcontext():
            assert (request_work.current() is None) is private
            assert ledger_identity(body) == {
                "org": "org-a",
                "namespace": "team-a",
                "agent": "reader",
                "user_id": "reader-user" if authenticated else "body-user",
                "session_id": "session-a",
            }

    contextvars.copy_context().run(exercise)


@pytest.mark.parametrize("private", [False, True])
def test_a_fresh_logical_call_cannot_inherit_an_earlier_admitted_scope(private):
    def exercise():
        set_current_auth(principal())
        with request_work.http_owner({"type": "http"}):
            request_work.begin().admitted = ("org-a", "team-a")
            request_work.begin()
            with private_work() if private else nullcontext():
                assert ledger_identity({}) == {
                    "org": "org-a",
                    "namespace": "-",
                    "agent": "-",
                    "user_id": "reader-user",
                    "session_id": "-",
                }

    contextvars.copy_context().run(exercise)
