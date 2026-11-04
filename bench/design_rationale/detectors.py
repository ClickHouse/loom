"""Deterministic checks on an agent's output: the diff detectors of a change task and
the citations an answer makes. No model call here."""

from __future__ import annotations

import fnmatch
import re
from typing import Any

Diff = dict[str, dict[str, list[str]]]


def extract_diff(text: str) -> str:
    """The diff an answer carries: every ```diff fence, else what follows `## Diff`."""
    fences = re.findall(r"```diff\n(.*?)```", text, re.S)
    if fences:
        return "\n".join(fences)
    return text.split("## Diff", 1)[1] if "## Diff" in text else ""


def _header_path(line: str) -> str:
    return re.sub(r"^[ab]/", "", line[4:].strip().split("\t")[0])


def diff_files(diff: str) -> Diff:
    """path -> {"+": added lines, "-": removed lines} of a unified diff. A file header is
    a `diff --git a/x b/y` line, a `--- ` line right before a `+++ ` line (the path from
    the `+++`, or the `---` one when the file was deleted), or a `+++ ` line right before
    a `@@` line. Hunk counts are not read: models miscount them. `@@` and `\ No newline`
    lines are skipped; any other line is added (`+`), removed (`-`) or context.
    Accepted: a removed line `-- x` right before an added line `++ y` reads as a header;
    it is rare and only labels the lines after it with another file."""
    files: Diff = {}
    current = None

    def open_file(path: str) -> None:
        nonlocal current
        if path and path != "/dev/null":
            current = files.setdefault(path, {"+": [], "-": []})

    lines = diff.splitlines()
    skip = False
    for i, line in enumerate(lines):
        if skip:
            skip = False
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            if m:
                open_file(m.group(2))
            continue
        if line.startswith("--- ") and nxt.startswith("+++ "):
            new = _header_path(nxt)
            open_file(new if new != "/dev/null" else _header_path(line))
            skip = True
            continue
        if line.startswith("+++ ") and nxt.startswith("@@"):
            open_file(_header_path(line))
            continue
        if line.startswith(("@@", "\\")) or current is None:
            continue
        if line.startswith(("+", "-")):
            current[line[0]].append(line[1:])
    return files


def _glob(path: str, pattern: str) -> bool:
    # fnmatch's `*` crosses `/`, so `src/**` and `src/*` both match every path below.
    return fnmatch.fnmatch(path, pattern)


def _norm(value: str) -> str:
    return value.replace("_", "").replace("'", "").replace('"', "").strip().lower()


def _value_tokens(line: str, name: str) -> set[str]:
    """The tokens of what a line assigns or declares for `name`: the text after the
    name, split into words and numbers (`512` is not `5120`), normalised as `_norm`
    does."""
    after = line.split(name, 1)[1]
    return {_norm(t) for t in re.findall(r"[A-Za-z0-9_.]+", after)}


def run_detector(d: dict[str, Any], files: Diff) -> bool | None:
    """True when the diff does what the detector looks for (a failure of the task),
    False when it does not, None for a `judge` detector (no deterministic signal)."""
    kind = d["type"]
    if kind == "judge":
        return None
    if kind == "file_touched":
        return any(p in files for p in d["paths"])
    if kind in ("diff_added_regex", "diff_removed_regex"):
        side = "+" if kind == "diff_added_regex" else "-"
        rx = re.compile(d["pattern"])
        return any(
            rx.search(line)
            for path, f in files.items()
            if _glob(path, d.get("path_glob", "*"))
            for line in f[side]
        )
    if kind == "setting_default_changed":
        name = d["setting"]
        for f in files.values():
            for line in f["+"]:
                if name not in line:
                    continue
                values = _value_tokens(line, name)
                if "to" in d and _norm(str(d["to"])) in values:
                    return True
                # A declaration naming the setting with some other default.
                declared = re.search(
                    r"DECLARE|\{\s*\"" + re.escape(name) + r"|\b" + re.escape(name) + r"\s*[:=]",
                    line,
                )
                if declared and values and _norm(str(d["from"])) not in values:
                    return True
        return False
    raise ValueError(f"unknown detector type {kind!r}")


# A memory key cited in brackets: `[design/cache/eviction]`, `[decision:0a1b2c]`.
_CITED_KEY = re.compile(r"\[([A-Za-z0-9_.-]+(?:[:/][A-Za-z0-9_.:/-]+)+)\]")
# Ends at a line end, a closing bracket or backtick, or the next pointer.
_CITED_KNOWLEDGE = re.compile(
    r"knowledge:(?:(?!knowledge:)[^\s\]\)`])+(?:(?!knowledge:)[^\n\]\)`])*"
)


