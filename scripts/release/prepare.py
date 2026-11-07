#!/usr/bin/env python3
"""The release pull request's changes: every version file, the chart, the notes.

Run with the project's environment (the notes import the server's registry):

    .venv/bin/python scripts/release/prepare.py 1.2.0        # notes, then the bump
    .venv/bin/python scripts/release/prepare.py --check      # one version everywhere
    .venv/bin/python scripts/release/prepare.py --check-chart 1.2.0  # the chart moved
    .venv/bin/python scripts/release/prepare.py --notes 1.2.0  # the notes alone
    .venv/bin/python scripts/release/prepare.py --check-tag 1.2.0-rc.1  # candidate order

One version names the server, the images, the web app, both SDKs, the root
skills and the chart's `appVersion` (docs/development/release-process.md). The
files carry final versions only: a release candidate is a `vX.Y.Z-rc.N` tag on
the commit the final tag goes on later. The chart's own `version` moves with
every release, since its `appVersion` does: a minor for a major or minor
release, a patch for a patch release. The coding-agent plugins keep their own
version and are left alone.

The notes are the changelog entries of the pull requests merged since the
previous release, in the template's category order, their operator steps, the
pull requests whose description names no single category, and the wire
changelog (scripts/codegen/wire_changelog.py), for the release pull request's
description.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
NOTES_FILE = ROOT / "release-notes.md"
TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?")
FINAL = re.compile(r"\d+\.\d+\.\d+")
CANDIDATE = re.compile(r"\d+\.\d+\.\d+-rc\.\d+")
REPOSITORY = "ClickHouse/loom"


@dataclass(frozen=True)
class Pin:
    """One place a file carries the version: `pattern`'s group `v`, found exactly once."""

    path: str
    pattern: str

    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.MULTILINE)


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
CHART_DIR = "deploy/helm/loom"


def _match(pin: Pin, text: str) -> re.Match[str]:
    found = list(pin.regex().finditer(text))
    if len(found) != 1:
        raise ValueError(f"{pin.path}: {pin.pattern!r} matches {len(found)} times, not once")
    return found[0]


