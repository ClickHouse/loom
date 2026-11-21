"""Optional native memory tools with controller-bound identity and honest receipts."""

from __future__ import annotations

import re
import time

from examples.software_factory.factory.model import digest, tool

OPS = {
    f"loom_memory_{name}": f"memory.{name}"
    for name in ("search", "get", "set", "update", "for_file")
}
FIELDS = {
    "found",
    "unchanged",
    "memory_key",
    "memory_id",
    "value",
    "memory_type",
    "namespace",
    "version",
    "version_text",
    "new_version",
    "content_sha256",
    "code",
    "code_state",
    "tags",
    "files",
    "confidence",
    "status",
    "pinned",
    "updated",
    "reason",
    "noop",
    "dry_run",
    "merged_into_key",
    "conflict_verdict",
    "conflict_peers",
    "duplicate_of",
    "degraded",
    "advisories",
    "next",
    "results",
    "hits",
    "rendered",
    "truncated",
    "namespaces_searched",
    "namespaces_skipped",
    "partial",
    "partial_reason",
}
HIT_FIELDS = {
    "memory_key",
    "memory_id",
    "namespace",
    "content_excerpt",
    "value",
    "memory_type",
    "version",
    "content_sha256",
    "code_state",
    "code_changed_since",
    "code",
    "score",
    "status",
    "tags",
    "files",
    "matched_files",
    "value_truncated",
    "body_truncated",
    "next",
    "verification",
    "source_resource_id",
    "suggestion",
    "changed_since",
}
LINK_FIELDS = {"code_namespace", "path", "symbol", "state", "changed_since", "suggestion"}


def schemas():
    string = {"type": "string"}
    key = {"memory_key": string}
    return [
        tool(
            "loom_memory_search",
            "Search persisted project knowledge and historical observations. Empty results do not "
            "establish that a fact is true or false.",
            {"query": string},
        ),
        tool(
            "loom_memory_get",
            "Read a persisted memory and its native code links, versions and content hash.",
            key,
        ),
        tool(
            "loom_memory_set",
            "Optionally persist a code observation for later sessions. Use exact source "
            "paths in files; native links are asynchronous.",
            {
                **key,
                "value": string,
                "files": {"type": "array", "items": string},
                "tags": {"type": "array", "items": string},
            },
            ["memory_key", "value"],
        ),
        tool(
            "loom_memory_update",
            "Revise an existing memory using its content hash. Changing code does not "
            "automatically rewrite a memory claim.",
            {**key, "value": string, "if_match_sha256": string},
            ["memory_key", "value", "if_match_sha256"],
        ),
        tool(
            "loom_memory_for_file",
            "Read project memories associated with an allowed source file, including "
            "native code-state warnings.",
            {"path": string},
        ),
    ]


