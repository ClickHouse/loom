"""The LibreChat integration keeps its guarantees: the pinned release, a per-user token on
the assistant toolset, no shared credential, LibreChat memory and search off, chats that
expire, and a case set the harness can score."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
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
    assert {"memory", "inventory", "inspect", "tune", "refusal", "abstain", "document"} <= kinds
    assert len({c["id"] for c in cases}) == len(cases)
    assert any(p["revoked"] for p in fixture["people"])
    for case in cases:
        assert case["person"] in people, case["id"]
        assert case["baseline"]["op"], case["id"]
        if case["kind"] in ("memory", "inventory", "inspect", "tune", "document"):
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
        ("abstain-wifi", "Nothing stored in Loom answers this.", True),
        ("abstain-wifi", "**Nothing stored in Loom answers this.**", True),
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
    # An earlier version of a key is a valid citation of what it once held.
    history = {"policy/deploy/freeze": [1793745407448901606, 1793745400000000000]}

    def fake_call(base, token, op, body):
        assert op in ("memory.get", "memory.history") and token == "viewer-token"
        if op == "memory.history":
            versions = history.get(body["memory_key"], [])
            return 200, {"rows": [{"version": v} for v in versions]}, 0.0
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
    earlier = "Before: `policy/deploy/freeze` (version 1793745400000000000)."
    assert q.check_citations(st, person, earlier, history_ok=True) == (["policy/deploy/freeze"], [])
    # Outside a question about history, an earlier version cited is stale.
    assert q.check_citations(st, person, earlier)[1] == ["policy/deploy/freeze@1793745400000000000"]
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


_PROBE = {
    "id": "cancel-probe",
    "kind": "cancel",
    "ended_before_cut": False,
    "error": "",
    "loom_calls_before_disconnect": 0,
    "loom_calls_after_disconnect": 0,
}


@pytest.mark.parametrize(
    ("row", "verdict"),
    [
        ({}, "no Loom call on either side of the cut (0 Loom calls"),
        ({"loom_calls_after_disconnect": 1}, "the run went on after the disconnect"),
        (
            {"loom_calls_before_disconnect": 1, "loom_calls_after_disconnect": 1},
            "the run went on after the disconnect",
        ),
        ({"loom_calls_before_disconnect": 2}, "inconclusive: its tool calls began"),
        ({"ended_before_cut": True}, "inconclusive: the answer ended"),
        (
            {"ended_before_cut": True, "loom_calls_after_disconnect": 1},
            "inconclusive: the answer ended",
        ),
        ({"ended_before_cut": True, "error": "HTTP 403: no"}, "refused: HTTP 403: no"),
    ],
)
def test_the_cancel_probe_verdicts(row, verdict):
    assert f"cancel probe: {verdict}" in _qualify().report([{**_PROBE, **row}], "m")


def test_a_cancel_row_without_its_counts_is_not_read_as_a_pass():
    old = {
        "id": "cancel-probe",
        "kind": "cancel",
        "ttft_s": 0.6,
        "loom_calls": 1,
        "loom_calls_after_disconnect": 0,
    }
    assert "cancel probe: saved by an older harness, not a result" in _qualify().report([old], "m")


def test_the_cancel_probe_sorts_the_calls_by_when_they_began(monkeypatch):
    # The calls are sorted on `started_ms` (the ledger's `ts - latency_ms`) against the
    # disconnect: one begun before the cut counts before it, one begun after counts after.
    q = _qualify()
    seen = {}

    async def read_until(url, body, headers, cut_s):
        seen["cut_s"] = cut_s
        seen["cut_at"] = q.time.time()
        return False, ""

    def loom_calls(ch_url, subject, since, until):
        cut_ms = seen["cut_at"] * 1000
        return [{"started_ms": cut_ms - 100}, {"started_ms": cut_ms + 5000}]

    monkeypatch.setattr(q, "read_until", read_until)
    monkeypatch.setattr(q, "loom_calls", loom_calls)
    st = {"librechat": {"alice": {"agent_id": "a", "api_key": "k"}}, "librechat_url": "http://lc"}
    args = type("A", (), {"cancel_after": 0.5, "cancel_wait": 0.0, "ch_url": "http://ch"})()
    row = q.cancel_probe(st, args)
    assert seen["cut_s"] == 0.5
    assert row["loom_calls_before_disconnect"] == 1 and row["loom_calls_after_disconnect"] == 1
    assert "the run went on after the disconnect" in q.probe_line(row)


def _patched_client(monkeypatch, q, transport):
    real = q.httpx.AsyncClient
    monkeypatch.setattr(q.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


class _Slow(httpx.AsyncByteStream):
    """An answer whose body never comes within the test: the tool calls are still running."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        await asyncio.sleep(30)
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


def test_the_cancel_probe_disconnects_at_its_delay_while_the_answer_runs(monkeypatch):
    q = _qualify()
    body = _Slow()
    _patched_client(
        monkeypatch, q, httpx.MockTransport(lambda req: httpx.Response(200, stream=body))
    )
    started = time.monotonic()
    assert asyncio.run(q.read_until("http://lc/x", {}, {}, 0.2)) == (False, "")
    assert body.closed
    assert time.monotonic() - started < 5


