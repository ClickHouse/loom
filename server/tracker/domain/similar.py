"""tracker.similar — the pure half of the duplicate / related-item finder:
query tokenisation and term selection against the corpus
vocabulary, frame normalisation (the shape `code.trace` parses), the
position-decayed frame score, the field-equality boosts, reciprocal-rank
fusion and the `why` list. Nothing here reads anything; the live reads are
`TrackerSource.similar_*` and the handler joins the two.

Why the lexical leg is BM25F and not the upstream text index alone: the
index answers "carries every token" (`similar_bugs`) — a duplicate rarely
repeats every word, and "carries any token" ranks nothing. BM25F ranks by rare
shared words with the title weighted over the body, and a tuned lexical baseline
is a strong duplicate detector; frames are scored by position-decayed frame IDF.
The upstream QA embedding column is zeros on public rows, so the vector leg uses
loom's own embeddings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.core import bm25

# BM25F constants: k1 / b are the shared BM25 defaults; the title weight is
# 3:1, which measured best on the 104-query gold.
K1 = bm25.K1
B = bm25.B
TITLE_WEIGHT = 3.0
RRF_K = 60
_LEG_NAMES = ("lexical", "frames", "mentions", "vector")
# The legs that only ADD candidates — they never move one
# another leg ranked (measured: fused as a fifth ranked list the vector leg
# took the default arm from 0.846 to 0.808 hit@5 on the duplicate gold)
RECALL_ONLY_LEGS = ("vector",)
FRAME_DECAY = 0.85
MAX_TERMS = 8
# tokens the vocabulary never saw (df < 3) allowed among the terms
UNSEEN_CAP = 1
_LINE_REF_RE = re.compile(r"^l\d{1,6}$")
# a token in more than this share of the corpus prunes nothing and costs a
# body scan per row it admits (`values` is 21 % of the issues)
MAX_DF_RATIO = 0.08
# the candidate set is the union of the kept tokens' postings: cap its size
# so the body-scan stage stays inside the interactive budget (measured
# ~0.2 ms per candidate row live)
DF_BUDGET = 2500

# the frame shapes `code.trace` parses (server/code/app/symbol/trace.py);
# mirrored here rather than imported so the tracker plane does not load the code
# handlers — tests/unit/test_tracker_similar.py pins the two in step
# Group 1 is the name, read from the first letter of a word: a start inside
# a word would rescan it, so a long word costs one pass, not one per letter.
TRACE_SYMBOL_RE = re.compile(r"(?<![\w~])[^\WA-Za-z_]*+((?:[A-Za-z_~][\w~]*+::)+[A-Za-z_~][\w~]*+)")
# A path is read from the start of its first directory only, so a long
# path costs one pass, not one per directory.
TRACE_FILELINE_RE = re.compile(
    r"(?<![\w.+-])(?<![\w.+-]/)((?:[\w.+-]+/)+[\w.+-]+\.(?:cpp|cc|cxx|c|h|hpp|hh|proto))[:(](\d+)"
)
_NOISE_PREFIXES = ("std::", "__", "absl::", "boost::")
# Frames every trace carries whatever broke — the capture and
# assertion plumbing, the sanitizer runtimes, third-party code — and a data
# part name (`a5b308ea49b::all_0_11_1_4`) that parses like a symbol
_NOISE_SYMBOL_PARTS = frozenset(
    ("StackTrace", "abortOnFailedAssertion", "handle_error_code", "terminate_handler")
)
_NOISE_FILE_STEMS = frozenset(("StackTrace", "SignalHandlers", "Exception", "BaseDaemon"))
_NOISE_PATH_RE = re.compile(
    r"(?:^|/)(?:contrib|usr|base/glibc-compatibility|sysroot)/|compiler-rt|/[atmub]+san/"
)
_ERROR_CODE_RE = re.compile(r"\bCode:\s*(\d{1,4})\b")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# a hex fragment with a digit in it (a fiddle id, a commit, a UUID piece) is
# never a word two reports share on purpose
_HEXISH_RE = re.compile(r"^(?=.*\d)[0-9a-f]{4,}$")
# the `store/06f/06f90420-f81b-…/` metadata path every ClickHouse log carries:
# the three-hex directory is the UUID's own prefix, never a word (a bare
# three-hex rule would also drop ec2, e2e, c99 — real words in this corpus)
_UUID_RE = re.compile(
    r"\b([0-9a-f]{3})[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
# camelCase / PascalCase pieces: `dateTrunc` → date, trunc — the upstream
# tokenizer splits `date_trunc` the same way, so a report in one spelling
# still meets a report in the other
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
# an item another report names: `#70529`, `/issues/70529`, `/pull/70529`
_MENTION_RE = re.compile(r"(?:#|/issues/|/pull/)(\d{4,7})\b")
MAX_TOKEN_LEN = 32

STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if
    in into is it its just may me my no not of on or our out over so than that the their them
    then there these they this those to too under up us use used using very was we were what
    when where which while who why will with would you your yes also any all both each more
    most much some such only same own other about after before again against because between
    during through above below here should now new like get got set see same still since
    however thanks thank please hi hello issue issues bug bugs clickhouse company project name
    describe wrong expected behavior behaviour reproduce reproduces reproduction steps version
    versions error errors exception query queries table tables select insert create function
    result results return returns value values data type types file files column columns row
    rows server client run running works work working case cases example examples code
    https http com github www org md txt""".split()
)


