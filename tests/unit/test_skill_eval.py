"""The memory guidance is graded by the namespace it produces.

Both replay fixtures run through `bench/skill_eval.py` against a fake loom
over `httpx.MockTransport`. The fake's `namespace.audit` computes its findings
from the rows and the ledger it received with the REAL classifier helpers of
`server/identity/app/namespace_audit.py` (the hash-key regex, `is_hash_shaped_query`,
`index_rows`, the grading thresholds) plus the double-encoded predicate
ported from its SQL. Of the evidence contract it fills only what the output model
requires (kind, rule_version, source_op, sample_limit, samples_complete, coverage); populations,
exceptions and citations are left out. The bad agent fails with the expected
finding codes; the good agent passes. No server, no LLM, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from bench import skill_eval as se
from server.identity.api.models_namespace_catalog import NamespaceAuditOutput
from server.identity.app import namespace_audit as na
from server.memory.domain.excerpt import EXCERPT_LIMIT
from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES
from server.settings import settings

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "integrations" / "claude-code-plugin"
EVALS = PLUGIN / "evals" / "loom-memory"
ORG = "acme"

_HASH_KEY_RE = re.compile(na._HASH_KEY_SEGMENT_RE)


def is_double_encoded(value: Any) -> bool:
    """`JSONType(value) = 'String' AND JSONType(JSONExtractString(value)) IN
    ('Object', 'Array')` — the stored value is a JSON string whose text is
    itself a JSON object or array."""
    if not isinstance(value, str):
        return False
    try:
        inner = json.loads(value)
    except ValueError:
        return False
    return isinstance(inner, (dict, list))


class FakeLoom:
    """Enough of loom to take the two replays and audit what it received.
    Stored values are `json.dumps(value)` like `loom.memory.value`."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}  # key -> {value, memory_type, versions}
        self.ledger: list[tuple[str, str]] = []  # (op, key or query)
        self.docs: dict[str, str] = {}
        self.schemas: dict[str, list[dict[str, Any]]] = {}
        self.records: dict[str, dict[str, dict[str, Any]]] = {}  # schema -> key -> fields
        self.blocks: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- transport -------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        op = request.url.path.removeprefix("/v1/")
        body = json.loads(request.content or b"{}")
        self.calls.append((op, body))
        fn = getattr(self, "op_" + op.replace(".", "_"), None)
        if fn is None:
            return httpx.Response(404, json={"error": f"unknown op {op}"})
        return httpx.Response(200, json=fn(body))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- writes ----------------------------------------------------------
    def op_memory_set(self, b: dict[str, Any]) -> dict[str, Any]:
        row = self.rows.setdefault(
            b["memory_key"], {"value": None, "memory_type": "semantic", "versions": 0}
        )
        row["value"] = b["value"]
        row["memory_type"] = b.get("memory_type", "semantic")
        row["tags"] = list(b.get("tags", []))
        row["versions"] += 1
        self.ledger.append(("memory.set", b["memory_key"]))
        return {"memory_id": f"m-{len(self.rows)}", "version": row["versions"], "advisories": []}

    def op_knowledge_upsert(self, b: dict[str, Any]) -> dict[str, Any]:
        self.docs[b["source_uri"]] = b["content"]
        return {
            "doc_id": f"d-{len(self.docs)}",
            "chunks_created": 3,
            "chunks_superseded": 0,
            "embedding_model": "fake",
            "embedding_dim": 8,
        }

    def op_memory_schema_register(self, b: dict[str, Any]) -> dict[str, Any]:
        self.schemas[b["name"]] = b["fields"]
        self.records.setdefault(b["name"], {})
        return {
            "schema_id": "s-1",
            "name": b["name"],
            "field_count": len(b["fields"]),
            "status": "active",
        }

    def op_memory_record_write(self, b: dict[str, Any]) -> dict[str, Any]:
        assert b["schema_name"] in self.schemas, "record before schema"
        self.records[b["schema_name"]][b["memory_key"]] = b["fields"]
        return {
            "record_id": f"r-{b['memory_key']}",
            "field_count": len(b["fields"]),
            "memory_key": b["memory_key"],
            "key_source": "caller",
        }

    def op_working_memory_set(self, b: dict[str, Any]) -> dict[str, Any]:
        self.blocks[b["block_key"]] = b["content"]
        return {"block_key": b["block_key"], "version": 1}

    # -- reads -----------------------------------------------------------
    def op_memory_get(self, b: dict[str, Any]) -> dict[str, Any]:
        self.ledger.append(("memory.get", b["memory_key"]))
        row = self.rows.get(b["memory_key"])
        if not row:
            return {"found": False}
        return {"found": True, "value": row["value"], "version": row["versions"], "memory_id": "m"}

    def op_memory_search(self, b: dict[str, Any]) -> dict[str, Any]:
        self.ledger.append(("memory.search", b["query"]))
        return {"results": [], "abstain": True}

    def op_working_memory_get(self, b: dict[str, Any]) -> dict[str, Any]:
        return {
            "block_key": b["block_key"],
            "content": self.blocks.get(b["block_key"], ""),
            "found": True,
        }

    def op_memory_list(self, b: dict[str, Any]) -> dict[str, Any]:
        keys = sorted(self.rows)
        off, lim = int(b.get("offset", 0)), int(b.get("limit", 100))
        page = keys[off : off + lim]
        fields = set(b.get("fields") or [])
        entries = []
        for k in page:
            e: dict[str, Any] = {
                "memory_key": k,
                "memory_type": self.rows[k]["memory_type"],
                "value": self.rows[k]["value"],
            }
            if fields:
                e = {f: v for f, v in e.items() if f in fields or f == "memory_key"}
            entries.append(e)
        return {"entries": entries, "truncated": off + lim < len(keys), "total": len(keys)}

    def op_knowledge_list(self, b: dict[str, Any]) -> dict[str, Any]:
        return {
            "rows": [
                {"source_uri": u, "title": "", "tags": [], "chunk_count": 3, "superseded": False}
                for u in sorted(self.docs)
            ],
            "next_cursor": None,
        }

    def op_memory_schema_list(self, b: dict[str, Any]) -> dict[str, Any]:
        return {"schemas": [{"schema_id": "s", "name": n, "fields": []} for n in self.schemas]}

    def op_memory_record_query(self, b: dict[str, Any]) -> dict[str, Any]:
        rows = self.records.get(b["schema_name"], {})
        return {"rows": [{"record_id": f"r-{k}", "memory_key": k, "record": {}} for k in rows]}

    def op_working_memory_render(self, b: dict[str, Any]) -> dict[str, Any]:
        blocks = [
            {"block_key": k, "content": v, "char_limit": 4000, "version": 1}
            for k, v in self.blocks.items()
        ]
        return {"rendered": "", "blocks": blocks, "block_count": len(blocks), "total_chars": 0}

    # -- the audit, from the real classifiers ---------------------------
    def op_namespace_audit(self, b: dict[str, Any]) -> dict[str, Any]:
        stored = {k: json.dumps(r["value"]) for k, r in self.rows.items()}
        live = len(stored)
        live_bytes = sum(len(v) for v in stored.values())
        searchable = {
            k for k, r in self.rows.items() if r["memory_type"] not in NON_SEARCHABLE_TYPES
        }
        gets = sum(1 for op, _ in self.ledger if op in ("memory.get", "memory.bulk_get"))
        queries = [q for op, q in self.ledger if op in ("memory.search", "memory.ask")]
        sets = sum(1 for op, _ in self.ledger if op == "memory.set")
        reads: dict[str, int] = {}
        for op, k in self.ledger:
            if op == "memory.get":
                reads[k] = reads.get(k, 0) + 1
        hot = {k: n for k, n in reads.items() if n >= na.HOT_KEY_READS}
        findings: list[dict[str, Any]] = []

        def add(
            code: str,
            severity: str,
            count: int,
            share: float,
            samples: list[dict[str, Any]],
            bytes_: int = 0,
        ) -> None:
            detail, remedy = na._TEXT[code]
            kept = samples[: b.get("sample_limit", 5)]
            findings.append(
                {
                    "code": code,
                    "severity": severity,
                    "count": count,
                    "share": min(1.0, max(0.0, share)),
                    "bytes": bytes_,
                    "samples": kept,
                    "detail": detail,
                    "remedy": remedy,
                    "kind": na._CONTRACT[code].kind,
                    "rule_version": na._CONTRACT[code].version,
                    "source_op": "namespace.audit",
                    "samples_complete": count <= len(kept),
                    "sample_limit": b.get("sample_limit", 5),
                }
            )

        def value_check(code: str, keys: list[str], population: int, **grade: Any) -> None:
            share = na._share(len(keys), population)
            add(
                code,
                na._grade(len(keys), share, **grade),
                len(keys),
                share,
                [{"memory_key": k, "value_chars": len(stored[k])} for k in keys],
                sum(len(stored[k]) for k in keys),
            )

        critical = gets >= na.EXACT_KEY_STORE_MIN_GETS and gets >= na.EXACT_KEY_STORE_RATIO * len(
            queries
        )
        add(
            "exact_key_store",
            "critical" if critical else na._grade(gets, 0.0),
            gets,
            na._share(gets, gets + len(queries)),
            [{"memory_key": k, "reads": n} for k, n in sorted(hot.items(), key=lambda kv: -kv[1])],
        )
        # No `hash_shaped_queries`: the ledger keeps no query text (every
        # free-text arg is a sha256 shape), so such a check would read 100 % on
        # every namespace. The fake audit follows the real one.
        idx = na.index_rows(
            [
                {"memory_key": k, "memory_id": "", "value": v[: na.INDEX_ROW_VALUE_CLIP]}
                for k, v in stored.items()
            ]
        )
        share = na._share(len(idx), live)
        add(
            "hand_rolled_index_rows",
            na._grade(len(idx), share, warning_any=True),
            len(idx),
            share,
            idx,
        )
        value_check(
            "double_encoded_values",
            [k for k, r in self.rows.items() if is_double_encoded(r["value"])],
            live,
            critical_share=na.DOUBLE_ENCODED_CRITICAL_SHARE,
            warning_any=True,
        )
        value_check(
            "json_prefix_excerpts",
            [
                k
                for k in searchable
                if len(stored[k]) > EXCERPT_LIMIT
                and (
                    is_double_encoded(self.rows[k]["value"])
                    or isinstance(self.rows[k]["value"], (dict, list))
                )
            ],
            len(searchable),
            warning_share=na.JSON_PREFIX_WARNING_SHARE,
        )
        value_check(
            "oversized_values",
            [k for k in searchable if len(stored[k]) > settings.memory_value_advisory_chars],
            len(searchable),
            warning_any=True,
        )
        add("unembedded_rows", "info", 0, 0.0, [])
        # The replay writes through the ledger, stores no procedure and embeds every row.
        add("telemetry_unavailable", "info", 0, 0.0, [])
        add("intentional_exact_key_reads", "info", 0, 0.0, [])
        add("index_backlog", "info", 0, 0.0, [])
        value_check(
            "hash_keys",
            [k for k in stored if _HASH_KEY_RE.search(k)],
            live,
            warning_share=na.HASH_KEY_WARNING_SHARE,
        )
        coord = [
            k
            for k in stored
            if (len(stored[k]) < na.COORDINATION_VALUE_CHARS and hot.get(k, 0) >= na.HOT_KEY_READS)
            or set(k.lower().split("/")) & set(na.COORDINATION_SEGMENTS)
        ]
        value_check("coordination_as_memory", coord, live, warning_any=True)
        episodic = [k for k, r in self.rows.items() if r["memory_type"] == "episodic"]
        value_check(
            "episodic_blobs",
            [k for k in episodic if len(stored[k]) > na.EPISODIC_BLOB_CHARS],
            len(episodic),
            warning_any=True,
        )
        versions = sum(r["versions"] for r in self.rows.values())
        vbytes = sum(len(stored[k]) * r["versions"] for k, r in self.rows.items())
        churn = live > 0 and (
            versions >= na.VERSION_CHURN_ROWS_RATIO * live
            or vbytes >= na.VERSION_CHURN_BYTES_RATIO * live_bytes
        )
        add(
            "version_churn",
            "warning" if churn else na._grade(versions, na._share(versions - live, versions)),
            versions,
            na._share(versions - live, versions),
            [],
            vbytes,
        )

        crit = sum(1 for f in findings if f["severity"] == "critical")
        warn = sum(1 for f in findings if f["severity"] == "warning")
        out = NamespaceAuditOutput(
            org=b["org"],
            namespace=b["namespace"],
            computed_at="2026-09-19T09:00:00",
            window_seconds=b["window_seconds"],
            summary={
                "live_rows": live,
                "live_bytes": live_bytes,
                "version_rows": versions,
                "version_bytes": vbytes,
                "gets": gets,
                "searches": len(queries),
                "sets": sets,
                "get_search_ratio": round(gets / max(len(queries), 1), 2),
            },
            findings=findings,
            skipped=[],
            coverage={
                # The real report's order: summary reads, checks, then the evidence read.
                "rules_run": [*na._SUMMARY_STEPS, *(f["code"] for f in findings), "evidence"],
                "rules_skipped": [],
                "complete": True,
            },
            healthy=crit == 0,
            criticals=crit,
            warnings=warn,
            infos=len(findings) - crit - warn,
        )
        return out.model_dump(mode="json")


