#!/usr/bin/env python3
"""The release pull request's changes (every version file, the chart, the notes)
and the version checks the release workflow runs.

Run with the project's environment (the notes import the server's registry):

    .venv/bin/python scripts/release/prepare.py bump 1.2.0       # notes, then the bump
    .venv/bin/python scripts/release/prepare.py notes 1.2.0      # the notes alone
    .venv/bin/python scripts/release/prepare.py check            # one version everywhere
    .venv/bin/python scripts/release/prepare.py check-chart 1.2.0       # the chart moved
    .venv/bin/python scripts/release/prepare.py check-tag 1.2.0-rc.1    # candidate order

What each file carries, the chart rule, the release candidates and the notes:
docs/development/release-process.md.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
NOTES_FILE = ROOT / "release-notes.md"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?")
FINAL = re.compile(r"\d+\.\d+\.\d+")
CANDIDATE = re.compile(r"\d+\.\d+\.\d+-rc\.\d+")
REPOSITORY = "ClickHouse/loom"


class Pin(NamedTuple):
    """One place a file carries the version: `pattern`'s group `v`, found exactly once."""

    path: str
    pattern: str


_TOP_JSON = r'^  "version": "(?P<v>[^"]+)",$'
# npm's lockfile names the package's own version twice: at the top and as packages[""].
_LOCK_ROOT = r'"packages": \{\n    "": \{\n      "name": "[^"]+",\n      "version": "(?P<v>[^"]+)"'
_PROJECT = r'^version = "(?P<v>[^"]+)"$'

# The version every other pin follows.
SERVER = Pin("server/__init__.py", r'^__version__ = "(?P<v>[^"]+)"$')
FOLLOWERS = [
    Pin("pyproject.toml", _PROJECT),
    Pin("uv.lock", r'^name = "clickhouse-loom-server"\nversion = "(?P<v>[^"]+)"$'),
    Pin("server.json", _TOP_JSON),
    Pin("sdk/python/pyproject.toml", _PROJECT),
    Pin("sdk/python/loom/_version.py", r'^__version__ = "(?P<v>[^"]+)"$'),
    Pin("sdk/typescript/package.json", _TOP_JSON),
    Pin("sdk/typescript/package-lock.json", _TOP_JSON),
    Pin("sdk/typescript/package-lock.json", _LOCK_ROOT),
    Pin("sdk/typescript/src/version.ts", r'^export const SDK_VERSION = "(?P<v>[^"]+)";$'),
    Pin("frontend/package.json", _TOP_JSON),
    Pin("frontend/package-lock.json", _TOP_JSON),
    Pin("frontend/package-lock.json", _LOCK_ROOT),
    Pin(".claude-plugin/plugin.json", _TOP_JSON),
    Pin("skills/loom-code/SKILL.md", r'^  version: "(?P<v>[^"]+)"$'),
    Pin("skills/loom/SKILL.md", r'^  version: "(?P<v>[^"]+)"$'),
    Pin("deploy/helm/loom/Chart.yaml", r'^appVersion: "(?P<v>[^"]+)"$'),
]
PINS = [SERVER, *FOLLOWERS]
CHART = Pin("deploy/helm/loom/Chart.yaml", r"^version: (?P<v>\S+)$")


def _match(pin: Pin, text: str) -> re.Match[str]:
    found = list(re.finditer(pin.pattern, text, re.MULTILINE))
    if len(found) != 1:
        raise ValueError(f"{pin.path}: {pin.pattern!r} matches {len(found)} times, not once")
    return found[0]


def _text(path: str, root: Path) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{path}: missing") from None


def read(pin: Pin, root: Path = ROOT) -> str:
    return _match(pin, _text(pin.path, root)).group("v")


