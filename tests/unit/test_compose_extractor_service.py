"""The extractor service in compose.yaml: what it may hold and reach."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_the_extractor_holds_only_its_token_and_reaches_only_loom() -> None:
    compose = _compose()
    extractor = compose["services"]["extractor"]
    # .env holds every secret of the stack; the extractor gets one variable.
    assert "env_file" not in extractor
    assert extractor["environment"] == {
        "LOOM_EXTRACTOR_TOKEN": "${LOOM_DOCUMENT_EXTRACTOR_TOKEN:-}"
    }
    assert "ports" not in extractor and "volumes" not in extractor
    assert extractor["networks"] == ["extractor"]
    assert compose["networks"]["extractor"] == {"internal": True}
    assert "extractor" in compose["services"]["loom"]["networks"]
    # No other service shares its network.
    sharing = [n for n, s in compose["services"].items() if "extractor" in s.get("networks", [])]
    assert sorted(sharing) == ["extractor", "loom"]


def test_the_extractor_runs_locked_down_and_bounded() -> None:
    extractor = _compose()["services"]["extractor"]
    assert extractor["read_only"] is True
    assert extractor["cap_drop"] == ["ALL"]
    assert extractor["security_opt"] == ["no-new-privileges:true"]
    assert {"mem_limit", "cpus", "pids_limit"} <= extractor.keys()
    image = "${LOOM_EXTRACTOR_IMAGE_REPOSITORY:-ghcr.io/clickhouse/loom-extractor}:${LOOM_VERSION:-latest}"
    assert extractor["image"] == image


def test_the_dev_stack_builds_the_extractor_and_points_loom_at_it() -> None:
    dev = yaml.safe_load((ROOT / "dev" / "docker-compose.dev.yml").read_text())
    assert dev["services"]["extractor"]["build"]["dockerfile"] == "extractor/Dockerfile"
    url = dev["services"]["loom"]["environment"]["LOOM_DOCUMENT_EXTRACTOR_URL"]
    assert url == "http://extractor:8790"


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm (the CI helm job runs it)")
def test_the_charts_extractor_is_selected_by_nothing_of_loom_and_reaches_nothing() -> None:
    from scripts.gates import helm_extractor_isolation

    helm_extractor_isolation.main()
