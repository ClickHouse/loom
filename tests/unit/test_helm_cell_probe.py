"""The standalone render gate and pytest exercise the same cell probe contract."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.gates import helm_cell_probe as probe

ENABLED = probe.ENABLED
pytestmark = pytest.mark.skipif(
    not shutil.which("helm"), reason="needs helm (the CI helm job runs it)"
)


@pytest.mark.parametrize("workers", [False, True])
def test_probe_is_off_by_default_even_with_other_env_sources(workers: bool) -> None:
    probe.check_probe_is_off_by_default_even_with_other_env_sources(workers)


@pytest.mark.parametrize("workers", [False, True])
def test_probe_selects_every_listener_workload_and_no_hook_or_test_pod(workers: bool) -> None:
    probe.check_probe_selects_every_listener_workload_and_no_hook_or_test_pod(workers)


@pytest.mark.parametrize(
    "overlay",
    [
        (),
        ("values-kind.yaml",),
        ("values-eks.yaml",),
        ("values-eks.yaml", "values-clickhouse-cloud.yaml"),
    ],
)
@pytest.mark.parametrize("policy", [False, True])
def test_probe_changes_no_traffic_route_or_network_policy(
    overlay: tuple[str, ...], policy: bool
) -> None:
    probe.check_probe_changes_no_traffic_route_or_network_policy(overlay, policy)


@pytest.mark.parametrize("port", [1, 7776, 7778, 65535])
def test_numeric_ports_reach_listener_and_service(port: int) -> None:
    probe.check_numeric_ports_reach_listener_and_service(port)


@pytest.mark.parametrize("port", [0, ENABLED])
def test_values_file_and_json_numbers_are_accepted(tmp_path: Path, port: int) -> None:
    probe.check_values_file_and_json_numbers_are_accepted(tmp_path, port)


@pytest.mark.parametrize("value", [None, {"port": None}])
def test_an_absent_port_after_value_coalescing_is_off(value: Any) -> None:
    probe.check_an_absent_port_after_value_coalescing_is_off(value)


@pytest.mark.parametrize("port", [0, ENABLED])
def test_the_explicit_port_takes_precedence_over_env_from(port: int) -> None:
    probe.check_the_explicit_port_takes_precedence_over_env_from(port)


@pytest.mark.parametrize("port", [0, ENABLED])
def test_every_configured_pod_has_one_explicit_port_beside_external_env_from(port: int) -> None:
    probe.check_every_configured_pod_has_one_explicit_port_beside_external_env_from(port)


def test_probe_service_keeps_its_suffix_with_a_long_fullname() -> None:
    probe.check_probe_service_keeps_its_suffix_with_a_long_fullname()


@pytest.mark.parametrize("value", [-1, 65536, 7777, 8777.5, True, False, "8777", "0", "", [], {}])
def test_bad_probe_ports_are_refused_by_name(value: Any) -> None:
    probe.check_bad_probe_ports_are_refused_by_name(value)


@pytest.mark.parametrize("value", ["0", "8777"])
def test_set_string_ports_are_refused(value: str) -> None:
    probe.check_set_string_ports_are_refused(value)


@pytest.mark.parametrize("value", [False, True, "8777", 8777, []])
def test_bad_probe_blocks_are_refused_by_name(value: Any) -> None:
    probe.check_bad_probe_blocks_are_refused_by_name(value)


@pytest.mark.parametrize("port", [0, ENABLED])
@pytest.mark.parametrize("source", ["env", "extraEnv", "secrets.extra", "settingsFile"])
def test_probe_port_cannot_be_set_twice(port: int, source: str) -> None:
    probe.check_probe_port_cannot_be_set_twice(port, source)


@pytest.mark.parametrize("value", ["false", "true"])
def test_user_pod_labels_cannot_override_probe_endpoint_membership(value: str) -> None:
    probe.check_user_pod_labels_cannot_override_probe_endpoint_membership(value)


@pytest.mark.parametrize("policy", [False, True])
def test_the_main_service_and_ingress_use_the_web_http_port(policy: bool) -> None:
    probe.check_the_main_service_and_ingress_use_the_web_http_port(policy)


def test_other_settings_and_pod_labels_still_render() -> None:
    probe.check_other_settings_and_pod_labels_still_render()


@pytest.mark.parametrize(
    ("name", "through"),
    [
        ("LOOM_EMBEDDING_PROVIDER", "embedding.*"),
        ("LOOM_EMBEDDING_MODEL", "embedding.*"),
        ("LOOM_EMBEDDING_DIM", "embedding.*"),
        ("LOOM_EMBEDDING_BASE_URL", "embedding.*"),
        ("LOOM_EMBEDDING_SEND_DIMENSIONS", "embedding.*"),
        ("LOOM_JWT_SECRET", "secrets.jwtSecret"),
        ("LOOM_JWT_SECRET_PREVIOUS", "secrets.jwtSecretPrevious"),
    ],
)
@pytest.mark.parametrize("source", ["env", "extraEnv", "secrets.extra", "settingsFile"])
def test_existing_embedding_and_root_names_keep_their_config_route_refusal(
    name: str, through: str, source: str
) -> None:
    probe.check_existing_embedding_and_root_names_keep_their_config_route_refusal(
        name, through, source
    )
