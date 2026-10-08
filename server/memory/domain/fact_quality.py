"""Is a stored row a fact worth returning?

One deterministic classifier, shared by every read path that hands an agent
"what Loom knows" (loom_search's memory section, context.pack's memories) and
by the write side (`durable_facts` may call it before storing). A row is junk
when it is one of these classes; `None` means it may be returned.

- ``transcript`` — a trajectory row ("Step 2/5 · After Bash …", an
  observe.record slice): evidence of a run, never a claim.
- ``narration`` — what an agent or the user DID in a session ("User read lines
  1001-2000 …", "Claude ran a command on …"), from `activity_facts`' rule.
- ``hint`` — a generic tool tip distilled from a session ("Use `grep -n` …",
  source `observe.extract:hint`): procedure material, not a fact.
- ``code_location`` — where a symbol or file sits (a path with a line number,
  a plugin-cache path): the code index answers it exactly, and the stored copy
  goes stale with the next merge.
- ``probe`` — a test/probe/placeholder write (a probe key, a value that
  describes the test itself, a placeholder value like "xyz").
- ``session_status`` — the state of one moment of one session ("User is
  checking if …", "for this run", "this morning").

The classes are conservative by construction: each was checked against every real fact
of a labelled set and must never fire on one. Precision over recall — a junk row that
slips through is ranked below real ones by the caller; a real row classed as junk is
lost.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, NamedTuple

from server.memory.domain.activity_facts import is_activity_narration
from server.memory.domain.transcript_rows import is_transcript_row
from server.settings import settings

HINT_TRACE = "observe.extract:hint"

_PROBE_KEY_RE = re.compile(
    r"(?:^|[/_:-])(?:probe|probes|smoke|phase1|p1b|fresh_key|bulk_probe|writepath_probe)"
    r"(?:$|[/_:-]|\d)",
    re.IGNORECASE,
)
_PROBE_VALUE_RE = re.compile(
    r"\b(?:must (?:be|count as) (?:created|updated)|should count as (?:created|updated)"
    r"|never (?:been )?written before|brand new key|a brand new|dup(?:licate)? second"
    r"|instrumentation probe|second write to the same key)\b",
    re.IGNORECASE,
)
_PLACEHOLDERS = frozenset(
    {
        "xyz",
        "abc",
        "foo",
        "bar",
        "baz",
        "test",
        "testing",
        "asdf",
        "qwerty",
        "random stuff",
        "lorem ipsum",
        "todo",
        "tbd",
        "n/a",
        "none",
        "null",
        "placeholder",
        "dummy",
    }
)
_STOP_WORDS = frozenset(
    "a an the is are was were be been of to in on at for and or with by from as that this "
    "it its my me i you your user users user's her his their our we us s "
    "has have had do does did".split()
)


class _ActorPatterns(NamedTuple):
    """The patterns that name a session's actor, built for one deployment's names."""

    code_location: re.Pattern[str]
    session_status: re.Pattern[str]
    more_narration: re.Pattern[str]
    file_stats: re.Pattern[str]
    actor_start: re.Pattern[str]
    stop: frozenset[str]


@lru_cache(maxsize=4)
def _actor_patterns(names: str) -> _ActorPatterns:
    """`names` is `settings.memory_quality_actor_names`: the people whose sessions
    this deployment records, narrated like "the user" ("Jane ran a command …")."""
    people = sorted(
        {n.strip().lower() for n in names.split(",") if n.strip()}, key=len, reverse=True
    )
    alt = "".join("|" + r"\s+".join(map(re.escape, n.split())) for n in people)
    return _ActorPatterns(
        code_location=re.compile(
            # the claim IS a location: "X is defined at Foo.cpp:7137", "declared in …h at
            # line 951" — not a finding that cites a path:line among other content
            r"\b(?:is|are|was)\s+(?:defined|declared|located|implemented)\s+(?:at|in)\s+\S+"
            r"\.(?:cpp|cc|cxx|h|hpp|py|go|rs|ts|tsx|js|java|sql|md|sh)\b"
            r"|\b(?:is|are)\s+in\s+\S+\.(?:cpp|cc|cxx|h|hpp|py|go|rs|ts|js)\s+at\s+line\s+\d+"
            rf"|^\W*(?:the\s+)?(?:user|claude|assistant{alt})\b[^.]{{0,30}}\bread\s+"
            r"(?:lines\s+\d|the file\b|all [\d,]+ lines)"
            r"|/plugins/cache/"
            r"|\bbase directory for the skill\b",
            re.IGNORECASE,
        ),
        session_status=re.compile(
            rf"^\W*(?:the\s+)?(?:user|assistant|claude{alt})\b[^.]{{0,40}}?\b(?:is|was)\s+"
            r"(?:checking|testing|looking|trying|asking|using the|currently)\b"
            r"|\bfor this run\b|\bthis morning\b|\bin this session\b"
            # a verdict of one moment of one investigation, not a standing fact
            r"|\bshould file (?:it|this issue) as a new\b|\bthe search included\b"
            r"|^\W*(?:the\s+)?(?:user|assistant)(?:'s)?\s+(?:case|found no|new issue advice)\b"
            r"|\breporting window\b",
            re.IGNORECASE,
        ),
        # what an actor did in a session beyond activity_facts' verbs: found /
        # accessed / conducted / analysed / summarised / retrieved / fetched
        more_narration=re.compile(
            rf"^\W*(?:the\s+)?(?:user|assistant|claude(?:\s+code)?|the agent{alt})\b"
            r"[^.]{0,30}?\b(?:found|accessed|conducted|analy[sz]ed|summari[sz]ed|retrieved"
            r"|fetched|executed|encountered|quoted|reported the results|ran a|ran the)\b",
            re.IGNORECASE,
        ),
        # statistics or a gloss of one file: the code index answers it and it goes stale
        file_stats=re.compile(
            rf"^\W*(?:the\s+)?(?:user|claude{alt})\b[^\n]{{0,40}}\bfile\b[^\n]{{0,160}}"
            r"\bhas\s+[\d,]+\s+lines\b"
            r"|^\W*there are (?:roughly|about)\s+\d[\d,]*\s+methods\b"
            r"|^\W*the file is the implementation of\b",
            re.IGNORECASE,
        ),
        actor_start=re.compile(
            rf"^\W*(?:the\s+)?(?:user|assistant|claude(?:\s+code)?|the agent|agent{alt})\b",
            re.IGNORECASE,
        ),
        stop=_STOP_WORDS | {w for n in people for w in n.split()},
    )


def _patterns() -> _ActorPatterns:
    return _actor_patterns(settings.memory_quality_actor_names)


# A description of a Loom tool or plugin restated as a fact about the user.
_TOOL_DOC_RE = re.compile(
    r"\bloom(?:-code| code| holds)\b[^.]{0,40}\b(?:index|plugin|tool)\b"
    r"|\bloom-memory (?:plugin|skill)\b",
    re.IGNORECASE,
)
# Auto-generated planes that are not facts: a session's procedure GRAPH
# (`{"graph": …}`) and reflections (hidden from unfiltered search). A
# stated fix recipe or lesson (procedural, prose) stays a fact.
_SESSION_PROCEDURE_KEY_RE = re.compile(r"^procedure_(?!fr_[0-9a-f]+$)[a-z0-9_]+$")


def _text(value: Any) -> str:
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    return s.replace("\\n", "\n").strip()


def junk_class(
    memory_key: str,
    value: Any,
    *,
    tags: Any = None,
    source_trace: Any = "",
    memory_type: str = "",
) -> str | None:
    """The junk class of a stored row, or None when it may be returned."""
    key = str(memory_key or "")
    trace = str(source_trace or "")
    text = _text(value)
    if key.startswith(("event:", "obs:")) or is_transcript_row(tags, trace):
        return "transcript"
    if trace.startswith(HINT_TRACE) or key.startswith("hint:"):
        return "hint"
    if _PROBE_VALUE_RE.search(text) or (_PROBE_KEY_RE.search(key) and len(text) < 120):
        return "probe"
    if text.lower().strip(" .!\n") in _PLACEHOLDERS:
        return "probe"
    if (
        str(memory_type or "") == "reflection"
        or key.startswith("reflection:")
        or text.startswith('{"graph"')
        # a session's crystallized procedure (`procedure_<title slug>`, value a
        # {"graph": …}); at pool stage only its rendering is at hand. A mined
        # fix recipe (`procedure_fr_<hash>`) is prose knowledge and stays.
        or (_SESSION_PROCEDURE_KEY_RE.match(key) and str(memory_type or "") == "procedural")
    ):
        return "procedure"
    p = _patterns()
    if p.actor_start.search(text) and (
        is_activity_narration(text) or p.more_narration.search(text)
    ):
        return "narration"
    if p.code_location.search(text) or p.file_stats.search(text):
        return "code_location"
    if _TOOL_DOC_RE.search(text):
        return "tool_doc"
    if p.session_status.search(text):
        return "session_status"
    return _capture_junk_class(text, tags)


# The write-side `durable_facts` shapes, applied to rows already stored. A
# coding-session capture (`source:claude-code` / `source:codex` …) restating what a tool
# returned, and an observe replay's actor-led action.
_CODING_SOURCE_TAGS = ("source:claude-code", "source:codex", "source:cursor")


def _capture_junk_class(text: str, tags: Any) -> str | None:
    from server.memory.domain import durable_facts

    tag_set = {str(t).lower() for t in (tags or [])}
    if "from_observation" in tag_set and "crystallized" in tag_set:
        # Known actors only: a stored row naming a person ("Alex used the
        # bisect script for …") is left to the narration rule above.
        if durable_facts.replay_action_class({"value": text}, named=False):
            return "narration"
    if tag_set & set(_CODING_SOURCE_TAGS) or "from_observation" in tag_set:
        cls = durable_facts.capture_outcome_class({"value": text}) or (
            durable_facts.session_action_class({"value": text})
        )
        if cls:
            return cls
    return None


_WORD_RE = re.compile(r"[a-z]+|[0-9]+(?:\.[0-9]+)*")


def content_words(text: str) -> frozenset[str]:
    """The words a fact claims, minus the subject and function words; digits
    and letters split ("13pt" and "13 pt" are the same claim)."""
    return frozenset(w for w in _WORD_RE.findall(_text(text).lower()) if w not in _patterns().stop)


def _hit_text(h: Any) -> str:
    return re.sub(r"\s*\(#[^)]*\)\s*$", "", str(getattr(h, "content_excerpt", "") or ""))


_QUESTION_ECHO_RE = re.compile(r"^\s*\[user @[^\]]*\]", re.IGNORECASE)
FACT_LEAD_MARGIN = 0.15
_DIGEST_DATE_RE = re.compile(r"^\s*on\s+\d{4}-\d{2}-\d{2}\s*,\s*", re.IGNORECASE)


def chunk_junk_class(text: str) -> str | None:
    """A transcript chunk that is not evidence for anything: the user asking
    a question with no answer in the chunk (a re-asked question matches it
    word for word and outranks the fact that answers it), or Loom's own
    output quoted back (`is_loom_output` markers)."""
    from server.memory.domain.loom_output import is_loom_output

    t = str(text or "")
    if is_loom_output(t):
        return "loom_output"
    if _QUESTION_ECHO_RE.match(t) and "[assistant @" not in t and "?" in t:
        return "question_echo"
    return None


def facts_first(hits: list[Any], *, margin: float = FACT_LEAD_MARGIN) -> list[Any]:
    """A distilled fact ahead of the transcript it came from: every `fact` hit
    whose similarity is within `margin` of the best non-fact hit moves ahead
    of the non-facts, order otherwise kept. Without it a real fact at
    similarity 0.52 can rank 8th behind seven transcript chunks at 0.53-0.63,
    most of them the user's own earlier wording of the question."""

    def sim(h: Any) -> float:
        return float(getattr(h, "similarity", 0.0) or 0.0)

    non_facts = [h for h in hits if str(getattr(h, "source", "fact") or "fact") != "fact"]
    if not non_facts:
        return hits
    best = max(sim(h) for h in non_facts)
    best_rank = max(float(getattr(h, "rank_score", 0.0) or 0.0) for h in non_facts)
    lead = [h for h in hits if h not in non_facts and sim(h) >= best - margin]
    if not lead:
        return hits
    # The assembler re-sorts by rank_score, so the lead is lifted just above
    # the best transcript row (order among the lead kept) and says so.
    n = len(lead)
    for i, h in enumerate(lead):
        target = best_rank + 1e-4 * (n - i)
        if float(getattr(h, "rank_score", 0.0) or 0.0) < target:
            try:
                comps = dict(getattr(h, "score_components", None) or {})
                comps["fact_first_lift"] = round(target - float(h.rank_score or 0.0), 4)
                h.rank_score = target
                h.score_components = comps
            except Exception:  # noqa: BLE001 — a frozen hit keeps its rank
                pass
    lead_ids = {id(h) for h in lead}
    return lead + [h for h in hits if id(h) not in lead_ids]