def parse(version: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"{version!r} is not a SemVer X.Y.Z[-pre] version")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def next_chart_version(version: str, previous_version: str, previous_chart: str) -> str:
    """The chart after `previous_chart`, which `previous_version` shipped: a patch
    release moves it a patch, any other a minor."""
    major, minor, patch = parse(previous_chart)
    if parse(version)[:2] == parse(previous_version)[:2]:
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def check(root: Path = ROOT) -> list[tuple[str, str]]:
    """(file, error) for every pin that does not carry the server's version."""
    try:
        want = read(SERVER, root)
    except ValueError as e:
        return [(SERVER.path, str(e))]
    errors = []
    for pin in FOLLOWERS:
        try:
            got = read(pin, root)
        except ValueError as e:
            errors.append((pin.path, str(e)))
            continue
        if got != want:
            errors.append((pin.path, f"{pin.path} names {got}, server/__init__.py {want}"))
    return errors


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _sha(root: Path, ref: str) -> str:
    """The commit `ref` names, or "" when there is none; a git that cannot read
    the repository raises."""
    found = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode not in (0, 1):  # 1 is "no such ref"
        found.check_returncode()
    return found.stdout.strip()


def final_of(tag_version: str) -> str:
    """X.Y.Z of a tag version, its own or its candidate's."""
    return tag_version.split("-", 1)[0]


def chart_baseline(tag_version: str, root: Path = ROOT) -> str | None:
    """The release before `tag_version`'s, or None when `vX.Y.Z` is on another
    commit (a dry run of a branch still naming a shipped release)."""
    version = final_of(tag_version)
    if _sha(root, f"v{version}") and not tag_on_head(version, root):
        return None
    return previous_release(version, root)


def previous_release(version: str, root: Path = ROOT) -> str:
    """The highest final v* tag below `version`."""
    listed = _git(root, "tag", "--list", "v*")
    listed.check_returncode()
    finals = [t for t in listed.stdout.split() if FINAL.fullmatch(t[1:])]
    older = [t for t in finals if parse(t[1:]) < parse(version)]
    if not older:
        raise ValueError(f"no final release tag below {version} (are the tags fetched?)")
    return max(older, key=lambda t: parse(t[1:]))


def check_chart(baseline: str, root: Path = ROOT) -> str | None:
    """An error when the chart's version is not above the one `baseline` shipped:
    every release moves the chart's `appVersion`, so every release changes it."""
    new = read(CHART, root)
    old = released_chart(baseline, root)
    if parse(new) <= parse(old):
        return f"the chart's version is {new}, not above the {old} that {baseline} shipped"
    return None


def released_chart(tag: str, root: Path = ROOT) -> str:
    """The chart's own version at a release tag."""
    shown = _git(root, "show", f"{tag}:{CHART.path}")
    shown.check_returncode()
    try:
        version = _match(CHART, shown.stdout).group("v")
        parse(version)
    except ValueError as e:
        raise ValueError(f"{e} (at {tag})") from None
    return version


def require_final(version: str) -> None:
    """A version file names a final X.Y.Z; a candidate is a tag on the release commit."""
    if FINAL.fullmatch(version):
        return
    if SEMVER.fullmatch(version):
        raise ValueError(f"{version} is not a final X.Y.Z: a candidate is a tag, not a bump")
    raise ValueError(f"{version!r} is not X.Y.Z")


def tag_on_head(tag_version: str, root: Path = ROOT) -> bool:
    """`v<tag_version>` is the checked-out commit: the tag's own run, not a dry
    run or a branch run."""
    own = _sha(root, f"v{tag_version}")
    return bool(own) and own == _sha(root, "HEAD")


def check_tag(tag_version: str, root: Path = ROOT) -> str | None:
    """An error for a tag that breaks the candidate order: `vX.Y.Z-rc.N` once
    `vX.Y.Z` is out, or a final `vX.Y.Z` on another commit than its last
    candidate."""
    final = final_of(tag_version)
    if tag_version != final:  # a candidate: the version job takes no other prerelease
        released = _sha(root, f"v{final}")
        if released == _sha(root, "HEAD"):
            # No deletion advice: the final's runs may read this tag as the one soaked.
            return (
                f"v{tag_version} is on v{final}'s commit, which is released: run v{final} instead"
            )
        if released:
            return (
                f"v{final} is already tagged: a candidate for it would publish after it;"
                f" delete this tag (git push origin :refs/tags/v{tag_version}), or, when"
                f" v{final}'s run published nothing, delete v{final} instead"
            )
        return None
    tags = _git(root, "tag", "--list", f"v{final}-rc.*")
    tags.check_returncode()  # an empty list would pass the final
    listed = tags.stdout.split()
    candidates = sorted(
        (t for t in listed if CANDIDATE.fullmatch(t[1:])),
        key=lambda t: int(t.rsplit(".", 1)[1]),
    )
    if candidates and _sha(root, candidates[-1]) != _sha(root, "HEAD"):
        return (
            f"v{final} is not on {candidates[-1]}'s commit: tag the final there,"
            f" or delete {candidates[-1]} if it is a stray"
        )
    return None


