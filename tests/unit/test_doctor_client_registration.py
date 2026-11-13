"""The doctor names a double Loom registration and a checkout
whose code index no credential on the machine can read.

The case pinned: Claude Code carries the plugin's mount AND a user-scope `loom`
entry (every Loom tool listed twice per session), and neither the plugin's
self-signup token nor the user-scope bearer can read the restricted code
namespace, so every loom_code_* call is refused on both connections.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import server.cli.doctor_client as dc


def _grades(checks: list[tuple[str, str, str]]) -> dict[str, str]:
    return {n: g for n, g, _ in checks}


def _detail(checks: list[tuple[str, str, str]], name: str) -> str:
    return next(d for n, _, d in checks if n == name)


def _plugin_and_entry(home: Path, url: str = "https://loom.test/mcp") -> None:
    (home / ".claude" / "plugins" / "data" / "loom").mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"loom": {"type": "http", "url": url}}})
    )


def test_plugin_plus_user_scope_entry_warns_registered_twice(tmp_path: Path) -> None:
    _plugin_and_entry(tmp_path)
    checks = dc.claude_code_checks(tmp_path, "https://loom.test")
    assert _grades(checks)["client:claude-code:mcp-duplicate"] == "WARN"
    d = _detail(checks, "client:claude-code:mcp-duplicate")
    assert "registered twice" in d and "claude mcp remove loom -s user" in d
    assert "toolset=core: a second roster" in d


def test_the_warning_names_the_entry_toolset(tmp_path: Path) -> None:
    _plugin_and_entry(tmp_path, "https://loom.test/mcp?toolset=all")
    d = _detail(
        dc.claude_code_checks(tmp_path, "https://loom.test"), "client:claude-code:mcp-duplicate"
    )
    assert "toolset=all: a second roster" in d


def test_entry_without_plugin_or_plugin_without_entry_is_not_a_duplicate(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"loom": {"type": "http", "url": "https://loom.test/mcp"}}})
    )
    assert "client:claude-code:mcp-duplicate" not in _grades(
        dc.claude_code_checks(tmp_path, "https://loom.test")
    )
    other = tmp_path / "other"
    (other / ".claude" / "plugins" / "data" / "loom").mkdir(parents=True)
    assert "client:claude-code:mcp-duplicate" not in _grades(
        dc.claude_code_checks(other, "https://loom.test")
    )


def _repos(readable_by: set[str]):
    def post(base: str, op: str, body: dict[str, Any], token: str, timeout: float):
        assert op == "code.repos" and body["repo"] == "ClickHouse/loom"
        rows = (
            [{"namespace": "code-loom", "repo": "ClickHouse/loom"}] if token in readable_by else []
        )
        return 200, {"repos": rows}

    return post


def test_code_access_names_every_blind_credential_and_the_fix() -> None:
    creds = [("plugin/hooks", "sa-tok"), ("user-scope 'loom'", "email-tok")]
    none = dc.code_access_check("ClickHouse/loom", "https://loom.test", creds, _repos(set()))
    assert none[0][:2] == ("client:code-access", "WARN")
    assert "no credential on this machine reads an index" in none[0][2]
    assert "claim-*.txt" in none[0][2] and "loom mint-token --save" in none[0][2]
    assert "request access from the dashboard's Public namespaces page" in none[0][2]
    one = dc.code_access_check("ClickHouse/loom", "https://loom.test", creds, _repos({"sa-tok"}))
    assert one[0][1] == "WARN" and "NOT by user-scope 'loom'" in one[0][2]
    both = dc.code_access_check(
        "ClickHouse/loom", "https://loom.test", creds, _repos({"sa-tok", "email-tok"})
    )
    assert both[0][1] == "PASS" and "code-loom" in both[0][2]


def test_code_access_is_silent_outside_a_checkout_or_offline() -> None:
    creds = [("plugin/hooks", "t")]
    assert dc.code_access_check("", "https://loom.test", creds, _repos(set())) == []
    assert dc.code_access_check("ClickHouse/loom", "https://loom.test", creds, None) == []