# ---------------------------------------------------------------- tokens


def query_tokens(text: str) -> list[str]:
    """Lower-cased alphanumeric tokens of the query text in first-seen
    order: `splitByNonAlpha`-compatible (the upstream tokenizer); no
    stopwords, no single characters, no numbers other than zero-led test
    ids, no hex fragments, pointers or blobs longer than 32 characters (all
    seen filling the term slots otherwise), and no `store/06f/` directory of a
    UUID the text names."""
    out: list[str] = []
    seen: set[str] = set()
    uuid_prefixes = {m.group(1).lower() for m in _UUID_RE.finditer(text or "")}

    def keep(tok: str) -> None:
        if len(tok) >= 2 and tok not in STOPWORDS and tok not in seen:
            seen.add(tok)
            out.append(tok)

    for m in _TOKEN_RE.finditer(text or ""):
        raw = m.group(0)
        tok = raw.lower()
        if len(tok) > MAX_TOKEN_LEN or tok.startswith("0x"):
            continue  # a base64 blob out of a link, a pointer
        if tok in uuid_prefixes:
            continue  # the `store/06f/` directory of a UUID named in the text
        if tok.isdigit():
            # a year, a line number, an issue number: never a word two reports
            # share on purpose — a test id (`03532_…`) starts with a zero
            if len(tok) < 4 or not tok.startswith("0"):
                continue
        elif _LINE_REF_RE.match(tok):
            continue  # `l533` / `L2247`: a finding's line reference
        elif _HEXISH_RE.match(tok):
            continue
        keep(tok)
    return out


