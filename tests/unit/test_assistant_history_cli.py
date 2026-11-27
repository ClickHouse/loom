"""Converted owner-bound conversation archives stay private and never replace files."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from scripts.ops import assistant_history
from server.assistant.domain import history


@pytest.fixture
def conversion(monkeypatch, tmp_path):
    tmp_path.chmod(0o755)
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "conversationId": "c1",
                "user": "export-owner",
                "messages": [
                    {
                        "messageId": "m1",
                        "parentMessageId": "",
                        "role": "user",
                        "text": "private question",
                    }
                ],
            }
        )
    )
    source.chmod(0o600)
    bindings = tmp_path / "bindings.json"
    bindings.write_text(json.dumps({"export-owner": "alice"}))
    bindings.chmod(0o600)
    output = tmp_path / "archive.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "assistant-history",
            "--input",
            str(source),
            "--owner-bindings",
            str(bindings),
            "--subject",
            "alice",
            "--org",
            "org",
            "--namespace",
            "ns",
            "--output",
            str(output),
        ],
    )
    return source, output


@pytest.mark.parametrize("mask", [0o022, 0o000])
def test_converted_archive_is_private_under_permissive_umask(conversion, mask):
    source, output = conversion
    original = source.read_bytes()
    previous = os.umask(mask)
    try:
        assistant_history.main()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    archive = json.loads(output.read_text())
    assert archive["conversation_id"] == "c1"
    assert archive["provenance"]["export_owner"] == "export-owner"
    assert history.history(archive, subject="alice", org="org", namespace="ns", leaf="m1") == [
        {"role": "user", "content": "private question"}
    ]
    assert source.read_bytes() == original


@pytest.mark.parametrize("symlink", [False, True])
def test_conversion_never_overwrites_an_existing_archive(conversion, symlink):
    _, output = conversion
    if symlink:
        target = output.with_name("retained.json")
        target.write_text("retained")
        output.symlink_to(target)
    else:
        output.write_text("retained")
    with pytest.raises(FileExistsError):
        assistant_history.main()
    assert output.read_text() == "retained"
