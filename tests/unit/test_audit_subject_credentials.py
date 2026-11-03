"""Audited ops that take a credential record none in their audit row:
`loom.audit_log` outlives erasure and the source or repository itself."""

from __future__ import annotations

import json
from typing import Any

from server.code.api.models_repos import CodeRepoAddInput
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.tracker.api.models import TrackerSourceAddInput, TrackerSourceRotateInput

SECRET = "s3cret-value-never-audited"
CREDENTIAL_FIELDS = {"password", "access_token", "token", "secret", "api_key", "credential"}


def _subject(op_name: str, input_obj: Any, response: dict[str, Any]) -> dict[str, Any]:
    op = OPERATIONS_BY_NAME[op_name]
    assert op.audit and op.audit_subject is not None
    return op.audit_subject(input_obj, response)


def _assert_no_credential(subject: dict[str, Any]) -> None:
    assert not CREDENTIAL_FIELDS & set(subject)
    assert SECRET not in json.dumps(subject)


def test_tracker_source_add_audits_the_source_without_its_password() -> None:
    body = TrackerSourceAddInput(
        org="acme",
        name="ci",
        host="https://ch.example.com:8443/",
        database="qa",
        user="reader",
        password=SECRET,
        namespaces=[{"namespace": "qa-ci"}],
    )
    subject = _subject("tracker.source.add", body, {"name": "ci", "written": True})
    _assert_no_credential(subject)
    assert subject["name"] == "ci"
    assert subject["host"] == "ch.example.com"
    assert subject["user"] == "reader"
    assert subject["namespaces"][0]["namespace"] == "qa-ci"


def test_tracker_source_rotate_audits_the_new_user_without_the_password() -> None:
    body = TrackerSourceRotateInput(org="acme", name="ci", user="reader2", password=SECRET)
    subject = _subject("tracker.source.rotate", body, {"name": "ci"})
    _assert_no_credential(subject)
    assert subject == {"org": "acme", "name": "ci", "user": "reader2"}


def test_code_repo_add_audits_the_repository_without_its_token() -> None:
    body = CodeRepoAddInput(org="acme", repo="acme/app", branch="main", access_token=SECRET)
    subject = _subject("code.repo.add", body, {"namespace": "code-acme-app", "repo": "acme/app"})
    _assert_no_credential(subject)
    assert subject["repo"] == "acme/app"
    assert subject["branch"] == "main"
    assert subject["namespace"] == "code-acme-app"
    assert subject["with_token"] is True


def test_every_audited_op_with_a_credential_field_declares_its_audit_subject() -> None:
    # Without one the whole input, credential included, becomes the audit subject.
    missing = [
        op.name
        for op in ALL_OPERATIONS
        if op.audit
        and op.audit_subject is None
        and CREDENTIAL_FIELDS & set(op.input_model.model_fields)
    ]
    assert missing == []
