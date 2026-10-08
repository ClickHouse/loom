"""Pure parsers for `code.setting_facts`: what the indexed tree says about one
ClickHouse setting, from the four places that define it.

- **Declaration** — `DECLARE(Type, name, default, R"(description)", flags)`
  (or `DECLARE_WITH_ALIAS(..., alias)`) in `src/Core/Settings.cpp`,
  `src/Storages/MergeTree/MergeTreeSettings.cpp`, `src/Core/ServerSettings.cpp`,
  `src/Core/FormatFactorySettings.h`. `flags` is `0` (production),
  `IMPORTANT`, `BETA` or `EXPERIMENTAL`; `MAKE_OBSOLETE(M, Type, name, v)`
  is the obsolete tier.
- **History** — `src/Core/SettingsChangesHistory.cpp`: blocks
  `addSettingsChanges(settings_changes_history, "26.4", { {"name", old, new,
  "reason"}, … })`. An entry's release is the nearest block header above it,
  which is why the file is parsed end to end from its chunks in line order.
- **CI randomization** — `tests/clickhouse-test`, `class SettingsRandomizer`
  / `class MergeTreeSettingsRandomizer`: `"name": <sampler>,`. A setting
  listed there takes a random value in every stateless CI run — a failure
  that appears only with one value of it is the randomizer's, not the
  PR's.

Everything here is text → dict; the handler owns the reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DECLARATION_FILES: tuple[str, ...] = (
    "src/Core/Settings.cpp",
    "src/Storages/MergeTree/MergeTreeSettings.cpp",
    "src/Core/ServerSettings.cpp",
    "src/Core/FormatFactorySettings.h",
)
HISTORY_FILE = "src/Core/SettingsChangesHistory.cpp"
RANDOMIZER_FILE = "tests/clickhouse-test"

TIERS = ("production", "important", "beta", "experimental", "obsolete")

_VERSION_HEADER_RE = re.compile(r'addSettingsChanges\(\s*settings_changes_history,\s*"(\d+\.\d+)"')
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or "")) and len(name) <= 128


@dataclass
class Declaration:
    file: str = ""
    line: int = 0
    type: str = ""
    default: str = ""
    description: str = ""
    tier: str = ""
    # for a primary: the alias it also answers to; for an alias: the primary
    alias: str = ""
    is_alias: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0, False)}


@dataclass
class HistoryEntry:
    version: str
    old: str
    new: str
    reason: str
    line: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "old": self.old,
            "new": self.new,
            "reason": self.reason[:200],
            "line": self.line,
        }


@dataclass
class Randomization:
    randomized: bool = False
    file: str = ""
    line: int = 0
    sampler: str = ""
    randomizer: str = ""  # SettingsRandomizer | MergeTreeSettingsRandomizer

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"randomized": self.randomized}
        if self.randomized:
            out.update(
                {
                    "file": self.file,
                    "line": self.line,
                    "sampler": self.sampler[:160],
                    "randomizer": self.randomizer,
                }
            )
        return out


@dataclass
class Facts:
    name: str
    declaration: Declaration | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    randomization: Randomization = field(default_factory=Randomization)
    tests_total: int = 0
    # known | not_a_setting | unknown
    verdict: str = "unknown"
    causes: list[str] = field(default_factory=list)


def _first_sentence(text: str, *, limit: int = 240) -> str:
    t = " ".join((text or "").split())
    if not t:
        return ""
    for stop in (". ", "\n"):
        i = t.find(stop)
        if 0 < i < limit:
            return t[: i + 1].strip()
    return (t[:limit].rstrip() + ("…" if len(t) > limit else "")).strip()


def tier_of(flags: str) -> str:
    f = (flags or "").strip().upper()
    if "EXPERIMENTAL" in f:
        return "experimental"
    if "BETA" in f:
        return "beta"
    if "IMPORTANT" in f:
        return "important"
    return "production"


# Either description form: a raw string `R"(...)"` (most settings) or a plain C string
# literal `"..."` (a few dozen; of the 248 CI-randomized settings, three would be missed
# by a raw-string-only parser).
_DESC = r'(?:R"\((?P<desc>.*?)\)"|"(?P<desc_plain>(?:[^"\\]|\\.)*)")'
# A default is a quoted string (commas allowed inside: `"basic, uniq_v2"`) or
# an expression without a top-level comma (`2 * DBMS_DEFAULT_BUFFER_SIZE`).
_DEFAULT = r'"(?:[^"\\]|\\.)*"|[^,]+?'


def _first_desc(m: re.Match[str]) -> str:
    return _first_sentence(m.group("desc") or m.group("desc_plain") or "")


def parse_declaration(
    content: str, name: str, *, path: str, chunk_start: int
) -> Declaration | None:
    """The `DECLARE(...)` (or `DECLARE_WITH_ALIAS`) of `name` inside one
    chunk's content; None when the chunk does not declare it. The
    description may run past the chunk — what is present is summarised.
    `name` may also be the ALIAS of a `DECLARE_WITH_ALIAS` (its last
    argument): then the declaration found is the primary's, and `alias`
    names the primary the alias stands for."""
    n = re.escape(name)
    # the raw-string description may run past the chunk (Settings.cpp
    # descriptions are paragraphs): it ends at `)"` or at the end of the text,
    # and the flags / alias tail is then optional
    pat = re.compile(
        r"DECLARE(?P<alias>_WITH_ALIAS)?\(\s*(?P<type>\w+)\s*,\s*"
        + n
        + r"\s*,\s*(?P<default>"
        + _DEFAULT
        + r")\s*,\s*"
        + r'(?:R"\((?P<desc>.*?)(?:\)"|\Z)|"(?P<desc_plain>(?:[^"\\]|\\.)*)")'
        + r"(?:\s*,\s*(?P<flags>[^,\)]+?)(?:\s*,\s*(?P<aliasname>\w+))?\s*\))?",
        re.DOTALL,
    )
    m = pat.search(content or "")
    if m:
        line = chunk_start + (content or "")[: m.start()].count("\n")
        return Declaration(
            file=path,
            line=line,
            type=m.group("type"),
            default=m.group("default").strip(),
            description=_first_desc(m),
            tier=tier_of(m.group("flags") or "0"),
            alias=(m.group("aliasname") or "").strip(),
        )
    # the alias form: `DECLARE_WITH_ALIAS(Type, primary, default, desc, flags, name)`;
    # the head of the macro may sit in the previous chunk, so match from the
    # description onward and take what is present
    alias_pat = re.compile(
        r"(?:DECLARE_WITH_ALIAS\(\s*(?P<type>\w+)\s*,\s*(?P<primary>\w+)\s*,\s*"
        r"(?P<default>"
        + _DEFAULT
        + r")\s*,\s*)?"
        + _DESC
        + r"\s*,\s*(?P<flags>[^,\)]+?)\s*,\s*"
        + n
        + r"\s*\)",
        re.DOTALL,
    )
    m = alias_pat.search(content or "")
    if not m:
        return None
    line = chunk_start + (content or "")[: m.start()].count("\n")
    return Declaration(
        file=path,
        line=line,
        type=(m.group("type") or "").strip(),
        default=(m.group("default") or "").strip(),
        description=_first_desc(m),
        tier=tier_of(m.group("flags") or "0"),
        alias=(m.group("primary") or "").strip(),
        is_alias=True,
    )


def parse_obsolete(content: str, name: str, *, path: str, chunk_start: int) -> Declaration | None:
    """`MAKE_OBSOLETE(M, Type, name, value)` — the obsolete tier."""
    m = re.search(
        r"MAKE_OBSOLETE\(\s*M\s*,\s*(?P<type>\w+)\s*,\s*"
        + re.escape(name)
        + r"\s*,\s*(?P<v>[^)]+)\)",
        content or "",
    )
    if not m:
        return None
    line = chunk_start + (content or "")[: m.start()].count("\n")
    return Declaration(
        file=path,
        line=line,
        type=m.group("type"),
        default=m.group("v").strip(),
        description="Obsolete: kept for compatibility, has no effect.",
        tier="obsolete",
    )


def parse_history(chunks: list[dict[str, Any]], name: str) -> list[HistoryEntry]:
    """Every change entry of `name` with the release it belongs to. Chunks
    arrive in line order; the current release carries across chunk
    boundaries. Newest release first."""
    entry_re = re.compile(
        r'\{\s*"' + re.escape(name) + r'"\s*,\s*(?P<old>[^,]+?)\s*,\s*(?P<new>[^,]+?)\s*,\s*'
        r'"(?P<reason>(?:[^"\\]|\\.)*)"\s*\}'
    )
    version = ""
    out: list[HistoryEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for ch in sorted(chunks, key=lambda c: int(c.get("start_line") or 0)):
        content = str(ch.get("content") or "")
        start = int(ch.get("start_line") or 0)
        # walk headers and entries in document order
        events: list[tuple[int, str, Any]] = []
        for hm in _VERSION_HEADER_RE.finditer(content):
            events.append((hm.start(), "v", hm.group(1)))
        for em in entry_re.finditer(content):
            events.append((em.start(), "e", em))
        for pos, kind, payload in sorted(events, key=lambda e: e[0]):
            if kind == "v":
                version = str(payload)
                continue
            em = payload
            key = (version, em.group("old").strip(), em.group("new").strip())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                HistoryEntry(
                    version=version,
                    old=em.group("old").strip(),
                    new=em.group("new").strip(),
                    reason=em.group("reason").replace('\\"', '"'),
                    line=start + content[:pos].count("\n"),
                )
            )
    out.sort(key=lambda e: _version_key(e.version), reverse=True)
    return out


def _version_key(v: str) -> tuple[int, int]:
    try:
        a, b = v.split(".")[:2]
        return int(a), int(b)
    except (ValueError, AttributeError):
        return (0, 0)


def parse_randomization(
    rows: list[dict[str, Any]], name: str, *, path: str = RANDOMIZER_FILE
) -> Randomization:
    """From the `chunk_contains` excerpts of `tests/clickhouse-test` for
    `"name":` — the sampler expression that follows it. The randomizer
    class is inferred from the chunk's scope when it names one."""
    needle = f'"{name}":'
    for r in rows:
        excerpt = str(r.get("excerpt") or r.get("content") or "")
        i = excerpt.find(needle)
        if i < 0:
            continue
        tail = excerpt[i + len(needle) :]
        # the sampler runs to the end of the dict entry: a top-level comma
        # or the next key; keep it short and one-line
        sampler = " ".join(tail.split())
        cut = _sampler_end(sampler)
        sampler = sampler[:cut].strip().rstrip(",")
        scope = str(r.get("scope") or "")
        which = (
            "MergeTreeSettingsRandomizer"
            if "MergeTree" in scope
            else ("SettingsRandomizer" if "Randomizer" in scope else "")
        )
        return Randomization(
            randomized=True,
            file=path,
            line=int(r.get("start_line") or 0),
            sampler=sampler,
            randomizer=which,
        )
    return Randomization(randomized=False)


def _sampler_end(text: str) -> int:
    """Index where a dict value ends: the first top-level comma."""
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif ch == "," and depth == 0:
            return i
    return len(text)


def verdict_of(facts: Facts, *, control_ok: bool) -> tuple[str, list[str]]:
    """`known` when the tree declares the setting; `not_a_setting` when the
    positive control found its own declaration and this name has none;
    `unknown` when the control failed (the declaration files are not in the
    index or the read fell over)."""
    if not control_ok:
        return "unknown", ["control_failed"]
    if facts.declaration is None:
        return "not_a_setting", ["no_declaration"]
    return "known", []


__all__ = [
    "DECLARATION_FILES",
    "HISTORY_FILE",
    "RANDOMIZER_FILE",
    "TIERS",
    "Declaration",
    "Facts",
    "HistoryEntry",
    "Randomization",
    "parse_declaration",
    "parse_history",
    "parse_obsolete",
    "parse_randomization",
    "tier_of",
    "valid_name",
    "verdict_of",
]