def _run(fixture: str, task: str = "sre_alert_investigation") -> tuple[dict[str, Any], FakeLoom]:
    fake = FakeLoom()
    client = se.LoomClient("http://loom.test", "tok", transport=fake.transport())
    result = se.evaluate(
        client, se.load_task(task), runner="replay", org=ORG, replay_path=se.REPLAYS_DIR / fixture
    )
    return result, fake


def _by_code(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["code"]: f for f in result["score"]["findings"]}


# ------------------------------------------------------------------ the two replays


def test_bad_agent_fails_with_the_expected_codes() -> None:
    result, fake = _run("bad_agent.jsonl")
    assert not fake.calls[0][0] == "namespace.audit"
    assert not result["replay"]["errors"], result["replay"]["errors"]
    f = _by_code(result)
    assert not result["score"]["passed"]
    assert {c for c, x in f.items() if x["severity"] == "critical"} >= {
        "exact_key_store",
        "double_encoded_values",
    }
    assert {c for c, x in f.items() if x["severity"] == "warning"} >= {
        "hash_keys",
        "coordination_as_memory",
        "hand_rolled_index_rows",
        "oversized_values",
    }
    assert f["hand_rolled_index_rows"]["count"] == 13
    assert f["coordination_as_memory"]["samples"][0]["memory_key"] == "sre/lease/case-4711"
    assert result["score"]["summary"]["gets"] >= 200 and result["score"]["summary"]["searches"] == 3
    # every finding carries the writer-side remedy from the handler's table
    assert all(x["remedy"] for x in f.values())
    sh = result["shape"]
    assert sh["rows"] == len(fake.rows) and sh["readable_key_share"] < 0.05
    assert sh["plain_text_value_share"] < 0.05
    assert sh["documents"] == 0 and sh["records"] == 0 and sh["working_memory_blocks"] == 0


