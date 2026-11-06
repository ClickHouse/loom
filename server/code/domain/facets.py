"""Deterministic code-native facet extraction.

extract_code_facets(text) -> (files, concepts). LLM-free, pure stdlib regex
heuristics. The atomic fact value is NEVER coarsened — this only populates
additive side-channel arrays. Non-coding text yields ([], []). The line suffix
is dropped from stored file paths so "what touches X" matches regardless of
line; the query side shares this same parsing.
"""

from __future__ import annotations

import re

# A path-ish token: at least one "/" separating segments, ending in a
# recognized source/config extension, with an optional ":<line>" suffix.
# Anchored to non-path boundaries so prose like "and/or" never matches.
# Extension list is closed-class (the languages a coding agent edits) so
# arbitrary "a/b" fragments don't become files.
_CODE_FILE_EXTS = (
    "py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|kts|c|h|cc|cpp|hpp|cs|"
    "php|swift|scala|clj|ex|exs|erl|sh|bash|zsh|sql|proto|graphql|gql|"
    "json|yaml|yml|toml|ini|cfg|conf|env|md|rst|txt|html|css|scss|less|"
    "vue|svelte|dart|lua|r|jl|tf|tfvars|dockerfile|makefile|gradle|xml"
)
_FILE_PATH_RE = re.compile(
    r"(?<![\w./@-])"  # left boundary: not mid-token
    r"((?:[\w.@-]+/)+[\w.@-]+\.(?:" + _CODE_FILE_EXTS + r"))"
    r"(?::\d+(?::\d+)?)?"  # optional :line or :line:col
    r"(?![\w])",
    re.IGNORECASE,
)
# A bare dotted module path with a code extension and no slash
# ("settings.py", "Dockerfile" handled separately) — only when the stem
# is plausibly a filename, not a sentence ending. Requires the extension
# to be one of the closed-class code extensions.
_BARE_FILE_RE = re.compile(
    r"(?<![\w./@-])([\w@-]+\.(?:" + _CODE_FILE_EXTS + r"))(?::\d+(?::\d+)?)?(?![\w])",
    re.IGNORECASE,
)
# Extensionless conventional filenames coding agents touch constantly.
_BARE_NAMED_FILES = frozenset(
    {
        "dockerfile",
        "makefile",
        "rakefile",
        "gemfile",
        "procfile",
        "vagrantfile",
        "brewfile",
        "justfile",
        ".gitignore",
        ".dockerignore",
        ".env",
    }
)

# Error / exception token: a CamelCase identifier ending in a recognized
# failure suffix ("NullPointerException", "KeyError", "ValueError",
# "ECONNREFUSED"-style screaming-snake codes). High precision: these are
# almost always real error types in a coding transcript.
_ERROR_TOKEN_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning|Fault|Failure|"
    r"Timeout|Panic|Overflow|Underflow))\b"
)
_SCREAMING_ERR_RE = re.compile(r"\b(E[A-Z]{3,}|[A-Z]{2,}_[A-Z_]{2,})\b")
# A dotted symbol path ("psycopg2.connect", "np.array", "os.environ").
_DOTTED_SYMBOL_RE = re.compile(r"\b([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+)\b")
# A function-call surface ("useEffect(", "get_client(", "render()").
_FUNC_CALL_RE = re.compile(r"\b([a-zA-Z_][\w]{2,})\s*\(")
# Backtick-quoted inline code spans — strong code-token signal.
_BACKTICK_RE = re.compile(r"`([^`]{1,80})`")
# A snake_case or camelCase identifier worth indexing as a concept when
# it appears inside a backtick span (we don't grab every word from prose,
# only from explicit code spans / the high-signal regexes above).
_IDENT_RE = re.compile(r"^[a-zA-Z_][\w]*$")
# A git commit SHA: 7-40 hex chars. Captured as a `commit_sha` concept and
# (in the graph path) a commit node so session↔commit linkage is queryable.
_COMMIT_SHA_RE = re.compile(r"(?<![\w])([0-9a-f]{7,40})(?![\w])", re.IGNORECASE)

_MAX_FACET_ITEMS = 12
# Dotted tokens whose every segment is one letter are abbreviations in prose ("U.S.A",
# "D.C", "Ph.D" once lowered), not symbols: `os.path` and `np.array` have a multi-letter
# segment, "d.c" does not.
_ABBREVIATION_RE = re.compile(r"^(?:[a-zA-Z]\.)+[a-zA-Z]\.?$")
# Words that look like dotted symbols but are sentence prose ("e.g.",
# "i.e.", "etc.") or version-ish numbers — excluded from concepts.
_DOTTED_STOPWORDS = frozenset({"e.g", "i.e", "etc", "vs", "a.m", "p.m"})


def _is_abbreviation(token: str) -> bool:
    """True for dotted single-letter runs ("U.S.A", "d.c") and for a bare
    "<letter>.<ext>" that only looks like a file because "c" is a C source
    extension — a one-letter stem is never a filename in practice."""
    t = token.strip().rstrip(".")
    if _ABBREVIATION_RE.match(t + "."):
        return True
    stem = t.split(".")[0]
    return len(stem) <= 1


