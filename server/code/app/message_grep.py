"""code.grep(mode="message"): a printed message finds the statement that printed it.

An agent greps what the server PRINTED; the source holds the TEMPLATE (`"Cannot convert
string {} to type {}"`), so a literal grep of the rendered text returns nothing, and
agents grep rendered messages often. The mechanism: one template per statement,
constants kept and arguments made wildcards, candidates retrieved on the constant tokens
and verified in rank order against the message. Here:

1. `split_message` cuts the rendered text into literal spans at likely
   argument boundaries — numbers, quoted values, identifiers carrying `.`,
   `::`, `(`, `/`, `<`, `=`, runs of two or more spaces (an argument the
   caller already stripped), `...`, a leading `Code: N.` / `DB::Exception:`
   / `Received from host.` chain and a trailing `(ERROR_NAME)`. Spans of
   three or more words (or twelve or more characters) are the retrieval
   keys; the template is the text with every boundary replaced by `{}`.
2. The repository matches the spans' three-word shingles with
   `multiSearchAnyCaseInsensitive` over chunk content restricted to chunks
   that carry a throw / `Exception(` / `onError` / `ErrorCodes::` / `LOG_`
   (plain WHERE), ranked by shingles found.
3. `rank_statements` walks each chunk for statement windows opening on one
   of those keywords, joins the adjacent string literals of the window
   into the statement's own template, and verifies that template against
   the message: a character-weighted longest common subsequence over words,
   then the regex-verify step — every unmatched run of message words must
   sit against a `{}` of the template (an argument) and no template literal
   may be skipped at the head or in the middle (a truncated message may
   stop early; a suffix paste may start late). Coverage = matched message
   characters / message characters; `raised_at` needs a verified alignment
   at 0.5 or better with at least three words.
4. `Code: N` maps to its `ErrorCodes` name through the indexed
   `ErrorCodes.cpp` (`M(N, NAME)` lines, cached ten minutes); a statement
   that names that code outranks one that does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.app.ttl_cache import TtlCache

# Chunk-level pre-filter: a raising or logging statement carries one of these.
STATEMENT_KEYWORDS: tuple[str, ...] = ("throw", "Exception(", "onError", "ErrorCodes::", "LOG_")
# Statement windows are read at most this many lines past the opening line.
WINDOW_LINES = 8
# A span is a retrieval key at three words or twelve characters.
SPAN_MIN_WORDS = 3
SPAN_MIN_CHARS = 12
SHINGLE_WORDS = 3
MAX_NEEDLES = 24
# raised_at: at least half the message's characters aligned, three words up —
# or six aligned words at a third of the characters, for a message whose
# arguments are long identifiers (`PartialSortingTransform and
# LazyOutputFormat`) that dilute the character share of a full template.
RAISED_AT_COVERAGE = 0.5
RAISED_AT_WORDS = 3
RAISED_AT_LONG_COVERAGE = 0.35
RAISED_AT_LONG_WORDS = 6
ERROR_CODE_BONUS = 0.15
# Where the repository keeps its error-code table (`M(N, NAME)` lines).
ERROR_CODES_PATHS: tuple[str, ...] = ("src/Common/ErrorCodes.cpp",)
ERROR_CODES_TTL_S = 600.0

_BOUNDARY = "\x00"

_CODE_PREFIX_RE = re.compile(r"\bCode\s*:\s*(\d*)\s*\.?\s*")
_DB_EXCEPTION_RE = re.compile(r"\b(?:DB::)?Exception:\s*")
_RECEIVED_FROM_RE = re.compile(r"\bReceived from \S+?\.\s+")
_VERSION_RE = re.compile(r"\s*\(version [^)]*\)")
_TRAILING_CODE_NAME_RE = re.compile(r"\s*\(([A-Z][A-Z0-9_]{2,})\)\s*\.?\s*$")
_CONTEXT_CUT_RE = re.compile(
    r"[:.]?\s*\((?:while|While)\b|:\s*(?:while|While) (?:executing|processing|reading)\b"
)
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
_ELLIPSIS_RE = re.compile(r"\.\.\.|…")
_NUMBER_RE = re.compile(r"(?<![\w.])[+-]?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)*)(?![\w])")
_MULTISPACE_RE = re.compile(r" {2,}|\t+")
_TOKEN_RE = re.compile(r"\S+")
_ARG_TOKEN_RE = re.compile(r"::|[()<>=\[\]{}/\\]|\w\.\w")
_EDGE_PUNCT = " \t\n.,;:!?"

_STOPWORDS = frozenset(
    "a an the is are was were be been not no and or of to in on at for with by from as "
    "that this these those it its if then than so do does did has have had can cannot "
    "could should would will may might must there here which who what when where".split()
)


def _all_stopwords(shingle: str) -> bool:
    return all(w.strip(_WORD_STRIP) in _STOPWORDS for w in shingle.split())


_STRING_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_ERROR_CODE_LINE_RE = re.compile(r"\bM\(\s*(\d+)\s*,\s*([A-Z][A-Z0-9_]*)\s*\)")
_WORD_STRIP = ".,;:!?'\"`()[]{}<>"


@dataclass(frozen=True)
class ParsedMessage:
    spans: tuple[str, ...]
    template: str
    words: tuple[str, ...]  # comparison words of the message, in order
    error_code: int | None = None
    error_name: str = ""

    @property
    def needles(self) -> list[str]:
        """Retrieval keys: three-word shingles of each span (whole span when
        shorter), lower-cased, capped at MAX_NEEDLES, longest first. A
        shingle made only of stopwords ("that is not") admits half the
        corpus and is dropped when the message has a better one."""
        out: list[str] = []
        for span in self.spans:
            words = span.split()
            if len(words) <= SHINGLE_WORDS:
                out.append(span.lower())
                continue
            out.extend(
                " ".join(words[i : i + SHINGLE_WORDS]).lower()
                for i in range(len(words) - SHINGLE_WORDS + 1)
            )
        specific = [sh for sh in out if not _all_stopwords(sh)]
        uniq = list(dict.fromkeys(n for n in (specific or out) if n))
        uniq.sort(key=len, reverse=True)
        return uniq[:MAX_NEEDLES]

    @property
    def total_chars(self) -> int:
        return sum(len(w) for w in self.words)


def _strip_edges(s: str) -> str:
    return s.strip(_EDGE_PUNCT)


def _compare_word(tok: str) -> str:
    return tok.strip(_WORD_STRIP).lower()


def split_message(text: str) -> ParsedMessage:
    """Rendered message → literal spans, a `{}` template, and the error code
    when the text carried `Code: N` or a trailing `(ERROR_NAME)`."""
    raw = " ".join(text.strip().split("\n"))
    error_code: int | None = None
    error_name = ""
    m = _CODE_PREFIX_RE.search(raw)
    if m is not None:
        if m.group(1):
            error_code = int(m.group(1))
        raw = raw[: m.start()] + " " + raw[m.end() :]
    raw = _RECEIVED_FROM_RE.sub(" ", raw)
    raw = _DB_EXCEPTION_RE.sub(" ", raw)
    raw = _VERSION_RE.sub(" ", raw).strip()
    m = _TRAILING_CODE_NAME_RE.search(raw)
    if m is not None:
        error_name = m.group(1)
        raw = raw[: m.start()]
    cut = _CONTEXT_CUT_RE.search(raw)
    if cut is not None and cut.start() > 0:
        raw = raw[: cut.start()]
    # argument boundaries → \x00 (each keeps a space on both sides so the
    # neighbouring literal words stay whole)
    work = _QUOTED_RE.sub(f" {_BOUNDARY} ", raw)
    work = _ELLIPSIS_RE.sub(f" {_BOUNDARY} ", work)
    work = _MULTISPACE_RE.sub(f" {_BOUNDARY} ", work)
    work = _NUMBER_RE.sub(f" {_BOUNDARY} ", work)

    def _arg(tok: re.Match[str]) -> str:
        t = tok.group(0)
        if t == _BOUNDARY:
            return t
        core = t.strip(_EDGE_PUNCT)
        if core and _ARG_TOKEN_RE.search(core):
            return f" {_BOUNDARY} "
        return t

    work = _TOKEN_RE.sub(_arg, work)
    spans: list[str] = []
    template_parts: list[str] = []
    for piece in work.split(_BOUNDARY):
        clean = " ".join(piece.split())
        clean = _strip_edges(clean)
        if not clean:
            if template_parts and template_parts[-1] != "{}":
                template_parts.append("{}")
            elif not template_parts:
                template_parts.append("{}")
            continue
        template_parts.append(clean)
        if template_parts[:-1] and template_parts[-2] != "{}":
            template_parts.insert(len(template_parts) - 1, "{}")
        words = clean.split()
        if len(words) >= SPAN_MIN_WORDS or len(clean) >= SPAN_MIN_CHARS:
            spans.append(clean)
    template = " ".join(template_parts).strip()
    message_words = tuple(w for w in (_compare_word(t) for t in raw.split()) if w)
    return ParsedMessage(
        spans=tuple(spans),
        template=template,
        words=message_words,
        error_code=error_code,
        error_name=error_name,
    )


# ---------------------------------------------------------------- statements


@dataclass
class Statement:
    path: str
    line: int
    text: str  # opening line, stripped
    template: str  # the statement's own literal text, `{}` for arguments
    scope: str
    coverage: float = 0.0
    matched_words: int = 0
    verified: bool = False
    missing_chars: int = 0
    names_code: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float:
        # a verified alignment outranks any unverified one; among verified
        # statements, the one skipping fewer template literals (the right
        # variant of a family) wins at equal coverage
        return (
            (1.0 if self.verified else 0.0)
            + self.coverage
            + (ERROR_CODE_BONUS if self.names_code else 0.0)
            - min(0.2, self.missing_chars / 200.0)
        )


def _unescape(lit: str) -> str:
    return lit.replace("\\n", " ").replace("\\t", " ").replace('\\"', '"').replace("\\\\", "\\")


def statement_template(window: str) -> str:
    """The longest run of adjacent string literals in a statement window,
    joined the way the compiler joins them, with every `{…}` placeholder
    normalised to `{}`. "" when the window carries no literal."""
    best = ""
    run: list[str] = []
    last_end = -1
    for m in _STRING_LITERAL_RE.finditer(window):
        between = window[last_end : m.start()] if last_end >= 0 else ""
        if last_end >= 0 and between.strip():
            joined = "".join(run)
            if len(joined) > len(best):
                best = joined
            run = []
        run.append(_unescape(m.group(1)))
        last_end = m.end()
    joined = "".join(run)
    if len(joined) > len(best):
        best = joined
    return " ".join(_PLACEHOLDER_RE.sub(" {} ", best).split())


def statement_windows(lines: list[str], start_line: int) -> list[tuple[int, str, str]]:
    """(line number, opening line, window text) for every statement that
    opens on a STATEMENT_KEYWORDS line; the window runs to the line that
    closes the statement (`;`) or WINDOW_LINES lines."""
    out: list[tuple[int, str, str]] = []
    covered_until = -1  # a keyword inside an open window opens no new one
    for i, line in enumerate(lines):
        if i <= covered_until or not any(k in line for k in STATEMENT_KEYWORDS):
            continue
        window = [line]
        for follow in lines[i + 1 : i + WINDOW_LINES]:
            if line.rstrip().endswith(";"):
                break
            window.append(follow)
            if follow.rstrip().endswith(";"):
                break
        covered_until = i + len(window) - 1
        out.append((start_line + i, line.strip(), "\n".join(window)))
    return out


WILD = "{}"


def _template_tokens(template: str) -> list[str]:
    """Comparison tokens of a statement template; WILD where an argument
    stands."""
    out: list[str] = []
    for tok in template.split():
        if tok == WILD:
            out.append(WILD)
            continue
        w = _compare_word(tok)
        if w:
            out.append(w)
    return out


def align(message_words: tuple[str, ...], template_tokens: list[str]) -> list[tuple[int, int]]:
    """Character-weighted LCS over words, WILD never matching; returns the
    aligned (message index, template index) pairs in order."""
    n, m = len(message_words), len(template_tokens)
    if not n or not m:
        return []
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        wi = message_words[i - 1]
        row, prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            tj = template_tokens[j - 1]
            if tj != WILD and wi == tj:
                row[j] = prev[j - 1] + len(wi)
            else:
                row[j] = prev[j] if prev[j] >= row[j - 1] else row[j - 1]
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        tj = template_tokens[j - 1]
        if tj != WILD and message_words[i - 1] == tj and dp[i][j] == dp[i - 1][j - 1] + len(tj):
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


@dataclass(frozen=True)
class Verification:
    coverage: float  # matched message chars / message chars
    matched_words: int
    # every unmatched run of message words sits against a `{}` of the
    # template (an argument), and no template literal is skipped at the head
    # or in the middle — the "regex verify" of the mechanism
    verified: bool
    missing_chars: int  # template literal chars skipped at head / middle


def verify(parsed: ParsedMessage, template: str) -> Verification:
    """The statement's template against the message. A message may be
    truncated at either end (an agent pastes a prefix or a suffix) and may
    carry arguments where the template has `{}`; it may NOT carry words
    where the template has none, and the template's literal words must be
    in the message unless they lie past the message's end."""
    total = parsed.total_chars
    tokens = _template_tokens(template)
    if not total or not tokens:
        return Verification(0.0, 0, False, 0)
    pairs = align(parsed.words, tokens)
    if not pairs:
        return Verification(0.0, 0, False, 0)
    matched = sum(len(parsed.words[i]) for i, _ in pairs)
    verified = True
    missing = 0
    prev_i, prev_j = -1, -1
    for k, (i, j) in enumerate(pairs):
        msg_gap = parsed.words[prev_i + 1 : i]
        tpl_gap = tokens[prev_j + 1 : j]
        wild = WILD in tpl_gap
        literals = [t for t in tpl_gap if t != WILD]
        if k == 0:
            # head: an unmatched message head is an argument only when the
            # template head is a bare `{}`; a clean message head against a
            # literal template head is a suffix paste (the agent copied the
            # tail) and is allowed; both sides starting with words the other
            # lacks is a different statement
            if msg_gap and (literals or not wild):
                verified = False
            missing += sum(len(t) for t in literals) if msg_gap else 0
        else:
            if msg_gap and not wild:
                verified = False
            missing += sum(len(t) for t in literals)
        prev_i, prev_j = i, j
    return Verification(min(1.0, matched / total), len(pairs), verified, missing)


