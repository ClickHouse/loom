"""Publish verified translation data and reserve a finite translation allowance.

The workflow executes this module from main. Pending branches supply git objects only.
The budget branch must be initialized explicitly once; a missing ledger fails closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.i18n import refresh
from scripts.i18n import translate as t

PENDING = "refs/heads/i18n/update"
BUDGET = "refs/heads/i18n/budget"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def command(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        args, cwd=t.ROOT, input=input_text, check=True, capture_output=True, text=True
    ).stdout.strip()


def git(*args: str, input_text: str | None = None) -> str:
    return command("git", *args, input_text=input_text)


def fetch(ref: str) -> str | None:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", ref],
        cwd=t.ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode == 2:
        return None
    result.check_returncode()
    git("fetch", "--no-tags", "origin", ref)
    return git("rev-parse", "FETCH_HEAD")


def file_at(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=t.ROOT, check=True, capture_output=True, text=True
    ).stdout


def money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("translation allowance must be a finite nonnegative USD amount") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("translation allowance must be a finite nonnegative USD amount")
    return amount


def budget_total(tip: str) -> Decimal:
    """Verify the entire append-only ledger, including every reservation's parent."""
    commits = git("rev-list", "--reverse", tip).splitlines()
    previous = ""
    total = Decimal(0)
    for commit in commits:
        parents = git("show", "-s", "--format=%P", commit)
        if parents != previous or git("ls-tree", "--name-only", commit) != "budget.json":
            raise ValueError("invalid translation budget history")
        if git("ls-tree", commit).split()[0] != "100644":
            raise ValueError("translation budget must be a regular file")
        data = json.loads(file_at(commit, "budget.json"))
        if set(data) != {"schema", "previous", "reserved_usd", "amount_usd", "run", "source"}:
            raise ValueError("invalid translation budget fields")
        amount = money(data["amount_usd"])
        if (
            data["schema"] != 1
            or data["previous"] != previous
            or money(data["reserved_usd"]) != total + amount
            or not SHA.fullmatch(data["source"])
            or not isinstance(data["run"], str)
            or (not previous and (amount != 0 or data["run"] != "init"))
            or (previous and amount == 0)
        ):
            raise ValueError("invalid translation budget reservation")
        total += amount
        previous = commit
    return total


def append_budget(previous: str | None, total: Decimal, amount: Decimal) -> str:
    data = {
        "schema": 1,
        "previous": previous or "",
        "reserved_usd": str(total + amount),
        "amount_usd": str(amount),
        "run": os.environ.get("GITHUB_RUN_ID", "operator") if previous else "init",
        "source": git("rev-parse", "HEAD"),
    }
    blob = git("hash-object", "-w", "--stdin", input_text=json.dumps(data, sort_keys=True) + "\n")
    tree = git("mktree", input_text=f"100644 blob {blob}\tbudget.json\n")
    parent = ["-p", previous] if previous else []
    commit = git(
        "commit-tree",
        tree,
        *parent,
        input_text=f"Reserve translation allowance\n\nSigned-off-by: {BOT_NAME} <{BOT_EMAIL}>\n",
    )
    git("push", "origin", f"{commit}:{BUDGET}", f"--force-with-lease={BUDGET}:{previous or ''}")
    return commit


def configure_git() -> None:
    git("config", "user.name", BOT_NAME)
    git("config", "user.email", BOT_EMAIL)


def reserve(total: Decimal, every: bool, codes: list[str] | None) -> float:
    """Burn a conservative request envelope before any provider request can start."""
    if total == 0:
        return 0.0
    tip = fetch(BUDGET)
    if tip is None:
        raise ValueError("i18n/budget is missing; initialize the cumulative ledger explicitly")
    used = budget_total(tip)
    remaining = total - used
    if remaining <= 0:
        return 0.0
    cap = 5.0
    for _ in range(24):
        amount = money(str(t.batch_reservation(every, codes, cap=cap)))
        if amount == 0:
            return 0.0
        if amount <= remaining:
            append_budget(tip, used, amount)
            return cap
        cap /= 2
    return 0.0


