"""Loom as the model's tools.

Six verbs an LLM can call — remember, recall, ask, offload, fetch, note —
as tool definitions in the shape each provider expects, and one
`execute()` that runs them against an `Agent` under a scope and returns the
string the model reads::

    tools = bot.tools(user_id="u1", session_id="thread-42")
    defs = tools.definitions("openai")          # or "anthropic" | "generic"
    # … the model returns a tool call {name, arguments} …
    result = await tools.execute(call.name, call.arguments)

The parameter schemas are hand-written subsets of the generated op inputs:
what a model should decide (key, value, query), not what the SDK stamps
(identity) or the operator tunes (budgets, modes). The TypeScript SDK
carries the same six with the same schemas.
"""

# ruff: noqa: E501  (tool descriptions are prose the model reads)
from __future__ import annotations

import functools
import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from loom.errors import LoomError
from loom.run import credit_answer

if TYPE_CHECKING:
    from loom.agent import Agent

ToolFormat = Literal["anthropic", "openai", "generic"]

LOOM_TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "loom_remember",
        "description": (
            "Store one fact about the user or the task under a stable key so it can be "
            "recalled in later sessions. Use a short snake_case key; the same key updates the fact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "memory_key": {
                    "type": "string",
                    "description": "Stable identifier, e.g. seat_preference",
                },
                "value": {
                    "type": "string",
                    "description": "The fact, one or two sentences, in the third person",
                },
                "memory_type": {
                    "type": "string",
                    "description": "semantic (default): a fact; preference: what the user likes; procedural: how to do something",
                    "enum": ["semantic", "preference", "procedural"],
                },
            },
            "required": ["memory_key", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "loom_recall",
        "description": (
            "Search stored memories by meaning. Returns the closest facts with a similarity score; "
            "call before answering anything the user may have told you earlier. A result too long "
            "to return leaves out its lowest-ranked memories and counts them in omitted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you want to know, as a question or phrase",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many facts to return (default 5)",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "loom_ask",
        "description": (
            "Ask the memory store a question and get a grounded answer with the memory keys it cites, "
            "or an abstention when the memories do not say. Prefer it over loom_recall when you need "
            "an answer, not raw facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The question"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "loom_offload",
        "description": (
            "Park a large piece of text (a log, a page, a file) outside the conversation and get back "
            "a one-line pointer to keep instead. Use for anything over a few hundred lines you may need again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The text to park"},
                "summary": {"type": "string", "description": "One line saying what it is"},
                "blob_key": {
                    "type": "string",
                    "description": "Optional stable key; omit to address by content",
                },
            },
            "required": ["content", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "loom_fetch",
        "description": (
            "Read a parked text back: the lines matching a regex, or a character range. "
            "Page with offset until next_offset is -1."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "blob_key": {"type": "string", "description": "The key from the pointer"},
                "pattern": {
                    "type": "string",
                    "description": "Regex; only matching lines are returned",
                },
                "offset": {
                    "type": "integer",
                    "description": "Character offset to start from",
                    "minimum": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum characters to return",
                    "minimum": 1,
                },
            },
            "required": ["blob_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "loom_note",
        "description": (
            "Append a line to the working notes that are loaded at the start of every turn: "
            "the current plan, an open question, a decision taken."
        ),
        "parameters": {
            "type": "object",
            "properties": {"addition": {"type": "string", "description": "The line to add"}},
            "required": ["addition"],
            "additionalProperties": False,
        },
    },
)

TOOL_NAMES: tuple[str, ...] = tuple(s["name"] for s in LOOM_TOOL_SPECS)


def tool_definitions(
    fmt: ToolFormat = "generic", only: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Tool definitions in a provider's shape."""
    keep = set(only) if only is not None else None
    specs = [s for s in LOOM_TOOL_SPECS if keep is None or s["name"] in keep]
    if fmt == "anthropic":
        return [
            {"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
            for s in specs
        ]
    if fmt == "openai":
        return [
            {
                "type": "function",
                "function": {
                    "name": s["name"],
                    "description": s["description"],
                    "parameters": s["parameters"],
                    "strict": False,
                },
            }
            for s in specs
        ]
    return [dict(s) for s in specs]


class AgentTools:
    """The tools bound to one Agent and scope."""

    def __init__(
        self,
        agent: Agent,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        only: Iterable[str] | None = None,
        max_result_chars: int = 8_000,
    ) -> None:
        self.agent = agent
        self.scope: dict[str, str | None] = {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
        }
        keep = set(only) if only is not None else None
        self.names: tuple[str, ...] = tuple(n for n in TOOL_NAMES if keep is None or n in keep)
        self.max_result_chars = max_result_chars

    def definitions(self, fmt: ToolFormat = "generic") -> list[dict[str, Any]]:
        return tool_definitions(fmt, self.names)

    def has(self, name: str) -> bool:
        return name in self.names

    async def execute(self, name: str, args: str | Mapping[str, Any] | None) -> str:
        """Run one tool call; the result is the string to hand back to the model."""
        if not self.has(name):
            raise LoomError(f"unknown tool {name}; this agent offers {', '.join(self.names)}")
        a = _parse_args(args)
        out = await self._run(name, a)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        memories = out.get("memories") if isinstance(out, dict) else None
        if memories and len(memories) > 1 and len(text) > self.max_result_chars:
            text = self._fit_memories(out, memories)
        if len(text) > self.max_result_chars:
            return text[: self.max_result_chars] + "…[truncated]"
        return text

    def _fit_memories(self, out: dict[str, Any], memories: list[Any]) -> str:
        """Recalled memories over the cap: keep the best ranked whole, so the JSON stays valid."""
        dump = functools.partial(json.dumps, ensure_ascii=False, default=str)
        used = len(dump({**out, "memories": [], "omitted": len(memories)}))
        keep = 0
        for memory in memories:
            used += len(dump(memory)) + (2 if keep else 0)  # ", " between items
            if keep and used > self.max_result_chars:
                break
            keep += 1
        out["omitted"] = len(memories) - keep
        del memories[keep:]
        return dump(out)

    async def _call(self, op: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.agent.call(
            op,
            body,
            user_id=self.scope["user_id"],
            session_id=self.scope["session_id"],
            run_id=self.scope["run_id"],
        )

    async def _run(self, name: str, a: dict[str, Any]) -> Any:
        call = self._call
        if name == "loom_remember":
            body: dict[str, Any] = {"memory_key": _str(a, "memory_key"), "value": _str(a, "value")}
            if a.get("memory_type"):
                body["memory_type"] = str(a["memory_type"])
            r = await call("memory.set", body)
            return {
                "stored": True,
                "memory_key": body["memory_key"],
                "version": r.get("version"),
                "verdict": r.get("conflict_verdict"),
            }
        if name == "loom_recall":
            # Unpinned, a large top_k serves excerpts (above 15 by default)
            # and drops each hit's explanation.
            body = {"query": _str(a, "query"), "bodies": "full", "explain": False}
            if a.get("top_k") not in (None, ""):  # unset: the server's default
                body["top_k"] = _bounded("loom_recall", a, "top_k")
            r = await call("memory.search", body)
            return {
                "memories": [
                    {
                        "memory_key": h.get("memory_key"),
                        "content": h.get("content_excerpt"),
                        "similarity": round(float(h.get("similarity") or 0.0), 3),
                    }
                    for h in (r.get("results") or [])
                ]
            }
        if name == "loom_ask":
            r = await call("memory.ask", {"query": _str(a, "query")})
            credit_answer(r)
            return {
                "answer": r.get("answer") or "",
                "abstain": bool(r.get("abstain")),
                "cited": r.get("memory_keys") or [],
            }
        if name == "loom_offload":
            body = {"content": _str(a, "content"), "summary": _str(a, "summary")}
            if a.get("blob_key"):
                body["blob_key"] = str(a["blob_key"])
            r = await call("context.offload", body)
            return str(r.get("pointer") or "")
        if name == "loom_fetch":
            body = {"blob_key": _str(a, "blob_key")}
            for k in ("pattern", "offset", "limit"):
                if a.get(k) not in (None, ""):
                    body[k] = a[k] if k == "pattern" else _bounded("loom_fetch", a, k)
            r = await call("context.fetch", body)
            if not r.get("found"):
                return f"no parked text under blob_key={body['blob_key']} (expired or never stored)"
            return {
                "content": r.get("content") or "",
                "next_offset": r.get("next_offset", -1),
                "matched_lines": r.get("matched_lines"),
            }
        if name == "loom_note":
            r = await call("working_memory.append", {"addition": _str(a, "addition")})
            return {"noted": True, "version": r.get("version"), "fill_ratio": r.get("fill_ratio")}
        raise LoomError(f"unknown tool {name}")


def _parse_args(args: str | Mapping[str, Any] | None) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args.strip() else {}
        except ValueError as e:
            raise LoomError("tool arguments are not valid JSON") from e
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return dict(args)


def _bounded(tool: str, a: Mapping[str, Any], key: str) -> int:
    """``_int`` within the schema's minimum and maximum, as the TypeScript SDK checks it."""
    n = _int(a, key)
    (spec,) = [s for s in LOOM_TOOL_SPECS if s["name"] == tool]
    prop = spec["parameters"]["properties"][key]
    lo, hi = prop.get("minimum"), prop.get("maximum")
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        shown = f"{'' if lo is None else lo}..{'' if hi is None else hi}"
        raise LoomError(f"tool argument {key} must be within {shown}, got {n}")
    return n


def _int(a: Mapping[str, Any], key: str) -> int:
    # A whole number, quoted or not, 5.0 included; a bool is not a number here.
    v = a[key]
    n = v
    if isinstance(v, str) and v.strip():  # models sometimes quote a number
        try:
            n = float(v)
        except ValueError:
            pass
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if isinstance(n, bool) or not isinstance(n, int):
        raise LoomError(f"tool argument {key} must be an integer, got {json.dumps(v, default=str)}")
    return n


def _str(a: Mapping[str, Any], key: str) -> str:
    v = a.get(key)
    if not isinstance(v, str) or not v:
        raise LoomError(f"tool argument {key} is required and must be a non-empty string")
    return v
