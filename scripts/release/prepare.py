#!/usr/bin/env python3
"""The release pull request's changes (every version file, the chart, the notes)
and the version checks the release workflow runs.

`bump` and `notes` run with the project's environment (the notes import the
server's registry); the other commands, which the release workflow runs on the
python3 without the project's environment, need only the standard library:

    .venv/bin/python scripts/release/prepare.py bump 1.2.0   # notes, then the bump
    .venv/bin/python scripts/release/prepare.py notes 1.2.0  # the notes alone
    python3 scripts/release/prepare.py check 1.2.0           # one version everywhere
    python3 scripts/release/prepare.py check-tag 1.2.0-rc.1  # candidate order
    python3 scripts/release/prepare.py check-chart 1.2.0     # the chart moved
    python3 scripts/release/prepare.py previous 1.2.0-rc.1   # the notes' base tag
    python3 scripts/release/prepare.py candidates-on-head 1.2.0  # its candidates on this commit

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


def parse(version: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError(f"{version!r} is not a SemVer X.Y.Z[-pre] version")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def final_of(tag_version: str) -> str:
    """X.Y.Z of a tag version, its own or its candidate's."""
    return tag_version.split("-", 1)[0]


class PinError(ValueError):
    """A version file that cannot be read. The path and the reason are kept apart
    so an `::error file=` annotation names the file once."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _match(pin: Pin, text: str) -> re.Match[str]:
    found = list(re.finditer(pin.pattern, text, re.MULTILINE))
    if len(found) != 1:
        raise PinError(pin.path, f"{pin.pattern!r} matches {len(found)} times, not once")
    return found[0]


def _text(path: str, root: Path) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PinError(path, "missing") from None


def read(pin: Pin, root: Path = ROOT) -> str:
    return _match(pin, _text(pin.path, root)).group("v")


def check(tag_version: str | None = None, root: Path = ROOT) -> list[tuple[str, str]]:
    """(file, error) for every pin that does not carry the server's version, and
    for the server's version when it is not the final `tag_version` names."""
    try:
        want = read(SERVER, root)
    except PinError as e:
        return [(e.path, e.reason)]
    errors = []
    final = final_of(tag_version) if tag_version else want
    if want != final:
        errors.append((SERVER.path, f"names {want}, not {final} (the tag v{tag_version})"))
    for pin in FOLLOWERS:
        try:
            got = read(pin, root)
        except PinError as e:
            errors.append((e.path, e.reason))
            continue
        if got != want:
            errors.append((pin.path, f"names {got}, not {want} ({SERVER.path})"))
    return errors


def validate(version: str, root: Path = ROOT) -> None:
    """Refuse a final version the files cannot take, before any `gh` call or
    write: every pin is found once, the chart's own version parses, and the version
    is not below the current one."""
    for pin in FOLLOWERS:
        read(pin, root)
    chart = read(CHART, root)
    try:
        parse(chart)
    except ValueError as e:  # the next chart version is planned from it
        raise PinError(CHART.path, str(e)) from None
    current = read(SERVER, root)
    if parse(version) < parse(current):
        raise ValueError(f"{version} is below the current version {current}")


def bump(version: str, chart: str, root: Path = ROOT) -> None:
    """Every pin to `version` and the chart's own version to `chart`. Every file
    is edited in memory first, so a pin not found writes nothing."""
    texts: dict[str, str] = {}
    edits = [(CHART, chart)] + [(p, version) for p in PINS]
    for pin, value in edits:
        if pin.path not in texts:
            texts[pin.path] = _text(pin.path, root)
        text = texts[pin.path]
        start, end = _match(pin, text).span("v")
        texts[pin.path] = text[:start] + value + text[end:]
    for path, text in texts.items():
        (root / path).write_text(text, encoding="utf-8")


def _git(root: Path, *args: str, raise_on_error: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=raise_on_error
    )


