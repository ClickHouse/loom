"""Which release.yml jobs run, worked out from the workflow's own `if:`s.

A small evaluator for the expressions the release workflow uses, with GitHub's
rule that an `if` naming no status function also needs every job upstream to
have succeeded (an implicit `success()`), run over the paths a release takes:
the tag push, the registry dispatch on the published tag, a dry run, and a
failed build. Only a published release runs the tag paths for real, so this is
where they are checked before one."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
JOBS = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
STATUS = re.compile(r"\b(success|failure|always|cancelled)\(\)")


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _upstream(name: str) -> set[str]:
    seen: set[str] = set()
    todo = _needs(JOBS[name])
    while todo:
        job = todo.pop()
        if job not in seen:
            seen.add(job)
            todo += _needs(JOBS[job])
    return seen


def _evaluate(expression: str, context: dict[str, object]) -> bool:
    """The expression as Python: `&&`, `||`, `!`, `==`/`!=` on strings, the
    contexts as lookups. A missing output reads as '' and inputs on a push as
    None, as on GitHub."""
    text = expression.strip()
    if text.startswith("${{"):
        text = text[3:].removesuffix("}}")
    text = re.sub(
        r"\bneeds\.([\w-]+)\.(result|outputs\.\w+)",
        lambda m: f"N({m.group(1)!r}, {m.group(2)!r})",
        text,
    )
    text = re.sub(r"\b(inputs|github)\.([\w]+)", lambda m: f"C({m.group(0)!r})", text)
    text = text.replace("cancelled()", "False").replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    # eval of our own workflow file's expressions, rewritten to Python above.
    return bool(eval(text, {"N": context["N"], "C": context["C"]}))


def run(*, event: str, dry_run: bool | None, published: bool, fail: set[str] = frozenset(),
        prerelease: bool = False) -> set[str]:  # fmt: skip
    """The jobs that run, every one that runs succeeding unless named in `fail`."""
    result: dict[str, str] = {}
    outputs = {
        ("preflight", "outputs.images_published"): "true" if published and not dry_run else "",
        ("version", "outputs.prerelease"): "true" if prerelease else "false",
    }
    plain = {"inputs.dry_run": dry_run, "github.event_name": event}

    for name in JOBS:  # the file lists each job after the ones it needs
        direct = set(_needs(JOBS[name]))

        # needs.X.* is defined for a direct need only; anything else reads as ''.
        def lookup(job: str, field: str, direct: set[str] = direct) -> str:
            if job not in direct:
                return ""
            return result[job] if field == "result" else outputs.get((job, field), "")

        context = {"N": lookup, "C": plain.__getitem__}
        condition = JOBS[name].get("if", "")
        implicit = not STATUS.search(str(condition))
        ready = all(result[up] == "success" for up in _upstream(name)) if implicit else True
        runs = ready and (not condition or _evaluate(str(condition), context))
        result[name] = ("failure" if name in fail else "success") if runs else "skipped"
    return {name for name, state in result.items() if state != "skipped"}


BUILD = {"version", "preflight", "clickhouse", "build", "verify"}
REGISTRIES = {"pypi", "npm", "mcp-registry"}


def test_a_tag_push_builds_tags_smokes_and_releases() -> None:
    ran = run(event="push", dry_run=None, published=False)
    assert ran == BUILD | {"docker", "docker-tag", "docker-smoke", "github-release"}


def test_the_registry_dispatch_on_the_published_tag_reaches_every_registry() -> None:
    """No image is built or tagged again, and the registries still publish."""
    ran = run(event="workflow_dispatch", dry_run=False, published=True)
    assert ran == BUILD | {"docker-smoke", "github-release"} | REGISTRIES


def test_a_dispatch_on_a_tag_whose_images_are_not_out_builds_them_and_publishes() -> None:
    ran = run(event="workflow_dispatch", dry_run=False, published=False)
    assert ran == BUILD | {"docker", "docker-tag", "docker-smoke", "github-release"} | REGISTRIES


def test_a_prerelease_dispatch_skips_only_the_mcp_registry_listing() -> None:
    ran = run(event="workflow_dispatch", dry_run=False, published=True, prerelease=True)
    assert ran == BUILD | {"docker-smoke", "github-release", "pypi", "npm"}


def test_a_dry_run_builds_and_smokes_and_publishes_nothing() -> None:
    ran = run(event="workflow_dispatch", dry_run=True, published=True)
    assert ran == BUILD | {"docker", "docker-smoke"}


@pytest.mark.parametrize("failed", ["docker", "docker-tag", "docker-smoke", "verify"])
def test_a_failure_before_the_release_publishes_nothing(failed: str) -> None:
    ran = run(event="workflow_dispatch", dry_run=False, published=False, fail={failed})
    assert not ran & ({"github-release"} | REGISTRIES)


def test_a_tag_push_whose_images_are_published_builds_and_tags_none() -> None:
    """A re-run of every job, or the tag pushed again at the same commit."""
    ran = run(event="push", dry_run=None, published=True)
    assert ran == BUILD | {"docker-smoke", "github-release"}


def test_a_failed_smoke_on_the_published_tag_publishes_nothing() -> None:
    ran = run(event="workflow_dispatch", dry_run=False, published=True, fail={"docker-smoke"})
    assert not ran & ({"github-release"} | REGISTRIES)


def test_a_dry_run_whose_build_fails_smokes_nothing() -> None:
    ran = run(event="workflow_dispatch", dry_run=True, published=False, fail={"docker"})
    assert "docker-smoke" not in ran


def test_a_failed_release_step_publishes_no_registry() -> None:
    ran = run(event="workflow_dispatch", dry_run=False, published=True, fail={"github-release"})
    assert not ran & REGISTRIES
