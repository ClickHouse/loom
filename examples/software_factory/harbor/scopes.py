"""Resolve only frozen attempt bindings and shared read-only code certificates."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from .arms import ALL


def _require(condition, message):
    if not condition:
        raise PermissionError(message)


def frozen_json(path, sha):
    path = Path(path)
    _require(
        isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) and not path.is_symlink(),
        "Artifact must be an immutable regular input",
    )
    raw = path.read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == sha, "Frozen artifact bytes changed")
    return json.loads(raw)


def binding_for(protocol, slot):
    """Validate a scheduled treatment; never infer a missing live namespace or token."""
    try:
        order = slot["order"]
        _require(
            type(order) is int
            and 0 <= order < len(protocol["schedule"])
            and protocol["schedule"][order] == slot,
            "Attempt differs from frozen schedule",
        )
        arm = ALL[slot["arm"]]
        _require(
            type(slot.get("memory_enabled")) is bool
            and all(slot.get(key) == value for key, value in arm.identity().items()),
            "Attempt treatment identity differs",
        )
        binding = protocol["attempt_scopes"][str(order)]
        _require(
            binding["task_id"] == slot["task_id"]
            and binding["index_tier"] == arm.index_tier
            and binding["base_commit"] == protocol["tasks"][slot["task_id"]]["base_commit"],
            "Attempt task, tier or base differs",
        )
        if arm.index_tier is None:
            _require(
                binding["org"] == "-"
                and binding["code_namespace"] == "-"
                and binding.get("token_file") is None
                and binding.get("memory_namespace") is None
                and binding.get("memory_history_sha256") is None,
                "Baseline received a live Loom binding",
            )
            return copy.deepcopy(binding)
        _require(
            all(
                isinstance(binding.get(key), str) and binding[key] and binding[key] != "-"
                for key in ("org", "code_namespace", "token_file")
            ),
            "Live Loom scope or token is missing",
        )
        _require(
            isinstance(binding.get("repository_tag"), str)
            and re.fullmatch(r"repo:[0-9a-f]{10}", binding["repository_tag"]),
            "Repository tag is not canonical",
        )
        if arm.memory_enabled:
            _require(
                isinstance(binding.get("memory_namespace"), str)
                and binding["memory_namespace"]
                and binding["memory_namespace"] != binding["code_namespace"]
                and isinstance(binding.get("memory_history_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", binding["memory_history_sha256"]),
                "Memory attempt lacks a separate frozen history scope",
            )
        else:
            _require(
                binding.get("memory_namespace") is None
                and binding.get("memory_history_sha256") is None,
                "Code-only arm received memory",
            )
        for key, other in protocol["attempt_scopes"].items():
            if key == str(order) or other["index_tier"] is None:
                continue
            same_case = (
                other["task_id"] == binding["task_id"]
                and other["index_tier"] == binding["index_tier"]
            )
            same_scope = (other["org"], other["code_namespace"]) == (
                binding["org"],
                binding["code_namespace"],
            )
            _require(
                same_case == same_scope, "Each case/tier requires one shared read-only code binding"
            )
            _require(
                other["org"] != binding["org"] or same_scope,
                "Repository trunk mapping is ambiguous within an org",
            )
            if same_case:
                _require(
                    all(
                        other.get(field) == binding.get(field)
                        for field in (
                            "base_commit",
                            "repository_tag",
                            "compiler_rows",
                            "source_scope",
                        )
                    ),
                    "Shared code bindings disagree about frozen source",
                )
            if arm.memory_enabled and other.get("memory_namespace") is not None:
                _require(
                    (other["org"], other["memory_namespace"])
                    != (binding["org"], binding["memory_namespace"]),
                    "Two attempts share mutable memory",
                )
        certificate = frozen_json(
            protocol["index_certificate_file"], protocol["index_certificate_sha256"]
        )
        proof = certificate["scopes"][slot["task_id"] + ":" + arm.index_tier]
        expected = {
            "org": binding["org"],
            "namespace": binding["code_namespace"],
            "git_head": binding["base_commit"],
            "build_commit": protocol["server_commit"],
        }
        _require(
            all(proof.get(key) == value for key, value in expected.items())
            and proof.get("compiler_rows") == binding["compiler_rows"]
            and proof.get("source_scope") == binding["source_scope"],
            "Live code binding differs from its shared certificate",
        )
        source = binding["source_scope"]
        _require(
            source.get("verified") is True and source.get("base_commit") == binding["base_commit"],
            "Code source scope is unverified",
        )
        return copy.deepcopy(binding)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, OSError) as error:
        raise PermissionError(f"Attempt binding refused: {error}") from error


def selected_scopes(protocol, slot=None):
    """Return the deduplicated certified code scopes, excluding every memory scope."""
    slots = [slot] if slot is not None else protocol["schedule"]
    scopes = set()
    for selected in slots:
        binding = binding_for(protocol, selected)
        if selected["index_tier"] is not None:
            scopes.add((binding["org"], binding["code_namespace"]))
    return [{"org": org, "namespace": namespace} for org, namespace in sorted(scopes)]