class MemoryTools:
    def __init__(
        self,
        backend,
        *,
        org,
        namespace,
        agent,
        code_namespaces,
        allowed_paths,
        repository_tag,
        history_sha256,
        user_id="-",
        session_id="-",
        ledger=None,
    ):
        if not all(isinstance(x, str) and x for x in (org, namespace, agent, user_id, session_id)):
            raise ValueError("Memory identity must be explicit")
        if not code_namespaces or not allowed_paths:
            raise ValueError("Memory requires certified code namespaces and allowed paths")
        if not isinstance(repository_tag, str) or not re.fullmatch(
            r"repo:[0-9a-f]{10}", repository_tag
        ):
            raise ValueError("Memory requires the certified canonical repository tag")
        if not isinstance(history_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", history_sha256):
            raise ValueError("Memory requires a frozen historical corpus hash")
        self.history_sha256 = history_sha256
        self.repository_tag = repository_tag
        self.backend = backend
        self.org, self.namespace, self.agent = org, namespace, agent
        self.user_id, self.session_id = user_id, session_id
        self.code_namespaces, self.allowed_paths = tuple(code_namespaces), tuple(allowed_paths)
        self.ledger = ledger
        self.persisted_memory_writes = 0
        self.write_outcomes_unknown = False

    def bind_ledger(self, ledger):
        if self.ledger is not None and self.ledger is not ledger:
            raise ValueError("Use a fresh memory adapter for each session")
        self.ledger = ledger

    def project(self, data):
        if not isinstance(data, dict):
            raise PermissionError("Memory response is not an object")

        def links(items):
            result = []
            for item in items:
                if (
                    item.get("code_namespace") not in self.code_namespaces
                    or item.get("path") not in self.allowed_paths
                ):
                    raise PermissionError("Memory link exceeds certified source scope")
                result.append({key: item[key] for key in LINK_FIELDS if key in item})
            return result

        def row(item, fields):
            if item.get("namespace", self.namespace) != self.namespace:
                raise PermissionError("Memory response exceeds bound namespace")
            for field in ("files", "matched_files"):
                if any(path not in self.allowed_paths for path in item.get(field, [])):
                    raise PermissionError("Memory file exceeds source scope")
            out = {key: item[key] for key in fields if key in item}
            if "code" in item:
                out["code"] = links(item["code"])
            return out

        out = row(data, FIELDS)
        if any(ns != self.namespace for ns in data.get("namespaces_searched", [])):
            raise PermissionError("Memory search exceeds bound namespace")
        for field in ("results", "hits"):
            if field in data:
                out[field] = [row(hit, HIT_FIELDS) for hit in data[field]]
        return out

    def execute(self, name, args):
        if name not in OPS:
            raise ValueError("Unknown memory operation")
        if self.ledger is None:
            raise ValueError("Memory adapter requires an attempt ledger")
        allowed = {
            "loom_memory_search": {"query"},
            "loom_memory_get": {"memory_key"},
            "loom_memory_set": {"memory_key", "value", "files", "tags"},
            "loom_memory_update": {"memory_key", "value", "if_match_sha256"},
            "loom_memory_for_file": {"path"},
        }[name]
        required = (
            allowed
            if name not in {"loom_memory_set", "loom_memory_update"}
            else {"memory_key", "value", "if_match_sha256"}
            if name == "loom_memory_update"
            else {"memory_key", "value"}
        )
        if set(args) - allowed or required - set(args):
            raise ValueError("Unexpected or missing memory argument")
        for key in ("query", "memory_key", "value", "path"):
            if key in args and (
                not isinstance(args[key], str) or not args[key] or len(args[key]) > 16000
            ):
                raise ValueError("Memory text must contain 1..16000 characters")
        if "files" in args and (
            not isinstance(args["files"], list)
            or any(p not in self.allowed_paths for p in args["files"])
        ):
            raise PermissionError("Memory files exceed source scope")
        if "path" in args and args["path"] not in self.allowed_paths:
            raise PermissionError("Memory path exceeds source scope")
        if "tags" in args and (
            not isinstance(args["tags"], list)
            or any(not isinstance(tag, str) or tag.startswith("repo:") for tag in args["tags"])
        ):
            raise ValueError("Repository tags are controller-bound")
        if name == "loom_memory_update" and (
            not isinstance(args["if_match_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", args["if_match_sha256"])
        ):
            raise ValueError("Update requires the current native content hash")
        body = {"org": self.org, "namespace": self.namespace, **args}
        if name != "loom_memory_for_file":
            body.update(agent=self.agent)
        if name in {"loom_memory_get", "loom_memory_set"}:
            body.update(user_id=self.user_id, session_id=self.session_id)
        if name == "loom_memory_search":
            body.update(namespace_scope="this", auto_touch=False, agent="*")
        if name == "loom_memory_set":
            body.update(memory_type="semantic", tags=[self.repository_tag, *args.get("tags", [])])
        if name == "loom_memory_update":
            body.update(user_id=self.user_id)
        writing = name in {"loom_memory_set", "loom_memory_update"}
        started = time.monotonic()
        try:
            receipt = self.backend.request(OPS[name], body)
        except Exception as error:  # A failed dispatch may still have committed a write.
            self.write_outcomes_unknown |= writing
            self.ledger.emit(
                "retrieval",
                caller="model",
                stage="memory",
                op=OPS[name],
                request=body,
                logical_requests=1,
                physical_requests=getattr(error, "physical_requests", None),
                latency_s=time.monotonic() - started,
                error=str(error),
                cost_usd=None,
                cost_status="unknown",
            )
            raise
        self.ledger.emit(
            "retrieval",
            caller="model",
            stage="memory",
            op=OPS[name],
            request=body,
            logical_requests=1,
            latency_s=time.monotonic() - started,
            result_sha256=digest(receipt.get("data")),
            native_result=receipt.get("data"),
            **{k: v for k, v in receipt.items() if k != "data"},
        )
        if (
            receipt.get("scope") != {"org": self.org, "namespace": self.namespace}
            or receipt.get("permission") != "allowed"
            or receipt.get("ignored_args")
            or receipt.get("data", {}).get("ignored_args")
        ):
            self.write_outcomes_unknown |= writing
            raise PermissionError("Memory scope or accepted arguments are unverified")
        try:
            data = self.project(receipt["data"])
        except (PermissionError, TypeError, KeyError):
            self.write_outcomes_unknown |= writing
            raise
        version = data.get("new_version" if name == "loom_memory_update" else "version")
        proof = (
            type(version) is int
            and version > 0
            and bool(data.get("memory_id"))
            and isinstance(data.get("content_sha256"), str)
            and len(data["content_sha256"]) == 64
        )
        persisted = (
            writing
            and proof
            and (
                data.get("updated") is True
                if name == "loom_memory_update"
                else bool(data.get("status"))
                and data.get("noop") is False
                and data.get("dry_run") is False
            )
        )
        if persisted:
            self.persisted_memory_writes += 1
            self.ledger.emit(
                "memory_persisted",
                operation=OPS[name],
                memory_id=data["memory_id"],
                memory_key=args["memory_key"],
                version=version,
                content_sha256=data["content_sha256"],
                namespace=self.namespace,
            )
        elif writing and not (data.get("noop") is True or data.get("updated") is False):
            self.write_outcomes_unknown = True
        return data
