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
        json.dumps({"model": "m", "rows": [{**row, "librechat": side, "native": native}]})
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