def _sha(root: Path, ref: str) -> str:
    """The commit `ref` names, or "" when there is none; a git that cannot read
    the repository raises."""
    found = _git(
        root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", raise_on_error=False
    )
    if found.returncode not in (0, 1):  # 1 is "no such ref"
        found.check_returncode()
    return found.stdout.strip()


def _tag_sha(root: Path, tag: str) -> str:
    """The commit tag `tag` names, or "" when there is none; a branch of the same
    name is not a tag."""
    return _sha(root, f"refs/tags/{tag}")


def tag_on_head(tag_version: str, root: Path = ROOT) -> bool:
    """`v<tag_version>` is the checked-out commit (a run on the tag's ref)."""
    own = _tag_sha(root, f"v{tag_version}")
    return bool(own) and own == _sha(root, "HEAD")


def _version_of(tag: str) -> str:
    """`v1.2.0-rc.1` as `1.2.0-rc.1`."""
    return tag.removeprefix("v")


def previous_release(version: str, root: Path = ROOT) -> str:
    """The highest final v* tag below `version`."""
    listed = _git(root, "tag", "--list", "v*")
    finals = [t for t in listed.stdout.split() if FINAL.fullmatch(_version_of(t))]
    older = [t for t in finals if parse(_version_of(t)) < parse(version)]
    if not older:
        raise ValueError(f"no final release tag below {version} (are the tags fetched?)")
    return max(older, key=lambda t: parse(_version_of(t)))


def candidates(final: str, root: Path = ROOT) -> list[str]:
    """The candidate tags of `final`, by their number, lowest first."""
    listed = _git(root, "tag", "--list", f"v{final}-rc.*").stdout.split()
    return sorted(
        (t for t in listed if CANDIDATE.fullmatch(_version_of(t))),
        key=lambda t: int(t.rsplit(".", 1)[1]),
    )


def candidates_on_head(final: str, root: Path = ROOT) -> list[str]:
    """The candidate tags of `final` on the checked-out commit, newest first."""
    on_head = set(_git(root, "tag", "--points-at", "HEAD").stdout.split())
    return [t for t in reversed(candidates(final, root)) if t in on_head]


def refuse_tagged(version: str, root: Path = ROOT) -> None:
    """A bump for a version already tagged would rename a shipped release. The
    notes alone may be made again for it."""
    if _tag_sha(root, f"v{version}"):
        raise ValueError(f"v{version} is already tagged: prepare the next version")


def check_tag(tag_version: str, root: Path = ROOT) -> str | None:
    """An error for a tag that breaks the candidate order: `vX.Y.Z-rc.N` once
    `vX.Y.Z` is out, or a final `vX.Y.Z` on another commit than its last
    candidate."""
    final = final_of(tag_version)
    if tag_version != final:  # a candidate, X.Y.Z-rc.N
        released = _tag_sha(root, f"v{final}")
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
                f" (git push origin :refs/tags/v{final})"
            )
        return None
    tags = candidates(final, root)
    if tags and _tag_sha(root, tags[-1]) != _sha(root, "HEAD"):
        return (
            f"v{final} is not on {tags[-1]}'s commit: delete {tags[-1]}"
            f" (git push origin :refs/tags/{tags[-1]}) if it is a stray;"
            f" if v{final}'s run published nothing, delete v{final}"
            f" (git push origin :refs/tags/v{final}) and tag it on {tags[-1]}'s commit"
        )
    return None


def released_chart(tag: str, root: Path = ROOT) -> str:
    """The chart's own version at a release tag."""
    shown = _git(root, "show", f"{tag}:{CHART.path}")
    try:
        version = _match(CHART, shown.stdout).group("v")
        parse(version)
    except PinError as e:
        raise PinError(CHART.path, f"{e.reason} (at {tag})") from None
    except ValueError as e:  # the version is not X.Y.Z[-pre]
        raise PinError(CHART.path, f"{e} (at {tag})") from None
    return version


