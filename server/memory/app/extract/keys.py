"""Deterministic rewrites of the memory keys the model writes.

The sanitised key shape, entity-stable keys (a concept key such as
`user_task_status` becomes `pr107585_status` when the fact names a
high-precision identifier), per-run state keys scoped to the session's task,
relation-canonical keys, and attribute suffixes (added when the value states
the attribute, stripped when they repeat the value or mark a variant). The
detectors favour precision over recall: a false rewrite would merge two
unrelated facts under one key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from server.memory.app.extract.models import ExtractedFact

_KEY_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def _sanitize_key(raw: str) -> str:
    """Memory keys are stored as-is in ClickHouse — clamp to a snake-case
    shape so callers can predict what they'll see later."""
    cleaned = _KEY_SANITIZE_RE.sub("_", raw.lower()).strip("_")
    return cleaned[:64] if cleaned else "extracted_fact"


# Generic leading subject tokens dropped when deriving the concept suffix,
# so `user_task_status` under entity `pr107585` becomes `pr107585_status`
# (not `pr107585_user_task_status`). Never stripped down to nothing.
_GENERIC_KEY_PREFIXES = frozenset(
    {
        "user",
        "the",
        "my",
        "our",
        "your",
        "their",
        "task",
        "current",
        "assistant",
        "agent",
        "a",
        "an",
    }
)

# PR / issue references. No trailing \b after the keyword so `pr107585`
# (no separator) still matches; ≥2 digits so a bare "pr 5" doesn't.
_PR_RE = re.compile(
    r"\b(?:pr|pull\s*request|pull|gh|mr)[\s:#/_-]*(\d{2,7})\b",
    re.IGNORECASE,
)
_ISSUE_RE = re.compile(r"\bissue[\s:#/_-]*(\d{2,7})\b", re.IGNORECASE)
# Bare `#1234` GitHub-style reference; ≥3 digits to avoid `#1` noise and
# markdown fragments. Treated as a PR-style entity (the common case).
_HASH_RE = re.compile(r"(?:^|[^\w])#(\d{3,7})\b")
# ClickHouse functional test ids: an explicit `test 04653` keyword form,
# and the bare `04653_stateless_name` file-name convention (5 digits +
# `_word`), which is specific enough to be high-precision on its own.
_TEST_RE = re.compile(r"\btest[\s:#/_-]*(\d{4,6})\b", re.IGNORECASE)
_TEST_FILE_RE = re.compile(r"\b(\d{5})_[a-z]", re.IGNORECASE)
# @handle: must start with a letter, 2-30 chars, and NOT be preceded by a
# word char (excludes the local part of an email like `foo@bar`).
_HANDLE_RE = re.compile(r"(?:^|[^\w@])@([a-z][a-z0-9_-]{1,29})\b", re.IGNORECASE)
# Agent task slugs: an ISO date followed by a
# hyphenated slug (`2026-08-30-fix-flaky-merge-test`). The
# date prefix plus ≥6 slug chars is specific enough to be a high-precision
# identifier; the stable form keeps the date and the first three slug words
# (`task20260830_fix_flaky_merge`) so keys stay short and human-readable.
_TASK_SLUG_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})-([a-z0-9][a-z0-9-]{5,})", re.IGNORECASE)


def _task_slug_entity(m: re.Match[str]) -> str:
    words = [w for w in m.group(4).lower().split("-") if w][:3]
    return f"task{m.group(1)}{m.group(2)}{m.group(3)}_" + "_".join(words)


