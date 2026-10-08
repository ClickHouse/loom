"""Constrained and scoped code.search / code.grep.

A pattern narrows the pool BEFORE the legs rank it: `position(content, pat)` /
`positionCaseInsensitive` / `match(content, re)` and the path filters (rg-style globs → RE2 for
`multiMatchAny(path, ...)`, extension sets via `endsWith`) go in the plain WHERE of both legs, next
to the identity keys — PREWHERE is banned in the code plane, and a text skip index over tokens does
not help a substring predicate; the n-gram index over `lower(content)` does, when the statement
carries the `LIKE` guard `grep_plan.content_predicate` emits.

Bytes consequence, stated once here: a whole-namespace content predicate
reads every granule's `content` under the identity prefix (on the order
of the lexemes the lexical leg already reads), and the dense leg then
reads `embedding` only for the granules that hold a match. Selective
patterns (a few percent of files) cut the vector bytes to a few granules;
a common pattern ADDS a content scan to the vector leg and buys nothing,
so the pool is capped by matched-file count (`code_search_pattern_max_files`)
and an over-cap pattern is refused with the count, never scanned.

The glob converter is deliberately small: `**`, `*`, `?`, `[...]`,
`{a,b}`; a glob without `/` matches a basename anywhere in the tree, a
glob with `/` anchors at the path root (rg's `-g` semantics). Everything
else is escaped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.index.grep_plan import content_predicate
from server.core.errors import ValidationError

MAX_GLOBS = 8
MAX_LANGS = 8
PATTERN_MIN = 2
PATTERN_MAX = 500

# Extension sets behind `langs` (rg `-t` names where rg has one).
LANG_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "cpp": (".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh", ".hxx", ".inl", ".ipp", ".cu"),
    "c": (".c", ".h"),
    "python": (".py", ".pyi"),
    "py": (".py", ".pyi"),
    "js": (".js", ".mjs", ".cjs", ".jsx"),
    "ts": (".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "kotlin": (".kt", ".kts"),
    "sql": (".sql",),
    "sh": (".sh", ".bash", ".zsh"),
    "md": (".md", ".markdown"),
    "yaml": (".yaml", ".yml"),
    "json": (".json",),
    "toml": (".toml",),
    "proto": (".proto",),
    "cmake": (".cmake", "CMakeLists.txt"),
    "xml": (".xml",),
    "html": (".html", ".htm"),
    "css": (".css", ".scss"),
    "txt": (".txt",),
}


def glob_to_re2(glob: str) -> str:
    """rg-style glob → anchored RE2 for `multiMatchAny(path, ...)`.

    `**/` any directory run (or none); `/**` everything below; `*` within
    one component; `?` one non-slash char; `[...]` a class (`[!x]` → `[^x]`);
    `{a,b}` alternation. A glob without a `/` (after a leading `**/`) matches
    the basename anywhere: `*.cpp` → `(?:^|/)[^/]*\\.cpp$`. A glob with a
    `/` anchors at the root: `src/Parsers/*` → `^src/Parsers/[^/]*$`."""
    g = glob.strip()
    if not g:
        raise ValidationError("empty glob")
    if len(g) > 200:
        raise ValidationError("glob longer than 200 chars")
    anchored_root = g.startswith("/")
    g = g.lstrip("/")
    leading_any_dir = g.startswith("**/")
    body = g[3:] if leading_any_dir else g
    if not body or body == "**":
        # "**", "**/" or "/" : everything
        return "^.*$"
    slash_inside = "/" in body
    out: list[str] = []
    i = 0
    n = len(body)
    depth_brace = 0
    while i < n:
        c = body[i]
        if c == "*":
            if body.startswith("**", i):
                j = i + 2
                if j < n and body[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = body.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                cls = body[i + 1 : j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                # a class may contain only its own safe characters
                cls = cls.replace("\\", "\\\\")
                out.append("[" + cls + "]")
                i = j
        elif c == "{":
            depth_brace += 1
            out.append("(?:")
        elif c == "}" and depth_brace:
            depth_brace -= 1
            out.append(")")
        elif c == "," and depth_brace:
            out.append("|")
        else:
            out.append(re.escape(c))
        i += 1
    if depth_brace:
        raise ValidationError(f"unbalanced braces in glob {glob!r}")
    core = "".join(out)
    if leading_any_dir:
        return "^(?:.*/)?" + core + "$"
    if slash_inside or anchored_root:
        return "^" + core + "$"
    return "(?:^|/)" + core + "$"


def extensions_for(langs: list[str]) -> list[str]:
    """Union of the extension sets behind `langs`; unknown names refused
    with the known list so an agent can correct itself in one round."""
    exts: list[str] = []
    for raw in langs:
        name = raw.strip().lower().lstrip(".")
        if not name:
            continue
        if name not in LANG_EXTENSIONS:
            raise ValidationError(
                f"unknown lang {raw!r}; known: {', '.join(sorted(LANG_EXTENSIONS))}"
            )
        for e in LANG_EXTENSIONS[name]:
            if e not in exts:
                exts.append(e)
    return exts


@dataclass(frozen=True)
class SearchScope:
    """Everything that narrows a leg's pool before ranking. `empty()` is the
    unconstrained default and adds no SQL; the legs' statements are then byte for byte
    the unscoped ones."""

    pattern: str = ""
    regex: bool = False
    case_sensitive: bool = False
    path_globs_re: tuple[str, ...] = ()
    exclude_globs_re: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    # the request's own spellings, for explanations and memo keys
    path_globs: tuple[str, ...] = field(default=(), compare=False)
    exclude_globs: tuple[str, ...] = field(default=(), compare=False)
    langs: tuple[str, ...] = field(default=(), compare=False)

    @property
    def has_pattern(self) -> bool:
        return bool(self.pattern)

    @property
    def has_path_filters(self) -> bool:
        return bool(self.path_globs_re or self.exclude_globs_re or self.extensions)

    @property
    def constrained(self) -> bool:
        return self.has_pattern or self.has_path_filters

    def explain(self) -> str:
        """`constrained=pattern|globs|excludes|langs` for score_explanation."""
        parts: list[str] = []
        if self.has_pattern:
            parts.append("regex" if self.regex else "pattern")
        if self.path_globs_re:
            parts.append(f"globs={len(self.path_globs_re)}")
        if self.exclude_globs_re:
            parts.append(f"excludes={len(self.exclude_globs_re)}")
        if self.extensions:
            parts.append(f"langs={len(self.extensions)}ext")
        return "constrained=" + "|".join(parts) if parts else ""

    def content_condition(self) -> tuple[str, dict[str, Any]]:
        """(SQL fragment without a leading AND, params) for the pattern, or
        ("", {}). Same three exact shapes as code.grep's condition, each
        preceded by the index-visible `lower(content) LIKE` guard the ngram
        skip index prunes on (`grep_plan.content_predicate`)."""
        if not self.has_pattern:
            return "", {}
        return content_predicate(
            self.pattern,
            regex=self.regex,
            case_sensitive=self.case_sensitive,
            param="sc_pat",
            prefix="sc",
        )

    def path_conditions(self) -> tuple[list[str], dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        if self.path_globs_re:
            conds.append("multiMatchAny(path, {sc_globs:Array(String)})")
            params["sc_globs"] = list(self.path_globs_re)
        if self.exclude_globs_re:
            conds.append("NOT multiMatchAny(path, {sc_excl:Array(String)})")
            params["sc_excl"] = list(self.exclude_globs_re)
        if self.extensions:
            conds.append("arrayExists(e -> endsWith(path, e), {sc_exts:Array(String)})")
            params["sc_exts"] = list(self.extensions)
        return conds, params

    def where_sql(self) -> tuple[str, dict[str, Any]]:
        """`AND ... AND ...` fragment (empty string when unconstrained) plus
        its params. Goes in the plain WHERE next to the identity keys."""
        content, params = self.content_condition()
        path_conds, path_params = self.path_conditions()
        params.update(path_params)
        conds = ([content] if content else []) + path_conds
        if not conds:
            return "", {}
        return " ".join("AND " + c for c in conds), params


_EMPTY = SearchScope()


def build_scope(
    *,
    pattern: str = "",
    regex: bool = False,
    case_sensitive: bool = False,
    path_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    langs: list[str] | None = None,
) -> SearchScope:
    """Validate the request fields into a SearchScope; ValidationError (422)
    names the field so the agent can correct itself in one round."""
    pat = pattern or ""
    if pat and not (PATTERN_MIN <= len(pat) <= PATTERN_MAX):
        raise ValidationError(f"pattern must be {PATTERN_MIN}-{PATTERN_MAX} chars")
    if pat and regex:
        try:
            re.compile(pat)
        except re.error as e:
            raise ValidationError(f"pattern is not a valid regex: {e}") from e
    globs = [g for g in (path_globs or []) if g and g.strip()]
    excl = [g for g in (exclude_globs or []) if g and g.strip()]
    if len(globs) > MAX_GLOBS:
        raise ValidationError(f"path_globs: at most {MAX_GLOBS}")
    if len(excl) > MAX_GLOBS:
        raise ValidationError(f"exclude_globs: at most {MAX_GLOBS}")
    lang_list = [x for x in (langs or []) if x and x.strip()]
    if len(lang_list) > MAX_LANGS:
        raise ValidationError(f"langs: at most {MAX_LANGS}")
    if not pat and not globs and not excl and not lang_list:
        return _EMPTY
    return SearchScope(
        pattern=pat,
        regex=bool(regex),
        case_sensitive=bool(case_sensitive),
        path_globs_re=tuple(glob_to_re2(g) for g in globs),
        exclude_globs_re=tuple(glob_to_re2(g) for g in excl),
        extensions=tuple(extensions_for(lang_list)),
        path_globs=tuple(g.strip() for g in globs),
        exclude_globs=tuple(g.strip() for g in excl),
        langs=tuple(x.strip().lower() for x in lang_list),
    )