def chart_baseline(tag_version: str, root: Path = ROOT) -> str | None:
    """The release before `tag_version`'s, or None when `vX.Y.Z` is on another
    commit (a dry run of a branch still naming a shipped release)."""
    version = final_of(tag_version)
    own = _tag_sha(root, f"v{version}")
    if own and own != _sha(root, "HEAD"):
        return None
    return previous_release(version, root)


def check_chart(baseline: str, root: Path = ROOT) -> str | None:
    """An error when the chart's version is not above the one `baseline` shipped:
    every release moves the chart's `appVersion`, so every release changes it."""
    new = read(CHART, root)
    old = released_chart(baseline, root)
    if parse(new) <= parse(old):
        return f"the chart's version is {new}, not above the {old} that {baseline} shipped"
    return None


def next_chart_version(version: str, previous_version: str, previous_chart: str) -> str:
    """The chart after `previous_chart`, which `previous_version` shipped: a patch
    release moves it a patch, any other a minor."""
    major, minor, patch = parse(previous_chart)
    if parse(version)[:2] == parse(previous_version)[:2]:
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def planned_chart(
    version: str, previous_version: str, previous_chart: str, root: Path = ROOT
) -> str:
    """One step above the chart `previous_version` shipped, or the current chart
    when it was raised higher since; so a second bump moves it once."""
    chart = next_chart_version(version, previous_version, previous_chart)
    return max(chart, read(CHART, root), key=parse)


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
    """Pull request number to squash-merge author time, oldest first, for the
    merges since `previous_tag`; the release pull request's own merge (the one
    that moves `__version__`) is left out."""
    # The author date: a commit cherry-picked onto an older line keeps it, and gets a
    # new committer date after the pull request's merge.
    log = _git(root, "log", "--reverse", "--format=%H %at %s", f"{previous_tag}..HEAD")
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
    releases = set(bumps.stdout.split())
    merged: dict[int, int] = {}
    for line in log.stdout.splitlines():
        sha, authored, subject = line.split(" ", 2)
        if sha not in releases and (m := re.search(r"\(#(\d+)\)$", subject)):
            merged[int(m.group(1))] = int(authored)
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
    errors = check(tag_version=args.tag_version)
    for path, error in errors:
        print(f"::error file={path}::{error}")
    if errors:
        raise SystemExit(1)


def _previous(args: argparse.Namespace) -> None:
    print(previous_release(final_of(args.tag_version)))


def _check_tag(args: argparse.Namespace) -> None:
    tag_version = args.tag_version
    if not tag_on_head(tag_version):
        if not args.dry_run:  # a tag run's tag moved after the push
            raise ValueError(f"v{tag_version} is not on the checked-out commit")
        print(f"::notice::no candidate-order check: v{tag_version} is not on this commit")
        return
    tag_error = check_tag(tag_version)
    if tag_error:
        raise ValueError(tag_error)


def _check_chart(args: argparse.Namespace) -> None:
    tag_version = args.tag_version
    baseline = chart_baseline(tag_version)
    if not baseline:
        # A tag run never gets here: check-tag refuses every vX.Y.Z off HEAD first.
        print(f"::notice::no chart check: v{final_of(tag_version)} is tagged on another commit")
        return
    # Caught here, not in main(), for the annotation's file= and the baseline's name.
    try:
        chart_error = check_chart(baseline)
    except subprocess.CalledProcessError as e:
        chart_error = f"could not read the chart at {baseline}: {e.stderr.strip()}"
    except PinError as e:  # a chart version missing, here or at the baseline
        chart_error = e.reason
    except ValueError as e:  # the working tree's chart version is not X.Y.Z[-pre]
        chart_error = str(e)
    if chart_error:
        raise SystemExit(f"::error file={CHART.path}::{chart_error}")


def _fetch_tags() -> None:
    """The upstream repository's tags, forced, so a tag moved upstream replaces a
    stale local one."""
    _git(ROOT, "fetch", "--quiet", "--force", "--tags", f"https://github.com/{REPOSITORY}.git")


def _candidates_on_head(args: argparse.Namespace) -> None:
    for tag in candidates_on_head(args.version):
        print(tag)