def _detect_entity_slug(
    entities: list[str] | None,
    value: str,
    tags: list[str] | None = None,
) -> str | None:
    """Return a short, stable entity slug for a fact, or None when no
    high-precision identifier is present. Detectors are checked in
    priority order (explicit PR/issue > test id > bare #ref > @handle);
    the first that matches the combined text wins."""
    surfaces: list[str] = []
    for e in entities or []:
        s = str(e).strip()
        if s:
            surfaces.append(s)
    for t in tags or []:
        s = str(t).strip()
        if s:
            surfaces.append(s)
    if value:
        surfaces.append(str(value))
    text = " ".join(surfaces)
    if not text:
        return None

    m = _PR_RE.search(text)
    if m:
        return f"pr{m.group(1)}"
    m = _ISSUE_RE.search(text)
    if m:
        return f"issue{m.group(1)}"
    m = _TEST_RE.search(text)
    if m:
        return f"test{m.group(1)}"
    m = _TEST_FILE_RE.search(text)
    if m:
        return f"test{m.group(1)}"
    m = _HASH_RE.search(text)
    if m:
        return f"pr{m.group(1)}"
    m = _TASK_SLUG_RE.search(text)
    if m:
        return _task_slug_entity(m)
    m = _HANDLE_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


def _concept_suffix(concept_key: str) -> str:
    """Strip leading generic subject tokens from a concept key so the
    entity-stable form stays short and field-distinct. Never strips to
    empty — a key that is ALL generic tokens is returned as-is."""
    parts = [p for p in concept_key.split("_") if p]
    while len(parts) > 1 and parts[0] in _GENERIC_KEY_PREFIXES:
        parts = parts[1:]
    return "_".join(parts)


def entity_stable_key(
    concept_key: str,
    entities: list[str] | None,
    value: str,
    tags: list[str] | None = None,
) -> str:
    """Rewrite a concept-level memory key to an entity-stable form when the
    fact carries a salient identifier; otherwise return the (sanitized)
    concept key unchanged. Pure + deterministic; `rewrite_fact_key` applies
    it to a fact.

    `user_task_status` + entity `#107585` -> `pr107585_status`. The result
    is sanitized and clamped to the 64-char key cap like any other key.
    """
    base = _sanitize_key(concept_key)
    slug = _detect_entity_slug(entities, value, tags)
    if not slug:
        return base
    suffix = _concept_suffix(base)
    combined = f"{slug}_{suffix}" if suffix else slug
    return _sanitize_key(combined)


# Keys whose concept is per-RUN state of the agent — the task it is on, the
# slot it holds, the phase it is in. Written once per session under the same
# key, each session buries the previous one. Matched on the sanitized
# key: an optional generic subject, an optional `clickhouse_`, then the token.
_SESSION_SCOPED_KEY_RE = re.compile(
    r"^(?:(?:user|my|our|the|current|agent|assistant)_)?(?:clickhouse_)?"
    r"(?:task|pipeline|worker|run|job|ticket)_"
)


def batch_task_slug(facts: list[ExtractedFact]) -> str | None:
    """The ONE task slug a batch of facts (one session) names, or None when
    it names none or more than one. A batch that mentions two tasks gives no
    licence to key its generic state facts by either."""
    slugs: set[str] = set()
    for f in facts:
        text = " ".join([str(f.value or ""), *(f.entities or []), *(f.tags or [])])
        for m in _TASK_SLUG_RE.finditer(text):
            slugs.add(_task_slug_entity(m))
            if len(slugs) > 1:
                return None
    return next(iter(slugs)) if len(slugs) == 1 else None


