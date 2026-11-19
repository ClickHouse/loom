"""A durable unknown mutation prevents network or key access on resume."""

from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

RUN_ID = "a046002d-c888-4b54-852b-167f1cb8a443"


def _module() -> Any:
    return importlib.import_module("server.cli.cell_each_state")


def _state(module: Any) -> dict[str, Any]:
    from server.cli.cell_each import parse_command

    command = parse_command(["settings", "set", "memory_ask_recency_weight", "0.25"])
    return module.new_state(
        command,
        targets=[
            {
                "cell": "alpha",
                "deployment_id": "deploy-alpha",
                "origin": "https://alpha.example.com",
                "state": "serving",
                "mode": "enforced",
                "mutation_generation": "c33e76ce-f023-4b45-92ea-2e9bfa008583",
                "configuration_digests": ["1" * 64],
                "automation": {"name": "automation-alpha", "thumbprint": "A" * 43},
                "recovery": {"name": "recovery-alpha", "thumbprint": "B" * 43},
            }
        ],
        run_id=RUN_ID,
    )


@pytest.mark.parametrize("status", ["dispatching", "UNSETTLED"])
def test_uncertain_resume_stops_before_credentials_or_http(
    status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    from server.cli.cell_each_run import main

    module = _module()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = _state(module)
    state["outcomes"]["alpha"].update(status=status, attempt=1)
    module.write_state(path, state, exclusive=True)
    assert module.read_state(path) == state
    before = path.read_bytes()

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an uncertain mutation forbids all network access")

    monkeypatch.setattr(httpx, "Client", no_network)
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    # Missing credentials prove resume inspects durable uncertainty before any key/manifest read.
    assert (
        main(
            [
                "--router",
                "https://router.example.com",
                "--credentials",
                str(directory / "absent"),
                "--resume",
                str(path),
            ]
        )
        != 0
    )
    after = module.read_state(path)
    assert after["outcomes"]["alpha"]["status"] in ("dispatching", "UNSETTLED")
    assert path.read_bytes() == before or after["outcomes"]["alpha"]["status"] == "UNSETTLED"
    output = capsys.readouterr()
    assert "UNSETTLED" in output.out + output.err
    assert "absent" not in output.out + output.err


def test_state_creation_is_exclusive_and_owner_only(tmp_path: Path) -> None:
    module = _module()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = _state(module)
    module.write_state(path, state, exclusive=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert module.read_state(path) == state
    before = path.read_bytes()
    with pytest.raises((FileExistsError, ValueError)):
        module.write_state(path, state, exclusive=True)
    assert path.read_bytes() == before


def test_failed_replace_preserves_last_valid_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = _state(module)
    module.write_state(path, state, exclusive=True)
    before = path.read_bytes()
    state["outcomes"]["alpha"].update(status="dispatching", attempt=1)

    def disk_failure(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", disk_failure)
    with pytest.raises(OSError):
        module.write_state(path, state)
    assert path.read_bytes() == before
    assert [p.name for p in directory.iterdir()] == ["state.json"]


def test_outcome_replacement_syncs_file_then_rename_then_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = _state(module)
    module.write_state(path, state, exclusive=True)
    state["outcomes"]["alpha"].update(status="dispatching", attempt=1)
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        events.append("directory_sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_sync")
        real_fsync(fd)

    def replace(*args: Any, **kwargs: Any) -> None:
        events.append("rename")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    module.write_state(path, state)
    rename = events.index("rename")
    assert "file_sync" in events[:rename]
    assert "directory_sync" in events[rename + 1 :]
    assert module.read_state(path)["outcomes"]["alpha"]["status"] == "dispatching"


def test_duplicate_field_in_an_otherwise_valid_state_refuses(tmp_path: Path) -> None:
    module = _module()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = _state(module)
    module.write_state(path, state, exclusive=True)
    assert module.read_state(path) == state
    encoded = path.read_text(encoding="utf-8")
    path.write_text(
        '{"run_id":' + json.dumps(RUN_ID) + "," + encoded.lstrip()[1:], encoding="utf-8"
    )
    with pytest.raises(ValueError):
        module.read_state(path)


@pytest.mark.parametrize(
    "bad", [b'{"version":1,"version":1}', b'{"value":NaN}', b'{"value":1e9999}', b"\xff"]
)
def test_malformed_state_is_never_adopted(bad: bytes, tmp_path: Path) -> None:
    module = _module()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    path.write_bytes(bad)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        module.read_state(path)


def test_outcome_state_omits_credential_material() -> None:
    module = _module()
    state = _state(module)
    encoded = json.dumps(state)
    assert "private_key" not in encoded and "bearer" not in encoded and "credentials" not in encoded
    assert "deployment_id" in encoded and RUN_ID in encoded
