"""The agent setup contract: the release renders the page with its version and commit
filled in and every link spelled out, and the page's check script calls only ops the
server serves, with fields they have."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest

from scripts.release import agent_setup
from server.operations.registry import OPERATIONS_BY_NAME

SHA = "0123456789abcdef0123456789abcdef01234567"
PAGE = agent_setup.PAGE.read_text()


def test_the_release_text_fills_in_every_version_and_commit() -> None:
    text = agent_setup.render(PAGE, "1.2.3", SHA)
    assert "<version>" not in text and "<commit>" not in text and "<!--" not in text
    assert f"tagged `v1.2.3` at commit `{SHA}`" in text
    assert "releases/download/v1.2.3/install.sh" in text
    assert "archive/refs/tags/v1.2.3.tar.gz" in text


def test_the_rendered_script_carries_the_pins_and_nothing_else_changes() -> None:
    page_script = agent_setup.check_script(PAGE)
    script = agent_setup.check_script(agent_setup.render(PAGE, "1.2.3", SHA))
    pinned = page_script.replace("<version>", "1.2.3").replace("<commit>", SHA)
    assert script == pinned
    assert f'PIN_VERSION = "1.2.3"\nPIN_COMMIT = "{SHA}"' in script


def test_every_link_is_spelled_out_at_the_release_tag() -> None:
    text = agent_setup.render(PAGE, "1.2.3", SHA)
    assert not agent_setup.LINK.search(text)
    assert (
        "quickstart (https://github.com/ClickHouse/loom/blob/v1.2.3/"
        "docs/getting-started/quickstart.md#1-boot-the-stack)"
    ) in text


def test_an_anchor_keeps_its_text_and_an_absolute_link_its_url() -> None:
    page = (
        "<!-- pins -->\n<!-- /pins -->\nSee [Pins](#pins) and [the site](https://example.com/x).\n"
    )
    text = agent_setup.render(page, "1.2.3", SHA)
    assert "See Pins and the site (https://example.com/x)." in text


def test_a_page_without_its_pins_block_is_refused() -> None:
    with pytest.raises(ValueError, match="pins"):
        agent_setup.render("# no pins here\n", "1.2.3", SHA)


def _checks() -> dict[str, tuple[dict[str, set[str]], set[str]]]:
    """For each function of the check script: the fields it sends each op, and the
    strings it indexes an answer with or hands hit_problem as a field name."""
    found = {}
    for func in ast.parse(agent_setup.check_script(PAGE)).body:
        if not isinstance(func, ast.FunctionDef):
            continue
        sent: dict[str, set[str]] = {}
        read: set[str] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "call":
                    op = node.args[0]
                    assert isinstance(op, ast.Constant)
                    sent.setdefault(op.value, set()).update(
                        kw.arg for kw in node.keywords if kw.arg
                    )
                elif node.func.id == "hit_problem":
                    read |= {arg.value for arg in node.args if isinstance(arg, ast.Constant)}
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                if isinstance(node.slice.value, str):
                    read.add(node.slice.value)
        if sent:
            found[func.name] = (sent, read)
    return found


def test_the_check_script_sends_each_op_only_its_own_fields() -> None:
    checks = _checks()
    assert set(checks) == {"memory", "knowledge", "cleanup"}
    for sent, _ in checks.values():
        for op, fields in sent.items():
            known = set(OPERATIONS_BY_NAME[op].input_model.model_fields)
            assert fields | {"org", "namespace"} <= known, (op, fields - known)


def test_each_check_reads_only_fields_its_own_ops_answer() -> None:
    for name, (sent, read) in _checks().items():
        answered: set[str] = set()
        for op in sent:
            fields = OPERATIONS_BY_NAME[op].output_model.model_fields
            answered |= set(fields)
            if "results" in fields:
                (hit,) = get_args(fields["results"].annotation)
                answered |= set(hit.model_fields)
        assert read and read <= answered, (name, read - answered)


def _run_offline(script: str, home: Path, base_url: str) -> subprocess.CompletedProcess[str]:
    """The script with no server to reach: each case is refused before any call."""
    path = home / "loom-setup-check.py"
    path.write_text(script)
    env = {"HOME": str(home), "SETUP_CHECK_URL": base_url, "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(path)], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize(
    ("rendered", "base_url", "bind", "said"),
    [
        (False, "http://localhost:9", "", "this copy names no release"),
        (True, "http://loom.example.com:7777", "", "is not a loopback address"),
        (True, "http://localhost:9", "0.0.0.0", "publishes Loom on 0.0.0.0"),
    ],
)
def test_the_server_check_refuses_before_any_call(
    tmp_path: Path, rendered: bool, base_url: str, bind: str, said: str
) -> None:
    text = agent_setup.render(PAGE, "1.2.3", SHA) if rendered else PAGE
    if bind:
        (tmp_path / ".loom").mkdir()
        (tmp_path / ".loom" / ".env").write_text(f"LOOM_PORT=7777\nLOOM_BIND_HOST={bind}\n")
    done = _run_offline(agent_setup.check_script(text), tmp_path, base_url)
    assert done.returncode == 1, done.stdout + done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("  [")]
    assert len(lines) == 1 and lines[0].startswith("  [FAIL]  server") and said in lines[0], lines
