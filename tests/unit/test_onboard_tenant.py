"""`dev/onboard_tenant.py` can talk to a deployment with auth on.

Its `/v1/admin/*` posts carry a bearer
(`--token`, `$LOOM_ADMIN_TOKEN`, else the ADMIN-bearing service-account token
loom_init just minted) and the success output ends with next steps an
operator can paste as-is.
"""

from __future__ import annotations

import io
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dev import loom_init, onboard_tenant  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _capture_urlopen(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    seen: list[urllib.request.Request] = []

    def _fake(req: urllib.request.Request, timeout: float = 0) -> _Resp:
        seen.append(req)
        return _Resp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return seen


def test_post_json_sends_the_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_urlopen(monkeypatch)
    onboard_tenant._post_json("http://x/v1/admin/ratelimit.set", {"a": 1}, token="tok-1")
    assert len(seen) == 1
    assert seen[0].get_header("Authorization") == "Bearer tok-1"
    assert seen[0].get_header("Content-type") == "application/json"


def test_post_json_without_token_sends_no_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_urlopen(monkeypatch)
    onboard_tenant._post_json("http://x/v1/admin/budget.set", {})
    assert seen[0].get_header("Authorization") is None


def test_rate_limit_and_budget_calls_carry_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_urlopen(monkeypatch)
    onboard_tenant._apply_rate_limits("http://x/", "ns", {"memory": (1.0, 1.0)}, token="t")
    onboard_tenant._apply_budget_overrides("http://x/", {"memory.set": 10}, token="t")
    assert [r.get_header("Authorization") for r in seen] == ["Bearer t", "Bearer t"]
    assert [r.full_url for r in seen] == [
        "http://x/v1/admin/ratelimit.set",
        "http://x/v1/admin/budget.set",
    ]


def test_token_resolution_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("LOOM_BASE_URL=http://x\nLOOM_API_KEY=from-env-file\n")
    monkeypatch.delenv("LOOM_ADMIN_TOKEN", raising=False)
    assert onboard_tenant._resolve_admin_token(None, env_path) == "from-env-file"
    monkeypatch.setenv("LOOM_ADMIN_TOKEN", "from-process-env")
    assert onboard_tenant._resolve_admin_token(None, env_path) == "from-process-env"
    assert onboard_tenant._resolve_admin_token("explicit", env_path) == "explicit"


def test_next_steps_are_pasteable_and_never_print_the_token() -> None:
    text = onboard_tenant._next_steps(
        base_url="http://loom.example:7777/",
        org="dev",
        namespace="eng-tooling",
        agent="detector",
        env_path=Path("/srv/out/.env"),
    )
    assert "set -a && . /srv/out/.env && set +a" in text
    assert (
        "claude mcp add --transport http loom http://loom.example:7777/mcp?toolset=coding" in text
    )
    assert '-H "Authorization: Bearer $LOOM_API_KEY" -s user' in text
    assert "from loom import Agent" in text
    assert "namespace='eng-tooling'" in text and "api_key" not in text
    assert "http://loom.example:7777/" in text
    assert "http://loom.example:7777//" not in text


def test_anchor_overrides_are_the_multiplier_times_the_server_defaults() -> None:
    from server.core.ratelimit import DEFAULT_LIMITS

    out = onboard_tenant.anchor_overrides(3.0)
    assert set(out) == {"memory", "knowledge.search", "knowledge.read", "knowledge.add"}
    for cls, (cap, rate) in out.items():
        assert cap == DEFAULT_LIMITS[cls].capacity * 3
        assert rate == DEFAULT_LIMITS[cls].rate * 3


def test_bad_arguments_exit_2_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(**_: Any) -> None:
        raise AssertionError("loom_init must not run")

    monkeypatch.setattr(onboard_tenant, "_run_loom_init", _boom)
    base = ["--namespace", "ns", "--agent", "a", "--output-dir", str(tmp_path)]
    assert onboard_tenant.main([*base, "--budget-override", "memory.set"]) == 2
    assert onboard_tenant.main([*base, "--budget-override", "memory.set=fast"]) == 2
    assert onboard_tenant.main([*base, "--rate-limit-multiplier", "0"]) == 2
    assert onboard_tenant.parse_budget_overrides(["memory.set=250"]) == {"memory.set": 250}


def test_a_failed_step_exits_1_with_its_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fail(**_: Any) -> None:
        raise RuntimeError("loom_init failed (exit 2)")

    monkeypatch.setattr(onboard_tenant, "_run_loom_init", _fail)
    argv = ["--namespace", "ns", "--agent", "a", "--output-dir", str(tmp_path)]
    assert onboard_tenant.main(argv) == 1
    assert "loom_init failed" in capsys.readouterr().err


def test_loom_init_is_passed_the_org(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": True, "env_path": "/x/.env"})
        stderr = ""

    def _run(cmd: list[str], **_: Any) -> _Proc:
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(onboard_tenant.subprocess, "run", _run)
    onboard_tenant._run_loom_init(
        org="acme",
        namespace="ns",
        agent="a",
        base_url="http://x",
        output_dir=Path("/x"),
        force=False,
    )
    cmd = calls[0]
    assert cmd[cmd.index("--org") + 1] == "acme"


def test_default_org_is_dev_and_help_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    """The help text renders `%(default)s`, so it cannot drift from the
    default."""
    for module in (loom_init, onboard_tenant):
        with pytest.raises(SystemExit) as ei:
            module.main(["--help"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        org_help = re.search(r"--org ORG\s+(.*?)(?=\n\s+--|\n\n)", out, re.S)
        assert org_help, out
        assert "default: dev" in " ".join(org_help.group(1).split())
        assert "acme" not in out and "clickhouse" not in org_help.group(1)


def test_loom_init_points_at_a_docs_file_that_exists() -> None:
    assert "./ARCHITECTURE.md" not in loom_init.STARTER_TEMPLATE
    assert "docs/index.md" in loom_init.STARTER_TEMPLATE
    assert (ROOT / "docs" / "index.md").exists()


def test_loom_init_writes_an_owner_only_env(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert loom_init.main(["--namespace", "ns", "--agent", "a", "--output-dir", str(out)]) == 0
    env = out / ".env"
    assert env.stat().st_mode & 0o777 == 0o600
    assert "LOOM_API_KEY=" in env.read_text()
    env.chmod(0o644)
    argv = ["--namespace", "ns", "--agent", "a", "--output-dir", str(out), "--force"]
    assert loom_init.main(argv) == 0
    assert env.stat().st_mode & 0o777 == 0o600


def _starter_server(search_hits: list[dict[str, Any]]) -> tuple[Any, list[str]]:
    import httpx

    calls: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        calls.append(op)
        body = json.loads(req.content or b"{}")
        if op == "memory.get":
            return httpx.Response(200, json={"found": True, "version": 1, **body})
        if op == "knowledge.search":
            return httpx.Response(200, json={"results": search_hits})
        return httpx.Response(200, json={"ok": True, "tombstoned": True})

    return httpx.MockTransport(handle), calls


_HIT = {
    "source_uri": "u",
    "chunk_idx": 0,
    "content": "x",
    "tags": [],
    "distance": 0.1,
    "similarity": 0.9,
}


@pytest.mark.parametrize("hits", [[], [_HIT]])
async def test_starter_deletes_what_it_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hits: list[dict[str, Any]]
) -> None:
    import functools
    import importlib.util

    import httpx

    transport, calls = _starter_server(hits)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )
    for k, v in {"LOOM_ORG": "o", "LOOM_NAMESPACE": "n", "LOOM_AGENT": "a"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LOOM_API_KEY", "k")
    path = tmp_path / "loom_starter.py"
    path.write_text(loom_init.STARTER_TEMPLATE)
    spec = importlib.util.spec_from_file_location("loom_starter_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = await mod.main()
    assert rc == (0 if hits else 1)
    assert "memory.delete" in calls and "knowledge.delete" in calls


def _refusing_onboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> int:
    """`main` with loom_init faked and every admin call refused with 403."""
    env_path = tmp_path / ".env"
    env_path.write_text("LOOM_API_KEY=namespace-admin\n")

    def fake_init(**kw: object) -> dict:
        return {"env_path": str(env_path), "subject": "sa:x"}

    def refuse(url: str, body: dict, timeout_s: float = 5.0, *, token: str = "") -> dict:
        raise onboard_tenant.AdminRefused(f"{url} returned HTTP 403")

    monkeypatch.setattr(onboard_tenant, "_run_loom_init", fake_init)
    monkeypatch.setattr(onboard_tenant, "_post_json", refuse)
    base = ["--namespace", "ns", "--agent", "a", "--output-dir", str(tmp_path), "--skip-smoke"]
    return onboard_tenant.main([*base, "--json", *argv])


def test_without_a_platform_admin_token_the_admin_steps_are_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LOOM_ADMIN_TOKEN", raising=False)
    assert _refusing_onboard(monkeypatch, tmp_path, []) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["ratelimits"] == {}
    assert "platform_admin" in out.err


def test_a_refused_explicit_admin_token_fails_the_onboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOOM_ADMIN_TOKEN", raising=False)
    assert _refusing_onboard(monkeypatch, tmp_path, ["--token", "weak"]) == 1
