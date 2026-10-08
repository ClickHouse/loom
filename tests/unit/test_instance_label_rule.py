"""A server process is either the compose deployment
(`LOOM_DEPLOYMENT=1`) or it carries `LOOM_INSTANCE_LABEL`; with neither it
refuses to start, and the lifespan applies the rule before anything connects,
so a hand-rolled launcher cannot write ledger rows nobody can attribute."""

from __future__ import annotations

import pytest

from server.observability import instance
from server.observability.instance import InstanceLabelMissing, require_instance_label


def test_a_labelled_process_starts_and_keeps_its_label() -> None:
    assert require_instance_label("iso:w6o2:7880", deployment=False) == "iso:w6o2:7880"
    # The deployment may also carry a label (the compose files set deploy:web /
    # deploy:workers); the label wins over the empty deployment default.
    assert require_instance_label("deploy:workers", deployment=True) == "deploy:workers"


def test_the_deployment_may_run_unlabelled() -> None:
    assert require_instance_label("", deployment=True) == ""
    assert require_instance_label(None, deployment=True) == ""


def test_neither_switch_refuses_with_the_fix_in_the_message() -> None:
    with pytest.raises(InstanceLabelMissing) as ei:
        require_instance_label("", deployment=False)
    msg = str(ei.value)
    assert "LOOM_INSTANCE_LABEL" in msg and "LOOM_DEPLOYMENT" in msg
    assert "iso:<worktree>:<port>" in msg and "decision" not in msg
    # Whitespace is not a label.
    with pytest.raises(InstanceLabelMissing):
        require_instance_label("   ", deployment=False)


def test_the_label_is_bounded_like_the_stamp() -> None:
    long = "x" * 200
    assert require_instance_label(long, deployment=False) == "x" * 64


def test_the_rule_reads_the_same_env_the_stamp_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """`instance_label()` and the rule must never disagree: the
    lifespan passes the former to the latter."""
    monkeypatch.setenv("LOOM_INSTANCE_LABEL", "ci:integration")
    assert require_instance_label(instance.instance_label(), deployment=False) == "ci:integration"
    monkeypatch.delenv("LOOM_INSTANCE_LABEL")
    with pytest.raises(InstanceLabelMissing):
        require_instance_label(instance.instance_label(), deployment=False)


def test_the_setting_is_the_compose_switch(settings_override) -> None:
    from server.settings import settings

    assert settings.deployment is False  # nothing outside compose sets it
    settings_override(deployment=True)
    assert require_instance_label("", deployment=bool(settings.deployment)) == ""


def test_the_lifespan_applies_the_rule_before_connecting() -> None:
    """Source-level: the rule is the first thing the lifespan does, so an
    unlabelled process never opens a ClickHouse connection or takes a lock."""
    import inspect

    from server import main

    src = inspect.getsource(main.lifespan)
    rule_at = src.index("require_instance_label(")
    connect_at = src.index("await ch.connect()")
    assert rule_at < connect_at