def drop_junk_and_restatements(hits: list[Any]) -> tuple[list[Any], dict[str, int]]:
    """The fused pool without junk-class fact rows and without
    a fact whose claim is contained in a higher-ranked fact's (the same
    statement stored under a second key — "espresso" and "User's coffee type
    is Espresso"). Chunks and summaries pass untouched (they are not rows this
    classifier judges). Order is kept; returns (kept, counts by reason)."""
    kept: list[Any] = []
    seen: list[frozenset[str]] = []
    dropped: dict[str, int] = {}
    for h in hits:
        src = str(getattr(h, "source", "fact") or "fact")
        if src != "fact":
            if src == "chunk":
                ccls = chunk_junk_class(_hit_text(h))
            elif src == "summary":  # a digest line: "On <date>, the user edited …"
                ccls = junk_class("", _DIGEST_DATE_RE.sub("", _hit_text(h)))
            else:
                ccls = None
            if ccls:
                dropped[ccls] = dropped.get(ccls, 0) + 1
            else:
                kept.append(h)
            continue
        text = _hit_text(h)
        cls = junk_class(
            str(getattr(h, "memory_key", "") or ""),
            text,
            tags=getattr(h, "_row_tags", None) or getattr(h, "tags", None),
            memory_type=str(getattr(h, "memory_type", "") or ""),
        )
        if cls:
            dropped[cls] = dropped.get(cls, 0) + 1
            continue
        words = content_words(text)
        if words and any(words <= prev or prev <= words for prev in seen if prev):
            dropped["restatement"] = dropped.get("restatement", 0) + 1
            continue
        seen.append(words)
        kept.append(h)
    ordered = _drop_redundant_evidence(facts_first(kept), dropped)
    lifted = sum(
        1 for h in ordered if "fact_first_lift" in (getattr(h, "score_components", None) or {})
    )
    if lifted:
        dropped["fact_first_lift"] = lifted
    return ordered, dropped


def _drop_redundant_evidence(hits: list[Any], dropped: dict[str, int]) -> list[Any]:
    """A summary or chunk that states a fact ranked above it adds nothing the
    fact does not (the fact was distilled from it): the non-fact whose words
    contain a higher-ranked kept fact's claim leaves."""
    out: list[Any] = []
    facts: list[frozenset[str]] = []
    for h in hits:
        src = str(getattr(h, "source", "fact") or "fact")
        words = content_words(_hit_text(h))
        if src == "fact":
            if words:
                facts.append(words)
            out.append(h)
            continue
        if any(f and len(f) >= 2 and f <= words for f in facts):
            dropped["restated_evidence"] = dropped.get("restated_evidence", 0) + 1
            continue
        out.append(h)
    return out