def rank_statements(
    parsed: ParsedMessage,
    chunks: list[dict[str, Any]],
    *,
    error_name: str = "",
    continued_prefix: str = "// … continued from ",
) -> list[Statement]:
    """Every raising/logging statement in the chunks, scored against the
    message; best first, then path/line. One entry per (path, line)."""
    seen: set[tuple[str, int]] = set()
    out: list[Statement] = []
    for c in chunks:
        path = str(c.get("path") or "")
        lines = str(c.get("content") or "").splitlines()
        if lines and lines[0].startswith(continued_prefix):
            lines = lines[1:]
        start = int(c.get("start_line") or 1)
        scope = str(c.get("scope") or "")
        for line_no, text, window in statement_windows(lines, start):
            key = (path, line_no)
            if key in seen:
                continue
            seen.add(key)
            tpl = statement_template(window)
            if not tpl:
                continue
            v = verify(parsed, tpl)
            if v.matched_words == 0:
                continue
            out.append(
                Statement(
                    path=path,
                    line=line_no,
                    text=text[:300],
                    template=tpl,
                    scope=scope,
                    coverage=round(v.coverage, 3),
                    matched_words=v.matched_words,
                    verified=v.verified,
                    missing_chars=v.missing_chars,
                    names_code=bool(error_name) and (f"ErrorCodes::{error_name}" in window),
                )
            )
    out.sort(key=lambda s: (-s.score, -s.matched_words, s.path, s.line))
    return out


