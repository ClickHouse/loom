"""The Langfuse prompt push plans offline by default and needs an explicit
host to write."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from scripts.codegen import push_langfuse_prompts as mod


def test_plan_names_locked_new_and_stale_entries() -> None:
    kept, edited = "kept prompt", "edited prompt"
    lock = {
        mod.sha_of(kept): {"name": "loom-kept", "version": 3},
        "0123456789ab": {"name": "loom-gone", "version": 1},
    }
    lines = mod.plan([("loom-edited", edited), ("loom-kept", kept)], lock)
    assert [line.split()[0:2] for line in lines] == [
        ["+", "loom-edited"],
        ["=", "loom-kept"],
        ["-", "loom-gone"],
    ]
    assert "locked at v3" in lines[1]


def test_default_run_is_offline_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("the plan must not touch the network")

    lock = tmp_path / "lock.json"
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(mod, "LOCKFILE", lock)
    monkeypatch.setattr(mod, "registry", lambda: {"loom-a": "text a"})
    assert mod.main([]) == 0
    assert not lock.exists()
    assert "not in the lockfile" in capsys.readouterr().out


def test_write_needs_an_explicit_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_LANGFUSE_URL", raising=False)
    monkeypatch.setenv("LOOM_LANGFUSE_ADMIN_TOKEN", "pk:sk")
    with pytest.raises(SystemExit, match="LOOM_LANGFUSE_URL"):
        mod.LangfusePrompts.from_env()