def test_good_agent_passes_and_leaves_the_recipe_shape() -> None:
    result, fake = _run("good_agent.jsonl")
    assert not result["replay"]["errors"], result["replay"]["errors"]
    assert result["score"]["passed"], se.render_report(result)
    assert result["score"]["criticals"] == 0 and result["score"]["warnings"] <= 1
    f = _by_code(result)
    assert f["double_encoded_values"]["count"] == 0 and f["hash_keys"]["count"] == 0
    assert f["coordination_as_memory"]["count"] == 0 and f["hand_rolled_index_rows"]["count"] == 0
    assert "hash_shaped_queries" not in f
    sh = result["shape"]
    assert sh["readable_key_share"] == 1.0 and sh["plain_text_value_share"] == 1.0
    assert sh["documents"] == 2 and sh["records"] == 8 and sh["working_memory_blocks"] == 1
    # a revision rewrote the same key: two versions, one live row
    assert fake.rows["sre/case/4711/evidence/E-03"]["versions"] == 2
    assert result["replay"]["by_op"]["memory.record.write"] == 8


def test_audit_runs_before_the_shape_reads() -> None:
    _, fake = _run("good_agent.jsonl")
    ops = [op for op, _ in fake.calls]
    assert ops.index("namespace.audit") < ops.index("memory.list")
    audit_body = next(b for op, b in fake.calls if op == "namespace.audit")
    assert audit_body["window_seconds"] == 3600 and audit_body["sample_limit"] == 5