def verdict_for(best: Statement | None) -> str:
    if best is None or not best.verified:
        return "no_template_match"
    if best.coverage >= RAISED_AT_COVERAGE and best.matched_words >= RAISED_AT_WORDS:
        return "raised_at"
    if best.coverage >= RAISED_AT_LONG_COVERAGE and best.matched_words >= RAISED_AT_LONG_WORDS:
        return "raised_at"
    return "no_template_match"


# ---------------------------------------------------------------- error codes

_ERROR_CODES_CACHE: TtlCache[tuple[str, str], dict[int, str]] = TtlCache(ERROR_CODES_TTL_S)


def parse_error_codes(chunks: list[dict[str, Any]]) -> dict[int, str]:
    """`M(N, NAME)` lines of the indexed ErrorCodes table → {N: NAME}."""
    out: dict[int, str] = {}
    for c in chunks:
        for m in _ERROR_CODE_LINE_RE.finditer(str(c.get("content") or "")):
            out.setdefault(int(m.group(1)), m.group(2))
    return out


async def error_code_name(repo: Any, org: str, ns: str, code: int | None) -> str:
    """The ErrorCodes name for `code` in this namespace, "" when the table
    is not indexed or the number is unknown. Cached ERROR_CODES_TTL_S."""
    if code is None:
        return ""
    table = _ERROR_CODES_CACHE.get((org, ns))
    if table is None:
        table = {}
        for path in ERROR_CODES_PATHS:
            table = parse_error_codes(await repo.chunks_of_path(org, ns, path, limit=400))
            if table:
                break
        _ERROR_CODES_CACHE.put((org, ns), table)
    return table.get(code, "")