def approved_languages(codes: list[str] | None = None) -> list[str]:
    approved = os.environ.get("LOOM_I18N_LANGUAGES", "").split() or list(t.ROLLOUT_LANGUAGES)
    known = {lang.code for lang in t.LANGUAGES}
    if not set(approved) <= known:
        raise ValueError("LOOM_I18N_LANGUAGES contains an unknown language")
    if not set(approved) <= set(t.ROLLOUT_LANGUAGES):
        raise ValueError("configured language is outside the active translation rollout")
    selected = os.environ.get("LOOM_I18N_BATCH_LANGUAGES", "").split() or approved
    if not set(selected) <= set(approved):
        raise ValueError("batch language lacks repository approval")
    if codes is not None and not set(codes) <= set(selected):
        raise ValueError("requested language lacks repository approval")
    return sorted(set(selected if codes is None else codes))


def prepare() -> None:
    codes = approved_languages()
    tip = fetch(PENDING)
    if tip is not None:
        t.import_pending(tip, codes)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"pending_sha={tip or ''}\n")


def update(every: bool, codes: list[str] | None) -> int:
    codes = approved_languages(codes)
    configure_git()
    total = money(os.environ.get("LOOM_I18N_TOTAL_USD", "0"))
    cap = reserve(total, every, codes) if codes else 0.0
    previous = os.environ.get("LOOM_I18N_MAX_USD")
    os.environ["LOOM_I18N_MAX_USD"] = str(cap)
    try:
        return t.update(every and bool(codes), codes)
    finally:
        if previous is None:
            os.environ.pop("LOOM_I18N_MAX_USD", None)
        else:
            os.environ["LOOM_I18N_MAX_USD"] = previous


def generated_path(path: str, text: str, *, deleted: bool, readme_blob: str | None) -> bool:
    if path == refresh.PATH:
        try:
            refresh.parse(text)
        except (ValueError, TypeError):
            return False
        return True
    match = t.STAMP.match(text)
    checkpoint = path.startswith("i18n/.pending/")
    mapped = "i18n/" + path.removeprefix("i18n/.pending/") if checkpoint else path
    parts = PurePosixPath(mapped).parts
    if match is None or len(parts) < 3:
        return False
    source, blob, model = match.groups()
    language = next((item for item in t.LANGUAGES if item.code == parts[1]), None)
    if language is None or mapped != t.target_path(language.code, source):
        return False
    if not t.lifecycle.source_path(source) or model not in t.PRICES:
        return False
    if not text.startswith(t.header(source, language, model, blob)):
        return False
    # Hidden archives carry ownership, not permission to activate.
    if checkpoint and not deleted:
        return True
    return (
        (checkpoint or source not in t.sources())
        if deleted
        else (
            source in t.sources()
            and blob == (readme_blob if source == "README.md" else t.blob(source))
        )
    )


def readme_blobs(candidate: str) -> dict[str, str]:
    paths = ["README.md"] + [t.target_path(lang.code, "README.md") for lang in t.LANGUAGES]
    objects = {}
    for entry in git("ls-tree", "-z", candidate, "--", *paths).split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if mode in {"100644", "100755"} and kind == "blob":
            objects[path] = blob
    return objects


def validate_changes(base: str, candidate: str) -> None:
    """Only generated data may differ from the trusted main tree."""
    readmes = readme_blobs(candidate)
    fields = git("diff", "--name-status", "--no-renames", "-z", base, candidate).split("\0")[:-1]
    for status, path in zip(fields[::2], fields[1::2], strict=True):
        mode_ref = base if status == "D" else candidate
        mode = git("ls-tree", mode_ref, "--", path).split()[0]
        if mode != "100644":
            raise ValueError(f"translation publisher refuses nonregular file: {path}")
        text = file_at(mode_ref, path)
        if path == "README.md" and status == "M":
            before = file_at(base, path)
            match = t.LANGUAGE_LINE.search(before)
            expected = (
                before[: match.start()]
                + t.language_line(path, available=set(readmes))
                + before[match.end() :]
                if match
                else before
            )
            if text == expected:
                continue
        if not path.startswith("i18n/") or not generated_path(
            path, text, deleted=status == "D", readme_blob=readmes.get("README.md")
        ):
            raise ValueError(f"translation publisher refuses path: {path}")
    workflow = ".github/workflows/ci.yml"
    if git("rev-parse", f"{base}:{workflow}") != git("rev-parse", f"{candidate}:{workflow}"):
        raise ValueError("translation CI must use the trusted main workflow")


