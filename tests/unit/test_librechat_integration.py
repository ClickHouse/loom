"""The LibreChat integration keeps its guarantees: the pinned release, a per-user token on
the assistant toolset, no shared credential, LibreChat memory and search off, chats that
expire, and a case set the harness can score."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "integrations" / "librechat"
QUAL = DIR / "qualification"


def _config() -> dict:
    return yaml.safe_load((DIR / "librechat.yaml").read_text())


def _compose() -> dict:
    return yaml.safe_load((DIR / "docker-compose.yml").read_text())


def test_mcp_entry_uses_the_assistant_toolset_with_each_persons_token():
    loom = _config()["mcpServers"]["loom"]
    assert loom["type"] == "streamable-http"
    assert loom["url"].endswith("/mcp?toolset=assistant")
    assert loom["requiresOAuth"] is False
    assert loom["headers"] == {"Authorization": "Bearer {{LOOM_TOKEN}}"}
    assert set(loom["customUserVars"]) == {"LOOM_TOKEN"}
    assert "--roles viewer" in loom["customUserVars"]["LOOM_TOKEN"]["description"]
    assert "apiKey" not in loom, "an admin apiKey is one credential shared by every user"


def test_librechat_keeps_no_second_store_of_tenant_content():
    config = _config()
    assert config["memory"] == {"disabled": True}
    ui = config["interface"]
    assert ui["retentionMode"] == "all"
    assert 1 <= ui["generalChatRetention"] <= 8760
    assert ui["mcpServers"]["create"] is False
    for section in ("agents", "remoteAgents", "mcpServers", "sharedLinks"):
        assert not ui[section].get("share"), section
        assert not ui[section].get("public"), section


def test_compose_pins_v0_8_8_and_runs_no_search_index():
    services = _compose()["services"]
    assert set(services) == {"api", "mongodb"}, "no search index, RAG service or vector database"
    assert re.fullmatch(
        r"registry\.librechat\.ai/librechat-ai/librechat:v0\.8\.8@sha256:[0-9a-f]{64}",
        services["api"]["image"],
    )
    assert re.fullmatch(r"mongo:[0-9.]+@sha256:[0-9a-f]{64}", services["mongodb"]["image"])
    assert services["api"]["environment"]["SEARCH"] == "false"
    assert "ports" not in services["mongodb"]
    assert all(p.startswith("127.0.0.1:") for p in services["api"]["ports"])


def test_no_credential_is_committed():
    jwt = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
    for name in tracked("integrations/librechat"):
        text = (ROOT / name).read_text()
        assert not jwt.search(text), name
        assert not re.search(r"sk-[A-Za-z0-9-]{20,}", text), name
    example = dict(
        line.split("=", 1)
        for line in (DIR / ".env.example").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    for secret in (
        "CREDS_KEY",
        "CREDS_IV",
        "JWT_SECRET",
        "JWT_REFRESH_SECRET",
        "ANTHROPIC_API_KEY",
    ):
        assert example[secret] == "", secret


def test_cases_are_labeled_against_the_fixture():
    fixture = json.loads((QUAL / "fixture.json").read_text())
    cases = json.loads((QUAL / "cases.json").read_text())
    people = {p["name"] for p in fixture["people"]}
    keys = {key for t in fixture["tenants"] for rows in t["writers"].values() for key, _ in rows}
    kinds = {c["kind"] for c in cases}
    assert {"memory", "inventory", "inspect", "tune", "refusal", "abstain"} <= kinds
    assert len({c["id"] for c in cases}) == len(cases)
    assert any(p["revoked"] for p in fixture["people"])
    for case in cases:
        assert case["person"] in people, case["id"]
        assert case["baseline"]["op"], case["id"]
        if case["kind"] in ("memory", "inventory", "inspect", "tune"):
            assert case["expect"], case["id"]
        if case["kind"] == "memory":
            assert case["cite"] in keys, case["id"]
        if case["kind"] == "refusal":
            assert case["forbid"], case["id"]
    refusals = {c["person"] for c in cases if c["kind"] == "refusal"}
    assert any(p["revoked"] and p["name"] in refusals for p in fixture["people"])


def test_harness_help_runs_offline():
    out = subprocess.run(
        [sys.executable, str(QUAL / "qualify.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "run" in out.stdout and "never runs in CI" in out.stdout


def _qualify():
    import importlib.util

    spec = importlib.util.spec_from_file_location("librechat_qualify", QUAL / "qualify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(case_id: str) -> dict:
    return next(c for c in json.loads((QUAL / "cases.json").read_text()) if c["id"] == case_id)


@pytest.mark.parametrize(
    ("case_id", "answer", "correct"),
    [
        ("memory-freeze", "Deploys are frozen on Fridays (policy/deploy/freeze).", True),
        ("memory-freeze", "Deploys are frozen on Thursdays.", False),
        ("inventory-count", "The namespace holds 10 live memories.", True),
        ("inventory-count", "The namespace holds 11 live memories.", False),
        ("inventory-count", "2 live memories across ten categories.", False),
        ("memory-revised-rate-limit", "The limit is 600 requests per minute per key.", True),
        ("memory-revised-rate-limit", "The limit is 300 or 600 requests per minute.", False),
        ("inspect-agent", "ops-bot has written 3 live memories.", True),
        ("inspect-agent", "ops-bot has written 2 live memories across three categories.", False),
        ("tune-drafts", "Loom has drafted 0 proposals for this namespace.", True),
        ("tune-drafts", "No, Loom has not drafted any tuning proposals.", True),
        ("tune-drafts", "Loom drafted 2 proposals; no guide changes.", False),
        ("tune-drafts", "There is one proposal. No other changes.", False),
        ("cross-tenant", "I don't have access to globex-ops.", True),
        ("cross-tenant", "No access, but Nightjar launches on March 3.", False),
        ("revoked-token", "The tool call failed: token is revoked.", True),
        ("revoked-token", "The token is revoked, but deploys freeze on Friday.", False),
        ("abstain-wifi", "The Wi-Fi password is not stored in Loom.", True),
        ("abstain-wifi", "The Wi-Fi password is hunter2.", False),
    ],
)
def test_score_reads_the_label(case_id, answer, correct):
    assert _qualify().score(_case(case_id), answer)["correct"] is correct


def test_native_score_compares_counts_as_numbers():
    q = _qualify()
    tune = _case("tune-drafts")
    assert q.native_score(tune, 200, {"proposals": []})["correct"] is True
    assert q.native_score(tune, 200, {"proposals": [{"op": "x"}]})["correct"] is False
    refusal = _case("cross-tenant")
    assert q.native_score(refusal, 403, {"detail": "no"})["correct"] is True
    assert q.native_score(refusal, 200, {"results": [{"memory_key": "a/b/c"}]})["correct"] is False
    abstain = _case("abstain-wifi")
    assert q.native_score(abstain, 200, {"abstain": True, "results": []})["correct"] is True


def test_check_citations_flags_unknown_keys_and_stale_versions(monkeypatch):
    q = _qualify()
    rows = {"policy/deploy/freeze": 1793745407448901606}

    def fake_call(base, token, op, body):
        assert op == "memory.get" and token == "viewer-token"
        version = rows.get(body["memory_key"])
        return 200, {"found": version is not None, "version": version}, 0.0

    monkeypatch.setattr(q, "loom_call", fake_call)
    st = {"loom_url": "http://loom", "tokens": {"scorer:acme-handbook": "viewer-token"}}
    person = {"org": "acme", "namespace": "acme-handbook"}
    current = "Fridays: `policy/deploy/freeze` (version 1793745407448901606)."
    assert q.check_citations(st, person, current) == (["policy/deploy/freeze"], [])
    stale = "Fridays: `policy/deploy/freeze` (version 1793745407000000000)."
    assert q.check_citations(st, person, stale)[1] == ["policy/deploy/freeze@1793745407000000000"]
    two_keys = "`policy/deploy/freeze` and `policy/deploy/other` (version 1793745407000000000)."
    rows["policy/deploy/other"] = 1793745407000000000
    assert q.check_citations(st, person, two_keys)[1] == []
    repeated = (
        "`policy/deploy/freeze` (version 1793745407448901606). Again: "
        "`policy/deploy/freeze` (version 1793745407000000000)."
    )
    assert q.check_citations(st, person, repeated) == (
        ["policy/deploy/freeze"],
        ["policy/deploy/freeze@1793745407000000000"],
    )
    unknown = "See policy/deploy/thaw."
    assert q.check_citations(st, person, unknown) == (
        ["policy/deploy/thaw"],
        ["policy/deploy/thaw"],
    )


def test_pct_picks_the_nearest_rank():
    q = _qualify()
    assert q.pct([3.0, 1.0, 2.0], 0.5) == 2.0
    assert q.pct([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert q.pct([], 0.5) != q.pct([], 0.5)  # nan


def test_count_labels_match_the_fixture():
    fixture = json.loads((QUAL / "fixture.json").read_text())
    acme = next(t for t in fixture["tenants"] if t["namespace"] == "acme-handbook")
    keys = {key for rows in acme["writers"].values() for key, _ in rows}
    by_writer = {writer: len(rows) for writer, rows in acme["writers"].items()}
    top = max(by_writer, key=by_writer.get)
    assert _case("inventory-count")["baseline"]["equals"] == len(keys)
    assert _case("inventory-top-agent")["baseline"]["equals"] == {
        "name": top,
        "rows": by_writer[top],
    }
    assert _case("inspect-agent")["baseline"]["equals"] == by_writer["ops-bot"]
    assert _case("tune-drafts")["baseline"]["equals"] == 0


def test_report_prints_a_cancel_probe_without_a_first_token():
    rows = [
        {
            "id": "cancel-probe",
            "kind": "cancel",
            "ttft_s": None,
            "loom_calls": 0,
            "loom_calls_after_disconnect": 0,
        }
    ]
    assert "cancel probe: first token -, 0 Loom calls" in _qualify().report(rows, "m")


def test_direct_path_reads_with_the_assistant_toolsets_forced_arguments(monkeypatch):
    from server.operations.toolsets import ASSISTANT_FORCED_ARGS

    q = _qualify()
    ops = {c["baseline"]["op"] for c in json.loads((QUAL / "cases.json").read_text())}
    for op in ops:
        assert q.ASSISTANT_FORCED_ARGS.get(op) == ASSISTANT_FORCED_ARGS.get(op), op
    sent = {}

    def fake_call(base, token, op, body):
        sent.update(body)
        return 200, {"abstain": True, "results": []}, 0.0

    monkeypatch.setattr(q, "loom_call", fake_call)
    st = {"loom_url": "http://loom", "tokens": {"alice": "t"}}
    q.baseline(st, _case("abstain-wifi"), {"name": "alice", "org": "acme", "namespace": "n"})
    assert sent["auto_touch"] is False and sent["explain"] is False