def has_code_signal(text: str) -> bool:
    """Whether a text surface is code-shaped enough to mine prose for symbols and calls:
    a path with a slash, a backtick span, an error token, or a screaming-snake constant.
    Plain sentences have none of these, and mining them yields "u.s.a"-style noise hung
    on the agent."""
    if not text:
        return False
    return bool(
        _FILE_PATH_RE.search(text)
        or _BACKTICK_RE.search(text)
        or _ERROR_TOKEN_RE.search(text)
        or _SCREAMING_ERR_RE.search(text)
    )


def _strip_line_suffix(path: str) -> str:
    """Drop a trailing ':<line>' / ':<line>:<col>' from a file path so the
    stored facet is the path itself (line-agnostic), matching how a
    'what touches this file' query would phrase it."""
    m = re.match(r"^(.*?)(?::\d+(?::\d+)?)?$", path)
    return m.group(1) if m else path


def _looks_like_commit_sha(token: str, text: str) -> bool:
    """A 7-40 hex run is only a commit SHA when the surrounding prose says
    so — otherwise hex-looking tokens (ids, hashes, colors) over-match.
    Closed-class: require a commit/sha/revision cue near the token."""
    if not (7 <= len(token) <= 40):
        return False
    # Pure-decimal runs are never SHAs (years, counts).
    if token.isdigit():
        return False
    lowered = text.lower()
    return any(
        cue in lowered
        for cue in ("commit", "sha", "revision", "rev ", "git ", "cherry-pick", "hash")
    )


def extract_code_facets(text: str) -> tuple[list[str], list[str]]:
    """Deterministically pull (files, concepts) out of one text surface.

    LLM-free. Returns ([], []) for non-code prose. Order-preserving,
    deduped, capped at _MAX_FACET_ITEMS each. The fact value is never
    modified — these are additive side-channels.
    """
    if not text or not text.strip():
        return ([], [])

    files: list[str] = []
    seen_files: set[str] = set()

    code_signal = has_code_signal(text)

    def _add_file(raw: str) -> None:
        path = _strip_line_suffix(raw).strip()
        if not path or ("/" not in path and _is_abbreviation(path)):
            return
        key = path.lower()
        if key in seen_files:
            return
        seen_files.add(key)
        if len(files) < _MAX_FACET_ITEMS:
            files.append(path)

    for m in _FILE_PATH_RE.finditer(text):
        _add_file(m.group(0))
    for m in _BARE_FILE_RE.finditer(text):
        _add_file(m.group(0))
    # Extensionless conventional filenames (Dockerfile, Makefile, .env).
    for tok in re.findall(r"(?<![\w./-])([.\w-]+)(?![\w/-])", text):
        if tok.lower() in _BARE_NAMED_FILES:
            _add_file(tok)

    concepts: list[str] = []
    seen_concepts: set[str] = set()

    def _add_concept(raw: str, *, from_prose: bool = False) -> None:
        c = raw.strip().strip("`.,;:()[]{}").strip()
        if not c or len(c) < 2 or len(c) > 80:
            return
        if c.lower() in _DOTTED_STOPWORDS or ("." in c and _is_abbreviation(c)):
            return
        # A symbol or call found in plain prose counts only when the surface
        # is code-shaped elsewhere; "Washington, D.C." is not a symbol.
        if from_prose and not code_signal:
            return
        # Don't double-index a file path as a concept.
        if c.lower() in seen_files or "/" in c:
            return
        key = c.lower()
        if key in seen_concepts:
            return
        seen_concepts.add(key)
        if len(concepts) < _MAX_FACET_ITEMS:
            concepts.append(c)

    # Error / exception tokens first (highest signal).
    for m in _ERROR_TOKEN_RE.finditer(text):
        _add_concept(m.group(1))
    for m in _SCREAMING_ERR_RE.finditer(text):
        _add_concept(m.group(1))
    # Backtick code spans: index the whole span when it's a single ident,
    # else mine identifiers / calls / dotted symbols out of it.
    for m in _BACKTICK_RE.finditer(text):
        span = m.group(1).strip()
        if _IDENT_RE.match(span):
            _add_concept(span)
            continue
        for cm in _DOTTED_SYMBOL_RE.finditer(span):
            _add_concept(cm.group(1))
        for fm in _FUNC_CALL_RE.finditer(span):
            _add_concept(fm.group(1))
    # Function calls + dotted symbols in the surrounding prose — only when the surface
    # carries a code signal.
    for m in _FUNC_CALL_RE.finditer(text):
        _add_concept(m.group(1), from_prose=True)
    for m in _DOTTED_SYMBOL_RE.finditer(text):
        sym = m.group(1)
        # A dotted symbol whose tail is a code extension is a file, not a
        # concept — already captured above; skip.
        if _BARE_FILE_RE.fullmatch(sym):
            continue
        _add_concept(sym, from_prose=True)
    # Commit SHA (gated on a nearby commit/sha cue).
    for m in _COMMIT_SHA_RE.finditer(text):
        if _looks_like_commit_sha(m.group(1), text):
            _add_concept(f"commit:{m.group(1).lower()}")

    return (files, concepts)