def scope_session_state_keys(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """Differentiation for per-run state. A fact whose key is
    per-run state (`user_task_status`, `user_clickhouse_worker_slot`) and
    carries no identifier of its own takes the batch's single task slug as
    its entity, so `user_task_status` from a session about task X becomes
    `task20260830_fix_flaky_merge_status` and the next session's status about
    task Y does not supersede it. Facts with their own entity, batches with
    zero or several slugs, and keys that are not per-run state are untouched.
    Pure."""
    slug = batch_task_slug(facts)
    if not slug:
        return facts
    out: list[ExtractedFact] = []
    for f in facts:
        base = _sanitize_key(f.memory_key)
        if _detect_entity_slug(f.entities, f.value, f.tags):
            out.append(f)  # keyed by its own identifier already (or about to be)
            continue
        if not _SESSION_SCOPED_KEY_RE.search(base):
            out.append(f)
            continue
        suffix = _concept_suffix(base)
        out.append(replace(f, memory_key=_sanitize_key(f"{slug}_{suffix}")))
    return out


def relation_canonical_key(
    fact: ExtractedFact, declared: Mapping[str, str] | None = None
) -> str | None:
    """`<subject>_<relation>` when the fact carries exactly one
    typed relation, that relation holds one value at a time
    (`is_single_valued_relation`, seed list or the namespace's declaration)
    and its subject is one of the fact's entities. None otherwise — a
    multi-valued relation (`friend_of`) under one key would make
    supersession retire real facts, and two relations leave the attribute
    ambiguous."""
    from server.memory.domain.graph_quality import is_single_valued_relation

    rels = [r for r in (fact.relations or []) if isinstance(r, (list, tuple)) and len(r) == 3]
    if len(rels) != 1:
        return None
    subject, verb, obj = (str(x or "").strip().lower() for x in rels[0])
    if not subject or not verb or not obj:
        return None
    if not is_single_valued_relation(verb, declared):
        return None
    ents = {str(e or "").strip().lower() for e in (fact.entities or [])}
    if subject not in ents and not fact.value.lower().lstrip('"').startswith(subject):
        return None
    # The key spells the relation as the model wrote it (not its stem), so it
    # matches the key a model writes for the same attribute when another
    # version of the fact carries no relation.
    phrases = list(fact.relation_phrases or [])
    readable = phrases[0].strip().lower() if len(phrases) == len(fact.relations or []) else ""
    key = _sanitize_key(f"{subject}_{readable or verb}")
    return key or None


def apply_relation_keys(
    facts: list[ExtractedFact], declared: Mapping[str, str] | None = None
) -> list[ExtractedFact]:
    """Rewrite each fact's key to its relation-canonical form where one
    exists; keys without one are left as the model wrote
    them. Pure; keeps order; never drops a fact."""
    out: list[ExtractedFact] = []
    for f in facts:
        key = relation_canonical_key(f, declared)
        out.append(replace(f, memory_key=key, key_from_relation=True) if key else f)
    return out


def rewrite_fact_key(fact: ExtractedFact) -> ExtractedFact:
    """`entity_stable_key` applied to one fact; the fact is returned unchanged
    when it carries no salient entity."""
    new_key = entity_stable_key(fact.memory_key, fact.entities, fact.value, fact.tags)
    if new_key == fact.memory_key:
        return fact
    return replace(fact, memory_key=new_key)


# Value shapes that name an attribute: the key of such a fact
# must end in the attribute so facet completion can address the set.
# Patterns are conservative — a value must state the attribute plainly.
_ATTRIBUTE_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "age",
        re.compile(
            r"\b(is|was|turned|aged)\s+\d{1,3}\s+years?\s+old\b|\b\d{1,3}\s+years?\s+old\b", re.I
        ),
    ),
    ("birthday", re.compile(r"\bbirthday\b|\bwas born on\b|\bborn on\b", re.I)),
    ("height", re.compile(r"\b\d{2,3}\s?cm\b|\b\d\s?(ft|feet)\b|\btall\b", re.I)),
    (
        "duration",
        re.compile(
            r"\blasts?\s+(for\s+)?(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+|a whole|about|around)\b.*\b(day|days|week|weeks|month|months|year|years|hour|hours)\b",
            re.I,
        ),
    ),
    (
        "location",
        re.compile(
            r"\b(is|are|was)\s+(happening|held|located|based|taking place|hosted)\s+(in|at)\b|\bwill be held in\b",
            re.I,
        ),
    ),
    (
        "hometown",
        re.compile(
            r"\b(is|are|was)\s+(originally\s+)?from\s+[A-Z]|\bhails? from\b|\bgrew up in\b|\bhometown\b",
            re.I,
        ),
    ),
    (
        "attendance",
        re.compile(
            r"\b(attendees|attendance|a crowd of|draws? a crowd|scale of|can accommodate|capacity of|turnout of)\b",
            re.I,
        ),
    ),
)
_ATTRIBUTE_SUFFIX_SYNONYMS: dict[str, tuple[str, ...]] = {
    "age": ("age",),
    "birthday": ("birthday", "birthdate", "born"),
    "height": ("height",),
    "duration": ("duration", "length"),
    "location": ("location", "venue", "city", "place"),
    "hometown": ("hometown", "origin", "from", "city"),
    "attendance": ("attendance", "scale", "capacity", "size", "crowd", "attendees", "turnout"),
}