def test_the_cancel_probe_says_when_the_answer_ended_before_its_delay(monkeypatch):
    q = _qualify()
    _patched_client(
        monkeypatch,
        q,
        httpx.MockTransport(lambda req: httpx.Response(200, text="data: [DONE]\n\n")),
    )
    assert asyncio.run(q.read_until("http://lc/x", {}, {}, 5.0)) == (True, "")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(403, text="no grant"), "HTTP 403: no grant"),
        (
            httpx.Response(200, text='data: {"error": {"code": "RATE_LIMITED"}}\n\n'),
            '{"code": "RATE_LIMITED"}',
        ),
    ],
)
def test_the_cancel_probe_keeps_a_refusal(monkeypatch, response, error):
    q = _qualify()
    _patched_client(monkeypatch, q, httpx.MockTransport(lambda req: response))
    assert asyncio.run(q.read_until("http://lc/x", {}, {}, 5.0)) == (True, error)


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


def _documents() -> dict[tuple[str, str], str]:
    """The live text of every fixture document by (namespace, source_uri): the last
    revision written."""
    fixture = json.loads((QUAL / "fixture.json").read_text())
    out: dict[tuple[str, str], str] = {}
    for tenant in fixture["tenants"]:
        rows = [d for docs in tenant.get("documents", {}).values() for d in docs]
        for doc in rows + tenant.get("document_revisions", []):
            out[(tenant["namespace"], doc["source_uri"])] = doc["content"]
    return out


def _sections(content: str) -> dict[str, str]:
    """Each heading breadcrumb of a document, as the server's chunker reads it, with the
    text under it and its subsections; '' is the whole document."""
    from server.knowledge.domain.chunking import SEPARATOR, detect_sections

    out = {"": content}
    for sec in detect_sections(content):
        text = "\n".join(sec.lines)
        for depth in range(1, len(sec.path) + 1):
            crumb = SEPARATOR.join(sec.path[:depth])
            out[crumb] = out.get(crumb, "") + text
    return out


def test_document_labels_name_a_section_that_states_the_answer():
    fixture = json.loads((QUAL / "fixture.json").read_text())
    people = {p["name"]: p for p in fixture["people"]}
    docs = _documents()
    cases = json.loads((QUAL / "cases.json").read_text())
    document_cases = [c for c in cases if c["baseline"]["op"] == "knowledge.search"]
    assert any(c["kind"] == "abstain" for c in document_cases)
    for case in document_cases:
        ns = people[case["person"]]["namespace"]
        if case["kind"] == "abstain":
            continue
        label = case["cite_doc"]
        sections = _sections(docs[(ns, label["source_uri"])])
        assert label["section"] in sections, (case["id"], sorted(sections))
        text = sections[label["section"]]
        for pattern in case["expect"]:
            assert re.search(pattern, text, re.IGNORECASE), (case["id"], pattern)
        for pattern in case.get("forbid", []):
            assert not re.search(pattern, text, re.IGNORECASE), (case["id"], pattern)
    # A revised document: its earlier revision states what the case forbids.
    revised = next(c for c in document_cases if c.get("forbid"))
    earlier = next(
        d
        for t in fixture["tenants"]
        for rows in t.get("documents", {}).values()
        for d in rows
        if d["source_uri"] == revised["cite_doc"]["source_uri"]
    )
    assert any(re.search(p, earlier["content"], re.IGNORECASE) for p in revised["forbid"])


def test_no_document_uri_reads_as_a_memory_key():
    for _, uri in _documents():
        assert not _qualify().KEY_RE.search(uri), uri


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        (
            "Two [handbook/deploy-runbook.md § Deploy runbook > Release train, revision 1].",
            [("handbook/deploy-runbook.md", "Deploy runbook > Release train", 1)],
        ),
        ("400 days [schema/orders.sql].", [("schema/orders.sql", "", None)]),
        (
            "See [config/cache.toml § Session cache, rev 2]",
            [("config/cache.toml", "Session cache", 2)],
        ),
        # A revision the model could not read is no revision.
        (
            "[schema/orders.sql, revision unknown] [config/cache.toml § Session cache, revision N]",
            [("schema/orders.sql", "", None), ("config/cache.toml", "Session cache", None)],
        ),
        # A heading may hold a comma; a word after it is no revision.
        (
            "[guides/a.md § Limits, quotas > Notes, reviewed, revision 4]",
            [("guides/a.md", "Limits, quotas > Notes, reviewed", 4)],
        ),
        # A source_uri holds a `/`: a bare name is no citation.
        ("Footnote [1], [README.md § Intro] and [a link](http://x) are no citations.", []),
    ],
)
def test_parse_doc_citations(text, parsed):
    got = _qualify().parse_doc_citations(text)
    assert [(c["source_uri"], c["section"], c["revision"]) for c in got] == parsed