def publish() -> None:
    codes = approved_languages()
    configure_git()
    if t.check(skip_stale=True):
        raise ValueError("translation validation failed")
    base = git("rev-parse", "HEAD")
    previous = fetch(PENDING)
    expected = os.environ.get("LOOM_I18N_PENDING_SHA")
    if expected is not None and (previous or "") != expected:
        raise ValueError("pending translations changed after import; refusing to overwrite")
    git("add", "--", "README.md", "i18n")
    tree = git("write-tree")
    if tree == git("rev-parse", f"{base}^{{tree}}"):
        print("No generated translation changes to publish")
        return
    if (
        previous
        and git("rev-parse", f"{previous}^{{tree}}") == tree
        and git("show", "-s", "--format=%P", previous) == base
    ):
        candidate = previous
    else:
        candidate = git(
            "commit-tree",
            tree,
            "-p",
            base,
            input_text=(
                f"Translations follow {base[:9]}\n\nSigned-off-by: {BOT_NAME} <{BOT_EMAIL}>\n"
            ),
        )
    validate_changes(base, candidate)
    if not rollout_changes_allowed(base, candidate, codes):
        raise ValueError("translation publisher refuses changes outside the selected batch")
    if candidate != previous:
        git(
            "push",
            "origin",
            f"{candidate}:{PENDING}",
            f"--force-with-lease={PENDING}:{previous or ''}",
        )
    publish_pr(f"Translations follow {base[:9]}")
    command(
        "gh",
        "workflow",
        "run",
        "ci.yml",
        "--ref",
        "i18n/update",
        "--raw-field",
        f"expected_sha={candidate}",
    )


PR_BODY = """### Changelog category (leave one):

- Documentation (changelog entry is not required)

### Why

Machine translations follow the English source after its merge.

### Scope and risk

This generated-data batch preserves pending coverage and link dependencies. Missing
or stale pages remain queued for bounded batches across all five translation locales.
Checkpoints outside a requested batch stay intact. No executable source changes.

### Compatibility, rollout and rollback

- Wire and operator steps: none.
- Rollback: revert this batch; preserve the cumulative translation budget history.

### Tests

Present pages pass provenance, content and link validation. Hosted CI validates
this exact commit. Merge waits for successful checks and required reviews.

### AI assistance in the code (leave one; writing this description with AI is fine):

- AI-generated: the configured translator generated documentation. The producer
  validates provenance, content invariants and links before publication.

- [x] This batch contains generated documentation data only.

### Checklist

- [x] Generated pages pass the producer's validation.
- [ ] Hosted CI and required repository reviews pass before merge.
- [x] No wire, migration, plugin or frontend change.
- [x] Commits are signed off.
"""


