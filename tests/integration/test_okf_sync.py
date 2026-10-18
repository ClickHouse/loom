"""export-okf → import-okf between two namespaces, as the scheduled copy runs it: a
second import of the same bundle writes no version and no audit row, and `--prune`
removes a copy whose source was deleted while another writer's row stays."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from tests.integration._resources import unique_org

ORG = unique_org("okfsync")
ROOT = Path(__file__).resolve().parents[2]


def _op(url: str, token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{url}/v1/{op}", json={"org": ORG, **body}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


def _loom(url: str, token: str, *argv: str) -> str:
    env = {**os.environ, "LOOM_ENV_FILE": ""}
    p = subprocess.run(
        [sys.executable, "-m", "server.cli", *argv, "--server-url", url, "--token", token],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def _versions(url: str, token: str, ns: str, key: str) -> int:
    rows = _op(
        url, token, "memory.history", {"namespace": ns, "agent": "okf-sync", "memory_key": key}
    )["rows"]
    return len(rows)


def _audit_rows(ch_client: Any, ns: str) -> int:
    res = ch_client.query(
        "SELECT count() FROM loom.audit_log WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ns},
    )
    return int(res.result_rows[0][0])


def test_reimport_writes_nothing_and_prune_removes_deleted_copies(
    control_plane_url: str, mint_test_token: Any, ch_client: Any, tmp_path: Path
) -> None:
    src, dst = f"okf-src-{uuid.uuid4().hex[:6]}", f"okf-dst-{uuid.uuid4().hex[:6]}"
    url, tok = control_plane_url, mint_test_token(org=ORG, namespaces=[src, dst])
    keys = [f"lesson-{i}" for i in range(3)]
    for k in keys:
        _op(
            url,
            tok,
            "memory.set",
            {
                "namespace": src,
                "agent": "clickgap",
                "memory_key": k,
                "memory_type": "semantic",
                "value": f"{k}: the test fails when the part count passes {len(k) * 100}.",
                "tags": ["lesson"],
                "entities": ["MergeTree"],
            },
        )
    _op(
        url,
        tok,
        "memory.link",
        {"namespace": src, "from_key": keys[0], "to_key": keys[1], "relation": "supports"},
    )
    # Another writer's row in the copy's namespace: prune must leave it.
    _op(
        url,
        tok,
        "memory.set",
        {"namespace": dst, "agent": "bot", "memory_key": "bot-note", "value": "kept"},
    )

    def sync(run: int, *flags: str) -> str:
        bundle = tmp_path / str(run) / "bundle"
        _loom(url, tok, "export-okf", "--namespace", src, "--no-evidence", "--out", str(bundle))
        return _loom(
            url, tok, "import-okf", str(bundle), "--namespace", dst, "--agent", "okf-sync", *flags
        )

    first = sync(1)
    assert "3 written, 0 unchanged, 0 failed" in first
    versions = {k: _versions(url, tok, dst, k) for k in keys}
    audit = _audit_rows(ch_client, dst)

    second = sync(2)
    assert "0 written, 3 unchanged, 0 failed" in second
    assert "0 typed edges linked (1 unchanged)" in second
    assert {k: _versions(url, tok, dst, k) for k in keys} == versions
    assert _audit_rows(ch_client, dst) == audit

    _op(url, tok, "memory.delete", {"namespace": src, "agent": "clickgap", "memory_key": keys[2]})
    third = sync(3, "--prune")
    assert "0 written, 2 unchanged, 0 failed" in third
    assert "1 pruned" in third

    def found(agent: str, key: str) -> bool:
        got = _op(url, tok, "memory.get", {"namespace": dst, "agent": agent, "memory_key": key})
        return bool(got["found"])

    assert not found("okf-sync", keys[2])
    assert found("okf-sync", keys[0])
    assert found("bot", "bot-note")