def camel_pieces(text: str) -> list[str]:
    """The camelCase pieces of the text's tokens (`dateTrunc` → date,
    trunc), lower-cased, first-seen, stopwords out — the spellings a
    snake_case report tokenises to upstream. Fill the term slots the
    original tokens leave free (`select_terms(defer=...)`), never displace
    them: as equals they measured −4 points hit@5 on the gold."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _TOKEN_RE.finditer(text or ""):
        raw = m.group(0)
        if len(raw) > MAX_TOKEN_LEN:
            continue
        parts = _CAMEL_RE.findall(raw)
        if len(parts) < 2:
            continue
        for p in parts:
            tok = p.lower()
            if len(tok) >= 3 and tok not in STOPWORDS and tok not in seen and not tok.isdigit():
                seen.add(tok)
                out.append(tok)
    return out


def mentioned_numbers(text: str, *, limit: int = 10) -> list[int]:
    """The issue / PR numbers a report names (`#N`, `/issues/N`, `/pull/N`),
    first-seen order: a report that names another is saying it is related."""
    out: list[int] = []
    for m in _MENTION_RE.finditer(text or ""):
        n = int(m.group(1))
        if n and n not in out:
            out.append(n)
        if len(out) >= limit:
            break
    return out


def case_variants(tok: str) -> list[str]:
    """The spellings a lower-cased token takes upstream (the text index is
    case-sensitive): lower, Capitalised, UPPER."""
    return sorted({tok, tok.upper(), tok.capitalize()})


@dataclass
class TokenStats:
    """The corpus vocabulary of one tracker namespace: document frequency per
    lower-cased token (`df >= 3`), the document count and the average field
    lengths BM25F normalises against. Read once per process and hour."""

    n: int
    df: dict[str, int] = field(default_factory=dict)
    avg_title_len: float = 10.0
    avg_body_len: float = 540.0

    def idf(self, tok: str) -> float:
        return bm25.idf(self.df.get(tok, 1), self.n)


def select_terms(
    tokens: list[str],
    stats: TokenStats,
    *,
    prefer: list[str] | None = None,
    defer: list[str] | None = None,
    max_terms: int = MAX_TERMS,
) -> list[str]:
    """The query tokens worth a candidate scan: rarest first, title tokens
    of the query item first among equals, `defer`red tokens (camelCase
    pieces) only in the slots the others leave, none above `MAX_DF_RATIO`
    of the corpus, and the union of their postings under `DF_BUDGET` rows.
    One cap: a token the vocabulary never saw (df < 3) takes at most one
    slot — it is the rarest of all, but a finding's own symbol names match no
    report. Title tokens keep their precedence: capping them at half the slots
    can drop a true target from lexical rank 1 to 46 when `mergetreedata` and
    `proxy` give way to `l533` and `trysyncreplica`."""
    pool = [*tokens, *[t for t in (defer or []) if t not in tokens]]
    if not pool or stats.n <= 0:
        return pool[:max_terms]
    preferred = set(prefer or [])
    deferred = set(defer or []) - set(tokens)
    ceiling = max(50, int(stats.n * MAX_DF_RATIO))

    def key(t: str) -> tuple[int, int]:
        tier = 0 if t in preferred else (2 if t in deferred else 1)
        return (tier, stats.df.get(t, 0))

    ranked = sorted((t for t in pool if stats.df.get(t, 0) <= ceiling), key=key)
    out: list[str] = []
    spent = 0
    unseen = 0
    for t in ranked:
        d = stats.df.get(t, 0)
        if d == 0:
            if unseen >= UNSEEN_CAP:
                continue
            unseen += 1
        if out and spent + d > DF_BUDGET:
            continue
        out.append(t)
        spent += d
        if len(out) >= max_terms:
            break
    if not out:
        # every token is common: the two rarest still give an answer
        out = sorted(pool, key=lambda t: stats.df.get(t, 0))[:2]
    return out


# A quoted server error name (`SIZES_OF_COLUMNS_DOESNT_MATCH`)
# and a CI crash fingerprint (`STID: 0883-49e8`)
_ERROR_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})\b")
_STID_RE = re.compile(r"\bSTID:?\s*([0-9]{4}-[0-9a-f]{4})\b", re.I)


def error_names_of(text: str) -> list[str]:
    """The upper-case snake names in `text`, in order, once each."""
    return list(dict.fromkeys(_ERROR_NAME_RE.findall(text or "")))


def stid_of(text: str) -> str:
    """`STID: 0883-49e8` → `0883-49e8`; '' when the text names none."""
    m = _STID_RE.search(text or "")
    return m.group(1).lower() if m else ""


def error_code_of(text: str) -> int:
    """`Code: 49` in the query text → 49; 0 when no server error code is named."""
    m = _ERROR_CODE_RE.search(text or "")
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------- frames


@dataclass(frozen=True)
class Frame:
    raw: str
    kind: str  # symbol | file
    # the token the body index carries for it: the method name of a
    # qualified symbol (`DB::MergeTreeData::loadDataParts` → loadDataParts),
    # the basename stem of a path (`src/.../MergeTreeData.cpp` → MergeTreeData)
    token: str
    # a path frame keeps its repository-relative suffix for `stack_functions`
    path: str = ""


def normalise_frames(frames: list[str], *, max_frames: int = 12) -> list[Frame]:
    """Caller frames or trace lines → ordered, deduped frames the way
    `code.trace` parses them: qualified symbols (stdlib / abseil / boost
    noise dropped) and repository paths with a line; order kept — the first
    frames are the crashing code and weigh most."""
    out: list[Frame] = []
    seen: set[str] = set()
    for line in frames or []:
        for m in TRACE_SYMBOL_RE.finditer(line or ""):
            name = m.group(1)
            if name.startswith(_NOISE_PREFIXES):
                continue
            parts = [p for p in name.split("::") if p]
            if _HEXISH_RE.match(parts[0].lower()) or any(p in _NOISE_SYMBOL_PARTS for p in parts):
                continue
            token = parts[-1]
            if token.startswith("~") or len(token) < 4 or token.startswith("operator"):
                token = parts[-2] if len(parts) > 1 else token
            token = token.lstrip("~")
            if not token or token.lower() in seen:
                continue
            seen.add(token.lower())
            out.append(Frame(raw=name, kind="symbol", token=token))
        for m in TRACE_FILELINE_RE.finditer(line or ""):
            path = m.group(1)
            stem_of = path.rsplit("/", 1)[-1].split(".", 1)[0]
            if _NOISE_PATH_RE.search(path) or stem_of in _NOISE_FILE_STEMS:
                continue
            rel = path.split("src/", 1)[1] if "/src/" in f"/{path}" else path
            stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            key = f"file:{rel.lower()}"
            if key in seen:
                continue
            seen.add(key)
            out.append(Frame(raw=path, kind="file", token=stem, path=rel))
        if len(out) >= max_frames:
            break
    return out[:max_frames]


# the first `Name::x` on the line: each word is read once from its first
# letter, so a long line is one pass, not one pass per character
_QUALIFIED_AHEAD = r".*?(?<!\w)[^\WA-Za-z_]*+[A-Za-z_]\w*+::[A-Za-z_~]"
# a frame line as ClickHouse and its sanitizers print one:
# `0. DB::Exception::Exception(...) @ 0x...`, the server log's
# `2. /…/src/Storages/MergeTree/MarkRange.cpp:19: DB::MarkRange::MarkRange(…)`
# (path first: `\S*::` never matched it, so no pasted server trace ever fed the
# frames leg), a sanitizer's `#3 0x55d1 in DB::Foo::bar(…) /…/Foo.cpp:12`, and
# the CI report's unnumbered `DB::Foo::bar(…) @ 0x000000001c465289`
_TRACE_LINE_RE = re.compile(
    r"^\s*+(?:(?:#\d+|\d+\.)\s++"
    + _QUALIFIED_AHEAD
    + "|(?>"
    + _QUALIFIED_AHEAD
    + r").*\s@\s0x[0-9A-Fa-f]{6,})"
)
MIN_TRACE_LINES = 3


def frames_from_text(text: str, *, max_frames: int = 12) -> list[Frame]:
    """The frames a pasted issue body carries — what a caller would have
    passed had it parsed them — but only when the body holds a stack trace
    (at least `MIN_TRACE_LINES` numbered frame lines). A `DB::Foo::bar`
    named in prose is not a frame: parsed from every body, the frames leg
    cost a point of hit@5 on the gold (0.500 → 0.490) while helping the
    three crash reports; parsed from traces only it keeps the help."""
    lines = (text or "").splitlines()
    if sum(1 for ln in lines if _TRACE_LINE_RE.match(ln)) < MIN_TRACE_LINES:
        return []
    return normalise_frames([ln for ln in lines if "::" in ln or "/" in ln], max_frames=max_frames)


def select_frames(frames: list[Frame], stats: TokenStats) -> list[Frame]:
    """The frames worth a leg: a frame whose token is in more than
    `MAX_DF_RATIO` of the corpus (`Exception`, `ThreadPool`) ranks nothing
    and, under reciprocal-rank fusion, would still hand its top candidate a
    full rank credit — so it is dropped before the read, order kept."""
    if stats.n <= 0:
        return list(frames)
    ceiling = max(50, int(stats.n * MAX_DF_RATIO))
    return [f for f in frames if stats.df.get(f.token.lower(), 0) <= ceiling]


def frame_score(hits: list[bool], frames: list[Frame], stats: TokenStats) -> float:
    """Position-decayed frame IDF: Σ decay^i · idf(frame_i) over the frames
    the candidate carries. An innermost frame shared with the query is
    worth more than a shared thread-pool frame ten levels up."""
    total = 0.0
    for i, (hit, fr) in enumerate(zip(hits, frames, strict=False)):
        if hit:
            total += (FRAME_DECAY**i) * stats.idf(fr.token.lower())
    return total


# ---------------------------------------------------------------- scoring


@dataclass
class Query:
    number: int = 0
    kind: str = ""
    text: str = ""
    title: str = ""
    component: str = ""
    versions: list[str] = field(default_factory=list)
    error_code: int = 0
    tokens: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    frames_source: str = ""  # caller | body | ""
    mentions: list[int] = field(default_factory=list)


def field_boosts(row: dict[str, Any], q: Query) -> tuple[float, list[str]]:
    """The equality features added to a lexical score, as a share of the
    leg's top score: same primary component, an affected version in common,
    the same server error code. Each names itself in `why`."""
    why: list[str] = []
    boost = 0.0
    comp = str(row.get("primary_component") or "")
    if q.component and comp and comp == q.component:
        boost += 0.10
        why.append("component")
    versions = {str(v) for v in (row.get("affected_versions") or []) if v}
    if q.versions and versions & set(q.versions):
        boost += 0.10
        why.append("version")
    code = int(row.get("error_code") or 0)
    if q.error_code and code and code == q.error_code:
        boost += 0.20
        why.append("error_code")
    return boost, why


def rank_lexical(rows: list[dict[str, Any]], q: Query, *, fields: bool) -> list[dict[str, Any]]:
    """Order the lexical candidates: BM25F from the statement, plus the field
    boosts scaled by the top BM25F score when the `fields` leg is on."""
    top = max((float(r.get("bm25") or 0.0) for r in rows), default=0.0) or 1.0
    out: list[dict[str, Any]] = []
    for r in rows:
        score = float(r.get("bm25") or 0.0)
        why = ["lexical"]
        if fields:
            boost, more = field_boosts(r, q)
            score += boost * top
            why.extend(more)
        out.append({**r, "leg_score": score, "why": why})
    out.sort(key=lambda r: (-r["leg_score"], str(r.get("source_id") or "")))
    return out


def rrf(lists: dict[str, list[dict[str, Any]]], *, k: int = RRF_K) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion of the legs' ranked lists keyed by
    `source_id`; every leg an item appears in is named in its `why`, and
    the field-equality reasons from the lexical leg ride along."""
    fused: dict[str, dict[str, Any]] = {}
    for leg, rows in lists.items():
        for rank, r in enumerate(rows, start=1):
            sid = str(r.get("source_id") or "")
            if not sid:
                continue
            cur = fused.get(sid)
            if cur is None:
                cur = {**r, "score": 0.0, "why": [], "leg_ranks": {}}
                fused[sid] = cur
            cur.setdefault("leg_ranks", {}).setdefault(leg, rank)
            cur["score"] += 1.0 / (k + rank)
            reasons = [x for x in r.get("why", []) if x not in _LEG_NAMES]
            for w in [leg, *reasons]:
                if w not in cur["why"]:
                    cur["why"].append(w)
    out = list(fused.values())
    out.sort(key=lambda r: (-r["score"], str(r.get("source_id") or "")))
    return out