def validate(version: str, root: Path = ROOT) -> None:
    """Refuse a final version the files cannot take, before any `gh` call or
    write: every pin is found once, and the version is not below the current one."""
    for pin in [*FOLLOWERS, CHART]:
        read(pin, root)
    current = read(SERVER, root)
    if parse(version) < parse(current):
        raise ValueError(f"{version} is below the current version {current}")


def refuse_tagged(version: str, root: Path = ROOT) -> None:
    """A bump for a version already tagged would rename a shipped release. The
    notes alone may be made again for it."""
    if _sha(root, f"v{version}"):
        raise ValueError(f"v{version} is already tagged: prepare the next version")


def planned_chart(
    version: str, previous_version: str, previous_chart: str, root: Path = ROOT
) -> str:
    """One step above the chart `previous_version` shipped, or the current chart
    when it was raised higher since; so a second bump moves it once."""
    chart = next_chart_version(version, previous_version, previous_chart)
    return max(chart, read(CHART, root), key=parse)


def bump(version: str, chart: str, root: Path = ROOT) -> None:
    """Every pin to `version` and the chart's own version to `chart`. Every file
    is edited in memory first, so a pin not found writes nothing."""
    texts: dict[str, str] = {}
    for pin, value in [(CHART, chart), *((pin, version) for pin in PINS)]:
        text = texts[pin.path] if pin.path in texts else _text(pin.path, root)
        start, end = _match(pin, text).span("v")
        texts[pin.path] = text[:start] + value + text[end:]
    for path, text in texts.items():
        (root / path).write_text(text, encoding="utf-8")


# The notes.

NO_ENTRY = "(changelog entry is not required)"
UNCLASSIFIED = "Unclassified"
# A whole "Operator steps:" text that says there are none; a step that only begins
# with "No" ("No downtime, but run X first") is a step.
_NO_STEP = re.compile(
    r"(none|nothing|no|n/?a)( operator)?( steps?)?( needed| required| to do)?\.?", re.I
)


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^### {re.escape(heading)}[^\n]*\n(.*?)(?=^### |\Z)", body, re.S | re.M)
    return match.group(1).strip() if match else ""


@functools.cache
def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _bare(category: str) -> str:
    """A category without the template's guidance in parentheses, which is for
    contributors and which they often leave out."""
    return re.sub(r"\s*\(.*\)$", "", category)


@functools.cache
def categories() -> tuple[str, ...]:
    """The changelog categories, in the template's order."""
    return tuple(
        line[2:].strip()
        for line in _section(_template(), "Changelog category").splitlines()
        if line.startswith("- ")
    )


# "- Operator steps:" as the template has it, bolded or not.
_OPERATOR = re.compile(r"^[-*] +(?:\*\*)?operator steps\b[^:\n]*:(?:\*\*)?(.*)$", re.I | re.M)


def _operator_item(body: str) -> str:
    """The operator-steps item as written, up to the section's next item."""
    section = _section(body, "Compatibility, rollout and rollback")
    match = _OPERATOR.search(section)
    if not match:
        return ""
    rest = section[match.end() :]
    next_item = re.search(r"^[-*] ", rest, re.M)
    # The lines under it keep their indent, so a list given alone stays nested.
    return match.group(1).strip() + rest[: next_item.start() if next_item else None].rstrip()


def _operator_steps(body: str) -> str:
    """The operator-steps item, or "" when it says there are none or is the
    template's own text left in."""
    text = _operator_item(body)
    bare = text.strip("`\"'* \n")
    if not bare or _NO_STEP.fullmatch(bare) or text == _operator_item(_template()):
        return ""
    return text