def _notes(args: argparse.Namespace) -> None:
    version = args.version
    _fetch_tags()
    validate(version)
    print(release_notes(version, previous_release(version)))


def _bump(args: argparse.Namespace) -> None:
    version = args.version
    _fetch_tags()
    validate(version)
    refuse_tagged(version)
    previous = previous_release(version)
    # Every refusal before the notes' gh call.
    released = released_chart(previous)  # names the tag when its chart is bad
    chart = planned_chart(version, _version_of(previous), released)
    NOTES_FILE.write_text(release_notes(version, previous), encoding="utf-8")
    bump(version, chart)
    print(f"Bumped to {version}; the release notes are in {NOTES_FILE}")


def _final_version(text: str) -> str:
    """An argparse type: a version the files can take. A candidate is a tag on
    the release commit, whose files name the final."""
    if CANDIDATE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a final X.Y.Z: a candidate is a tag")
    if not FINAL.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not X.Y.Z")
    return text


def _tag_version(text: str) -> str:
    """An argparse type: a release tag's version, final or candidate."""
    if not (FINAL.fullmatch(text) or CANDIDATE.fullmatch(text)):
        raise argparse.ArgumentTypeError(f"{text!r} is not X.Y.Z or X.Y.Z-rc.N")
    return text


def _one_line(e: Exception) -> str:
    if isinstance(e, FileNotFoundError):
        return f"{e.filename}: not found"
    if isinstance(e, subprocess.CalledProcessError):
        detail = f": {e.stderr.strip()}" if e.stderr else ""
        return f"{' '.join(map(str, e.cmd))} failed{detail}"
    return str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    bump_cmd = commands.add_parser("bump", help="write the release notes, then bump")
    bump_cmd.add_argument("version", type=_final_version, help="the release, X.Y.Z")
    bump_cmd.set_defaults(run=_bump)
    notes_cmd = commands.add_parser("notes", help="only print the release notes")
    notes_cmd.add_argument("version", type=_final_version, help="the release, X.Y.Z")
    notes_cmd.set_defaults(run=_notes)
    tag_version_help = "the tag's version, X.Y.Z or X.Y.Z-rc.N"
    check_cmd = commands.add_parser("check", help="every file names the tag's final version")
    check_cmd.add_argument("tag_version", type=_tag_version, help=tag_version_help)
    check_cmd.set_defaults(run=_check)
    tag_cmd = commands.add_parser(
        "check-tag", help="a candidate before its final, the final on its last candidate's commit"
    )
    tag_cmd.add_argument("tag_version", type=_tag_version, help=tag_version_help)
    tag_cmd.add_argument(
        "--dry-run", action="store_true", help="skip, with a notice, when the tag is not HEAD"
    )
    tag_cmd.set_defaults(run=_check_tag)
    chart_cmd = commands.add_parser(
        "check-chart", help="the chart's version is above the previous release's"
    )
    chart_cmd.add_argument("tag_version", type=_tag_version, help=tag_version_help)
    chart_cmd.set_defaults(run=_check_chart)
    previous_cmd = commands.add_parser(
        "previous", help="the highest final release tag below the tag's version"
    )
    previous_cmd.add_argument("tag_version", type=_tag_version, help=tag_version_help)
    previous_cmd.set_defaults(run=_previous)
    candidates_cmd = commands.add_parser(
        "candidates-on-head", help="the final's candidate tags on this commit, newest first"
    )
    candidates_cmd.add_argument("version", type=_final_version, help="the release, X.Y.Z")
    candidates_cmd.set_defaults(run=_candidates_on_head)
    args = parser.parse_args()
    # A refusal, a failed git or gh call, or a missing gh, git or file is one line;
    # in a check, an ::error:: annotation.
    try:
        args.run(args)
    except (ValueError, subprocess.CalledProcessError, FileNotFoundError) as e:
        prefix = "" if args.command in ("bump", "notes") else "::error::"
        raise SystemExit(prefix + _one_line(e)) from None


if __name__ == "__main__":
    main()
