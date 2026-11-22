"""Model-selected read-only tools with exact local source grounding."""

from __future__ import annotations

import json
import time

from examples.software_factory.factory.context import Context
from examples.software_factory.factory.model import digest, tool
from examples.software_factory.factory.patterns import compile_regex

OPERATIONS = {"code.symbol", "code.grep", "code.search", "code.outline", "code.enclosing"}
TOOL_OPS = {"loom_" + op.replace(".", "_"): op for op in sorted(OPERATIONS)}


def request_body(op, args, *, org, namespace, memory_namespaces=()):
    body = {"org": org, "namespace": namespace, **args}
    if op == "code.symbol":
        body.update(include_body=True, include_history=False, include_examples=False)
    elif op == "code.enclosing":
        body.update(include_body=True)
        body["locations"] = list(dict.fromkeys(body["locations"]))
    elif op == "code.grep":
        pattern = body.get("pattern")
        if not isinstance(pattern, str) or not 2 <= len(pattern) <= 1000:
            raise ValueError("Search pattern must contain 2 to 1000 characters")
        regex = body.get("regex", False)
        if type(regex) is not bool:
            raise ValueError("Search requires a boolean regex flag")
        if regex:
            compile_regex(pattern)
    if op in OPERATIONS:
        body["include_lessons"] = bool(memory_namespaces)
        if memory_namespaces:
            body["lesson_namespaces"] = list(memory_namespaces)
    return body


def schemas() -> list[dict]:
    string = {"type": "string"}
    return [
        tool(
            "loom_code_symbol",
            "Read by name or symbol_id. Definition unit_id from symbol results and symbol_id "
            "from enclosing results support exact lookup. Follow search cards using next/name. "
            "For absent/imported names use loom_code_grep; an outline can narrow an elided class.",
            {"name": string, "symbol_id": string},
            [],
        ),
        tool(
            "loom_code_grep",
            "Find source lines (literal by default, RE2 when regex=true), including imports. "
            "Use | for alternation. Narrow path_prefix when a file is "
            "known; follow next_cursor with cursor for more matches.",
            {
                "pattern": {"type": "string", "minLength": 2, "maxLength": 1000},
                "path_prefix": string,
                "regex": {"type": "boolean"},
                "cursor": string,
            },
            ["pattern"],
        ),
        tool(
            "loom_code_search",
            "Discover relevant code for a concept. Results may include source bodies and "
            "navigation cards; follow a card with loom_code_symbol when needed.",
            {"query": string, "path_prefix": string, "cursor": string},
            ["query"],
        ),
        tool(
            "loom_code_outline",
            "List definitions in a known file without reading their bodies.",
            {"path": string},
        ),
        tool(
            "loom_code_enclosing",
            "Read definitions containing selected path:line locations. Select relevant hits "
            "before fetching; repeated definitions are returned once.",
            {"locations": {"type": "array", "items": string, "minItems": 1}},
        ),
    ]