def read(pin: Pin, root: Path = ROOT) -> str:
    try:
        text = (root / pin.path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{pin.path}: missing") from None
    return _match(pin, text).group("v")


def write(pin: Pin, version: str, root: Path = ROOT) -> None:
    path = root / pin.path
    text = path.read_text(encoding="utf-8")
    start, end = _match(pin, text).span("v")
    path.write_text(text[:start] + version + text[end:], encoding="utf-8")


def parse(version: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"{version!r} is not a SemVer X.Y.Z[-pre] version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def next_chart_version(version: str, previous_version: str, previous_chart: str) -> str:
    """The chart after `previous_chart`, which `previous_version` shipped: a patch
    release moves it a patch, any other a minor."""
    major, minor, patch = parse(previous_chart)
    if parse(version)[:2] == parse(previous_version)[:2]:
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def check(root: Path = ROOT) -> list[str]:
    """Every pin that does not carry the server's version."""
    want = read(SERVER, root)
    errors = []
    for pin in FOLLOWERS:
        try:
            got = read(pin, root)
        except ValueError as e:
            errors.append(str(e))
            continue
        if got != want:
            errors.append(f"{pin.path} names {got}, server/__init__.py {want}")
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
    if tag_version != final:
        if not CANDIDATE.fullmatch(tag_version):
            return f"v{tag_version} is not a candidate: a candidate is vX.Y.Z-rc.N"
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
    """Refuse a version the files cannot take, before any `gh` call or write."""
    require_final(version)
    read_every_pin(root)
    current = read(SERVER, root)
    if parse(version) < parse(current):
        raise ValueError(f"{version} is below the current version {current}")


def refuse_tagged(version: str, root: Path = ROOT) -> None:
    """A bump for a version already tagged would rename a shipped release. The
    notes alone may be made again for it."""
    if _sha(root, f"v{version}"):
        raise ValueError(f"v{version} is already tagged: prepare the next version")


def read_every_pin(root: Path = ROOT) -> None:
    """Fail unless every pin is found once, so a bump writes all of them or none."""
    for pin in [*PINS, CHART]:
        read(pin, root)


def planned_chart(
    version: str, previous_version: str, previous_chart: str, root: Path = ROOT
) -> str:
    """One step above the chart `previous_version` shipped, or the current chart
    when it was raised higher since; so a second bump moves it once."""
    chart = next_chart_version(version, previous_version, previous_chart)
    return max(chart, read(CHART, root), key=parse)


def bump(version: str, chart: str, root: Path = ROOT) -> None:
    """Every pin to `version` and the chart's own version to `chart`. Every pin is
    read before the first write, so a bump writes all of them or none."""
    read_every_pin(root)
    write(CHART, chart, root)
    for pin in PINS:
        write(pin, version, root)


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


# "- Operator steps:" as the template has it, or bolded, starred or with a note
# before the colon.
_OPERATOR = re.compile(r"[-*]\s+(?:\*\*)?operator steps\b[^:]*:(?:\*\*)?\s*(.*)", re.I)
UNREAD_STEPS = "its description mentions operator steps that could not be read: check them"


def _operator_steps(body: str) -> list[str]:
    """The operator-steps item as written, none when it says none, or
    UNREAD_STEPS when it cannot be read."""
    section = _section(body, "Compatibility, rollout and rollback")
    text = _operator_text(section.splitlines())
    if text is None:
        return [UNREAD_STEPS] if "operator step" in section.lower() else []
    if not text:
        return [UNREAD_STEPS]
    if text == _placeholder_steps() or _NO_STEP.fullmatch(text.strip("`\"'* ")):
        return []
    return [text]


@functools.cache
def _placeholder_steps() -> str | None:
    """The template's own operator-steps text."""
    return _operator_text(_section(_template(), "Compatibility, rollout and rollback").splitlines())


def _operator_text(lines: list[str]) -> str | None:
    """The operator-steps item's text, None when the lines have no such item."""
    for i, line in enumerate(lines):
        match = _OPERATOR.fullmatch(line.strip())
        if match and not line.startswith((" ", "\t")):
            block: list[str] = []
            for more in lines[i + 1 :]:
                if more.startswith(("- ", "* ")):  # the section's next item
                    break
                block.append(more)
            # The lines under it lose their common indent only, so deeper lists
            # and code keep their shape.
            body = textwrap.dedent("\n".join(block))
            return "\n".join([match.group(1).strip(), body]).strip()
    return None


def changelog(body: str) -> tuple[str, str, list[str]]:
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


def merged_pull_requests(previous_tag: str, root: Path = ROOT) -> list[int]:
    """The squash merges since the previous release, oldest first, but a release
    pull request's own (the one that moves the server's version)."""
    log = _git(root, "log", "--reverse", "--format=%H %s", f"{previous_tag}..HEAD")
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
    merged = []
    for line in log.stdout.splitlines():
        sha, subject = line.split(" ", 1)
        if sha not in releases and (m := re.search(r"\(#(\d+)\)$", subject)):
            merged.append(int(m.group(1)))
    return merged


def notes(version: str, previous: str, pulls: list[dict[str, Any]], wire: str) -> str:
    """The release notes section of the release pull request's description."""
    grouped: dict[str, list[str]] = {}
    steps: list[str] = []
    for pull in pulls:
        category, entry, operator = changelog(pull["body"] or "")
        # A pull request that needs no entry can still have a deploy step.
        for step in operator:
            first, *rest = step.splitlines()
            if re.match(r"([-*]|\d+[.)]) ", first):  # a list alone: nest all of it
                first, rest = "", step.splitlines()
            head = f"- {first} (#{pull['number']})" if first else f"- (#{pull['number']})"
            steps += [
                head,
                *(f"  {line}" if line else "" for line in rest),
            ]
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


def release_notes(version: str, previous: str, root: Path = ROOT) -> str:
    pulls = []
    for number in merged_pull_requests(previous, root):
        out = subprocess.run(
            ["gh", "pr", "view", str(number), "-R", REPOSITORY, "--json", "number,title,body"],
            check=True,
            stdout=subprocess.PIPE,  # its error reaches the terminal
            text=True,
        ).stdout
        pulls.append(json.loads(out))
    wire = subprocess.run(
        [sys.executable, "scripts/codegen/wire_changelog.py", previous, "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    return notes(version, previous, pulls, wire)


def _run_check() -> None:
    errors = check()
    for error in errors:
        print(f"::error::{error}")
    if errors:
        raise SystemExit(1)


def _run_check_tag(tag_version: str) -> None:
    if not tag_on_head(tag_version):
        print(f"::notice::no candidate-order check: v{tag_version} is not on this commit")
        return
    tag_error = check_tag(tag_version)
    if tag_error:
        raise SystemExit(f"::error::{tag_error}")


def _run_check_chart(tag_version: str) -> None:
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


# Below, a refusal raises ValueError and a failed git or gh call
# CalledProcessError; main() prints either as one line.


def _fetch_tags_and_validate(version: str) -> None:
    require_final(version)  # needs no tags, so before the fetch
    # Forced, so a tag moved upstream replaces a stale local one.
    _git(
        ROOT, "fetch", "--quiet", "--force", "--tags", f"https://github.com/{REPOSITORY}.git"
    ).check_returncode()
    validate(version)


def _notes(version: str) -> None:
    _fetch_tags_and_validate(version)
    print(release_notes(version, previous_release(version)))


def _prepare(version: str) -> None:
    _fetch_tags_and_validate(version)
    refuse_tagged(version)
    previous = previous_release(version)
    # Every refusal before the notes' gh calls, which take minutes.
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
    parser.add_argument("version", nargs="?", help="the release, X.Y.Z")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="every file names one version")
    modes.add_argument(
        "--check-chart",
        metavar="TAG_VERSION",
        help="the chart's version is above the previous release's",
    )
    modes.add_argument("--notes", action="store_true", help="only print the release notes")
    modes.add_argument(
        "--check-tag",
        metavar="TAG_VERSION",
        help="a candidate before its final, the final on its last candidate's commit",
    )
    args = parser.parse_args()
    check_mode = bool(args.check or args.check_tag or args.check_chart)
    if args.version and check_mode:
        parser.error("a check takes no release version of its own")
    if not (check_mode or args.version):
        parser.error(
            "name the release (X.Y.Z) to prepare or --notes for,"
            " or --check / --check-chart / --check-tag"
        )
    try:
        if args.check:
            _run_check()
        elif args.check_tag:
            _run_check_tag(args.check_tag)
        elif args.check_chart:
            _run_check_chart(args.check_chart)
        elif args.notes:
            _notes(args.version)
        else:
            _prepare(args.version)
    except (ValueError, subprocess.CalledProcessError) as e:
        # The checks run in the release workflow, which reads `::error::` as an annotation.
        raise SystemExit(("::error::" if check_mode else "") + _one_line(e)) from None


if __name__ == "__main__":
    main()
