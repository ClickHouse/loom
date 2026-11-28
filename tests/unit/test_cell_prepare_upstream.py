"""Preparation verifies and preserves the registered main origin."""

from __future__ import annotations

import argparse
import copy

import pytest

from server.cli import cell as cell_cli
from server.cli import cell_prepare
from server.router import directory, routing
from tests.unit.test_cell_register import _body, _post
from tests.unit.test_cell_register import router as router
from tests.unit.test_router_attach import _cell_row
from tests.unit.test_router_routing import _cells

ORIGIN = "http://eu-2.internal:7777"
PROBE = "http://eu-2.internal:7790"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom")
    cell_cli._add_cell_parser(parser.add_subparsers(dest="cmd", required=True))
    return parser


@pytest.mark.parametrize(
    "first,stored,requested,probe,refused",
    [
        (False, ORIGIN, ORIGIN, PROBE, False),
        (False, ORIGIN, "http://replacement.internal:7777", PROBE, True),
        (False, "", ORIGIN, PROBE, True),
        (True, ORIGIN, ORIGIN, PROBE, False),
        (True, ORIGIN, "http://replacement.internal:7777", PROBE, True),
        (True, ORIGIN, "", PROBE, False),
        (False, ORIGIN, ORIGIN, "http://replacement.internal:7790", True),
    ],
    ids=[
        "later-exact",
        "later-cannot-move-main",
        "later-blank-row-refuses-publication",
        "first-exact",
        "first-router-config-cannot-move-main",
        "first-rerun-without-router-main",
        "later-cannot-move-probe",
    ],
)
def test_rerun_keeps_the_registered_addresses(first, stored, requested, probe, refused):
    row = _cell_row(
        cell="eu-1" if first else "eu-2",
        is_first=first,
        host="loom.example" if first else "eu-2.loom.example",
        mode="recording" if first else "enforced",
        probe_url=PROBE,
        upstream_url=stored,
    )
    if not first:
        body = _body(
            host=row["host"],
            deployment_id=row["deployment_id"],
            bucket=row["bucket"],
            bucket_prefix=row["bucket_prefix"],
            probe_url=PROBE,
        )
        row.update(current_fp=body["current_fp"], next_fp=body["next_fp"])
    reg = directory.Registration(
        cell=row["cell"],
        host=row["host"],
        deployment_id=row["deployment_id"],
        credential_sha256=row["credential_sha256"],
        bucket=row["bucket"],
        bucket_prefix=row["bucket_prefix"],
        probe_url=probe,
        is_first=first,
        tenancy_rows=0,
        state_rows=0,
        prefix_objects=0,
        processes_without_cell=0,
        upstream_url=requested,
        current_fp=row["current_fp"],
        next_fp=row["next_fp"],
    )
    before = copy.deepcopy(row)
    why = directory._rerun_refusal(row, reg)
    assert bool(why) is refused
    if refused:
        assert ("probe address" if probe != PROBE else "upstream") in why
    assert row == before


@pytest.mark.parametrize("later", [False, True], ids=["first-router-owned", "later-explicit"])
def test_normal_registration_binds_the_main_origin(router, later):
    client, listener, calls = router
    body = (
        _body(host="eu-2.loom.example", probe_url=PROBE, upstream_url=ORIGIN) if later else _body()
    )
    response = _post(client, body, cell="eu-2" if later else "eu-1")
    assert response.status_code == 200
    (reg,) = calls
    assert reg.is_first is not later
    assert reg.upstream_url == (ORIGIN if later else "http://upstream.internal:7777")
    assert listener.asked == [
        (PROBE if later else "http://upstream.internal:7790") + "/v1/health/deployment"
    ]


def test_later_registration_checks_the_main_ports_deployment(router):
    client, listener, calls = router
    listener.upstream_id = "dep-other"
    response = _post(
        client,
        _body(host="eu-2.loom.example", probe_url=PROBE, upstream_url=ORIGIN),
        cell="eu-2",
    )
    assert response.status_code == 409
    assert "reports another deployment id" in response.json()["error"]
    assert listener.asked == [PROBE + "/v1/health/deployment"]
    assert calls == []


def test_first_registration_refuses_an_operator_supplied_main_origin(router):
    client, listener, calls = router
    response = _post(client, _body(upstream_url=ORIGIN))
    assert response.status_code == 400
    assert listener.asked == []
    assert calls == []


def test_normal_prepare_parser_accepts_the_later_main_origin():
    args = _parser().parse_args(
        [
            "cell",
            "prepare",
            "--cell",
            "eu-2",
            "--host",
            "eu-2.loom.example",
            "--credential-sha256",
            "a" * 64,
            "--probe-url",
            PROBE,
            "--upstream-url",
            ORIGIN,
        ]
    )
    assert args.func is cell_prepare.cmd_prepare
    assert (args.cell, args.probe_url, args.upstream_url) == ("eu-2", PROBE, ORIGIN)


def test_normal_first_prepare_parser_keeps_its_existing_arguments():
    args = _parser().parse_args(
        [
            "cell",
            "prepare",
            "--cell",
            "eu-1",
            "--host",
            "loom.example",
            "--credential-sha256",
            "a" * 64,
        ]
    )
    assert args.func is cell_prepare.cmd_prepare
    assert (args.cell, args.host, args.probe_url) == ("eu-1", "loom.example", "")


@pytest.mark.parametrize("path", ["/oauth/token", "/v1/admin/schema.status"])
@pytest.mark.parametrize("upstream", ["", ORIGIN], ids=["unpublished", "published"])
def test_attaching_own_host_routes_only_with_a_published_main_origin(path, upstream):
    rows = _cells()
    rows[1].update(state="attaching", upstream_url=upstream)
    table = routing.build(rows, [], shared_host="loom.example", passthrough="")
    answer = routing.decide(table, host="eu-2.example", path=path)
    assert (answer.upstream, answer.status) == ((ORIGIN, 0) if upstream else ("", 503))