def fuse(
    lists: dict[str, list[dict[str, Any]]],
    *,
    recall_only: tuple[str, ...] = RECALL_ONLY_LEGS,
    k: int = RRF_K,
    agreement: bool = False,
    agreement_weight: float = 1.0,
    agreement_max_rank: int = 0,
) -> list[dict[str, Any]]:
    """Reciprocal-rank fusion of the ranking legs (`rrf`, the lexical
    order untouched), then the recall-only legs. A candidate they
    share with a ranked leg keeps its fused rank and gains the leg's name
    in `why` (and its `distance`). A candidate only they found is appended
    AFTER every ranked candidate, in the leg's own order, scored strictly
    below the lowest fused score. A recall-only leg never
    re-ranks or displaces what a ranked leg found — EXCEPT under
    `agreement`: a candidate a ranked leg AND a recall-only
    leg both found gains the recall-only leg's own RRF contribution
    (`agreement_weight / (k + rank)`, once per leg, only while the leg's
    rank is at most `agreement_max_rank` when that is set) and the ranked
    set is re-sorted, so
    "same defect, different vocabulary" pairs the lexical leg half-finds
    rise; candidates only the recall-only leg found still never enter the
    ranked set. The default arm is unchanged with `agreement=False`."""
    fused = rrf({leg: rows for leg, rows in lists.items() if leg not in recall_only}, k=k)
    by_sid = {str(r.get("source_id") or ""): r for r in fused}
    floor = min((float(r["score"]) for r in fused), default=1.0 / (k + 1))
    tail: list[dict[str, Any]] = []
    seen: set[str] = set()
    boosted: set[tuple[str, str]] = set()
    for leg in recall_only:
        for rank, r in enumerate(lists.get(leg) or [], start=1):
            sid = str(r.get("source_id") or "")
            if not sid:
                continue
            cur = by_sid.get(sid)
            if cur is not None:
                cur.setdefault("leg_ranks", {}).setdefault(leg, rank)
                if leg not in cur["why"]:
                    cur["why"].append(leg)
                if r.get("distance") is not None:
                    cur.setdefault("distance", r["distance"])
                if (
                    agreement
                    and (leg, sid) not in boosted
                    and (agreement_max_rank <= 0 or rank <= agreement_max_rank)
                ):
                    boosted.add((leg, sid))
                    cur["score"] += float(agreement_weight) / (k + rank)
                continue
            if sid in seen:
                continue
            seen.add(sid)
            reasons = [x for x in r.get("why", []) if x not in _LEG_NAMES]
            tail.append(
                {
                    **r,
                    "score": floor / (1 + rank),
                    "why": [leg, *reasons],
                    "leg_ranks": {leg: rank},
                }
            )
    if agreement and boosted:
        fused.sort(key=lambda r: (-float(r["score"]), str(r.get("source_id") or "")))
    return [*fused, *tail]


__all__ = [
    "B",
    "DF_BUDGET",
    "FRAME_DECAY",
    "K1",
    "MAX_DF_RATIO",
    "MAX_TERMS",
    "MIN_TRACE_LINES",
    "RECALL_ONLY_LEGS",
    "RRF_K",
    "STOPWORDS",
    "TITLE_WEIGHT",
    "TRACE_FILELINE_RE",
    "TRACE_SYMBOL_RE",
    "Frame",
    "Query",
    "TokenStats",
    "camel_pieces",
    "case_variants",
    "error_code_of",
    "error_names_of",
    "stid_of",
    "field_boosts",
    "frame_score",
    "frames_from_text",
    "fuse",
    "mentioned_numbers",
    "normalise_frames",
    "query_tokens",
    "rank_lexical",
    "rrf",
    "select_frames",
    "select_terms",
]
