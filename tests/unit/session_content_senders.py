"""Conservative source sites that require review when an automatic sender changes."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from scripts.codegen.op_reach_census import _TS_KEEP_OR_COMMENT, _files, _sdk_index

_SEND = frozenset(
    {
        "post",
        "_post",
        "post_json",
        "_post_json",
        "call",
        "_call",
        "postBounded",
        "postBoundedResult",
        "postFireAndForget",
        "requestJson",
        "fetch",
    }
)
_OP = re.compile(r"^[a-z_]+(?:\.[a-z_]+){1,2}$")
_URL = re.compile(r"/v1/([a-z_]+(?:\.[a-z_]+){1,2})(?![a-z_])")
_TOKEN = re.compile(
    r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\S"""
)
_JS_ROOTS = (
    "integrations/claude-code-plugin/hooks",
    "integrations/claude-code-plugin/lib",
    "integrations/codex-plugin/hooks",
    "integrations/codex-plugin/lib",
    "integrations/grep-shim/lib",
    "integrations/cline",
    "sdk/typescript/src/integrations",
)
_PY_ROOTS = ("sdk/python/loom/integrations",)
_SINGLE = (
    "sdk/python/loom/session.py",
    "sdk/python/loom/memory.py",
    "sdk/python/loom/run.py",
    "sdk/typescript/src/session.ts",
    "sdk/typescript/src/runtime.ts",
    "frontend/src/lib/vitals.ts",
    "server/cli/shell.py",
)


@dataclass(frozen=True, order=True)
class Site:
    path: str
    op: str
    fingerprint: str
    line: int
    occurrence: int = 0

    @property
    def key(self) -> str:
        return f"{self.path}:{self.op}:{self.fingerprint}:{self.occurrence}"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:20]


def _operation(value: str) -> str:
    if _OP.fullmatch(value):
        return value
    match = _URL.search(value)
    return match.group(1) if match else "<dynamic>"


def _end(tokens: list[str], start: int) -> int:
    stack = []
    pairs = {"(": ")", "{": "}", "[": "]"}
    for i in range(start, len(tokens)):
        token = tokens[i]
        if token in pairs:
            stack.append(pairs[token])
        elif token in pairs.values():
            if not stack or stack.pop() != token:
                raise ValueError("unbalanced sender expression")
            if not stack:
                return i
    raise ValueError("unterminated sender expression")


def javascript_sites(text: str, path: str) -> set[Site]:
    code = _TS_KEEP_OR_COMMENT.sub(lambda m: m.group(1) or "\n" * m.group().count("\n"), text)
    matches = list(_TOKEN.finditer(code))
    tokens = [m.group() for m in matches]
    constants = {}
    for i in range(len(tokens) - 3):
        if tokens[i] == "const" and tokens[i + 2] == "=" and tokens[i + 3][:1] in "\"'`":
            constants[tokens[i + 1]] = tokens[i + 3][1:-1]
    sites = set()
    occurrences: Counter[tuple[str, str]] = Counter()
    for i, token in enumerate(tokens[:-2]):
        deferred = token == "op" and tokens[i + 1] == ":" and i > 0 and tokens[i - 1] in {"{", ","}
        call = token in _SEND and tokens[i + 1] == "(" and (not i or tokens[i - 1] != "function")
        if not (call or deferred):
            continue
        start = i + 1
        if call:
            end = _end(tokens, start)
            # A named function/method declaration does not send a request.
            if end + 1 < len(tokens) and (
                tokens[end + 1] == "{"
                or (tokens[end + 1] == ":" and i + 3 < len(tokens) and tokens[i + 3] == ":")
            ):
                continue
        else:
            openings = []
            for j in range(i):
                if tokens[j] == "{":
                    openings.append(j)
                elif tokens[j] == "}" and openings:
                    openings.pop()
            if not openings:
                raise ValueError("op property outside an object")
            start = openings[-1]
            end = _end(tokens, start)
        value = tokens[i + 2]
        literal = value[1:-1] if value[:1] in "\"'`" else constants.get(value, "")
        op = _operation(literal)
        digest = _digest(tokens[start : end + 1])
        occurrences[op, digest] += 1
        sites.add(
            Site(
                path,
                op,
                digest,
                code.count("\n", 0, matches[i].start()) + 1,
                occurrences[op, digest],
            )
        )
    return sites


def _chain(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*_chain(node.value), node.attr]
    return []


def python_sites(text: str, path: str, classes, attrs) -> set[Site]:
    tree = ast.parse(text)
    nodes = list(ast.walk(tree))
    if path in {"server/cli/shell.py", "sdk/python/loom/memory.py"}:
        entry = "_cmd_ingest_transcript" if path.endswith("shell.py") else "_SetCoalescer"
        entries = [
            n
            for n in nodes
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.name == entry
        ]
        if len(entries) != 1:
            raise ValueError("automatic sender entry point changed: " + entry)
        nodes = list(ast.walk(entries[0]))
    parents = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
    sites = set()
    occurrences: Counter[tuple[str, str]] = Counter()
    for node in nodes:
        ops = set()
        expression = node
        if isinstance(node, ast.Call):
            parts = _chain(node.func)
            if parts and (parts[-1] in _SEND or parts == ["urllib", "request", "Request"]):
                argument = (
                    node.args[0]
                    if node.args
                    else next(
                        (k.value for k in node.keywords if k.arg in {"op", "operation"}), None
                    )
                )
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    literal = argument.value
                elif isinstance(argument, ast.JoinedStr):
                    literal = "".join(
                        part.value
                        if isinstance(part, ast.Constant) and isinstance(part.value, str)
                        else "{}"
                        for part in argument.values
                    )
                else:
                    literal = ""
                ops.add(_operation(literal))
        if isinstance(node, ast.Attribute):
            parts = _chain(node)
            if len(parts) >= 2:
                cls = attrs.get(parts[-2])
                ops.update(classes.get(cls, {}).get(parts[-1], ()))
                # Referenced methods passed to an executor are send sites too.
                parent = parents.get(node)
                while parent is not None and not isinstance(parent, (ast.Call, ast.stmt)):
                    parent = parents.get(parent)
                if isinstance(parent, ast.Call):
                    expression = parent
        for op in ops:
            scope = []
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope.append(parent.name)
                parent = parents.get(parent)
            digest = _digest([scope, ast.dump(expression, include_attributes=False)])
            occurrences[op, digest] += 1
            sites.add(Site(path, op, digest, node.lineno, occurrences[op, digest]))
    return sites


def collect(root: Path) -> set[Site]:
    paths = {p for rel in _JS_ROOTS for p in _files(root, rel, (".mjs", ".ts"))}
    paths.update(p for rel in _PY_ROOTS for p in _files(root, rel, (".py",)))
    paths.update(root / rel for rel in _SINGLE)
    paths.update(
        p
        for rel in ("integrations/cursor", "integrations/generic")
        for p in _files(root, rel, (".sh",))
        if p.name != "test.sh"
    )
    classes, attrs = _sdk_index(root)
    sites = set()
    for path in sorted(paths):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".sh":
            code = "\n".join(
                line
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            sites.add(Site(rel, "<shell-entry>", _digest(code), 1))
        else:
            sites.update(
                python_sites(text, rel, classes, attrs)
                if path.suffix == ".py"
                else javascript_sites(text, rel)
            )
    return sites
