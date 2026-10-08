"""The kind of a prose question: a *responsibility* question asks how or where the code does
something ("how does the server enforce the memory limit"); an *incident* names a symptom or a
change the way an issue or PR title does ("Received signal -1", "Fix crash in sequenceMatch",
"Backport #95164 to 25.10"). The two read the same legs but not the same votes: the file-card and
vocabulary legs lift responsibility questions and cost incident titles, so they are gated on the
kind. A multi-word topic phrase with no incident or change-title signal reads as a
responsibility question: coding agents search with the phrase, not the question. Deterministic,
no model, unit-tested; unsure = "other".
"""

from __future__ import annotations

import re

RESPONSIBILITY = "responsibility"
INCIDENT = "incident"
OTHER = "other"

_INTERROGATIVE_RE = re.compile(
    r"^(how|where|why|what|when|which|who|is|are|does|do|can|should|could|explain|describe|"
    r"show|find|tell)\b",
    re.I,
)
_RESPONSIBILITY_PHRASE_RE = re.compile(
    r"\b(how (does|do|is|are|did|can|would|should)|where (is|are|does|do|did)|"
    r"what (happens|does|do|is|are)|why (does|do|is|are|did)|when (does|do|is|are)|"
    r"which (part|component|class|function|module|file)|responsible for|in charge of|"
    r"implemented|implementation of|mechanism|decide[sd]? (to|whether|which)|"
    r"chosen|picked|enforced|scheduled|handled|resolved|evaluated|computed|persisted)\b",
    re.I,
)
_INCIDENT_WORD_RE = re.compile(
    r"\b(fix(es|ed)?|bug|crash(es|ed)?|segfault|regression|fail(s|ed|ure|ing)?|"
    r"error|exception|assert(ion)?|wrong|incorrect|unexpected|broken|hang(s|ing)?|"
    r"stuck|leak(s|ed)?|timeout|timed out|cannot|can't|doesn't|does not|not work(ing)?|"
    r"panic|abort(ed)?|corrupt(ed|ion)?|mismatch|missing|invalid|received signal|"
    r"logical_error|backport|bump|revert|cherry[- ]pick|flaky|slow(er|ness)?|"
    r"performance issue|degradation|oom|out of memory|deadlock|race condition|"
    r"issue with|problem with|trouble with|inconsistent|unable to)\b",
    re.I,
)
_CHANGE_TITLE_RE = re.compile(
    r"^(fix|add|allow|support|improve|make|remove|disable|enable|update|refactor|"
    r"implement|introduce|optimi[sz]e|speed ?up|reduce|increase|use|avoid|prevent|"
    r"handle|correct|adjust|rename|move|drop|delete|change|set|do not|don't|"
    r"backport|bump|revert|merge|sync|try|test|better|more|less|less|fewer)\b",
    re.I,
)
_ISSUE_REF_RE = re.compile(r"#\d{3,}|\b\d+\.\d+(\.\d+)?\b")


def question_kind(query: str) -> str:
    """`responsibility` | `incident` | `other` for a prose query."""
    q = (query or "").strip()
    if not q or " " not in q:
        return OTHER
    low = q.lower()
    interrogative = bool(_INTERROGATIVE_RE.match(q)) or q.endswith("?")
    responsibility_phrase = bool(_RESPONSIBILITY_PHRASE_RE.search(low))
    incident_word = bool(_INCIDENT_WORD_RE.search(low))
    change_title = bool(_CHANGE_TITLE_RE.match(q))
    issue_ref = bool(_ISSUE_REF_RE.search(q))
    if interrogative or responsibility_phrase:
        # a question about a symptom is still a question ("why does X crash")
        # unless it reads as a title with a change verb and no interrogative
        if incident_word and not interrogative:
            return INCIDENT
        return RESPONSIBILITY
    if incident_word or change_title or issue_ref:
        return INCIDENT
    # A topic phrase — several words naming what the code does ("replicated insert
    # deduplication block id") — is how coding agents search: a responsibility question
    # without its interrogative.
    if len(re.findall(r"[A-Za-z_]\w*", q)) >= 2:
        return RESPONSIBILITY
    return OTHER


def kind_allowed(kind: str, allowed: str) -> bool:
    """`allowed` is a comma-separated list of kinds, or `any`."""
    spec = {s.strip().lower() for s in (allowed or "").split(",") if s.strip()}
    return "any" in spec or kind in spec