def _fake_documents(monkeypatch, q):
    docs = {
        "handbook/oncall-policy.md": {
            "revision": 2,
            "content": "# On-call policy\n\n## Paging\n\nAck within 10 minutes.\n",
            "sections": {
                ("On-call policy",): "Ack within 10 minutes.",
                ("On-call policy", "Paging"): "Ack within 10 minutes.",
            },
        },
        "schema/orders.sql": {
            "revision": 1,
            "content": "TTL placed_at + INTERVAL 400 DAY",
            "sections": {},
        },
    }
    calls = []

    def fake_call(base, token, op, body):
        assert token == "viewer-token"
        calls.append(op)
        doc = docs.get(body["source_uri"])
        if op == "knowledge.get":
            if doc is None:
                return 200, {"found": False}, 0.0
            return 200, {"found": True, "revision": doc["revision"], "content": doc["content"]}, 0.0
        assert op == "knowledge.outline"
        path = tuple(body.get("section_path", []))
        if path:
            return 200, {"found": True, "section_content": doc["sections"].get(path)}, 0.0
        return 200, {"found": True, "sections": [{"path": list(p)} for p in doc["sections"]]}, 0.0

    monkeypatch.setattr(q, "loom_call", fake_call)
    return calls


def test_check_doc_citations_reads_the_document_section_and_revision(monkeypatch):
    q = _qualify()
    calls = _fake_documents(monkeypatch, q)
    st = {"loom_url": "http://loom", "tokens": {"scorer:acme-library": "viewer-token"}}
    person = {"org": "acme", "namespace": "acme-library"}
    case = _case("doc-revised-ack")
    good = "10 minutes [handbook/oncall-policy.md § on-call policy >  paging, revision 2]."
    assert q.check_doc_citations(st, person, case, good) == (
        ["handbook/oncall-policy.md § on-call policy >  paging @r2"],
        [],
        True,
    )
    # A section citation is read through knowledge.outline.
    assert "knowledge.outline" in calls
    # A parent section that states the answer is valid but not the labeled one.
    parent = "10 minutes [handbook/oncall-policy.md § On-call policy]."
    assert q.check_doc_citations(st, person, case, parent)[1:] == ([], False)
    stale = "[handbook/oncall-policy.md § On-call policy > Paging, revision 1]"
    assert q.check_doc_citations(st, person, case, stale)[1] == [
        "handbook/oncall-policy.md § On-call policy > Paging @r1: live revision is 2"
    ]
    missing = "[handbook/oncall-policy.md § On-call policy > Escalation]"
    assert q.check_doc_citations(st, person, case, missing)[1] == [
        "handbook/oncall-policy.md § On-call policy > Escalation: no such section"
    ]
    unknown = "[handbook/pager.md § Paging]"
    assert q.check_doc_citations(st, person, case, unknown)[1] == [
        "handbook/pager.md § Paging: no such document"
    ]
    # A live document that does not state the labeled value does not support the answer.
    wrong = "[schema/orders.sql]"
    assert q.check_doc_citations(st, person, case, wrong)[1] == [
        "schema/orders.sql: does not state the answer"
    ]
    # Without a section the whole document is the cited text.
    ttl = q.check_doc_citations(st, person, _case("doc-orders-ttl"), "[schema/orders.sql]")
    assert ttl == (["schema/orders.sql"], [], True)


def test_native_document_score_ranks_the_labeled_section():
    q = _qualify()
    case = _case("doc-migration-rollback")

    def hit(section, content):
        return {"source_uri": "handbook/deploy-runbook.md", "section": section, "content": content}

    out = {
        "results": [
            hit("Deploy runbook > Rollback > Application rollback", "deployctl rollback"),
            hit("Deploy runbook > Rollback > Database migrations", "Ship a forward fix instead"),
        ]
    }
    got = q.native_score(case, 200, out)
    assert (got["rank"], got["correct"], got["cited_expected"]) == (2, False, False)
    out["results"].reverse()
    got = q.native_score(case, 200, out)
    assert (got["rank"], got["correct"], got["cited_expected"]) == (1, True, True)
    assert q.native_score(case, 200, {"results": []})["rank"] is None
    # knowledge.search has no abstain signal: a hit is never an abstention.
    abstain = _case("abstain-doc-phone")
    assert q.native_score(abstain, 200, out)["correct"] is False
    assert q.native_score(abstain, 200, {"results": []})["correct"] is True


@pytest.mark.parametrize(
    ("answer", "abstained"),
    [
        ("The hosting agreement does not specify a service credit.", True),
        ("That is not in the documents.", True),
        ("A credit is not stated in the agreement.", True),
        ("The credit is 10 percent of the monthly fee.", False),
    ],
)
def test_score_reads_a_document_abstention(answer, abstained):
    assert _qualify().score(_case("abstain-doc-credit"), answer)["correct"] is abstained


