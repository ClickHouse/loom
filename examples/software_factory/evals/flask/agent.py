"""Flask pytest tools and reference controls inside Harbor task environments."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from examples.software_factory.evals.flask.grading import validate_regression_source
from examples.software_factory.factory.model import TestRefusal
from examples.software_factory.harbor.agent import envelope
from examples.software_factory.harbor.runtime import write


class VisibleGrader:
    supports_regression_tests = True

    def __init__(self, environment, loop, spec, output):
        self.environment, self.loop, self.spec, self.output = environment, loop, spec, output
        self.sha256, self.calls = spec["grader_sha256"], 0
        self.factory = None
        self.regression_calls = 0

    async def evaluate_regression(self, checkout, source):
        try:
            source = validate_regression_source(source)
        except ValueError as error:
            raise TestRefusal(str(error)) from error
        self.regression_calls += 1
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.json"
            authored = Path(directory) / "regression.py"
            write(candidate, envelope(self.factory))
            authored.write_text(source)
            await self.environment.upload_file(candidate, "/tmp/factory-candidate.json")
            await self.environment.upload_file(authored, "/tmp/factory-regression.py")
            response = await self.environment.exec(
                "python /factory/grader.py regression /tmp/factory-candidate.json "
                "/tmp/factory-regression.py",
                timeout_sec=40,
            )
        if response.return_code != 0:
            raise RuntimeError("Regression grader failed: " + (response.stderr or "")[-1000:])
        # JSON escaping can expand the bounded Unicode failure text.
        if len(response.stdout) > 64000:
            raise RuntimeError("Regression grader response exceeds its output bound")
        result = json.loads(response.stdout)
        if len(result.get("output", "")) > 4000:
            raise RuntimeError("Regression failure detail exceeds its output bound")
        if result["state"] != checkout.state():
            raise PermissionError("Regression test source differs from the candidate")
        (self.output / f"regression-{self.regression_calls}.py").write_text(source)
        write(self.output / f"regression-{self.regression_calls}.json", result)
        return result

    def regression(self, checkout, source):
        return asyncio.run_coroutine_threadsafe(
            self.evaluate_regression(checkout, source), self.loop
        ).result()

    async def evaluate(self, checkout):
        self.calls += 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            write(path, envelope(self.factory))
            await self.environment.upload_file(path, "/tmp/factory-candidate.json")
            response = await self.environment.exec(
                "python /factory/grader.py visible /tmp/factory-candidate.json",
                timeout_sec=40,
            )
        if response.return_code != 0:
            raise RuntimeError("Visible grader failed: " + (response.stderr or "")[-1000:])
        result = json.loads(response.stdout)
        if result["state"] != checkout.state():
            raise PermissionError("Visible test source differs from the candidate")
        write(self.output / f"visible-{self.calls}.json", result)
        return result

    def run(self, checkout, *, hidden=False):
        if hidden:
            raise PermissionError("Only Harbor may run the independent hidden verifier")
        limit = self.spec["visible_test_call_limit"]
        if limit is not None and self.calls >= limit:
            raise TestRefusal("Visible test budget exhausted")
        return asyncio.run_coroutine_threadsafe(self.evaluate(checkout), self.loop).result()


class ControlProvider:
    """Offline base/reference qualification; never used in a measured trial."""

    label = "scripted-control"

    def __init__(self, source, task, private, control):
        import tempfile

        from examples.software_factory.evals.flask.historical import git

        self.edits = []
        if control == "reference":
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name, content in task.files.items():
                    p = root / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
                git(root, "apply", "--include=src/flask/*", "-", data=private["patch"].encode())
                import difflib

                for name, before in task.files.items():
                    after = (root / name).read_text()
                    if before != after:
                        left, right = before.splitlines(True), after.splitlines(True)
                        for group in difflib.SequenceMatcher(None, left, right).get_grouped_opcodes(
                            3
                        ):
                            old = "".join(left[group[0][1] : group[-1][2]])
                            new = "".join(right[group[0][3] : group[-1][4]])
                            self.edits.append(
                                {
                                    "toolUseId": "control-" + str(len(self.edits)),
                                    "name": "edit",
                                    "input": {"path": name, "old": old, "new": new},
                                }
                            )

    def step(self, messages, tools, max_tokens):
        edits, self.edits = self.edits, []
        return {
            "blocks": [{"toolUse": e} for e in edits] or [{"text": "Control complete."}],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            "physical_requests": 0,
            "generation_cost_usd": 0,
            "cost_usd": 0,
            "retries": 0,
        }
