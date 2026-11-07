"""Audited ops that take a credential record none in their audit row:
`loom.audit_log` outlives erasure and the source or repository itself."""

from __future__ import annotations

import asyncio
import json
import typing
from typing import Any

import pytest
from pydantic import BaseModel

from server.code.api.models_repos import CodeRepoAddInput
from server.observability import budgets
from server.observability.audit import is_credential
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.tracker.api.models import TrackerSourceAddInput, TrackerSourceRotateInput
from server.transport import _common as common
from server.transport import post_rows

SECRET = "s3cret-value-never-audited"


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    """The BaseModel types an annotation holds, through list, Optional and unions."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in typing.get_args(annotation) for model in _models_in(arg)]


def _credential_fields(
    model: type[BaseModel], prefix: str = "", seen: frozenset[type] = frozenset()
) -> list[str]:
    """Credential-named fields of a model and of the models its fields hold. A
    `dict[str, Any]` field has no declared keys, so it cannot be checked here;
    what the audited ops put in one (a tracker mapping, a contract report) holds
    no credential."""
    if model in seen:  # a model that holds itself, directly or not
        return []
    seen = seen | {model}
    found = []
    for name, info in model.model_fields.items():
        if is_credential(name):
            found.append(prefix + name)
        for inner in _models_in(info.annotation):
            found.extend(_credential_fields(inner, f"{prefix}{name}.", seen))
    return found


def _subject(op_name: str, input_obj: Any, response: dict[str, Any]) -> dict[str, Any]:
    op = OPERATIONS_BY_NAME[op_name]
    assert op.audit and op.audit_subject is not None
    return op.audit_subject(input_obj, response)


def _assert_no_credential(subject: dict[str, Any]) -> None:
    assert not [key for key in subject if is_credential(key)]
    assert SECRET not in json.dumps(subject)


def _source_add_input() -> TrackerSourceAddInput:
    return TrackerSourceAddInput(
        org="acme",
        name="CI",
        host="https://ch.example.com:8443/",
        database="qa",
        user="reader",
        password=SECRET,
        namespaces=[{"namespace": "qa-ci"}],
    )


def test_tracker_source_add_audits_the_source_without_its_password() -> None:
    response = {"name": "ci", "host": "ch.example.com", "written": True}
    subject = _subject("tracker.source.add", _source_add_input(), response)
    _assert_no_credential(subject)
    assert subject["name"] == "ci"
    assert subject["host"] == "ch.example.com"
    assert subject["user"] == "reader"
    assert subject["namespaces"][0]["namespace"] == "qa-ci"


def test_tracker_source_rotate_audits_the_new_user_without_the_password() -> None:
    body = TrackerSourceRotateInput(org="acme", name=" CI ", user="reader2", password=SECRET)
    subject = _subject("tracker.source.rotate", body, {"name": "ci"})
    _assert_no_credential(subject)
    assert subject == {"org": "acme", "name": "ci", "user": "reader2"}


def test_code_repo_add_audits_the_repository_without_its_token() -> None:
    body = CodeRepoAddInput(
        org="acme", repo="acme/app", branch="main", access_token=SECRET, branches=["dev"]
    )
    response = {
        "namespace": "code-acme-app",
        "repo": "acme/app",
        "branch": "main",
        "join_policy": "open",
        "classification": "standard",
        "also": [{"branch": "dev", "namespace": "code-acme-app-dev", "remote_head": "abc"}],
    }
    subject = _subject("code.repo.add", body, response)
    _assert_no_credential(subject)
    assert subject["repo"] == "acme/app"
    assert subject["branch"] == "main"
    assert subject["namespace"] == "code-acme-app"
    assert subject["also"] == ["code-acme-app-dev"]
    assert subject["authenticated"] is True


def test_code_repo_add_audits_what_was_registered_not_what_was_asked() -> None:
    # A blank branch resolves to the remote's default; a restricted repository is
    # always registered as `approval`.
    body = CodeRepoAddInput(
        org="acme", repo="acme/app", branch="", classification="restricted", join_policy="open"
    )
    response = {
        "namespace": "code-acme-app",
        "repo": "acme/app",
        "branch": "trunk",
        "join_policy": "approval",
        "classification": "restricted",
        "also": [],
    }
    subject = _subject("code.repo.add", body, response)
    assert subject["branch"] == "trunk"
    assert subject["join_policy"] == "approval"
    assert subject["classification"] == "restricted"
    assert subject["also"] == []
    assert subject["authenticated"] is False


def test_the_matcher_tells_credentials_from_lookalikes() -> None:
    for name in (
        "password",
        "passwords",
        "db_passwd",
        "secrets",
        "apikey",
        "access_token",
        "authorization",
        "signing_key",
        "refresh_token_id",
        "session_token_key",
    ):
        assert is_credential(name), name
    for name in ("key", "secret_ref", "token_budget", "section_token_budgets", "max_tokens"):
        assert not is_credential(name), name


class _Node(BaseModel):
    child: _Node | None = None
    password: str = ""


def test_the_scan_stops_at_a_model_that_holds_itself() -> None:
    assert _credential_fields(_Node) == ["password"]


def test_the_audit_row_of_a_real_op_carries_its_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    subjects: list[Any] = []

    async def _audit(**kw: Any) -> None:
        subjects.append(kw["subject"])

    monkeypatch.setattr(budgets, "effective_budget_ms", lambda op, b: b)
    monkeypatch.setattr(common, "emit_op_audit", _audit)
    asyncio.run(
        post_rows.post_handler_rows(
            OPERATIONS_BY_NAME["tracker.source.add"],
            transport="http",
            request_id="r",
            org="acme",
            namespace="qa-ci",
            agent="a",
            actor="human:x",
            input_obj=_source_add_input(),
            elapsed_ms=1,
            response_body={"name": "ci", "host": "ch.example.com"},
            idem=(None, None),
            replay_headers={},
        )
    )
    assert len(subjects) == 1
    _assert_no_credential(subjects[0])
    assert subjects[0]["name"] == "ci"


def test_every_audited_op_with_a_credential_field_declares_its_audit_subject() -> None:
    # Without one the subject is `audit.input_subject`'s shape, which hashes a
    # credential: a hash of a short password can be recovered by guessing, so an op
    # that takes one must leave it out of its row.
    missing = {}
    for op in ALL_OPERATIONS:
        if op.audit and op.audit_subject is None:
            fields = _credential_fields(op.input_model)
            if fields:
                missing[op.name] = fields
    assert missing == {}
