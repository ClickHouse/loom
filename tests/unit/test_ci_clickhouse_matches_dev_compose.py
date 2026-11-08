"""dev/ci_start_clickhouse.sh (the CI ClickHouse) runs the same image, the
same credentials and the same config mounts as the bundled `clickhouse`
service of compose.yaml, so CI validates what `make dev` and the installer run. Two
intended differences: CI omits named_collections.xml (gitignored, built from
an operator secret, only used by SQL ai* functions), and adds
dev/clickhouse-ci/log_queries_readonly.xml, which pins query logging for
Loom's user as ClickHouse Cloud does."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "dev" / "ci_start_clickhouse.sh").read_text(encoding="utf-8")
CH = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["clickhouse"]


def _default_image(value: str) -> str:
    """The image a compose `image:` runs with LOOM_CLICKHOUSE_IMAGE unset."""
    m = re.fullmatch(r"\$\{LOOM_CLICKHOUSE_IMAGE:-([^}]+)\}", value)
    assert m, f"{value!r}: the ClickHouse image must default through LOOM_CLICKHOUSE_IMAGE"
    return m.group(1)


def test_same_image_the_latest_release() -> None:
    image = re.search(r'^IMAGE="([^"]+)"$', SCRIPT, re.M)
    assert image and _default_image(image.group(1)) == _default_image(CH["image"])
    assert _default_image(CH["image"]) == "clickhouse/clickhouse-server:latest"
    # A cached `latest` would hold an older release.
    assert "*:latest | *:head) PULL=always" in SCRIPT and '--pull "$PULL"' in SCRIPT
    # A checkout's stack pulls it again once a day (dev/docker-compose.dev.yml).
    dev = yaml.safe_load((ROOT / "dev" / "docker-compose.dev.yml").read_text(encoding="utf-8"))
    assert dev["services"]["clickhouse"]["pull_policy"] == "daily"


def test_same_credentials_and_database() -> None:
    env = dict(re.findall(r"-e (\w+)=(\S+)", SCRIPT))
    assert env == {k: str(v) for k, v in CH["environment"].items()}


def test_same_config_mounts_except_the_two_intended_differences() -> None:
    ci = {}
    for mount in re.findall(r'-v "([^"]+)"', SCRIPT):
        src, dst, *_ = mount.split(":")
        ci[dst] = src.replace("$ROOT/", "").replace("$CFG/", "deploy/clickhouse-config/")
    dev = {}
    for vol in CH["volumes"]:
        src, dst = (vol["source"], vol["target"]) if isinstance(vol, dict) else vol.split(":")[:2]
        if src.startswith("./"):
            dev[dst] = src.removeprefix("./")
    named = "/etc/clickhouse-server/config.d/named_collections.xml"
    assert named in dev and named not in ci
    dev.pop(named)
    pinned = "/etc/clickhouse-server/users.d/log_queries_readonly.xml"
    assert ci.pop(pinned) == "dev/clickhouse-ci/log_queries_readonly.xml"
    assert pinned not in dev
    assert ci == dev
