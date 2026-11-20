"""Source-only capabilities; no shell, git history, arbitrary import or execution."""

from __future__ import annotations

import ast
import difflib
import json
from pathlib import Path

from .model import Task, digest


class Checkout:
    def __init__(self, root: Path, task: Task):
        self.root = root.resolve()
        self.original = dict(task.files)
        self.root.mkdir(parents=True, exist_ok=False)
        for name, content in task.files.items():
            path = self.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def path(self, name: str) -> Path:
        if name not in self.original or any(p.startswith(".") for p in Path(name).parts):
            raise PermissionError("Source file is outside the task allowlist")
        candidate = self.root / name
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root):
            raise PermissionError("Source path escapes checkout")
        return candidate

    def full_read(self, name: str) -> str:
        path = self.path(name)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Source budget exceeded")
        return path.read_text()

    def read(self, name: str, start_line: int = 1) -> str:
        if type(start_line) is not int or start_line < 1:
            raise ValueError("start_line must be a positive integer")
        return "".join(self.full_read(name).splitlines(True)[start_line - 1 :])[:16000]

    def files(self) -> dict[str, str]:
        return {name: self.full_read(name) for name in self.original}

    def state(self) -> str:
        return digest(self.files())

    def edit(self, path: str, old: str, new: str) -> dict:
        before = self.full_read(path)
        if not old or before.count(old) != 1:
            return {"error": "Replacement must match exactly once"}
        after = before.replace(old, new, 1)
        if len(after.encode()) > 1024 * 1024 or len(new) > 16000:
            raise ValueError("Source budget exceeded")
        self.path(path).write_text(after)
        return {"edited": path, "state": self.state()}

    def diff(self) -> str:
        return "".join(
            "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    self.full_read(name).splitlines(True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            )
            for name, before in self.original.items()
        )


def expression(node: ast.AST, values: dict):
    """Interpret only finite scalar expressions used by the authored fixtures."""
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, bool, type(None)):
        if len(str(node.value)) > 1000:
            raise ValueError("Literal budget exceeded")
        return node.value
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.BoolOp):
        items = [expression(v, values) for v in node.values]
        return all(items) if isinstance(node.op, ast.And) else any(items)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not expression(node.operand, values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = expression(node.left, values), expression(node.right, values)
        if type(left) is not str or type(right) is not str or len(left) + len(right) > 4000:
            raise ValueError("Only bounded string addition is supported")
        return left + right
    if isinstance(node, ast.IfExp):
        branch = node.body if expression(node.test, values) else node.orelse
        return expression(branch, values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right = expression(node.left, values), expression(node.comparators[0], values)
        op = node.ops[0]
        if isinstance(op, ast.NotIn):
            return left not in right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.Eq):
            return left == right
    if isinstance(node, ast.Call) and not node.keywords:
        args = [expression(a, values) for a in node.args]
        if isinstance(node.func, ast.Name) and node.func.id == "bool" and len(args) == 1:
            return bool(args[0])
        if isinstance(node.func, ast.Attribute):
            value = expression(node.func.value, values)
            allowed = {"upper", "lower", "strip", "lstrip", "rstrip"}
            if type(value) is str and node.func.attr in allowed and len(args) <= 1:
                return getattr(value, node.func.attr)(*args)
    raise ValueError("Unsupported candidate expression")


def evaluate(source: str, symbol: str, args: list):
    tree = ast.parse(source)
    if len(list(ast.walk(tree))) > 150:
        raise ValueError("AST budget exceeded")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Exactly one function is supported")
    function = tree.body[0]
    if function.name != symbol or function.decorator_list or len(function.body) != 1:
        raise ValueError("Expected one undecorated function with a return expression")
    statement = function.body[0]
    if not isinstance(statement, ast.Return) or len(function.args.args) != len(args):
        raise ValueError("Function signature mismatch")
    return expression(
        statement.value, dict(zip((a.arg for a in function.args.args), args, strict=True))
    )


def require_independent_grader(grader) -> None:
    """Refuse graders whose candidate can write their result or alter assertions.

    The adapter is trusted host code, frozen with the protocol. This declaration
    is an admission check, not a certificate for an arbitrary external adapter.
    """
    if getattr(grader, "result_authority", None) != "host":
        raise PermissionError("A candidate-independent objective grader is required")


class ObjectiveGrader:
    # Candidate expressions are interpreted as data and cannot execute host code.
    result_authority = "host"

    def __init__(self, path: Path, expected_hash: str):
        self.data = json.loads(path.read_text())
        if digest(self.data) != expected_hash:
            raise ValueError("Frozen grader hash mismatch")
        self.sha256 = expected_hash

    def run(self, checkout: Checkout, *, hidden: bool = False) -> dict:
        cases = self.data["hidden" if hidden else "visible"]
        checks = []
        for case in cases:
            try:
                actual = evaluate(
                    checkout.full_read(self.data["path"]), self.data["symbol"], case["args"]
                )
                passed = type(actual) is type(case["expected"]) and actual == case["expected"]
                detail = {"args": case["args"], "actual": actual, "expected": case["expected"]}
            except (SyntaxError, ValueError, TypeError, KeyError, RecursionError) as error:
                passed, detail = False, {"error": str(error)}
            checks.append({"passed": passed, **({} if hidden else detail)})
        return {
            "passed": all(c["passed"] for c in checks),
            "result_authority": self.result_authority,
            "checks": checks,
            "state": checkout.state(),
            "grader_sha256": self.sha256,
        }