# ------------------------------------------------------------------ fixture integrity


def _ops(name: str) -> list[dict[str, Any]]:
    return se.load_replay(se.REPLAYS_DIR / name)


def test_bad_fixture_reproduces_the_anti_patterns_on_purpose() -> None:
    ops = _ops("bad_agent.jsonl")
    sets = [o["body"] for o in ops if o["op"] == "memory.set"]
    assert sum(1 for b in sets if is_double_encoded(b["value"])) >= 0.9 * len(sets)
    assert sum(1 for b in sets if _HASH_KEY_RE.search(b["memory_key"])) >= 0.9 * len(sets)
    assert any(b["memory_key"] == "sre/lease/case-4711" for b in sets)
    gets = [o["body"]["memory_key"] for o in ops if o["op"] == "memory.get"]
    assert len(gets) >= 200 and gets.count("sre/lease/case-4711") == 116
    searches = [o["body"]["query"] for o in ops if o["op"] == "memory.search"]
    assert searches and all(q.startswith("sha256:") for q in searches)
    index_rows = [b for b in sets if "/index/" in b["memory_key"]]
    assert len(index_rows) == 13
    assert all(len(json.loads(b["value"])["current"]) >= na.INDEX_ROW_MIN_KEYS for b in index_rows)
    assert not any(
        o["op"].startswith(("knowledge.", "working_memory.", "memory.record")) for o in ops
    )


