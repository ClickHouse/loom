"""Audit every frozen trial's actual traces before interpreting treatment deltas."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from examples.software_factory.factory.anthropic_provider import wire_messages
from examples.software_factory.factory.model import digest, tool_schemas
from examples.software_factory.factory.retrieval_tools import TOOL_OPS
from examples.software_factory.harbor.billing import _trace
from examples.software_factory.harbor.paired import analyze
from examples.software_factory.harbor.runtime import read, verify, write

ATTEMPT_CHECKS = (
    "artifact_integrity",
    "identity",
    "fresh_logs",
    "public_prompt",
    "schema_exposure",
    "generation_transport",
    "conversation",
    "no_host_or_hidden_work",
    "loom_accounting",
    "memory_evidence",
    "index_immutable",
    "billing_evidence",
    "execution_integrity",
)


def _jsonl(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Malformed trace rows")
    return rows


def _append(messages, role, blocks):
    if messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": list(blocks)})


def _prompt(protocol, slot, start, initial, public):
    issue = public[slot["task_id"]]
    if (
        len(initial) != 1
        or initial[0].get("role") != "user"
        or len(initial[0].get("content", [])) != 1
        or initial[0]["content"][0].get("type") != "text"
    ):
        return False
    text, problem = initial[0]["content"][0]["text"], issue["problem"]
    if not problem or not text.endswith(problem):
        return False
    prefix = text[: -len(problem)]
    metadata = [json.loads(line) for line in prefix.splitlines() if line.startswith("{")]
    if len(metadata) != 1 or set(metadata[0]) != {"repository", "base_commit", "source_paths"}:
        return False
    meta = metadata[0]
    scopes = protocol["tasks"][slot["task_id"]]["indexes"].values()
    return (
        meta["repository"] == issue["repository"]
        and meta["base_commit"] == issue["base_commit"]
        and isinstance(meta["source_paths"], list)
        and all(
            scope["source_scope"]["paths_sha256"] == digest(meta["source_paths"])
            for scope in scopes
        )
        and "named_symbol" not in prefix
        and "context_stage" not in prefix
        and wire_messages(start["prompt"]) == initial
    )


def _conversation(events, dispatches):
    messages, number, active_loom = [], 0, None
    host_free = True
    for row in events:
        kind = row["kind"]
        if kind == "start":
            messages = json.loads(json.dumps(row["prompt"]))
        elif kind == "provider_request":
            if number >= len(dispatches):
                return False, False
            payload = dispatches[number]["payload"]
            if (
                payload["messages"] != wire_messages(messages)
                or row["messages_sha256"] != digest(messages)
                or row["tools_sha256"] != digest(payload["tools"])
            ):
                return False, False
            number += 1
        elif kind == "provider_response":
            _append(messages, "assistant", row["blocks"])
        elif kind == "tool_request":
            active_loom = TOOL_OPS.get(row["name"])
        elif kind == "tool_result":
            _append(
                messages,
                "user",
                [
                    {
                        "toolResult": {
                            "toolUseId": row["tool_use_id"],
                            "content": [{"json": row["result"]}],
                        }
                    }
                ],
            )
            active_loom = None
        elif kind == "retrieval":
            host_free &= row.get("caller") == "model" and row.get("op") == active_loom
        elif kind in {"context_reuse", "local_scope_refusal"}:
            host_free &= active_loom is not None and row.get("caller") == "model"
        if kind == "hidden_verification" or kind == "context" and row.get("caller") == "host":
            host_free = False
    return number == len(dispatches), bool(host_free)


def audit_fairness(protocol, entries, *, billing=None):
    """Write a protocol-bound report; incomplete or unknown proof cannot pass."""
    observed = defaultdict(list)
    details = defaultdict(list)
    generations, prompts, sources, folders = [], defaultdict(set), defaultdict(set), []
    loom_ids = []
    counts = Counter()

    def check(name, value, reason="", order=None):
        details[name].append(dict(passed=value, reason=reason, order=order))

    try:
        analyze(
            {"attempts": protocol["schedule"], "schedule_sha256": protocol["schedule_sha256"]},
            [],
            resamples=100,
        )
        check("schedule", True)
    except Exception as error:  # noqa: BLE001 — report invalid schedules without destroying evidence
        check("schedule", False, type(error).__name__)
    try:
        verify(protocol)
        check("frozen_files", True)
    except Exception as error:  # noqa: BLE001 — a changed freeze must be a failed audit
        check("frozen_files", False, type(error).__name__)
    for entry in entries:
        if isinstance(entry, dict) and type(entry.get("order")) is int:
            observed[entry["order"]].append(entry.get("result"))
    complete = (
        len(entries) == len(protocol["schedule"])
        and set(observed) == set(range(len(protocol["schedule"])))
        and all(len(values) == 1 for values in observed.values())
    )
    check(
        "cohort_complete",
        complete,
        "" if complete else "Missing, duplicate or unscheduled attempts retained",
    )
    try:
        public = {row["task_id"]: row for row in read(protocol["historical_manifest"])["tasks"]}
        certificate = Path(protocol["index_certificate_file"])
        frozen = read(certificate)
        check(
            "index_certificate",
            hashlib.sha256(certificate.read_bytes()).hexdigest()
            == protocol["index_certificate_sha256"]
            and frozen["index_content_sha256"] == protocol["index_content_sha256"],
        )
    except Exception as error:  # noqa: BLE001 — missing public/index proof remains unknown
        public = {}
        check("index_certificate", None, type(error).__name__)
    if billing is None:
        try:
            billing = read(Path(protocol["output_root"]) / "provider-costs.json")
        except (OSError, ValueError):
            billing = {}
    common = tool_schemas("baseline", regression_tests=True)
    for slot in protocol["schedule"]:
        order = slot["order"]
        if len(observed.get(order, [])) != 1:
            for name in ATTEMPT_CHECKS:
                check(name, None, "Attempt is missing or duplicated", order)
            continue
        result = observed[order][0]
        try:
            identity = all(
                result.get(k) == v
                for k, v in {
                    **{key: slot[key] for key in ("task_id", "mode", "arm", "index_tier")},
                    "schedule_order": order,
                    "protocol_sha256": digest(protocol),
                }.items()
            )
            expected_source = protocol["harbor_specs"][slot["task_id"]]["source_sha256"]
            identity &= result.get("source_sha256") == expected_source
            check("identity", bool(identity), "Slot, source and protocol must match", order)
            folder = Path(result["harbor_trial"]).resolve()
            folders.append(str(folder))
            events = _jsonl(folder / "agent/factory/events.jsonl")
            starts = [r for r in events if r["kind"] == "start"]
            start = starts[0]
            fresh = (
                len(starts) == 1
                and events[0]["kind"] == "start"
                and [r["sequence"] for r in events] == list(range(len(events)))
            )
            fresh &= all(
                start.get(k) == v
                for k, v in {
                    "task_id": slot["task_id"],
                    "mode": slot["mode"],
                    "source_sha256": expected_source,
                    "provider": "anthropic-messages:" + protocol["model_id"],
                    "budget": protocol["attempt_budget"],
                    "grader_sha256": protocol["harbor_specs"][slot["task_id"]]["grader_sha256"],
                }.items()
            )
            check(
                "fresh_logs",
                bool(fresh),
                "One fresh matching journal with contiguous events",
                order,
            )
            transport = _jsonl(folder / "agent/provider-transport.jsonl")
            dispatches = [r for r in transport if r.get("phase") == "dispatch"]
            responses = [r for r in transport if r.get("phase") == "response"]
            expected_tools = tool_schemas(slot["mode"], regression_tests=True)
            exposure = start["tools"] == expected_tools and expected_tools[: len(common)] == common
            exposure &= all(r["payload"]["tools"] == expected_tools for r in dispatches)
            check("schema_exposure", bool(exposure), "Only optional read schemas differ", order)
            initial = dispatches[0]["payload"]["messages"]
            valid_prompt = _prompt(protocol, slot, start, initial, public)
            check(
                "public_prompt",
                bool(valid_prompt),
                "One public issue and common checkout metadata; no operator symbol hint",
                order,
            )
            prompts[slot["task_id"]].add(digest(initial))
            sources[slot["task_id"]].add(result["source_sha256"])
            generation = bool(dispatches) and len(dispatches) == len(responses) == result.get(
                "provider_physical_requests"
            ) == result.get("provider_logical_requests")
            generation &= (
                result.get("provider_failed_requests") == 0 and result.get("retry_count") == 0
            )
            generation &= len(transport) == 2 * len(dispatches)
            for index, (request, response) in enumerate(zip(dispatches, responses, strict=True)):
                payload = request["payload"]
                usage = response.get("usage", {})
                generation &= (
                    transport[index * 2] == request and transport[index * 2 + 1] == response
                )
                generation &= (
                    request.get("path") == response.get("path") == "/v1/messages"
                    and response.get("status") == 200
                )
                generation &= (
                    set(payload) == {"model", "messages", "tools", "max_tokens"}
                    and payload["model"] == protocol["model_id"]
                    and payload["max_tokens"] == protocol["attempt_budget"]["output_tokens"]
                )
                generation &= all(
                    type(usage.get(k)) is int and usage[k] == 0
                    for k in ("cache_creation_input_tokens", "cache_read_input_tokens")
                )
                generation &= all(
                    type(usage.get(k)) is int and usage[k] >= 0
                    for k in ("input_tokens", "output_tokens")
                )
                generation &= usage.get("service_tier", "standard") == "standard"
                identifier = response.get("request_id")
                generation &= isinstance(identifier, str) and bool(identifier)
                if isinstance(identifier, str):
                    generations.append(identifier)
            cost = result.get("generation_cost_usd")
            generation &= type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            provider_events = [r for r in events if r["kind"] == "provider_response"]
            generation &= len(provider_events) == len(responses) and all(
                r.get("provider_request_id") == response.get("request_id")
                and r.get("physical_requests") == 1
                and r.get("retries") == 0
                and r.get("usage") == response.get("usage")
                for r, response in zip(provider_events, responses, strict=False)
            )
            prices = [r.get("generation_cost_usd") for r in provider_events]
            priced = all(
                type(value) in (int, float) and math.isfinite(value) and value >= 0
                for value in prices
            )
            generation &= (
                priced
                and type(cost) in (int, float)
                and math.isclose(sum(prices), cost, abs_tol=1e-12)
            )
            check(
                "generation_transport",
                bool(generation),
                "Actual model/options/counts, no retries/cache and known generation cost",
                order,
            )
            conversation, host_free = _conversation(events, dispatches)
            check(
                "conversation",
                conversation,
                "Wire payloads replay the fresh event journal exactly",
                order,
            )
            check(
                "no_host_or_hidden_work",
                host_free,
                "Every retrieval follows a selected tool; hidden verifier stays outside agent",
                order,
            )
            calls, _ = _trace(protocol, slot, result)
            loom_ids.extend(identifier for identifier, _ in calls)
            lookups = [r for r in events if r["kind"] == "retrieval"]
            selected = sum(r["kind"] == "tool_request" and r["name"] in TOOL_OPS for r in events)
            accounting = len(lookups) == result.get(
                "loom_logical_requests"
            ) and selected == result.get("model_selected_loom_requests") == result.get(
                "loom_tool_calls"
            )
            accounting &= result.get("model_selected_loom_dispatches") == len(lookups)
            accounting &= all(type(r.get("physical_requests")) is int for r in lookups) and sum(
                r.get("physical_requests", 0) for r in lookups
            ) == len(calls)
            for kind, key in [
                ("context_reuse", "context_reuses"),
                ("local_scope_refusal", "local_scope_refusals"),
            ]:
                accounting &= sum(r["kind"] == kind for r in events) == result.get(key)
            if slot["mode"] == "baseline":
                accounting &= (
                    selected == len(lookups) == len(calls) == 0
                    and not (folder / "agent/loom-transport.jsonl").exists()
                )
            check(
                "loom_accounting",
                bool(accounting),
                "Choices/cache/refusals are distinct from actual HTTP",
                order,
            )
            check(
                "memory_evidence",
                type(result.get("memory_rows_written")) is int
                and result["memory_rows_written"] == 0
                and isinstance(result.get("memory_rows_written_basis"), str)
                and "read-only" in result["memory_rows_written_basis"].lower(),
                "Explicit zero and read-only basis required; schemas checked separately",
                order,
            )
            snapshots = [
                read(Path(protocol["output_root"]) / f"live-index-{phase}-{order:02d}.json")
                for phase in ("before", "after")
            ]
            check(
                "index_immutable",
                all(digest(snapshot) == protocol["index_content_sha256"] for snapshot in snapshots),
                "Both source audits match the frozen certificate",
                order,
            )
            proof = billing.get("attempts", {}).get(str(order), {})
            price = proof.get("loom_provider_cost_usd")
            priced = type(price) in (int, float) and math.isfinite(price) and price >= 0
            priced &= all(
                type(proof.get(k)) is int and proof[k] >= 0
                for k in (
                    "loom_provider_input_tokens",
                    "loom_provider_output_tokens",
                    "loom_embedding_tokens",
                )
            )
            bound = (
                priced
                and billing.get("cohort_complete") is True
                and billing.get("protocol_sha256") == digest(protocol)
                and proof.get("proof_status") == "known"
                and proof.get("physical_requests_matched")
                == proof.get("physical_requests_dispatched")
                == len(calls)
                and proof.get("physical_request_ids_sha256")
                == digest(sorted(identifier for identifier, _ in calls))
            )
            check(
                "billing_evidence",
                True if bound else None,
                "" if bound else "Missing complete request-ID-bound provider cost proof",
                order,
            )
            check(
                "execution_integrity",
                result.get("harbor_reward_valid") is True and result.get("status") == "completed",
                "Task failure is allowed; infrastructure/incomplete execution is not hidden",
                order,
            )
            check("artifact_integrity", True, order=order)
            counts.update(
                attempts_with_traces=1,
                generation_dispatches=len(dispatches),
                loom_dispatches=len(calls),
                selected_loom_tools=selected,
            )
        except Exception as error:  # noqa: BLE001 — preserve partial/failed attempts as unknown evidence
            for name in ATTEMPT_CHECKS:
                if not any(item["order"] == order for item in details[name]):
                    check(name, None, type(error).__name__, order)
    check(
        "unique_attempt_folders",
        len(folders) == len(set(folders)),
        "Each scheduled attempt owns fresh logs",
    )
    check(
        "unique_loom_ids",
        len(loom_ids) == len(set(loom_ids)),
        "Loom physical response IDs are globally unique",
    )
    check(
        "unique_generation_ids",
        len(generations) == len(set(generations)),
        "Actual generation response IDs are globally unique",
    )
    check(
        "same_initial_prompt_per_task",
        all(len(values) == 1 for values in prompts.values())
        and len(prompts) == len(protocol["tasks"]),
        "Identical issue conversations across treatments and A/A",
    )
    check(
        "same_source_per_task",
        all(len(values) == 1 for values in sources.values())
        and len(sources) == len(protocol["tasks"]),
        "All arms and repeats use the same source fingerprint",
    )
    checks = {}
    for name, items in details.items():
        value = (
            False
            if any(item["passed"] is False for item in items)
            else None
            if any(item["passed"] is None for item in items)
            else True
        )
        checks[name] = {
            "passed": value,
            "status": "passed" if value is True else "failed" if value is False else "unknown",
            "issues": [item for item in items if item["passed"] is not True],
        }
    receipt = {
        "schema_version": 1,
        "protocol_sha256": digest(protocol),
        "passed": complete and all(item["passed"] is True for item in checks.values()),
        "complete": complete,
        "checks": checks,
        "counts": {
            "scheduled_attempts": len(protocol["schedule"]),
            "observed_attempts": len(entries),
            **dict(counts),
        },
        "limits": [
            "Fairness is not task success, statistical significance or a win claim.",
            "Provider evidence is not an invoice; infrastructure remains separately unpriced.",
            "No database, model or index calls were made by this read-only artifact audit.",
        ],
    }
    write(Path(protocol["output_root"]) / "fairness-audit.json", receipt)
    return receipt