def test_document_abstention_phrases_leave_the_memory_cases_alone():
    q = _qualify()
    answer = "The CFO is not stated in Loom."
    assert q.score(_case("abstain-cfo"), answer)["abstained"] is False
    assert q.score(_case("abstain-doc-credit"), answer)["abstained"] is True


def test_report_counts_the_document_cases():
    q = _qualify()

    def side(correct, rank=None, cited=(), invalid=(), expected=None, abstained=False):
        return {
            "correct": correct,
            "abstained": abstained,
            "cited": list(cited),
            "invalid_citations": list(invalid),
            "cited_expected": expected,
            "tool_calls": 1,
            "latency_s": 1.0,
            "ttft_s": 0.5,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "rank": rank,
            "k": 5,
        }

    rows = [
        {
            "id": "doc-a",
            "kind": "document",
            "documents": True,
            "librechat": side(True, cited=["a"], expected=True),
            "native": side(True, rank=1, cited=["a"], expected=True),
        },
        {
            "id": "doc-b",
            "kind": "document",
            "documents": True,
            "librechat": side(
                True, cited=["b", "c"], invalid=["c: no such section"], expected=False
            ),
            "native": side(False, rank=3, cited=["d"], expected=False),
        },
        {
            "id": "doc-none",
            "kind": "abstain",
            "documents": True,
            "librechat": side(True, abstained=True),
            "native": side(False, cited=["e"]),
        },
    ]
    text = q.report(rows, "m")
    # Document rows stay out of the memory-key summary line.
    assert "\nlibrechat: " not in text and "expected key cited" not in text
    assert "2 answerable and 1 unanswerable cases; labeled section at rank 1 1/2" in text
    assert "within the top 5 2/2" in text
    assert (
        "librechat documents: correct 2/2; citations 2/3 valid, 0 naming a revision, labeled section cited 1/2; "
        in text
    )
    assert "abstained 1/1 where it should, 0 false abstentions" in text
    assert (
        "native documents: correct 1/2; citations -, labeled section cited 1/2; abstained 0/1"
        in text
    )


@pytest.mark.parametrize(
    ("out", "wait"),
    [
        # The rate limiter's and the admission slots' body: `detail.retry_after_s`.
        ({"detail": {"code": "RATE_LIMITED", "message": "m", "retry_after_s": 3}}, 3.0),
        # Self-signup's 429 names no hint.
        ({"detail": {"code": "RATE_LIMITED", "message": "m"}}, 1.0),
        ({"detail": "Too Many Requests"}, 1.0),
        ({"text": "rate limited"}, 1.0),
    ],
)
def test_retry_after_reads_every_429_body(out, wait):
    assert _qualify().retry_after(out) == wait


def test_scorer_read_waits_out_a_429_with_a_string_detail(monkeypatch):
    q = _qualify()
    answers = iter([(429, {"detail": "Too Many Requests"}, 0.0), (200, {"found": True}, 0.0)])
    monkeypatch.setattr(q, "loom_call", lambda *a: next(answers))
    slept = []
    monkeypatch.setattr(q.time, "sleep", slept.append)
    assert q.scorer_read({"loom_url": "u"}, "t", "knowledge.get", {}) == {"found": True}
    assert slept == [1.0]


