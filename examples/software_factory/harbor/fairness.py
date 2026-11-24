"""Audit native task inputs and passive receipts without changing agent behavior."""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

from examples.software_factory.factory.model import digest

from .index import select_snapshot
from .memory import validate_history_certificate
from .native_environment import CLAUDE_CACHE_ENV, mcp_settings
from .project_guidance import agent_image
from .runtime import read, verify, write
from .scopes import selected_scopes


def index_certificate_matches(protocol):
    certificate = Path(protocol["index_certificate_file"])
    frozen = read(certificate)
    expected = [
        (binding["task_id"] + ":" + binding["index_tier"], binding)
        for binding in protocol["attempt_scopes"].values()
        if binding["index_tier"] is not None
    ]
    return (
        hashlib.sha256(certificate.read_bytes()).hexdigest() == protocol["index_certificate_sha256"]
        and frozen.get("schema_version") == 1
        and set(frozen["scopes"]) == {key for key, _ in expected}
        and digest(read(protocol["index_content_file"])) == protocol["index_content_sha256"]
        and all(
            all(
                frozen["scopes"][key].get(field) == value
                for field, value in {
                    "org": binding["org"],
                    "namespace": binding["code_namespace"],
                    "repo": protocol["tasks"][binding["task_id"]]["repository"],
                    "git_head": binding["base_commit"],
                    "build_commit": protocol["server_commit"],
                    "source_scope": binding["source_scope"],
                    "compiler_rows": binding["compiler_rows"],
                }.items()
            )
            for key, binding in expected
        )
    )


def attempt_evidence_verified(protocol, audit, field):
    if audit is None:
        return None
    return (
        audit.get("protocol_sha256") == digest(protocol)
        and len(audit["attempts"]) == len(protocol["schedule"])
        and all(row.get(field) is True for row in audit["attempts"])
    )


def audit_fairness(protocol, entries, *, billing=None):
    verify(protocol)
    root = Path(protocol["output_root"])
    checks, seen = [], set()
    certificate = index_certificate_matches(protocol)
    for entry in entries:
        order, result = entry["order"], entry["result"]
        slot = protocol["schedule"][order]
        task = root / "tasks" / f"slot-{order:02d}"
        config = tomllib.loads((task / "task.toml").read_text())
        public = next(
            row
            for row in read(protocol["historical_manifest"])["tasks"]
            if row["task_id"] == slot["task_id"]
        )
        if slot["memory_enabled"]:
            validate_history_certificate(protocol, slot, protocol["attempt_scopes"][str(order)])
        scopes = selected_scopes(protocol, slot)
        expected = select_snapshot(read(root / "index-content.json"), scopes)
        trial = Path(result["harbor_trial"])
        raw = read(trial / "result.json")
        agent = raw["config"]["agent"]
        servers = config["environment"].get("mcp_servers", [])
        build = root / "builds" / slot["task_id"] / "agent"
        baseline_guidance = build / "workspace/CLAUDE.md"
        row = {
            "order": order,
            "unique_attempt": order not in seen,
            "public_prompt": (task / "instruction.md").read_bytes()
            == public["problem"].encode("utf-8"),
            "native_solver": agent.get("name") == "claude-code" and not agent.get("import_path"),
            "same_model": agent.get("model_name") == protocol["model_id"],
            "same_version": agent.get("kwargs") == {"version": protocol["solver"]["version"]},
            "expected_agent_image": config["environment"]["docker_image"]
            == agent_image(protocol["harbor_images"], slot),
            "project_guidance_scope": (
                not baseline_guidance.exists()
                and not baseline_guidance.is_symlink()
                and hashlib.sha256((build / "loom-guidance/CLAUDE.md").read_bytes()).hexdigest()
                == protocol["project_guidance"]["sha256"]
            ),
            "loom_source_used": slot["mode"] == "baseline"
            or type(result.get("first_loom_source_result_event_main")) is int,
            "prompt_cache_disabled": all(
                (agent.get("env") or {}).get(name) == value
                for name, value in CLAUDE_CACHE_ENV.items()
            )
            and result.get("solver_token_source") == "anthropic-reported"
            and all(
                type(result.get(key)) is int and result[key] == 0
                for key in (
                    "first_cache_read_tokens",
                    "first_cache_write_tokens",
                    "model_cache_read_tokens",
                    "model_cache_write_tokens",
                )
            ),
            "mcp_treatment": servers
            == tomllib.loads(mcp_settings(protocol, slot))
            .get("environment", {})
            .get("mcp_servers", []),
            "source_certificate": certificate,
            "index_immutable": all(
                read(root / f"live-index-{when}-{order:02d}.json") == expected
                for when in ("before", "after")
            ),
            "native_trajectory": (trial / "agent/trajectory.json").exists(),
            "no_custom_solver": not (trial / "agent/factory-result.json").exists(),
            "billing_evidence": (billing or {})
            .get("attempts", {})
            .get(str(order), {})
            .get("proof_status")
            == "known",
        }
        seen.add(order)
        row["passed"] = all(value is True for key, value in row.items() if key != "order")
        checks.append(row)
    receipt = {
        "protocol_sha256": digest(protocol),
        "auditor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "attempts": checks,
        "passed": len(checks) == len(protocol["schedule"]) and all(row["passed"] for row in checks),
    }
    write(root / "fairness-audit.json", receipt)
    return receipt