def test_good_fixture_follows_the_recipe() -> None:
    ops = _ops("good_agent.jsonl")
    sets = [o["body"] for o in ops if o["op"] == "memory.set"]
    assert sets and not any(is_double_encoded(b["value"]) for b in sets)
    assert not any(_HASH_KEY_RE.search(b["memory_key"]) for b in sets)
    assert all(isinstance(b["value"], str) and len(b["value"]) < 2000 for b in sets)
    assert not any("lease" in b["memory_key"].split("/") for b in sets)
    by_op = {}
    for o in ops:
        by_op[o["op"]] = by_op.get(o["op"], 0) + 1
    assert by_op["working_memory.set"] == 1 and by_op["knowledge.upsert"] == 2
    assert by_op["memory_schema.register"] == 1 and by_op["memory.record.write"] == 8
    assert by_op["memory.search"] > by_op.get("memory.get", 0)
    assert all(
        "$NAMESPACE" in o["body"]["source_uri"] for o in ops if o["op"] == "knowledge.upsert"
    )


def test_replay_binds_identity_and_substitutes_placeholders() -> None:
    body = se.substitute(
        {"source_uri": "loom://$ORG/$NAMESPACE/k", "n": ["$AGENT", 1]},
        org="o",
        namespace="n",
        agent="a",
    )
    assert body == {"source_uri": "loom://o/n/k", "n": ["a", 1]}
    bound = se.bind_identity("knowledge.upsert", body, org="o", namespace="n", agent="a")
    assert bound["org"] == "o" and bound["namespace"] == "n" and "agent" not in bound
    assert se.bind_identity("memory.set", {}, org="o", namespace="n", agent="a")["agent"] == "a"
    assert (
        se.bind_identity("memory.set", {"agent": "x"}, org="o", namespace="n", agent="a")["agent"]
        == "x"
    )


# ------------------------------------------------------------------ tasks


@pytest.mark.parametrize(
    "name", ["sre_alert_investigation", "pr_review_batch", "research_note_revisions"]
)
def test_task_states_its_namespace_and_the_audit(name: str) -> None:
    task = se.load_task(name)
    assert task.namespace.startswith("skill-eval-") and task.namespace in task.body
    flat = " ".join(task.body.lower().split())
    assert "before you finish, the namespace will be audited" in flat
    assert task.prompt.endswith("\n") and "---" not in task.prompt[:3]