def changelog(body: str) -> tuple[str, str, str]:
    """(category, entry, operator steps) from a pull request body that follows
    .github/PULL_REQUEST_TEMPLATE.md, UNCLASSIFIED as the category when the body
    names no single template category. A category that needs no entry still
    carries its operator steps."""
    body = re.sub(r"<!--.*?-->", "", body.replace("\r\n", "\n"), flags=re.S)
    named = [
        line[2:].strip()
        for line in _section(body, "Changelog category").splitlines()
        if line.startswith("- ")
    ]
    by_name = {_bare(c): c for c in categories()}
    category = by_name.get(_bare(named[0]), UNCLASSIFIED) if len(named) == 1 else UNCLASSIFIED
    entry = " ".join(_section(body, "Changelog entry").split())
    return category, "" if entry in ("", "...") else entry, _operator_steps(body)


def squash_merges(previous_tag: str, root: Path = ROOT) -> dict[int, int]:
    """Pull request number to squash-merge commit time, oldest first, for the
    merges since `previous_tag`; the release pull request's own merge (the one
    that moves `__version__`) is left out."""
    log = _git(root, "log", "--reverse", "--format=%H %ct %s", f"{previous_tag}..HEAD")
    log.check_returncode()
    bumps = _git(
        root,
        "log",
        "--format=%H",
        "-G",
        "^__version__ = ",
        f"{previous_tag}..HEAD",
        "--",
        SERVER.path,
    )
    bumps.check_returncode()
    releases = set(bumps.stdout.split())
    merged = {}
    for line in log.stdout.splitlines():
        sha, committed, subject = line.split(" ", 2)
        if sha not in releases and (m := re.search(r"\(#(\d+)\)$", subject)):
            merged[int(m.group(1))] = int(committed)
    return merged


def notes(version: str, previous: str, pulls: list[dict[str, Any]], wire: str) -> str:
    """The release notes section of the release pull request's description."""
    grouped: dict[str, list[str]] = {}
    steps: list[str] = []
    for pull in pulls:
        category, entry, operator = changelog(pull["body"] or "")
        # A pull request that needs no entry can still have a deploy step.
        if operator:
            first, *rest = operator.splitlines()
            steps.append(f"- #{pull['number']}: {first}".rstrip())
            steps += [f"  {line}" if line else "" for line in rest]
        if NO_ENTRY in category:
            continue
        line = f"- {entry or pull['title']} (#{pull['number']})"
        if category == UNCLASSIFIED:
            line += ": its description names no single changelog category"
        grouped.setdefault(category, []).append(line)
    out = [f"## Release notes for v{version}", ""]
    for category in [*categories(), UNCLASSIFIED]:
        if category in grouped:
            out += [f"### {_bare(category)}", "", *grouped[category], ""]
    out += ["### Operator steps", "", *(steps or ["None."]), ""]
    # wire_changelog.py's heading names the two revisions; this one names the release,
    # and its own sections sit one level under it.
    body = wire.strip().split("\n", 1)[1].strip()
    body = re.sub(r"^(#+) ", r"#\1 ", body, flags=re.M)
    out += [f"### What changed for clients since {previous}", "", body, ""]
    return "\n".join(out)


def pull_requests(merged: dict[int, int]) -> list[dict[str, Any]]:
    """The merged pull requests' number, title and body, in `merged`'s order,
    from one `gh pr list` call."""
    if not merged:
        return []
    # gh's `merged:` takes a date: a day of slack before the earliest merge.
    since = datetime.fromtimestamp(min(merged.values()), UTC) - timedelta(days=1)
    listed = subprocess.run(
        ["gh", "pr", "list", "-R", REPOSITORY, "--state", "merged",
         "--search", f"merged:>={since.date().isoformat()}", "--limit", "1000",
         "--json", "number,title,body"],
        check=True,
        stdout=subprocess.PIPE,  # its error reaches the terminal
        text=True,
    ).stdout  # fmt: skip
    by_number = {pull["number"]: pull for pull in json.loads(listed)}
    missing = ", ".join(f"#{n}" for n in merged if n not in by_number)
    if missing:
        raise ValueError(f"gh pr list did not return the merged pull requests {missing}")
    return [by_number[n] for n in merged]