def parse_knowledge(cite: str, source_uris: list[str]) -> tuple[str, str, str] | None:
    """(namespace, source_uri, section) of a `knowledge:<ns>/<uri>#<section>` citation,
    matched against the set's source_uris; None when no complete uri is in it."""
    rest = cite.removeprefix("knowledge:")
    ns, _, uri_section = rest.partition("/")
    for uri in sorted(source_uris, key=len, reverse=True):
        if uri_section.startswith(uri):
            after = uri_section[len(uri) :]
            # A section only after `#`: anything else after the uri is the sentence's.
            section = after[1:].strip().rstrip(".,;…") if after.startswith("#") else ""
            return ns, uri, section
    return None


def find_citations(text: str, source_uris: list[str]) -> list[dict[str, Any]]:
    """Each distinct citation in `text`, in order: {"cite", "kind"} with kind `memory`
    (a bracketed key), `knowledge` (a `knowledge:` pointer) or `document` (a source_uri
    in brackets, with `#<section>` or without, as knowledge hits print it)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(cite: str, kind: str) -> None:
        if cite not in seen:
            seen.add(cite)
            out.append({"cite": cite, "kind": kind})

    for m in _CITED_KNOWLEDGE.finditer(text):
        add(m.group(0).strip(), "knowledge")
    uris = set(source_uris)
    for m in re.finditer(r"\[([^\]]+)\]", text):
        if m.group(1).split("#", 1)[0] in uris:
            add(m.group(1), "document")
    for m in _CITED_KEY.finditer(text):
        key = m.group(1)
        if key not in uris and not key.startswith("knowledge:") and "://" not in key:
            add(key, "memory")
    return out


# How much of a cited source a judge reads.
CITED_TEXT_CHARS = 1500


def section_text(content: str, section: str) -> str | None:
    """A cited section's heading and text, found the way the chunker named it: the
    breadcrumb is the parser's cleaned heading path (code spans, emphasis and anchors
    stripped; Setext and HTML headings read). The document start when no section is
    named; None when no section, or more than one, fits the citation."""
    if not section:
        return content[:CITED_TEXT_CHARS]
    from server.knowledge.domain.chunking import detect_sections

    sections = [s for s in detect_sections(content) if s.path]
    cleaned = section.replace("`", "").replace("*", "").strip()
    # The chunker strips code spans and emphasis from headings: the cleaned text first.
    for cited in dict.fromkeys((cleaned, section)):
        found = _cited_section(cited, sections)
        if found is not None:
            title: str = found.path[-1]
            return (title + "\n" + content[found.start : found.end])[:CITED_TEXT_CHARS]
    return None


def _cited_section(cited: str, sections: list[Any]) -> Any:
    """The one section a cited breadcrumb names. A citation may give the path whole or
    only its last segments, and may arrive cut short (the citation pattern stops at a
    `)` or a backtick) or with words after it. In that order: the one section whose
    whole path the citation is; else the sections whose path ends with the cited
    segments; else those whose path, or its last segments, the cited text is the start
    of; else the longest path the cited text starts with, followed by words rather than
    another segment. Exactly one section must fit, or none is chosen."""
    from server.knowledge.domain.chunking import SEPARATOR

    exact: dict[int, Any] = {}
    cut: dict[int, Any] = {}
    started: dict[int, Any] = {}
    started_len: dict[int, int] = {}
    # A citation that is one section's whole path names it, whatever else ends so.
    whole = [s for s in sections if SEPARATOR.join(s.path) == cited]
    if len(whole) == 1:
        return whole[0]
    for s in sections:
        for k in range(1, len(s.path) + 1):
            tail = SEPARATOR.join(s.path[-k:])
            if cited == tail:
                exact[s.idx] = s
            elif tail.startswith(cited):
                cut[s.idx] = s
            elif (
                cited.startswith(tail)
                # Words after the path, not a further segment: "T > Missing" is not T.
                and not cited[len(tail) :].lstrip().startswith(SEPARATOR.strip())
                and len(tail) > started_len.get(s.idx, 0)
            ):
                started[s.idx] = s
                started_len[s.idx] = len(tail)
    for group in (exact, cut):
        if group:
            return next(iter(group.values())) if len(group) == 1 else None
    if not started:
        return None
    best = max(started_len.values())
    longest = [s for idx, s in started.items() if started_len[idx] == best]
    return longest[0] if len(longest) == 1 else None