async def error_code_names(repo: Any, org: str, ns: str) -> set[str]:
    """Every ErrorCodes name the indexed table declares (a draft's
    `ErrorCodes::BAD_ARGUMENTS` is a citation the symbol table cannot answer — it is an
    `extern const int`). Same cache as `error_code_name`; empty when the table is not
    indexed."""
    await error_code_name(repo, org, ns, 0)
    return set((_ERROR_CODES_CACHE.get((org, ns)) or {}).values())


# ---------------------------------------------------------------- regex shape

# Unescaped alternation or class, an anchor, a wildcard run, or a regex escape
# (`\(`, `\.`, `\w`, `\d`, `\s`, `\b`, `\|`): a literal that was written as a
# regex. A `.` alone is not a hint — `res.info.num_rows` is a member access.
_REGEX_SHAPE_RE = re.compile(r"(?<!\\)\||(?<!\\)\[|^\^|(?<!\\)\$$|\.[*+]|\\[()\[\].|wdsb*+?]|\(\?")


def regex_shaped(pattern: str) -> bool:
    """True when a literal pattern carries unescaped regex metacharacters."""
    return bool(_REGEX_SHAPE_RE.search(pattern))


def compile_fallback(pattern: str, *, case_sensitive: bool = True) -> Any:
    """The pattern as an RE2 regex (the engine the SQL step uses) when it is regex-shaped
    and valid, else None."""
    from server.core.caller_regex import compile_caller_regex
    from server.core.errors import ValidationError

    if not regex_shaped(pattern):
        return None
    try:
        return compile_caller_regex(pattern, case_sensitive=case_sensitive)
    except ValidationError:
        return None


REGEX_FALLBACK_NOTE = "pattern read as a regex; pass regex=true to make this explicit"
REGEX_INVALID_NOTE = "pattern looks like a regex but is not a valid one; it was matched literally"
