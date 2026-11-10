"""What `loom init` prints once a bot has signed up."""

from __future__ import annotations

from server.cli import init


def test_the_connect_hint_names_the_published_sdk() -> None:
    signed_up = {"namespace": "agent-bot", "org": "dev", "subject": "agent-bot", "claim_code": "c"}
    hint = init.connect_lines(signed_up, base_url="http://x", token_path="t", config_path="c")
    assert "pip install clickhouse-loom" in hint
