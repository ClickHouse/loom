"""mentor.guide serves skills/loom/ as shipped, and every op the guide names exists."""

from __future__ import annotations

import asyncio
import re

import pytest

import server
from server.auth.authz import Role
from server.auth.jwt import AuthInfo
from server.core.paths import AGENT_GUIDE_DIR, REPO_ROOT
from server.insights.api.models_mentor import MentorGuideInput
from server.insights.app.mentor_guide import handle_mentor_guide
from server.operations.registry import OPERATIONS_BY_NAME
from server.operations.toolsets import _MCP_HIDDEN_OPS
from server.transport import gates


def _guide(part: str):
    return asyncio.run(handle_mentor_guide(MentorGuideInput(part=part)))


def test_skill_part_is_the_body_without_frontmatter_and_carries_the_version() -> None:
    out = _guide("skill")
    assert out.version == server.__version__
    assert out.text.startswith("# loom")
    assert "description:" not in out.text


def test_all_is_the_skill_followed_by_the_reference() -> None:
    skill, reference, both = _guide("skill").text, _guide("reference").text, _guide("all").text
    assert both == skill + "\n" + reference
    assert reference == (AGENT_GUIDE_DIR / "reference.md").read_text(encoding="utf-8")


# Dotted names in the guide that are not ops: SDK call paths and the guide's own files.
_NOT_OPS = ("agent.", "loom.", "reference.md")


def _named_ops(text: str) -> set[str]:
    # A backticked dotted name, alone or opening a call (`run.begin(goal=...)`).
    names = set(re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)[`(]", text))
    return {n for n in names if not n.startswith(_NOT_OPS)}


def test_every_op_the_guide_names_is_registered() -> None:
    named = _named_ops(_guide("all").text)
    assert named, "the guide names no op"
    assert named <= set(OPERATIONS_BY_NAME), sorted(named - set(OPERATIONS_BY_NAME))


def test_the_skill_lists_every_experimental_op_it_names() -> None:
    text = _guide("skill").text
    listed = re.search(r"Experimental ops this file names: (.*?)\.\n\n", text, re.S)
    assert listed, "SKILL.md lists its experimental ops"
    experimental = {n for n in _named_ops(text) if OPERATIONS_BY_NAME[n].maturity == "experimental"}
    assert set(re.findall(r"`([a-z_.]+)`", listed.group(1))) == experimental


def test_ops_in_the_reference_carry_the_experimental_and_not_on_mcp_markers() -> None:
    for row in re.findall(r"^\| (`[^|]+)\|", _guide("reference").text, re.M):
        for name, experimental, hidden in re.findall(r"`([a-z_.]+)`(†?)(‡?)", row):
            if name in OPERATIONS_BY_NAME:
                assert bool(experimental) == (
                    OPERATIONS_BY_NAME[name].maturity == "experimental"
                ), name
                assert bool(hidden) == (name in _MCP_HIDDEN_OPS), name


def test_every_op_the_skill_names_that_mcp_refuses_carries_the_marker() -> None:
    text = _guide("skill").text
    for name in _named_ops(text) & _MCP_HIDDEN_OPS:
        assert f"`{name}`‡" in text, name


# Every kind of token that may read the guide, with the roles the server gives it
# (an end user's: server/auth/jwt.py; an end user reaches it over HTTP, not MCP).
_CALLERS = {
    "viewer": AuthInfo(
        subject="sa:bot",
        org="acme",
        namespaces=("n",),
        roles=(Role.VIEWER,),
        is_service_account=True,
    ),
    "narrowed-to-one-user": AuthInfo(
        subject="sa:bot",
        org="acme",
        namespaces=("n",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        read_scope="own_user",
        read_user_id="u1",
    ),
    "end-user": AuthInfo(
        subject="user:u1",
        org="acme",
        namespaces=(),
        roles=(Role.VIEWER,),
        is_service_account=False,
        read_scope="own_user",
        read_user_id="u1",
    ),
}


@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_every_token_that_may_read_the_guide_is_admitted(caller: str) -> None:
    # The guide holds no memory.
    auth = _CALLERS[caller]
    op = OPERATIONS_BY_NAME["mentor.guide"]
    target = gates.authz_target(op, org="acme", namespace=None, auth=auth)
    asyncio.run(gates.authorize(op, auth, target, transport="http"))
    gates.read_scope(op, auth, MentorGuideInput())


def test_the_image_ships_the_guide() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    assert "COPY skills/loom ./skills/loom" in dockerfile