def release_notes(version: str, previous: str, root: Path = ROOT) -> str:
    pulls = pull_requests(squash_merges(previous, root))
    wire = subprocess.run(
        [sys.executable, "scripts/codegen/wire_changelog.py", previous, "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    return notes(version, previous, pulls, wire)


def _check(args: argparse.Namespace) -> None:
    errors = check()
    for path, error in errors:
        print(f"::error file={path}::{error}")
    if errors:
        raise SystemExit(1)


def _check_tag(args: argparse.Namespace) -> None:
    tag_version = args.tag_version
    if not tag_on_head(tag_version):
        print(f"::notice::no candidate-order check: v{tag_version} is not on this commit")
        return
    tag_error = check_tag(tag_version)
    if tag_error:
        raise SystemExit(f"::error::{tag_error}")


def _check_chart(args: argparse.Namespace) -> None:
    tag_version = args.tag_version
    baseline = chart_baseline(tag_version)
    if not baseline:
        # Said in the log, so a skip never reads as a chart checked.
        print(f"::notice::no chart check: v{final_of(tag_version)} is tagged on another commit")
        return
    # Caught here, not in main(), for the annotation's file= and the baseline's name.
    try:
        chart_error = check_chart(baseline)
    except subprocess.CalledProcessError as e:
        chart_error = f"could not read the chart at {baseline}: {e.stderr.strip()}"
    except ValueError as e:  # a chart version missing or not X.Y.Z
        chart_error = str(e)
    if chart_error:
        raise SystemExit(f"::error file={CHART.path}::{chart_error}")


def _fetch_tags_and_validate(version: str) -> None:
    require_final(version)  # needs no tags, so before the fetch
    # Forced, so a tag moved upstream replaces a stale local one.
    _git(
        ROOT, "fetch", "--quiet", "--force", "--tags", f"https://github.com/{REPOSITORY}.git"
    ).check_returncode()
    validate(version)


def _notes(args: argparse.Namespace) -> None:
    version = args.version
    _fetch_tags_and_validate(version)
    print(release_notes(version, previous_release(version)))


def _bump(args: argparse.Namespace) -> None:
    version = args.version
    _fetch_tags_and_validate(version)
    refuse_tagged(version)
    previous = previous_release(version)
    # Every refusal before the notes' gh call.
    released = released_chart(previous)  # names the tag when its chart is bad
    try:
        chart = planned_chart(version, previous[1:], released)
    except ValueError as e:  # the working tree's chart version is not X.Y.Z
        raise ValueError(f"{CHART.path}: {e}") from None
    NOTES_FILE.write_text(release_notes(version, previous), encoding="utf-8")
    bump(version, chart)
    print(f"Bumped to {version}; the release notes are in {NOTES_FILE}")


def _one_line(e: Exception) -> str:
    if isinstance(e, subprocess.CalledProcessError):
        detail = f": {e.stderr.strip()}" if e.stderr else ""
        return f"{' '.join(map(str, e.cmd))} failed{detail}"
    return str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    bump = commands.add_parser("bump", help="write the release notes, then bump")
    bump.add_argument("version", help="the release, X.Y.Z")
    bump.set_defaults(run=_bump)
    notes = commands.add_parser("notes", help="only print the release notes")
    notes.add_argument("version", help="the release, X.Y.Z")
    notes.set_defaults(run=_notes)
    commands.add_parser("check", help="every file names one version").set_defaults(run=_check)
    chart = commands.add_parser(
        "check-chart", help="the chart's version is above the previous release's"
    )
    chart.add_argument("tag_version", help="the tag's version, X.Y.Z or X.Y.Z-rc.N")
    chart.set_defaults(run=_check_chart)
    tag = commands.add_parser(
        "check-tag", help="a candidate before its final, the final on its last candidate's commit"
    )
    tag.add_argument("tag_version", help="the tag's version, X.Y.Z or X.Y.Z-rc.N")
    tag.set_defaults(run=_check_tag)
    args = parser.parse_args()
    try:
        args.run(args)
    # A refusal raises ValueError and a failed git or gh call CalledProcessError;
    # either is one line, and in a check, which the release workflow runs, an
    # `::error::` annotation.
    except (ValueError, subprocess.CalledProcessError) as e:
        prefix = "::error::" if args.command.startswith("check") else ""
        raise SystemExit(prefix + _one_line(e)) from None


if __name__ == "__main__":
    main()