def test_three_tasks_and_the_sre_evidence_sizes() -> None:
    assert se.list_tasks() == [
        "pr_review_batch",
        "research_note_revisions",
        "sre_alert_investigation",
    ]
    sre = se.load_task("sre_alert_investigation").body
    log = sre.split("## Evidence 1", 1)[1].split("```", 2)[1]
    assert len(log) >= 4000, len(log)
    assert "```go" in sre and "```yaml" in sre and "rate(coredns_dns_requests_total" in sre
    model = se.TASKS_DIR / "fixtures" / "sre_case_4711_model.json"
    assert model.stat().st_size >= 60_000
    doc = json.loads(model.read_text())
    assert len(doc["nodes"]) >= 100 and len(doc["edges"]) >= 100
    assert str(model.relative_to(ROOT)) in sre
    pr = se.load_task("pr_review_batch").body
    assert pr.count("| 41") == 8 and "claim" in pr.lower()
    rn = se.load_task("research_note_revisions").body
    assert rn.count("## Revision") == 3 and "do not encode" in rn


# ------------------------------------------------------------------ scoring, report, compare, output


def _report(findings: list[tuple[str, str, int]]) -> dict[str, Any]:
    return {
        "findings": [
            {"code": c, "severity": s, "count": n, "share": 0.5, "remedy": "r"}
            for c, s, n in findings
        ],
        "summary": {},
        "skipped": [],
    }


def test_pass_rule_is_zero_criticals_and_at_most_one_warning() -> None:
    assert se.score_audit(_report([("a", "info", 0), ("b", "warning", 3)]))["passed"]
    assert not se.score_audit(_report([("a", "warning", 1), ("b", "warning", 3)]))["passed"]
    assert not se.score_audit(_report([("a", "critical", 1)]))["passed"]
    assert se.score_audit(_report([]))["passed"]


def test_shape_predicates_match_the_audit_classifier() -> None:
    assert se.is_readable_key("sre/case/4711/evidence/E-03")
    assert se.is_readable_key(
        "sre/case/0123456789abcdef0123456789abcdef/evidence/E-rule"
    )  # id mid-path
    assert not se.is_readable_key("sre/v1/evidence/" + "a" * 64)
    assert se.is_plain_text("Claim first. Source: loki.")
    assert se.is_plain_text("{not json")
    assert not se.is_plain_text(json.dumps({"a": 1}))
    assert not se.is_plain_text({"a": 1})


def test_report_and_compare_render_every_finding(tmp_path: Path) -> None:
    result, _ = _run("bad_agent.jsonl")
    text = se.render_report(result)
    assert "FAIL" in text and "double_encoded_values" in text and "remedy" in text
    assert "shape: rows" in text and "replay: 269 ops applied" in text
    good, _ = _run("good_agent.jsonl")
    table = se.compare(result, good)
    for code in na.FINDING_CODES:
        assert code in table
    assert "shape.records" in table and "PASS" in table and "FAIL" in table
    path = se.write_result(good, out_dir=tmp_path, baseline=True)
    assert (
        path.name.startswith("skill_eval_sre_alert_investigation_replay_")
        and path.suffix == ".json"
    )
    ref = tmp_path / "skill_eval_sre_alert_investigation_baseline.json"
    assert json.loads(ref.read_text())["baseline"] is True
    assert json.loads(path.read_text())["score"]["passed"] is True