def attribute_of_value(value: str) -> str:
    """The attribute a fact value plainly states, or ''."""
    v = value or ""
    for name, pat in _ATTRIBUTE_VALUE_PATTERNS:
        if pat.search(v):
            return name
    return ""


def strip_value_suffix(key: str, value: str) -> str:
    """`thomas_arne_birthplace_bengaluru` → `thomas_arne_birthplace` when the
    key's last token is a proper noun the VALUE states: the
    extractor minted a new key for an updated value instead of re-using the
    attribute's key, so the old and new rows both stayed live and the store
    could not supersede. Only a token that appears capitalised in the value
    (a name, not an attribute word like "sport" or "location") is stripped,
    at most two, only on keys of four or more parts, never below two. Pure."""
    parts = key.split("_")
    if len(parts) < 4:
        return key
    caps = {m.lower() for m in re.findall(r"\b[A-Z][\w\-]+", value or "")}
    if not caps:
        return key
    n = 0
    while len(parts) > 2 and n < 2 and parts[-1].lower() in caps:
        parts.pop()
        n += 1
    return "_".join(parts) if n else key


def strip_value_suffixes(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """Apply `strip_value_suffix` to every fact. Pure."""
    out: list[ExtractedFact] = []
    for f in facts:
        new_key = strip_value_suffix(f.memory_key or "", f.value or "")
        out.append(replace(f, memory_key=new_key) if new_key != f.memory_key else f)
    return out


def strip_variant_suffixes(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """`andy_warhol_death_location_alternate` → `andy_warhol_death_location`:
    the extractor was avoiding the key it had already used
    for the attribute; the store keeps every version under one key. Pure."""
    from server.memory.app.key_supersession import strip_variant_suffix

    out: list[ExtractedFact] = []
    for f in facts:
        new_key = strip_variant_suffix(f.memory_key or "")
        out.append(replace(f, memory_key=new_key) if new_key != f.memory_key else f)
    return out


def normalize_attribute_keys(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """Append the attribute to a key that lacks it (enforced):
    `user_niece_maya` with value "…is 26 years old" becomes
    `user_niece_maya_age`. A key already ending in the attribute or a synonym
    is untouched; so is a fact whose value states no attribute. Pure; keeps
    order; never drops a fact."""
    out: list[ExtractedFact] = []
    for f in facts:
        attr = attribute_of_value(f.value)
        if not attr:
            out.append(f)
            continue
        key = f.memory_key or ""
        last = key.rsplit("_", 1)[-1].lower() if "_" in key else key.lower()
        # keys like `..._duration_2` (a second value for the same attribute)
        stem = key.rsplit("_", 1)[0] if last.isdigit() and "_" in key else key
        stem_last = stem.rsplit("_", 1)[-1].lower() if "_" in stem else stem.lower()
        syns = _ATTRIBUTE_SUFFIX_SYNONYMS[attr]
        if last == attr or stem_last == attr:
            out.append(f)
            continue
        if last in syns:
            # `culinary_chat_venue` → `culinary_chat_location`: one suffix per
            # attribute, so the facet lookup's endsWith finds the whole set.
            out.append(replace(f, memory_key=key[: -len(last)] + attr))
            continue
        if stem_last in syns:
            digits = key[len(stem) :]  # "_2"
            out.append(replace(f, memory_key=stem[: -len(stem_last)] + attr + digits))
            continue
        out.append(replace(f, memory_key=f"{key}_{attr}"))
    return out