class QualifiedContext(Context):
    def navigation(self, item, *, path=""):
        path = item.get("path", path)
        source = self.checkout.full_read(path)
        if source != self.task.files[path]:
            raise ValueError("Base-index navigation refers to an edited file")
        start, end = item.get("start_line"), item.get("end_line")
        if start is not None or end is not None:
            if (
                type(start) is not int
                or type(end) is not int
                or start < 1
                or end < start
                or end > len(source.splitlines())
            ):
                raise ValueError("Base-index navigation has an invalid source span")
        result = {
            "path": path,
            **{
                key: item[key]
                for key in (
                    "unit_id",
                    "symbol_id",
                    "symbol",
                    "name",
                    "qualified_name",
                    "kind",
                    "tier",
                    "confidence",
                    "signature",
                    "description",
                    "granularity",
                    "start_line",
                    "end_line",
                    "next",
                    "elided",
                    "body_elided",
                )
                if key in item
            },
        }
        if item.get("outline"):
            result["outline"] = []
            for entry in item["outline"]:
                child = self.navigation(
                    {key: value for key, value in entry.items() if key != "outline"}, path=path
                )
                if (
                    child["path"] != path
                    or (start is not None and child["start_line"] < start)
                    or (end is not None and child["end_line"] > end)
                ):
                    raise ValueError("Member outline exceeds its declaration span")
                result["outline"].append(child)
        return result

    def navigation_cards(self, items, rejected):
        candidates, seen = [], set()
        for item in items:
            try:
                candidate = self.navigation(item)
                identity = digest(candidate)
                if identity not in seen:
                    candidates.append(candidate)
                    seen.add(identity)
            except (PermissionError, KeyError, ValueError) as error:
                rejected.append(str(error))
        return candidates

    def grounded_definitions(self, items, rejected, *, allow_context_prefix=False):
        definitions, seen = [], set()
        for item in items:
            if not item.get("start_line") or item.get("granularity") == "skeleton":
                continue
            identity = (item["path"], item["start_line"], item["end_line"])
            if identity in seen:
                continue
            seen.add(identity)
            grounded = self.ground(
                {
                    "definitions": [
                        {**item, "elided": item.get("elided") or item.get("body_elided")}
                    ]
                },
                allow_context_prefix=allow_context_prefix,
            )
            for unit in grounded["definitions"]:
                try:
                    unit.update(self.navigation(item))
                except (PermissionError, KeyError, ValueError) as error:
                    rejected.append(str(error))
                definitions.append(unit)
            rejected.extend(grounded["rejected"])
        return definitions

    def selected(self, op: str, args: dict, caller="model") -> dict:
        if op not in OPERATIONS:
            raise PermissionError("Undeclared retrieval operation")
        # The model cannot supply identity, index scope, provider or history settings.
        allowed = {
            "code.symbol": {"name", "symbol_id"},
            "code.grep": {"pattern", "path_prefix", "regex", "cursor"},
            "code.search": {"query", "path_prefix", "cursor"},
            "code.outline": {"path"},
            "code.enclosing": {"locations"},
        }[op]
        if set(args) - allowed:
            raise ValueError("Unexpected retrieval argument")
        body = request_body(
            op,
            args,
            org=self.org,
            namespace=self.task.namespace,
            memory_namespaces=self.memory_namespaces,
        )
        if op in {"code.grep", "code.search"}:
            prefix = args.get("path_prefix", "")
            if not isinstance(prefix, str):
                raise ValueError("path_prefix must be a string")
            if prefix and not any(path.startswith(prefix) for path in self.task.files):
                self.ledger.emit(
                    "local_scope_refusal",
                    caller=caller,
                    operation=op,
                    path_prefix=prefix,
                    physical_requests=0,
                )
                return {
                    "abstain": True,
                    "abstain_reason": "outside_factory_source_scope",
                    "scope": "factory_source_allowlist",
                    "server_coverage": "not_queried",
                    "request_dispatched": False,
                    "note": (
                        "This prefix matches none of the factory's source_paths. "
                        "Use source_paths for indexed lookup and ordinary search/read for "
                        "public repository docs, examples and test sources. "
                        "Loom's index coverage was not queried."
                    ),
                }
        key = digest([op, body, self.checkout.state()])
        if not self.memory_namespaces and key in self.cache:
            self.ledger.emit("context_reuse", stage="selected", caller=caller, key=key)
            return self.cache[key]
        if self.budget.retrievals is not None and self.requests >= self.budget.retrievals:
            raise TimeoutError("Retrieval budget exhausted")
        self.requests += 1
        started = time.monotonic()
        try:
            receipt = self.backend.request(op, body)
        except Exception as error:  # Retain every failed retrieval dispatch.
            self.ledger.emit(
                "retrieval",
                caller=caller,
                stage="selected",
                op=op,
                request=body,
                logical_requests=1,
                error=str(error),
                physical_requests=getattr(error, "physical_requests", None),
                latency_s=time.monotonic() - started,
                cost_usd=None,
                cost_status="unknown",
            )
            raise
        self.ledger.emit(
            "retrieval",
            caller=caller,
            stage="selected",
            op=op,
            request=body,
            logical_requests=1,
            latency_s=time.monotonic() - started,
            result_sha256=digest(receipt["data"]),
            **{k: v for k, v in receipt.items() if k != "data"},
        )
        expected = dict(
            org=self.org,
            namespace=self.task.namespace,
            repository=self.task.repository,
            commit=self.task.base_commit,
        )
        if (
            receipt.get("scope") != expected
            or receipt.get("permission") != "allowed"
            or receipt.get("ignored_args")
            or receipt["data"].get("ignored_args")
        ):
            raise PermissionError("Index scope or accepted arguments are unverified")
        evidence = self.project(op, receipt["data"])
        self.ledger.emit(
            "native_context",
            operation=op,
            response=receipt["data"],
            response_sha256=digest(receipt["data"]),
        )
        encoded = json.dumps(evidence)
        if (
            self.budget.context_chars is not None
            and self.chars + len(encoded) > self.budget.context_chars
        ):
            raise TimeoutError("Context budget exhausted")
        self.chars += len(encoded)
        if not self.memory_namespaces:
            self.cache[key] = evidence
        self.ledger.emit(
            "context",
            stage="selected",
            caller=caller,
            evidence=evidence,
            state=self.checkout.state(),
            operation=op,
        )
        return evidence

    def project(self, op, data):
        def lessons(items):
            if not self.memory_namespaces:
                return []
            allowed = {*self.memory_namespaces, *self.memory_code_namespaces}
            result = []
            for item in items:
                if item.get("namespace") not in allowed:
                    raise PermissionError("Code lesson exceeds bound memory namespaces")
                if item.get("path") and item["path"] not in self.task.files:
                    raise PermissionError("Code lesson exceeds source scope")
                result.append(
                    {
                        key: item[key]
                        for key in (
                            "namespace",
                            "memory_key",
                            "memory_id",
                            "excerpt",
                            "value",
                            "path",
                            "code_state",
                            "changed_since",
                            "code_changed_since",
                            "suggestion",
                            "version",
                            "version_text",
                            "content_sha256",
                            "value_truncated",
                            "next",
                        )
                        if key in item
                    }
                )
                if "code" in item:
                    links = []
                    for link in item["code"]:
                        if (
                            link.get("code_namespace") not in self.memory_code_namespaces
                            or link.get("path") not in self.task.files
                        ):
                            raise PermissionError("Code lesson link exceeds certified source scope")
                        links.append(
                            {
                                key: link[key]
                                for key in (
                                    "code_namespace",
                                    "path",
                                    "symbol",
                                    "state",
                                    "changed_since",
                                    "suggestion",
                                )
                                if key in link
                            }
                        )
                    result[-1]["code"] = links
            return result

        out = {
            key: data[key]
            for key in (
                "abstain",
                "abstain_reason",
                "note",
                "partial",
                "partial_reason",
                "truncated",
                "has_more",
                "not_covered",
                "coverage_floor",
                "uses_truncated",
                "ambiguous",
                "verdict",
                "verdict_reason",
                "resolved_by",
            )
            if key in data
        }
        if data.get("next_cursor"):
            out["next_cursor"] = data["next_cursor"]
        rejected = []
        if op in {"code.symbol", "code.enclosing"}:
            original = data.get("definitions" if op == "code.symbol" else "results", [])
            out["definitions"] = self.grounded_definitions(original, rejected)
            out["abstain"] = not out["definitions"]
            accepted = {
                (unit["path"], unit["start_line"], unit["end_line"]) for unit in out["definitions"]
            }
            unserved = [
                item
                for item in original
                if item.get("start_line")
                and (
                    item.get("elided")
                    or item.get("body_elided")
                    or item.get("granularity") == "skeleton"
                )
                and (item.get("path"), item.get("start_line"), item.get("end_line")) not in accepted
            ]
            exact_cards = self.navigation_cards(unserved, rejected)
            if self.memory_namespaces:
                for unit in [*out["definitions"], *exact_cards]:
                    matching = [
                        item
                        for item in original
                        if item.get("path") == unit["path"]
                        and item.get("start_line") == unit["start_line"]
                        and item.get("end_line") == unit["end_line"]
                    ]
                    attached = lessons(
                        [lesson for item in matching for lesson in item.get("lessons", [])]
                    )
                    if attached:
                        unit["lessons"] = attached
            cards = list(exact_cards)
            for candidate in self.navigation_cards(data.get("candidates", []), rejected):
                if candidate not in cards:
                    cards.append(candidate)
            if exact_cards:
                out["abstain"] = bool(data.get("abstain", False))
            if cards or data.get("candidates"):
                out["candidates"] = cards
                out["navigation_only"] = bool(cards) and not out["definitions"]
        elif op == "code.grep":
            matches = []
            for hit in data.get("matches", []):
                try:
                    lines = self.checkout.full_read(hit["path"]).split("\n")
                    line = int(hit["line"])
                    if line < 1 or line > len(lines) or lines[line - 1].strip() != hit["text"]:
                        raise ValueError("Retrieved line differs from working source")
                    matches.append(
                        {k: hit[k] for k in ("path", "line", "text", "scope") if k in hit}
                    )
                except (PermissionError, KeyError, ValueError) as error:
                    rejected.append(str(error))
            out["matches"] = matches
        else:
            entries = []
            for item in data.get("results" if op == "code.search" else "entries", []):
                path = item.get("path", data.get("path", ""))
                try:
                    entries.append(self.navigation(item, path=path))
                except (PermissionError, KeyError, ValueError) as error:
                    rejected.append(str(error))
            out["entries"] = entries
            if op == "code.search" and any(item.get("body") for item in data.get("results", [])):
                out["definitions"] = self.grounded_definitions(
                    data["results"], rejected, allow_context_prefix=True
                )
        out.update(
            rejected=rejected,
            index_commit=self.task.base_commit,
            freshness="checked_against_working_source",
            coverage="base_index_lower_bound",
        )
        attached = lessons(data.get("lessons", []))
        if attached:
            out["lessons"] = attached
            out["memory_freshness"] = "native_reported_state"
        if self.memory_namespaces and "lessons_left_out" in data:
            out["lessons_left_out"] = data["lessons_left_out"]
        return out