def publish_pr(title: str) -> None:
    existing = json.loads(
        command(
            "gh",
            "pr",
            "list",
            "--head",
            "i18n/update",
            "--base",
            "main",
            "--state",
            "open",
            "--json",
            "number",
        )
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md") as body:
        body.write(PR_BODY)
        body.flush()
        if existing:
            args = ["edit", str(existing[0]["number"])]
        else:
            args = ["create", "--head", "i18n/update", "--base", "main"]
        command("gh", "pr", *args, "--title", title, "--body-file", body.name)


def api(path: str) -> Any:
    return json.loads(command("gh", "api", path))


def trusted_ci(run: dict[str, Any], repository: str, sha: str) -> bool:
    return (
        run.get("event") == "workflow_dispatch"
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("head_branch") == "i18n/update"
        and run.get("head_sha") == sha
        and (run.get("head_repository") or {}).get("full_name", "").casefold()
        == repository.casefold()
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def checks_succeeded(checks: list[dict[str, Any]]) -> bool:
    return bool(checks) and all(
        item.get("state") == "SUCCESS"
        or (item.get("status") == "COMPLETED" and item.get("conclusion") == "SUCCESS")
        for item in checks
    )


def sync_site(repository: str) -> None:
    """Retry a suppressed or failed deployment after a generated PR has merged."""
    owner = repository.split("/")[0]
    merged = api(
        f"repos/{repository}/pulls?state=closed&head={owner}:i18n/update&base=main&per_page=10"
    )
    merged = [item for item in merged if item.get("merged_at") and item.get("merge_commit_sha")]
    if not merged:
        return
    latest = max(merged, key=lambda item: item["merged_at"])
    merge_sha = latest["merge_commit_sha"]
    runs = api(f"repos/{repository}/actions/workflows/docs-site.yml/runs?branch=main&per_page=20")
    for run in runs["workflow_runs"]:
        if run["status"] == "completed" and run["conclusion"] != "success":
            continue
        if run["head_branch"] != "main" or not SHA.fullmatch(run["head_sha"]):
            continue
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", merge_sha, run["head_sha"]],
            cwd=t.ROOT,
            capture_output=True,
        )
        if ancestry.returncode == 0:
            return
    command("gh", "workflow", "run", "docs-site.yml", "--ref", "main")


def rollout_changes_allowed(base: str, candidate: str, codes: list[str]) -> bool:
    """Retain deferred data; new hidden copies may preserve an earlier pending batch."""
    changed = git("diff", "--name-status", "--no-renames", "-z", base, candidate).split("\0")[:-1]
    for status, path in zip(changed[::2], changed[1::2], strict=True):
        if not path.startswith("i18n/") or path == refresh.PATH:
            continue
        checkpoint = path.startswith("i18n/.pending/")
        published = t.lifecycle.publication_path(path)
        if published.split("/", 2)[1] not in codes and (not checkpoint or status != "A"):
            return False
    return True


def finalize() -> None:
    """Merge only an exact successful hosted CI head, with repository protections."""
    repository = os.environ["GITHUB_REPOSITORY"]
    sync_site(repository)
    owner = repository.split("/")[0]
    prs = api(f"repos/{repository}/pulls?state=open&head={owner}:i18n/update&base=main&per_page=2")
    if len(prs) != 1:
        return
    pr = prs[0]
    sha = pr["head"]["sha"]
    if pr["draft"] or pr["head"]["repo"]["full_name"].casefold() != repository.casefold():
        return
    runs = api(
        f"repos/{repository}/actions/workflows/ci.yml/runs?branch=i18n/update&event=workflow_dispatch&status=success&head_sha={sha}&per_page=10"
    )
    if not any(trusted_ci(run, repository, sha) for run in runs["workflow_runs"]):
        return
    view = json.loads(
        command(
            "gh",
            "pr",
            "view",
            str(pr["number"]),
            "--json",
            "reviewDecision,statusCheckRollup,mergeStateStatus",
        )
    )
    if (
        view["reviewDecision"] in {"REVIEW_REQUIRED", "CHANGES_REQUESTED"}
        or view["mergeStateStatus"] != "CLEAN"
        or not checks_succeeded(view["statusCheckRollup"])
    ):
        print("Translation merge waits for successful checks and required reviews")
        return
    candidate = fetch(PENDING)
    base = git("rev-parse", "HEAD")
    if candidate != sha or git("show", "-s", "--format=%P", sha) != base:
        print("Translation merge waits for a batch based on current main")
        return
    validate_changes(base, sha)
    approved = approved_languages()
    if not rollout_changes_allowed(base, sha, approved):
        print("Translation merge waits for the producer to apply current language approval")
        return
    # Only validated generated data enters the trusted main checkout.
    git("restore", "--source", sha, "--worktree", "--", "README.md", "i18n")
    if t.check(skip_stale=True):
        raise ValueError("translation validation failed before merge")
    command("mkdocs", "build", "--strict")
    command("gh", "pr", "merge", str(pr["number"]), "--squash", "--match-head-commit", sha)
    merged = api(f"repos/{repository}/pulls/{pr['number']}")
    if not merged["merged"] or merged["head"]["sha"] != sha:
        raise ValueError("translation merge readback does not match validated head")
    command("gh", "workflow", "run", "docs-site.yml", "--ref", "main")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("prepare", "update", "publish", "finalize", "init-budget")
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--lang", action="append", choices=[item.code for item in t.LANGUAGES])
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare()
    elif args.command == "update":
        return update(args.all, args.lang)
    elif args.command == "publish":
        publish()
    elif args.command == "finalize":
        finalize()
    else:
        configure_git()
        if fetch(BUDGET) is not None:
            raise ValueError("translation budget already exists")
        append_budget(None, Decimal(0), Decimal(0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
