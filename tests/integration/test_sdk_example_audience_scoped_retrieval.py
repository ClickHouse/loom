"""sdk/python/examples/audience_scoped_retrieval.py against a live server, with end-user
tokens the test issuer signs: the private and shared requests, the refused scope
expansions and the revoke all hold where Loom itself checks each read.

Skips without LOOM_BASE_URL or the test end-user issuer."""

from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

import pytest

from tests import end_user_issuer
from tests.integration._resources import unique_org

pytestmark = pytest.mark.slow

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/audience_scoped_retrieval.py"


async def test_the_example_passes_every_check_against_a_live_server(
    control_plane_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if not end_user_issuer.configured():
        pytest.skip("no test end-user issuer: run `python -m tests.end_user_issuer <dir>`")
    spec = importlib.util.spec_from_file_location("audience_scoped_retrieval_example", EXAMPLE)
    assert spec and spec.loader
    example = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve their annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, example)
    spec.loader.exec_module(example)
    org = unique_org("audience")
    monkeypatch.setenv("LOOM_BASE_URL", control_plane_url)
    monkeypatch.setenv("LOOM_ORG", org)
    monkeypatch.delenv("LOOM_API_KEY", raising=False)

    assert await example.main(sign=functools.partial(end_user_issuer.mint, org)) == 0

    out = capsys.readouterr().out
    private, _, shared = out.partition("shared reply in channel:support may draw on:")
    assert example.NOTE in private and example.FINANCE in private and example.SUPPORT in private
    shared_reply = shared.partition("scope expansion attempts")[0]
    assert example.SUPPORT in shared_reply
    assert example.NOTE not in shared_reply and example.FINANCE not in shared_reply
    assert "after the revoke, channel:support: 0 passage(s)" in out
    assert "all checks passed" in out
