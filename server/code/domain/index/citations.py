"""Citation extraction for code.verify_citations.

A draft comment cites code in a handful of closed shapes — `path:line`,
`path:start-end`, `[path:start-end](…/blob/<sha>/path#L1-L9)`, a bare path,
`#N` / `PR N` / `issue N`, a qualified name `Ns::Name` — and every one of them
is a regex, never a model call. This module turns text into `Citation`
records with the shape the handler verifies; it reads nothing.

Fenced code blocks are a repro or a log, not a claim: numbers are not
extracted from inside them (a `#1` in a shell line, a colour code, an array
index), while paths, locations and qualified names are — a stack trace's
frames are exactly what a reviewer wants checked.

Two more shapes come from review findings rather than PR bodies: a bare `Filename.ext:NNN` with no
directory (`SSHPtyHandler.cpp:104`; the file table says which file that is) and a bare function name
— a backticked lower-camel identifier (`getClientExitCode`, `sumMap`) or a lower-case call with
empty parens (`readRow()`). Function names are camelCase and variables snake_case in the repository
this was built for, so an all-lower or snake_case word (`return`, `max_execution_time`) is a
keyword, a setting or a variable, never a citation. `parse_hunks` / `in_ranges` read a unified
diff's new-side line ranges for the `in_pr_diff` check.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

from server.core.relative_paths import without_dot_slashes

MAX_CITATIONS = 500
MAX_TEXT_CHARS = 200_000

# Shapes a file citation may take: an extension a source tree carries.
_EXTS = (
    "cpp|cc|cxx|c|h|hpp|hxx|ipp|tpp|inc|py|pyi|sql|sh|bash|reference|xml|yml|yaml|"
    "json|md|txt|cmake|proto|rs|go|java|ts|tsx|js|jsx|html|css|ini|toml|cfg|conf|"
    "lock|in|j2|expect|pl|rb|php|cs|swift|kt|scala|m|mm|s|asm|td|def|capnp|thrift|"
    "avsc|parquet|orc|csv|tsv|gz|zst|lz4|xz|bin|dat|so|a|lib|bat|ps1|dockerfile|"
    "jinja|tpl|patch|diff|gradle|sbt|mk|ninja|jsonl|ndjson|arrow|feather|npy|npz"
)
_PATH = rf"(?:[\w.+\-@]+/)+[\w.+\-@]+\.(?:{_EXTS})"
_PATH_RE = re.compile(rf"(?<![\w/.+\-@])(?P<path>{_PATH})(?![\w/])")
# `path:12`, `path:12-20`, `path:12:34` (column dropped), `path#L12-L20`,
# `path:L12`; inside brackets or backticks or bare.
_LOC_RE = re.compile(
    rf"(?<![\w/.+\-@])(?P<path>{_PATH})"
    r"(?:(?::|#)L?(?P<start>\d+)(?:(?:-|–|—|\.\.|-L)(?P<end>\d+))?(?::\d+)?)"
    r"(?![\w/])"
)
# github.com/<owner>/<repo>/blob/<rev>/<path>#L12-L20 — the rev rides along. The rev
# is one segment: the path after a longer one also follows the first.
_BLOB_URL_RE = re.compile(
    r"https?://github\.com/[\w.\-]+/[\w.\-]+/blob/(?P<rev>[\w.\-]+)/"
    rf"(?P<path>{_PATH})(?:#L(?P<start>\d+)(?:-L?(?P<end>\d+))?)?"
)
_ITEM_URL_RE = re.compile(r"https?://github\.com/[\w.\-]+/[\w.\-]+/(?:pull|issues)/(?P<number>\d+)")
_ANY_URL_RE = re.compile(r"https?://[^\s<>()\[\]`'\"]+")
# A bare `#N` below this is an ordinal (a setting's position, a perf row, a
# list item), not an item citation; the word forms (`PR 7`, `issue 12`) are
# taken at any size.
SMALL_NUMBER_MAX = 99
_NUMBER_RE = re.compile(
    r"(?<![\w/#&$])(?:(?P<word>PR|pull request|issue|Issue|Fixes|Closes|Resolves|Refs?|"
    r"Related to|Follow-?up to|Backport of|Part of)\s+#?|#)(?P<number>\d{1,8})\b"
)
# Ns::Name, Class::method, DB::Foo::~Foo, a::b::operator[] — two or more parts.
# Not from a destructor part's name (`::~Foo`): a start there rescans the chain.
_NAME_RE = re.compile(
    r"(?<![\w:>.])(?<!::~)(?P<name>[A-Za-z_]\w*(?:::(?:operator\s*(?:\[\]|\(\)|[^\s\w(]{1,3})|~?[A-Za-z_]\w*))+)"
    r"(?![\w:])"
)
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
# A bare filename — no directory — carries a narrower extension list than `_PATH`: `t.c`
# / `x.a` in prose are a table's column or an expression, so the short binary and
# single-letter extensions are out, and the stem is at least two characters (three when
# there is no line to anchor it, and then only inside backticks).
_BARE_EXTS = (
    "cpp|cc|cxx|h|hpp|hxx|ipp|py|pyi|sql|sh|xml|yml|yaml|json|md|txt|cmake|proto|rs|go|"
    "java|ts|tsx|js|reference|j2|expect|tpl|ini|toml|cfg|conf|jinja|patch|diff"
)
_BARE_FILE = rf"[A-Za-z_][\w.+\-]+\.(?:{_BARE_EXTS})"
_BARE_LOC_RE = re.compile(
    rf"(?<![\w/.\-])(?<![\w.+\-]\+)(?P<path>{_BARE_FILE})"
    r"(?:(?::|#)L?(?P<start>\d+)(?:(?:-|–|—|\.\.|-L)(?P<end>\d+))?(?::\d+)?)"
    r"(?![\w/])"
)
_TICK_FILE_RE = re.compile(rf"`(?P<path>[A-Za-z_][\w.+\-]{{2,}}\.(?:{_BARE_EXTS}))`")
# A function by the repository's naming rules: lower-camel with at least one
# capital (`getClientExitCode`, `sumMap`, `arrayAUCPR`) inside backticks, with
# or without `()`; or any lower-case identifier of three or more characters
# with empty parens anywhere (`readRow()`, `check_limits()`) — the parens say
# it is a call. Snake_case and all-lower words without parens are settings,
# keywords and variables.
# `(line 495)`, `lines 178-179`, `at line 12` — a line cited against the file
# or symbol named just before it (`NpyRowInputFormat::readRow` (line 495)).
# The handler binds it to the nearest preceding citation that resolved to a
# path; unanchored, it is skipped (a "line 3 of the output" is not a claim).
_LINE_REF_RE = re.compile(
    r"(?<![\w#/:.\-])[Ll]ines?\s+(?P<start>\d{1,6})(?:\s*(?:-|–|—|to)\s*(?P<end>\d{1,6}))?(?![\w.:]\d)"
)
_CAMEL = r"[a-z][a-z0-9]*[A-Z]\w*"
_BARE_NAME_RE = re.compile(
    rf"`(?P<tick>{_CAMEL})(?:\(\))?`|(?<![\w:.>`~])(?P<call>[a-z_]\w{{2,}})\(\)(?![\w(])"
)
# Names whose outermost scope is a library the repository does not define;
# they are listed, never verified, so a `std::vector` mention cannot make a
# draft "has_unresolved".
EXTERNAL_SCOPES: frozenset[str] = frozenset(
    {
        "std",
        "boost",
        "absl",
        "fmt",
        "Poco",
        "folly",
        "google",
        "arrow",
        "parquet",
        "orc",
        "llvm",
        "clang",
        "simdjson",
        "rapidjson",
        "re2",
        "__gnu_cxx",
        "__cxxabiv1",
        "grpc",
        "aws",
        "Aws",
        "Azure",
        "azure",
        "s2",
        "icu",
        "icu_",
        "protobuf",
        "capnp",
        "kj",
        "zstd",
        "lz4",
        "snappy",
        "brotli",
        "avro",
        "cctz",
        "date",
        "pcg",
        "roaring",
        "rocksdb",
        "sqlite",
        "libpq",
        "mysqlxx",
        "MySQLxx",
        "curl",
        "ssl",
        "OpenSSL",
        "nuraft",
        "Coordination_",
        "krb5",
        "ldap",
        "libunwind",
        "jemalloc",
        "gwp_asan",
        "__sanitizer",
        "__asan",
        "__tsan",
        "__msan",
        "__ubsan",
        "absl_",
        "testing",
        "gtest",
        "benchmark",
        "libc",
        "__libcpp",
        "__1",
        "__2",
        "_LIBCPP",
        "simdutf",
        "pqxx",
        "wide",
        "Int128",
        "Int256",
        "UInt128",
        "UInt256",
        "nlohmann",
        "magic_enum",
        "ryu",
        "double_conversion",
    }
)


@dataclass
class Citation:
    kind: str  # location | path | number | name
    text: str  # the citation as written (normalised path / number / name)
    path: str = ""
    start_line: int = 0
    end_line: int = 0
    number: int = 0
    name: str = ""
    rev: str = ""  # a blob URL's revision, when the citation carried one
    # basename: a `Filename.ext[:N]` cited without its directory; bare_name: a function
    # cited by its unqualified name; line_ref: `line N` bound to the citation before it
    form: str = ""
    # true | false | unknown once a PR's diff was read; "" when no `pr_number` was given
    in_pr_diff: str = ""
    in_code_block: bool = False
    occurrences: int = 1
    pos: int = 0  # offset of the first occurrence in the text
    verdict: str = ""
    reason: str = ""
    epistemic: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def key(self) -> tuple[str, str, int, int]:
        return (self.kind, self.text, self.start_line, self.end_line)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "text": self.text,
            "verdict": self.verdict,
            "reason": self.reason,
            "epistemic": self.epistemic,
            "occurrences": self.occurrences,
        }
        if self.path:
            out["path"] = self.path
        if self.start_line:
            out["start_line"] = self.start_line
            out["end_line"] = self.end_line or self.start_line
        if self.number:
            out["number"] = self.number
        if self.name:
            out["name"] = self.name
        if self.rev:
            out["rev"] = self.rev
        if self.form:
            out["form"] = self.form
        if self.in_pr_diff:
            out["in_pr_diff"] = self.in_pr_diff
        if self.in_code_block:
            out["in_code_block"] = True
        if self.target:
            out["target"] = self.target
        if self.candidates:
            out["candidates"] = self.candidates
        return out


@dataclass
class Extraction:
    citations: list[Citation]
    skipped: list[dict[str, Any]]  # {kind, text, reason}
    truncated: bool = False


def normalise_path(path: str) -> str:
    p = without_dot_slashes(path.strip()).lstrip("/")
    # each diff prefix is stepped past, not sliced off: a slice copies the rest of the path
    i, slashes = 0, p.count("/")
    while p.startswith(("a/", "b/"), i) and slashes > 1 and not p.startswith(("a/b", "b/a"), i):
        # unified-diff prefixes; a real `a/` top-level directory is rare
        # enough that the loss is one citation read as unresolved
        i += 2
        slashes -= 1
    return p[i:]


def _span(start: str | None, end: str | None) -> tuple[int, int]:
    s = int(start or 0)
    e = int(end or 0) or s
    if e < s:
        s, e = e, s
    return s, e


def _fence_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    """`spans` are in order and do not overlap, so the one that could hold
    `pos` is found by bisection, not by walking every fence."""
    i = bisect.bisect_right(spans, pos, key=lambda span: span[0]) - 1
    return i >= 0 and pos < spans[i][1]


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace matched spans with spaces so a later regex cannot re-match
    inside them (a blob URL's path is not a bare path; an item URL's number
    is not a `#N`) while every other offset stays where it was."""
    if not spans:
        return text
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _urls(text: str, fences: list[tuple[int, int]], out: list[Citation]) -> str:
    used: list[tuple[int, int]] = []
    for m in _BLOB_URL_RE.finditer(text):
        s, e = _span(m.group("start"), m.group("end"))
        path = normalise_path(m.group("path"))
        kind = "location" if s else "path"
        out.append(
            Citation(
                kind=kind,
                text=f"{path}:{s}-{e}" if s and e != s else (f"{path}:{s}" if s else path),
                path=path,
                start_line=s,
                end_line=e,
                rev=m.group("rev"),
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    for m in _ITEM_URL_RE.finditer(text):
        n = int(m.group("number"))
        out.append(
            Citation(
                kind="number",
                text=f"#{n}",
                number=n,
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    text = _blank(text, used)
    # every other URL: a raw.githubusercontent.com or docs link is not a path
    return _blank(text, [(m.start(), m.end()) for m in _ANY_URL_RE.finditer(text)])


def _locations(text: str, fences: list[tuple[int, int]], out: list[Citation]) -> str:
    used: list[tuple[int, int]] = []
    for m in _LOC_RE.finditer(text):
        s, e = _span(m.group("start"), m.group("end"))
        path = normalise_path(m.group("path"))
        out.append(
            Citation(
                kind="location",
                text=f"{path}:{s}-{e}" if e != s else f"{path}:{s}",
                path=path,
                start_line=s,
                end_line=e,
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    return _blank(text, used)


# a scheme-less URL read as a path: `raw.githubusercontent.com/X/Y/file.xml`
_HOSTNAME_RE = re.compile(r"^[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|ai|edu|gov|ru|cn|de|uk)$")


def _paths(text: str, fences: list[tuple[int, int]], out: list[Citation]) -> str:
    used: list[tuple[int, int]] = []
    for m in _PATH_RE.finditer(text):
        path = normalise_path(m.group("path"))
        if "/" not in path or _HOSTNAME_RE.match(path.split("/", 1)[0]):
            continue
        out.append(
            Citation(
                kind="path",
                text=path,
                path=path,
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    return _blank(text, used)


def _bare_files(text: str, fences: list[tuple[int, int]], out: list[Citation]) -> str:
    """`SSHPtyHandler.cpp:104` and `` `SetUtils.cpp` `` — a file cited by its basename.
    The path is the basename until the file table says which indexed file ends in it."""
    used: list[tuple[int, int]] = []
    for m in _BARE_LOC_RE.finditer(text):
        s, e = _span(m.group("start"), m.group("end"))
        path = m.group("path")
        out.append(
            Citation(
                kind="location",
                text=f"{path}:{s}-{e}" if e != s else f"{path}:{s}",
                path=path,
                start_line=s,
                end_line=e,
                form="basename",
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    text = _blank(text, used)
    used = []
    for m in _TICK_FILE_RE.finditer(text):
        path = m.group("path")
        out.append(
            Citation(
                kind="path",
                text=path,
                path=path,
                form="basename",
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    return _blank(text, used)


def _line_refs(text: str, fences: list[tuple[int, int]], out: list[Citation]) -> str:
    """`line 495` / `lines 178-179` with no path of their own; the handler binds each to the
    citation before it."""
    used: list[tuple[int, int]] = []
    for m in _LINE_REF_RE.finditer(text):
        s, e = _span(m.group("start"), m.group("end"))
        if not s:
            continue
        out.append(
            Citation(
                kind="location",
                text=f"lines {s}-{e}" if e != s else f"line {s}",
                start_line=s,
                end_line=e,
                form="line_ref",
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
        used.append((m.start(), m.end()))
    return _blank(text, used)


def _numbers(
    text: str, fences: list[tuple[int, int]], out: list[Citation], skipped: list[dict[str, Any]]
) -> None:
    seen_small: set[int] = set()
    for m in _NUMBER_RE.finditer(text):
        if _in_spans(m.start(), fences):
            continue
        n = int(m.group("number"))
        if n <= 0:
            continue
        if not m.group("word") and n <= SMALL_NUMBER_MAX:
            if n not in seen_small:
                seen_small.add(n)
                skipped.append({"kind": "number", "text": f"#{n}", "reason": "small_number"})
            continue
        out.append(Citation(kind="number", text=f"#{n}", number=n, pos=m.start()))


def _names(
    text: str, fences: list[tuple[int, int]], out: list[Citation], skipped: list[dict[str, Any]]
) -> str:
    seen_external: set[str] = set()
    used: list[tuple[int, int]] = []
    for m in _NAME_RE.finditer(text):
        used.append((m.start(), m.end()))
        name = m.group("name").replace(" ", "")
        head = name.split("::", 1)[0]
        if head in EXTERNAL_SCOPES:
            if name not in seen_external:
                seen_external.add(name)
                skipped.append({"kind": "name", "text": name, "reason": "external_scope"})
            continue
        out.append(
            Citation(
                kind="name",
                text=name,
                name=name,
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )
    return _blank(text, used)


def _bare_names(text: str, fences: list[tuple[int, int]], out: list[Citation]) -> None:
    """`` `getClientExitCode` ``, `` `sumMap()` ``, `readRow()` — a function cited by
    its bare name. Runs after the qualified pass blanked its matches, so `Cls::readRow`
    is never re-read as `readRow`."""
    for m in _BARE_NAME_RE.finditer(text):
        name = m.group("tick") or m.group("call")
        out.append(
            Citation(
                kind="name",
                text=name,
                name=name,
                form="bare_name",
                in_code_block=_in_spans(m.start(), fences),
                pos=m.start(),
            )
        )


def extract(text: str, *, limit: int = MAX_CITATIONS) -> Extraction:
    """Every citation in `text`, deduplicated by (kind, text, span) with an
    occurrence count, in first-appearance order. Precedence: blob / item
    URLs, then `path:line` locations, then bare paths, then basename
    locations and backticked basenames, then `line N` references, then
    numbers, then qualified names, then bare function names — each pass
    blanks what it matched so the next cannot read a URL's path as a path,
    an anchor as a number or a qualified name's tail as a bare one."""
    text = text[:MAX_TEXT_CHARS]
    fences = _fence_spans(text)
    found: list[Citation] = []
    skipped: list[dict[str, Any]] = []
    rest = _urls(text, fences, found)
    rest = _locations(rest, fences, found)
    rest = _paths(rest, fences, found)
    rest = _bare_files(rest, fences, found)
    rest = _line_refs(rest, fences, found)
    _numbers(rest, fences, found, skipped)
    rest = _names(rest, fences, found, skipped)
    _bare_names(rest, fences, found)
    merged: dict[tuple[str, str, int, int], Citation] = {}
    for c in found:
        k = c.key()
        if k in merged:
            merged[k].occurrences += 1
            merged[k].pos = min(merged[k].pos, c.pos)
            merged[k].in_code_block = merged[k].in_code_block and c.in_code_block
            if c.rev and not merged[k].rev:
                merged[k].rev = c.rev
            if c.form and not merged[k].form:
                merged[k].form = c.form
        else:
            merged[k] = c
    ordered = sorted(merged.values(), key=lambda c: c.pos)
    truncated = len(ordered) > limit
    return Extraction(citations=ordered[:limit], skipped=skipped, truncated=truncated)


def inside(rows: list[dict[str, Any]], line: int) -> dict[str, Any] | None:
    """The tightest span in `rows` containing `line` (rows carry
    start_line / end_line); None when no span does."""
    hits = [
        r
        for r in rows
        if int(r.get("start_line") or 0) <= line <= int(r.get("end_line") or 0)
        and int(r.get("end_line") or 0) >= int(r.get("start_line") or 0)
    ]
    if not hits:
        return None
    hits.sort(key=lambda r: int(r.get("end_line") or 0) - int(r.get("start_line") or 0))
    return hits[0]


def nearest(rows: list[dict[str, Any]], line: int) -> tuple[dict[str, Any] | None, int]:
    """The unit whose boundary is closest to `line` and the gap in lines
    (0 when inside) — for a citation that landed between units, the
    off-by-one reading the consumer wants."""
    best: dict[str, Any] | None = None
    best_gap = 0
    for r in rows:
        lo, hi = int(r.get("start_line") or 0), int(r.get("end_line") or 0)
        if not lo or hi < lo:
            continue
        gap = 0 if lo <= line <= hi else min(abs(line - lo), abs(line - hi))
        if best is None or gap < best_gap:
            best, best_gap = r, gap
    return best, best_gap


SHORTHAND_FORMS = frozenset({"basename", "line_ref"})


def bind_line_refs(citations: list[Citation], skipped: list[dict[str, Any]]) -> list[Citation]:
    """Give every `line N` reference the path of the nearest preceding
    citation that resolved to one; the rest are listed in `skipped` as
    `no_anchor` and dropped. Returns the bound references (to be read and
    judged like any location); `citations` is pruned in place."""
    bound: list[Citation] = []
    keep: list[Citation] = []
    anchor = ""
    for c in sorted(citations, key=lambda c: c.pos):
        if c.form == "line_ref" and not c.path:
            if anchor:
                c.path = anchor
                c.text = (
                    f"{anchor}:{c.start_line}-{c.end_line}"
                    if c.end_line != c.start_line
                    else f"{anchor}:{c.start_line}"
                )
                bound.append(c)
                keep.append(c)
            else:
                skipped.append({"kind": "location", "text": c.text, "reason": "no_anchor"})
            continue
        keep.append(c)
        if c.verdict in ("resolved", "stale") and c.kind in ("path", "location") and c.path:
            anchor = c.path
        elif c.kind == "name" and c.verdict == "resolved" and c.target.get("path"):
            anchor = str(c.target["path"])
    citations[:] = keep
    return bound


def fold_shorthands(citations: list[Citation]) -> list[Citation]:
    """After verification: a basename location or path that resolved to the same file
    and lines as a full-path citation in the same text
    (`[Foo.cpp:12](…/blob/…/src/Foo.cpp#L12)`), and a bare name that is the tail of a
    qualified name cited alongside (`readRow` beside `NpyRowInputFormat::readRow`), are
    one citation written twice — they fold into the full form, occurrences adding up.
    Ambiguous basenames keep their own row (nothing says which file they meant)."""
    by_loc: dict[tuple[str, str, int, int], Citation] = {}
    tails: dict[str, Citation] = {}
    for c in citations:
        if c.kind in ("path", "location") and c.form not in SHORTHAND_FORMS:
            by_loc.setdefault((c.kind, c.path, c.start_line, c.end_line), c)
        elif c.kind == "name" and c.form != "bare_name":
            tails.setdefault(c.name.split("::")[-1], c)
    keep: list[Citation] = []
    for c in citations:
        host: Citation | None = None
        if c.kind in ("path", "location") and c.form in SHORTHAND_FORMS:
            host = by_loc.get((c.kind, c.path, c.start_line, c.end_line))
        elif c.kind == "name" and c.form == "bare_name":
            host = tails.get(c.name)
        if host is not None and host is not c:
            host.occurrences += c.occurrences
            host.pos = min(host.pos, c.pos)
            continue
        keep.append(c)
    return sorted(keep, key=lambda c: c.pos)


def top_verdict(citations: list[Citation]) -> str:
    """all_resolved | has_unresolved | has_stale | no_citations. An
    unresolved citation outranks a stale one (a dead pointer is worse than
    a moved one); `ambiguous` counts as resolved — the name exists, the
    reader picks among its definitions."""
    if not citations:
        return "no_citations"
    verdicts = {c.verdict for c in citations}
    if "unresolved" in verdicts:
        return "has_unresolved"
    if "stale" in verdicts:
        return "has_stale"
    return "all_resolved"


def counts_of(citations: list[Citation]) -> dict[str, int]:
    out = {"total": len(citations), "resolved": 0, "unresolved": 0, "stale": 0, "ambiguous": 0}
    by_kind: dict[str, int] = {}
    for c in citations:
        out[c.verdict] = out.get(c.verdict, 0) + 1
        by_kind[c.kind] = by_kind.get(c.kind, 0) + 1
    for k, v in sorted(by_kind.items()):
        out[f"kind_{k}"] = v
    return out


# ------------------------------------------------------------ PR diff

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)$", re.M)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@", re.M)
WHOLE_FILE: tuple[int, int] = (1, 10**9)


def parse_hunks(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Path -> the new-side line ranges a unified diff touches, inclusive.
    A pure deletion (`+N,0`) sits between new lines N and N+1 and touches
    both; a deleted file (`+++ /dev/null`) is the whole file under its old
    path, so any line cited in it is inside the change."""
    out: dict[str, list[tuple[int, int]]] = {}
    heads = list(_DIFF_HEADER_RE.finditer(diff))
    for i, h in enumerate(heads):
        block = diff[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(diff)]
        path = normalise_path(h.group("b"))
        if "\n+++ /dev/null" in block:
            out[normalise_path(h.group("a"))] = [WHOLE_FILE]
            continue
        ranges: list[tuple[int, int]] = []
        for m in _HUNK_RE.finditer(block):
            start = int(m.group("start"))
            count = int(m.group("count")) if m.group("count") is not None else 1
            ranges.append((start, start + count - 1) if count else (start, start + 1))
        out[path] = ranges
    return out


def in_ranges(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    """Does [start, end] overlap any range (inclusive on both sides)?"""
    end = end or start
    return any(lo <= end and start <= hi for lo, hi in ranges)