def test_claude_code_command_is_the_documented_invocation_and_is_not_run() -> None:
    task = se.load_task("pr_review_batch")
    cmd = se.claude_code_command(task, mcp_config_path=Path("/tmp/x.json"))
    assert cmd[:2] == ["claude", "-p"]
    assert "--plugin-dir" in cmd and str(se.PLUGIN_DIR) in cmd
    assert "--strict-mcp-config" in cmd and "mcp__loom__*" in cmd and "Skill" in cmd
    assert cmd[-1] == task.prompt and "skill-eval-pr-review" in cmd[-1]
    cfg = se.mcp_config("http://h/", "T")
    assert cfg["mcpServers"]["loom"]["url"] == "http://h/mcp?toolset=all"
    assert cfg["mcpServers"]["loom"]["headers"]["Authorization"] == "Bearer T"
    codex = se.codex_command(task, base_url="http://h")
    assert codex[:2] == ["codex", "exec"] and "Bearer" not in " ".join(codex)
    assert 'bearer_token_env_var="LOOM_API_KEY"' in " ".join(
        codex
    )  # the token rides the env, never argv
    # the module docstring documents the same flags
    for flag in (
        "--plugin-dir",
        "--strict-mcp-config",
        '--allowedTools "mcp__loom__*"',
        "toolset=all",
    ):
        assert flag in se.__doc__


def test_manual_runner_prints_the_task_without_a_server(capsys: pytest.CaptureFixture[str]) -> None:
    assert se.main(["--task", "research_note_revisions", "--runner", "manual"]) == 0
    out = capsys.readouterr().out
    assert "skill-eval-research" in out and "--audit-only" in out


# ------------------------------------------------------------------ plugin eval suite


# The MCP tools that write a durable memory: memory.set, memory.update, and
# loom_op dispatching either.
_DURABLE_WRITE_TOOLS = ("loom_memory_set", "loom_memory_update", "loom_op")


def test_plugin_eval_cases_carry_the_task_bodies_and_trace_graders() -> None:
    cases = sorted(p.name for p in EVALS.iterdir() if p.is_dir())
    assert cases == ["pr_review_batch", "research_note_revisions", "sre_alert_investigation"]
    for name in cases:
        prompt = (EVALS / name / "prompt.md").read_text(encoding="utf-8")
        meta, body = se.parse_frontmatter(prompt)
        assert meta.get("max_turns") and meta.get("allowed_tools")
        assert body.strip() == se.load_task(name).body.strip(), (
            f"{name}: eval prompt drifted from the task"
        )
        graders = {
            p.stem: se.parse_frontmatter(p.read_text(encoding="utf-8"))[0]
            for p in (EVALS / name / "graders").glob("*.md")
        }
        assert "skill-fired" in graders and graders["skill-fired"]["type"] == "tool_used"
        assert (
            graders["skill-fired"]["tool"] == "Skill"
            and "loom-memory" in graders["skill-fired"]["input_match"]
        )
        # The anti-pattern graders assert absence (max: 0) on a durable-write
        # tool's input. memory.set carries them; the same shape may also be
        # guarded on the other ways to write a memory.
        absent = [
            g for g in graders.values() if g.get("type") == "tool_used" and g.get("max") == "0"
        ]
        assert len(absent) >= 2, graders
        assert all(
            g["tool"].startswith("mcp__") and g["tool"].endswith(_DURABLE_WRITE_TOOLS)
            for g in absent
        ), [g["tool"] for g in absent]
        assert any(g["tool"].endswith("loom_memory_set") for g in absent)
    mocks = PLUGIN / "evals" / "mocks" / "loom"
    assert (mocks / "loom_memory_set.md").is_file() and (
        mocks / "loom_working_memory_set.md"
    ).is_file()


def test_plugin_manifests_bumped_and_codex_copy_in_sync() -> None:
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads(
        (ROOT / "integrations" / "codex-plugin" / ".codex-plugin" / "plugin.json").read_text()
    )
    # The two manifests agree; the number itself moves with every plugin PR.
    assert claude["version"] == codex["version"]
    assert tuple(int(x) for x in claude["version"].split(".")) >= (0, 9, 2)


def test_docs_name_the_bench() -> None:
    readme = (ROOT / "bench" / "README.md").read_text(encoding="utf-8")
    assert "`skill_eval.py`" in readme and "namespace.audit" in readme
    recipe = (ROOT / "docs" / "benchmarks" / "skill-eval.md").read_text(encoding="utf-8")
    assert "bench/skill_eval.py" in recipe and "claude plugin eval" in recipe
    assert "bench/out/" in (ROOT / ".gitignore").read_text()  # shared with the side-by-side bench
