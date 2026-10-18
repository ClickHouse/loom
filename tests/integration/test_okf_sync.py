"""export-okf → import-okf from one tenant to another, as the scheduled private → public
copy runs it, with a viewer token on the source and a writer token on the target: a second
import of the same bundle writes no version and no audit row, a `high` row stays home, and
`--prune` removes a copy whose source was deleted while rows other writers put in the
target, under another agent or under the import's own agent name, stay."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

from tests.integration._resources import unique_org

SRC_ORG, DST_ORG = unique_org("okfsrc"), unique_org("okfdst")
ROOT = Path(__file__).resolve().parents[2]


def _op(url: str, token: str, org: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{url}/v1/{op}", json={"org": org, **body}, headers={"Authorization": f"Bearer {token}"}
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
        url,
        token,
        DST_ORG,
        "memory.history",
        {"namespace": ns, "agent": "okf-sync", "memory_key": key},
    )["rows"]
    return len(rows)


def _audit_rows(ch_client: Any, ns: str) -> int:
    res = ch_client.query(
        "SELECT count() FROM loom.audit_log WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": DST_ORG, "ns": ns},
    )
    return int(res.result_rows[0][0])


def test_reimport_writes_nothing_and_prune_removes_deleted_copies(
    control_plane_url: str, mint_test_token: Any, ch_client: Any, tmp_path: Path
) -> None:
    ns = f"okf-{uuid.uuid4().hex[:6]}"
    url = control_plane_url
    seed = mint_test_token(org=SRC_ORG, namespaces=[ns], subject="seeder")
    reader = mint_test_token(org=SRC_ORG, namespaces=[ns], roles=["viewer"], subject="reader")
    writer = mint_test_token(org=DST_ORG, namespaces=[ns], subject="okf-sync-writer")
    bot = mint_test_token(org=DST_ORG, namespaces=[ns], subject="other-bot")
    keys = [f"lesson-{i}" for i in range(3)]
    for k in keys:
        _op(
            url,
            seed,
            SRC_ORG,
            "memory.set",
            {
                "namespace": ns,
                "agent": "writer-a",
                "memory_key": k,
                "memory_type": "semantic",
                "value": f"{k}: the test fails when the part count passes {len(k) * 100}.",
                "tags": ["lesson"],
                "entities": ["MergeTree"],
                # Compared through memory.bulk_get rather than memory.list.
                "valid_to": "2030-01-01T00:00:00",
                "source_class": "derived",
            },
        )
    _op(
        url,
        seed,
        SRC_ORG,
        "memory.link",
        {"namespace": ns, "from_key": keys[0], "to_key": keys[1], "relation": "supports"},
    )
    # A high row stays on the source side.
    _op(
        url,
        seed,
        SRC_ORG,
        "memory.set",
        {
            "namespace": ns,
            "agent": "writer-a",
            "memory_key": "secret",
            "value": "s3cret",
            "sensitivity": "high",
        },
    )
    # Rows other writers put in the copy's namespace: prune must leave them, including one
    # written under the import's own agent name with an okf-looking trace.
    _op(
        url,
        writer,
        DST_ORG,
        "memory.set",
        {"namespace": ns, "agent": "bot", "memory_key": "bot-note", "value": "kept"},
    )
    _op(
        url,
        bot,
        DST_ORG,
        "memory.set",
        {
            "namespace": ns,
            "agent": "okf-sync",
            "memory_key": "bot-as-sync",
            "value": "kept too",
            "source_trace": "okf:bot.md",
        },
    )

    def sync(run: int, *flags: str) -> str:
        # A fresh directory each run, as the scheduled job's emptyDir gives it.
        bundle = tmp_path / str(run)
        _loom(
            url,
            reader,
            "export-okf",
            "--org",
            SRC_ORG,
            "--namespace",
            ns,
            "--no-evidence",
            "--out",
            str(bundle),
        )
        return _loom(
            url,
            writer,
            "import-okf",
            str(bundle),
            "--org",
            DST_ORG,
            "--namespace",
            ns,
            "--agent",
            "okf-sync",
            *flags,
        )

    def get(agent: str, key: str) -> dict[str, Any]:
        return _op(
            url, writer, DST_ORG, "memory.get", {"namespace": ns, "agent": agent, "memory_key": key}
        )

    first = sync(1)
    assert "3 written, 0 unchanged, 0 failed" in first
    assert not get("okf-sync", "secret")["found"]
    copied = get("okf-sync", keys[0])
    assert str(copied["valid_to"]).startswith("2030-01-01")
    assert copied["source_class"] == "derived"
    versions = {k: _versions(url, writer, ns, k) for k in keys}
    audit = _audit_rows(ch_client, ns)

    second = sync(2)
    assert "0 written, 3 unchanged, 0 failed" in second
    assert "0 typed edges linked (1 unchanged)" in second
    assert {k: _versions(url, writer, ns, k) for k in keys} == versions
    assert _audit_rows(ch_client, ns) == audit

    _op(
        url,
        seed,
        SRC_ORG,
        "memory.delete",
        {"namespace": ns, "agent": "writer-a", "memory_key": keys[2]},
    )
    third = sync(3, "--prune")
    assert "0 written, 2 unchanged, 0 failed" in third
    assert "1 pruned (1 kept: written by another token under this --agent" in third

    assert not get("okf-sync", keys[2])["found"]
    assert get("okf-sync", keys[0])["found"]
    assert get("bot", "bot-note")["found"]
    assert get("okf-sync", "bot-as-sync")["found"]
    # A prune is a tombstone: memory.restore brings the copy back.
    restored = _op(
        url,
        writer,
        DST_ORG,
        "memory.restore",
        {"namespace": ns, "agent": "okf-sync", "memory_key": keys[2]},
    )
    assert restored["restored"] and get("okf-sync", keys[2])["found"]