def test_rescore_checks_a_saved_document_rows_citations_again(monkeypatch, tmp_path):
    import argparse

    q = _qualify()
    _fake_documents(monkeypatch, q)
    documents = q.loom_call

    def fake_call(base, token, op, body):
        if op == "memory.get":
            return 200, {"found": True, "version": 1793745407448901606}, 0.0
        return documents(base, token, op, body)

    monkeypatch.setattr(q, "loom_call", fake_call)
    st = {"loom_url": "http://loom", "tokens": {"scorer:acme-library": "viewer-token"}}
    monkeypatch.setattr(q, "state", lambda: st)
    answer = (
        "Within 10 minutes [handbook/oncall-policy.md § On-call policy > Paging, revision 2]; "
        "see also policy/deploy/freeze (version 1793745407448901606)."
    )
    side = {
        "answer": answer,
        "error": "",
        "correct": False,
        "abstained": False,
        "cited": [],
        "invalid_citations": [],
        "cited_expected": None,
        "tool_calls": 2,
        "latency_s": 1.0,
        "ttft_s": 0.5,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    native = {**side, "correct": True, "rank": 1, "k": 5}
    row = {"id": "doc-revised-ack", "kind": "document", "documents": True}
    out = tmp_path / "results.json"
    out.write_text(
        json.dumps(
            {"model": "m", "bridge": False, "rows": [{**row, "librechat": side, "native": native}]}
        )
    )
    q.cmd_rescore(argparse.Namespace(out=str(out), citations=True))
    got = json.loads(out.read_text())["rows"][0]["librechat"]
    # The memory key and the document passage are both cited, both valid, and the labeled
    # section is among them.
    assert got["cited"] == [
        "policy/deploy/freeze",
        "handbook/oncall-policy.md § On-call policy > Paging @r2",
    ]
    assert got["invalid_citations"] == []
    assert got["cited_expected"] is True and got["correct"] is True


def test_seed_writes_each_documents_revision_after_its_first_write(monkeypatch):
    import argparse

    q = _qualify()
    written: list[tuple[str, dict]] = []

    def fake_call(base, token, op, body):
        written.append((op, body))
        return 200, {"version": 1}, 0.0

    monkeypatch.setattr(q, "loom_call", fake_call)
    monkeypatch.setattr(q, "mint", lambda *a, **k: {"token": "t", "jti": "j"})
    monkeypatch.setattr(q, "save_state", lambda data: None)
    q.cmd_seed(argparse.Namespace(loom_url="http://loom", loom_bin="loom"))
    docs = [b for op, b in written if op == "knowledge.upsert"]
    revised = _case("doc-revised-ack")["cite_doc"]["source_uri"]
    writes = [b["content"] for b in docs if b["source_uri"] == revised]
    # The last write of the revised document is the live revision the case labels.
    assert len(writes) == 2
    assert re.search(r"within 10 minutes", writes[-1]) and "15 minutes" in writes[0]
    # Every document is written before any revision of one.
    first = {b["source_uri"]: i for i, b in reversed(list(enumerate(docs)))}
    assert docs.index(next(b for b in docs if b["content"] == writes[-1])) > max(first.values())


_HELP_PAGE = """# Cost and usage

## Daily spend caps

Text.

## Limits

### Daily spend caps

More.

## Webhooks

### Retries
"""


@pytest.mark.parametrize(
    "breadcrumb",
    [
        "Cost and usage > Webhooks > Retries",  # exact
        "Webhooks > Retries",  # without the page title
        "Retries",  # a tail one section has
        "Daily spend caps",  # a tail two sections have
        "Limits > Daily spend caps",
        "webhooks > retries",  # the server compares case
        "Web hooks > Retries",
        "  Webhooks  >Retries ",  # and trims each heading
        "Cost and usage",
    ],
)
def test_help_section_rule_is_the_servers(breadcrumb):
    from server.help.domain.index import build, find_section, page_sections, section_path

    q = _qualify()
    index = build([("features/cost.md", _HELP_PAGE)])
    sections = [s.breadcrumb for s in page_sections(index, "features/cost.md")]
    server = find_section(index, "features/cost.md", section_path(breadcrumb)) is not None
    assert q.names_section(q.help_breadcrumb(breadcrumb), sections) is server


def test_check_help_citations_reads_the_page_and_its_sections(monkeypatch):
    q = _qualify()

    def fake_read(st, token, op, body):
        assert op == "help.get" and token == "viewer-token"
        if body["path"] != "features/cost.md":
            return {"found": False}
        return {"found": True, "sections": [{"section": "Cost and usage > Daily spend caps"}]}

    monkeypatch.setattr(q, "scorer_read", fake_read)
    st = {"tokens": {"scorer:acme-handbook": "viewer-token"}}
    person = {"namespace": "acme-handbook"}
    case = {"cite_help": ["features/cost.md"]}
    text = (
        "[help:docs/features/cost.md § Daily spend caps] "
        "[help:features/cost.md § daily spend caps] "
        "[help:features/other.md] "
        "[help:features/cost.md § >]"  # only separators: the page itself, as the server reads it
    )
    cited, invalid, expected = q.check_help_citations(st, person, case, text)
    assert cited == [
        "help:docs/features/cost.md § Daily spend caps",
        "help:features/cost.md § daily spend caps",
        "help:features/other.md",
        "help:features/cost.md § >",
    ]
    assert invalid == [
        "help:features/cost.md § daily spend caps: no such section",
        "help:features/other.md: no such page",
    ]
    assert expected is True


def test_memory_citations_skip_help_pages_and_quoted_examples(monkeypatch):
    q = _qualify()
    asked = []

    def fake_call(base, token, op, body):
        asked.append(body["memory_key"])
        return 200, {"found": True, "version": 1793745407448901606}, 0.0

    monkeypatch.setattr(q, "loom_call", fake_call)
    st = {"loom_url": "http://loom", "tokens": {"scorer:acme-handbook": "viewer-token"}}
    person = {"org": "acme", "namespace": "acme-handbook"}
    text = (
        "Keys look like `scope/kind/id` [help:features/memory/writing.md § Keys]. "
        "The freeze: [memory:policy/deploy/freeze@v1793745407448901606]."
    )
    # A help page's path is no key; on the direct path a quoted example still reads as one.
    assert q.check_citations(st, person, text)[0] == ["scope/kind/id", "policy/deploy/freeze"]
    # Through the bridge only a `[memory:]` citation is a memory citation.
    asked.clear()
    assert q.check_citations({**st, "bridge": True}, person, text) == (
        ["policy/deploy/freeze"],
        [],
    )
    assert asked == ["policy/deploy/freeze"]


def test_a_help_abstention_and_an_unknown_name_are_scored():
    q = _qualify()
    case = {"kind": "help_abstain", "expect": [], "baseline": {"op": "help.search"}}
    said = {"answer": "Loom's documentation doesn't cover a colour theme.", "error": ""}
    assert q.score_answer(case, said) == {"correct": True, "abstained": True}
    guessed = {"answer": "Set LOOM_DASHBOARD_THEME=dark.", "error": ""}
    assert q.score_answer(case, guessed)["correct"] is False
    # A right answer that names an op the build neither serves nor documents is wrong.
    case = {"kind": "fact", "expect": [r"revoke-token"], "baseline": {"op": "help.search"}}
    answer = {"answer": "Run `loom revoke-token`.", "error": ""}
    assert q.score_answer(case, answer)["correct"] is True
    assert q.score_answer(case, {**answer, "unknown_names": ["token.revoke_all"]}) == {
        "correct": False,
        "abstained": False,
    }


def test_the_direct_path_does_not_score_a_question_the_docs_leave_open():
    q = _qualify()
    case = {"kind": "help_abstain", "cite_help": [], "baseline": {"op": "help.search"}}
    hit = {"hits": [{"path": "features/dashboard.md"}]}
    assert q.native_score(case, 200, hit)["correct"] is None
    case = {"kind": "fact", "cite_help": ["features/dashboard.md"], "baseline": case["baseline"]}
    assert q.native_score(case, 200, hit)["correct"] is True


def test_report_counts_a_help_abstention_and_orders_the_use_cases():
    q = _qualify()

    def side(correct, abstained=False):
        return {
            "correct": correct,
            "abstained": abstained,
            "cited": [],
            "invalid_citations": [],
            "cited_expected": None,
            "tool_calls": 1,
            "latency_s": 1.0,
            "ttft_s": 0.5,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    rows = [
        {
            "id": f"case-{n}",
            "kind": "fact",
            "use_case": f"U{n} Case {n}",
            "librechat": side(True),
            "native": side(True),
        }
        for n in (10, 2, 1)
    ]
    rows.append(
        {
            "id": "help-undocumented",
            "kind": "help_abstain",
            "use_case": "U28 Refusals and abstentions",
            "librechat": side(True, abstained=True),
            "native": side(None),
        }
    )
    text = q.report(rows, "m")
    assert "librechat: correct 4/4;" in text
    assert "abstained 1/1 where it should, 0 false abstentions" in text
    # The direct path does not score that case.
    assert "native: correct 3/3;" in text and "abstained 0/0 where it should" in text
    order = [line.split(" | ")[0][2:] for line in text.splitlines() if line.startswith("| U")]
    assert order == ["U1 Case 1", "U2 Case 2", "U10 Case 10", "U28 Refusals and abstentions"]


def test_a_help_answer_is_read_for_an_abstention_from_the_docs():
    q = _qualify()
    case = {"kind": "help", "expect": [r"revoke-token"], "baseline": {"op": "help.search"}}
    left_open = "Loom's documentation doesn't cover revoking every token at once."
    assert q.score(case, left_open)["abstained"] is True
    # A how-to answer that quotes an error is no abstention.
    quoted = "`loom revoke-token` answers `not found` for a token it doesn't have."
    assert q.score(case, quoted) == {"correct": True, "abstained": False}


def test_the_connected_agents_direct_path_counts_the_agents():
    q = _qualify()
    case = _case("data-connected-agents")

    def inventory(agents):
        return {
            "populations": [{"population": p, "total": 0} for p in ("members",)]
            + [{"population": "agents", "total": agents}]
            + [{"population": p, "total": 0} for p in ("authors", "subjects")]
        }

    assert q.native_score(case, 200, inventory(2))["correct"] is True
    assert q.native_score(case, 200, inventory(1))["correct"] is False


def _saved_side(answer, **extra):
    return {
        "answer": answer,
        "error": "",
        "correct": True,
        "abstained": False,
        "cited": [],
        "invalid_citations": [],
        "cited_expected": None,
        "tool_calls": 1,
        "latency_s": 1.0,
        "ttft_s": 0.5,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        **extra,
    }


def test_rescore_keeps_the_unknown_name_rule_and_takes_the_current_labels(tmp_path):
    import argparse

    q = _qualify()
    answer = "Run `loom revoke-token <jti>`; `token.revoke_all` revokes them all."
    side = _saved_side(answer, unknown_names=["token.revoke_all"])
    row = {"id": "help-revoke-token", "kind": "help", "use_case": "-"}
    out = tmp_path / "results.json"
    out.write_text(json.dumps({"model": "m", "rows": [{**row, "librechat": side, "native": side}]}))
    q.cmd_rescore(argparse.Namespace(out=str(out), citations=False))
    got = json.loads(out.read_text())["rows"][0]
    assert got["librechat"]["correct"] is False
    assert got["use_case"] == _case("help-revoke-token")["use_case"]


def test_a_help_answer_that_cites_no_page_misses_the_labeled_one(monkeypatch):
    q = _qualify()
    monkeypatch.setattr(q, "check_citations", lambda st, person, text, **_: ([], []))
    case = _case("help-revoke-token")
    person = {"namespace": "acme-handbook"}
    got = q.answer_citations({}, case, person, "Run `loom revoke-token`.")
    assert got == {"cited": [], "invalid_citations": [], "cited_expected": False}
    # A memory key cited beside the page does not decide it: the labeled page does.
    monkeypatch.setattr(q, "check_citations", lambda st, person, text, **_: (["a/b/c"], []))
    monkeypatch.setattr(
        q, "check_help_citations", lambda st, person, case, text: (["help:x.md"], [], False)
    )
    got = q.answer_citations({}, {**case, "cite": "a/b/c"}, person, "[help:x.md]")
    assert got["cited_expected"] is False


def test_a_question_the_docs_leave_open_is_held_to_the_documented_settings():
    q = _qualify()
    case = _case("help-undocumented")
    # A real variable named beside the abstention is fine; an invented one is not, and the
    # bridge's unknown names count too.
    real = _saved_side("The docs don't cover a theme; `LOOM_COLOUR` picks a deployment.")
    assert q.score_answer(case, real)["correct"] is True
    assert q.score_answer(case, {**real, "unknown_names": ["LOOM_X"]})["correct"] is False
    assert q.undocumented_settings("set LOOM_UI_THEME, not LOOM_COLOUR") == ["LOOM_UI_THEME"]


def test_the_history_direct_path_counts_the_keys_versions():
    q = _qualify()
    case = _case("data-history")
    assert case["baseline"]["op"] == "memory.history"
    two = {"rows": [{"version": 2}, {"version": 1}]}
    assert q.native_score(case, 200, two)["correct"] is True
    assert q.native_score(case, 200, {"rows": [{"version": 1}]})["correct"] is False


def _help_cases():
    return [
        c
        for c in json.loads((QUAL / "cases.json").read_text())
        if c["baseline"]["op"] == "help.search"
    ]


@pytest.mark.parametrize("case", _help_cases(), ids=lambda c: c["id"])
def test_each_help_label_passes_its_right_samples_and_fails_its_wrong_ones(case):
    q = _qualify()
    samples = case["samples"]
    assert samples["right"] and samples["wrong"]
    for answer in samples["right"]:
        assert q.score_answer(case, {"answer": answer, "error": ""})["correct"], answer
    for answer in samples["wrong"]:
        assert not q.score_answer(case, {"answer": answer, "error": ""})["correct"], answer


def test_a_document_answer_that_cites_a_help_page_checks_it_as_help(monkeypatch):
    q = _qualify()
    docs_read = []
    monkeypatch.setattr(q, "check_citations", lambda st, person, text, **_: ([], []))

    def fake_docs(st, person, case, text):
        docs_read.append(text)
        return [], [], False

    monkeypatch.setattr(q, "check_doc_citations", fake_docs)
    monkeypatch.setattr(
        q, "check_help_citations", lambda st, person, case, text: (["help:a.md"], [], False)
    )
    case = _case("doc-cache-entries")
    got = q.answer_citations({}, case, {"namespace": "acme-library"}, "See [help:a.md § A].")
    assert docs_read == ["See ."]
    assert got["cited"] == ["help:a.md"] and got["invalid_citations"] == []


def test_rescore_reads_memory_citations_by_the_saved_runs_path(monkeypatch, tmp_path):
    import argparse

    q = _qualify()
    seen = []

    def fake_citations(st, case, person, text):
        seen.append(st.get("bridge"))
        return {"cited": [], "invalid_citations": [], "cited_expected": None}

    monkeypatch.setattr(q, "answer_citations", fake_citations)
    # The current state is the bridge's; the saved run went straight through LibreChat.
    monkeypatch.setattr(q, "state", lambda: {"bridge": True})
    side = _saved_side("The freeze is on Fridays (policy/deploy/freeze).")
    out = tmp_path / "results.json"
    row = {"id": "memory-freeze", "kind": "memory", "librechat": side, "native": side}
    out.write_text(json.dumps({"model": "m", "bridge": False, "rows": [row]}))
    q.cmd_rescore(argparse.Namespace(out=str(out), citations=True))
    assert seen == [False]


@pytest.mark.parametrize(
    "case", [c for c in _help_cases() if c.get("forbid")], ids=lambda c: c["id"]
)
def test_each_help_forbid_is_what_fails_one_wrong_sample(case):
    q = _qualify()
    # A wrong sample that states what the label expects, and fails only on the forbid.
    reached = [
        a
        for a in case["samples"]["wrong"]
        if q.score({**case, "forbid": []}, a)["correct"] and not q.score(case, a)["correct"]
    ]
    assert reached, case["id"]


def test_a_memory_citation_in_a_document_answer_is_no_document(monkeypatch):
    q = _qualify()
    read = []
    monkeypatch.setattr(q, "check_citations", lambda st, person, text, **_: ([], []))
    monkeypatch.setattr(
        q,
        "check_doc_citations",
        lambda st, person, case, text: read.append(text) or ([], [], False),
    )
    q.answer_citations({}, _case("doc-cache-entries"), {}, "A [memory:a/b/c@v1793745407448901606].")
    assert read == ["A ."]


def test_through_the_bridge_a_memory_citation_is_read_whole():
    q = _qualify()
    text = "`user/pref/example` [memory:user/pref/john_doe@v1793745407448901606]"
    assert q.memory_mentions({"bridge": True}, text) == [
        ("user/pref/john_doe", 1793745407448901606)
    ]


def test_rescore_asks_for_a_new_run_when_the_saved_path_is_unknown(tmp_path):
    import argparse

    q = _qualify()
    out = tmp_path / "results.json"
    out.write_text(json.dumps({"model": "m", "rows": []}))
    with pytest.raises(SystemExit, match="which path"):
        q.cmd_rescore(argparse.Namespace(out=str(out), citations=True))


def test_the_use_case_report_lists_each_use_cases_unknown_names():
    q = _qualify()
    rows = [
        {
            "use_case": "U14 Tokens",
            "librechat": _saved_side("a", unknown_names=["token.revoke_all"]),
        },
        {"use_case": "U14 Tokens", "librechat": _saved_side("b", correct=False)},
        {"use_case": "U1 Look up a stored fact", "librechat": _saved_side("c")},
    ]
    lines = q.use_case_report(rows)
    assert "| U1 Look up a stored fact | 1/1 | 1/1 | - |" in lines
    assert "| U14 Tokens | 1/2 | 2/2 | token.revoke_all |" in lines


def test_native_only_takes_the_cases_current_use_case_labels(monkeypatch, tmp_path):
    import argparse

    q = _qualify()
    side = _saved_side("ok")
    monkeypatch.setattr(q, "baseline", lambda st, case, person: side)
    out = tmp_path / "results.json"
    row = {"id": "help-revoke-token", "kind": "help", "librechat": side, "native": side}
    out.write_text(json.dumps({"model": "m", "bridge": True, "rows": [row]}))
    people = {p["name"]: p for p in q.load("fixture.json")["people"]}
    q.run_native_only({}, people, argparse.Namespace(out=str(out), only=[]))
    saved = json.loads(out.read_text())["rows"][0]
    assert saved["use_case"] == _case("help-revoke-token")["use_case"]


def test_the_harness_reads_citations_in_the_servers_own_forms():
    from server.assistant.domain import citations

    q = _qualify()
    assert q.MEMORY_CITE_RE.pattern == citations._MEMORY.pattern
    assert q.CODE_CITE_RE.pattern == citations._CODE.pattern
    assert q.HELP_CITE_RE.pattern == citations._HELP.pattern


def test_a_documented_settings_family_member_is_documented():
    q = _qualify()
    assert q.undocumented_settings("`LOOM_WEBHOOK_SECRET_ACME__HOOK`") == []
    assert q.undocumented_settings("`LOOM_RETENTION_EVENTS_DAYS`") == []
    # A family's prefix alone documents nothing: the placeholder's suffix must be there.
    assert q.undocumented_settings("`LOOM_RETENTION_THEME`") == ["LOOM_RETENTION_THEME"]
    assert q.undocumented_settings("`LOOM_UI_THEME`") == ["LOOM_UI_THEME"]


def test_a_history_case_takes_an_earlier_version_through_the_bridge(monkeypatch):
    q = _qualify()

    def fake_call(base, token, op, body):
        if op == "memory.history":
            return 200, {"rows": [{"version": 2}, {"version": 1}]}, 0.0
        return 200, {"found": True, "version": 2}, 0.0

    monkeypatch.setattr(q, "loom_call", fake_call)
    st = {"loom_url": "u", "bridge": True, "tokens": {"scorer:acme-handbook": "t"}}
    person = {"org": "acme", "namespace": "acme-handbook"}
    text = "Before: [memory:policy/api/rate-limit@v1]; now [memory:policy/api/rate-limit@v2]."
    assert q.check_citations(st, person, text, history_ok=True) == (["policy/api/rate-limit"], [])
    assert q.check_citations(st, person, text)[1] == ["policy/api/rate-limit@1"]


@pytest.mark.parametrize(
    "case", json.loads((QUAL / "cases.json").read_text()), ids=lambda c: c["id"]
)
def test_every_case_names_its_use_case_and_cites_pages_that_exist(case):
    assert re.fullmatch(r"U\d+ \S.*", case.get("use_case", "")), case["id"]
    for page in case.get("cite_help") or []:
        assert (ROOT / "docs" / page).is_file(), page


def test_a_help_citation_help_get_would_refuse_is_no_page(monkeypatch):
    q = _qualify()
    monkeypatch.setattr(q, "scorer_read", lambda *a: pytest.fail("help.get was called"))
    st = {"tokens": {"scorer:acme-handbook": "t"}}
    cited, invalid, _ = q.check_help_citations(
        st, {"namespace": "acme-handbook"}, {}, "[help:docs/]"
    )
    assert invalid == ["help:docs/: no such page"]
