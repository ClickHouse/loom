"""dev/ci_start_clickhouse.sh (the CI ClickHouse) runs the same image, the
same credentials and the same config mounts as the `clickhouse` service of
dev/docker-compose.dev.yml, so CI validates what `make dev` runs. The one
intended difference: CI omits named_collections.xml (gitignored, built from
an operator secret, only used by SQL ai* functions)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "dev" / "ci_start_clickhouse.sh").read_text(encoding="utf-8")
DEV = yaml.safe_load((ROOT / "dev" / "docker-compose.dev.yml").read_text(encoding="utf-8"))
CH = DEV["services"]["clickhouse"]


def _default_image(value: str) -> str:
    """The image a compose `image:` runs with LOOM_CLICKHOUSE_IMAGE unset."""
    m = re.fullmatch(r"\$\{LOOM_CLICKHOUSE_IMAGE:-([^}]+)\}", value)
    assert m, f"{value!r}: the ClickHouse image must default through LOOM_CLICKHOUSE_IMAGE"
    return m.group(1)


def test_same_pinned_image() -> None:
    image = re.search(r"^\s*(clickhouse/clickhouse-server:\S+)\s*$", SCRIPT, re.M)
    assert image and image.group(1) == _default_image(CH["image"])
    assert re.fullmatch(r"clickhouse/clickhouse-server:\d+\.\d+\.\d+\.\d+", image.group(1))


def test_the_standalone_stack_defaults_to_the_tested_release() -> None:
    standalone = yaml.safe_load(
        (ROOT / "deploy" / "standalone" / "compose.yaml").read_text(encoding="utf-8")
    )
    assert _default_image(standalone["services"]["clickhouse"]["image"]) == _default_image(
        CH["image"]
    )


def test_same_credentials_and_database() -> None:
    env = dict(re.findall(r"-e (\w+)=(\S+)", SCRIPT))
    assert env == {k: str(v) for k, v in CH["environment"].items()}


def test_same_config_mounts_except_named_collections() -> None:
    ci = {}
    for mount in re.findall(r'-v "([^"]+)"', SCRIPT):
        src, dst, *_ = mount.split(":")
        ci[dst] = src.replace("$ROOT/", "").replace("$CFG/", "deploy/clickhouse-config/")
    dev = {}
    for vol in CH["volumes"]:
        src, dst, *_ = vol.split(":")
        if src.startswith("../"):
            dev[dst] = src.removeprefix("../")
    named = "/etc/clickhouse-server/config.d/named_collections.xml"
    assert named in dev and named not in ci
    dev.pop(named)
    assert ci == dev
