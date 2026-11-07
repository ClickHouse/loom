"""The `files[]` facet a `memory.set` stores.

An explicit rule written with `memory.set` ("don't change the bucket count
in GraceHashJoin.cpp without the join benchmark") reaches `memory.for_file`
through it. The caller may name the files; when it does not, they are
detected from the value text with the extractor's own detector
(`code_facets.extract_code_facets`), URLs removed first. A rewrite that
names none and detects none keeps the row's files.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from server.code.domain.facets import extract_code_facets

MAX_FILES = 32
MAX_FILE_CHARS = 1024

# A URL's path segments look like repo paths (`github.com/o/r/blob/x.py`);
# they are removed before detection, never attached.
_URL_RE = re.compile(r"(?:[a-z][a-z0-9+.-]{0,31}://|www\.)\S+", re.IGNORECASE)
# Library names that read as a bare `<name>.js` file in prose.
_LIBRARY_NAMES = frozenset(
    {
        "node.js",
        "vue.js",
        "next.js",
        "nuxt.js",
        "express.js",
        "three.js",
        "d3.js",
        "chart.js",
        "react.js",
        "angular.js",
        "ember.js",
        "backbone.js",
        "deno.js",
    }
)


def normalize_file_ref(raw: Any) -> str:
    """One stored file reference: trimmed, forward slashes, no leading `./`,
    at most MAX_FILE_CHARS. '' for a value that is no reference."""
    s = str(raw or "").strip().replace("\\", "/")
    # step past every `./` before one slice: a slice per prefix copies the rest each time
    i = 0
    while s.startswith("./", i):
        i += 2
    return s[i : i + MAX_FILE_CHARS].strip()


def normalize_files(values: Iterable[Any], cap: int = MAX_FILES) -> list[str]:
    """Normalised, deduplicated (case-insensitive, first spelling kept) and
    capped at `cap`."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        s = normalize_file_ref(v)
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
        if len(out) >= cap:
            break
    return out


def detect_value_files(text: str) -> list[str]:
    """Repo-path-shaped tokens in a value: a path with a directory separator
    and a known source extension, or a bare filename with one
    (`GraceHashJoin.cpp`). URLs, version numbers and `node.js`-style library
    names are not files."""
    if not text:
        return []
    files, _concepts = extract_code_facets(_URL_RE.sub(" ", str(text)))
    return normalize_files(f for f in files if f.lower() not in _LIBRARY_NAMES)


def files_for_set(
    explicit: list[str] | None, value: Any, serialized: str, existing: Any
) -> list[str]:
    """The `files` a memory.set row stores: the caller's list when given
    (`[]` clears), else the files detected in the value, else the existing
    row's (a restatement keeps its links)."""
    if explicit is not None:
        return normalize_files(explicit)
    detected = detect_value_files(value if isinstance(value, str) else serialized)
    if detected:
        return detected
    return list(getattr(existing, "files", None) or []) if existing is not None else []
