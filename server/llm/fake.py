"""Fake LLM client for unit tests and local dev — no network, deterministic.

Returns shaped JSON keyed off an anchor phrase in the system prompt, so the
extraction pipelines run end-to-end without a real LLM; falls back to a
synthesis marker otherwise.
"""

from __future__ import annotations

import json
import re

from server.llm import customer
from server.llm.client import LLMResult

_EXTRACTION_ANCHOR = "evidence-bound memory extractor"
# The assistant-rules block (server.memory.domain.extraction_prompts).
_AGENT_RULES_ANCHOR = "THE ASSISTANT'S OWN STANDING RULES"
# An assistant sentence the fake reads as a standing rule about itself.
_AGENT_RULE_RE = re.compile(r"^I (?:always|never) ")
_ASSISTANT_TURN_RE = re.compile(r"^\[(\d+):assistant[^\]]*\]\s*(.*)$", re.M)
_SCHEMA_ANCHOR = "extract one structured record"
_STAGE1_ANCHOR = "decide whether the source messages contain"
_STAGE2_ANCHOR = "decide which schema fields are CLEARLY mentioned"
_STAGE3_ANCHOR = "extract values for specific fields from source"
_PROFILE_ANCHOR = "You fill a structured profile of one user"
_PROFILE_PROP_RE = re.compile(r'^- "(?P<name>\w+)" \((?P<kind>[^)]*)\)', re.M)
_SCHEMA_FIELD_RE = re.compile(r'- "(?P<name>[^"]+)": (?P<type>\w+)')
_SCHEMA_ENUM_RE = re.compile(
    r'- "(?P<name>[^"]+)": one of \[(?P<values>[^\]]+)\]',
)
# Plain-arg form used in Stage 2/3 prompts:  - field_name: type_spec
_PLAIN_FIELD_RE = re.compile(r"-\s+(?P<name>\w+):\s+(?P<type>[\w\s]+)$", re.M)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# server.memory.domain.decision_extract's candidate prompt.
_DECISION_ANCHOR = "You extract design decisions"
_SECTION_RE = re.compile(
    r"^\[section: (?P<name>[^\]]*)\]\n\n(?P<body>.*?)(?=\n\n\[section: |\Z)", re.M | re.S
)
_CAMEL_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b")


def _decisions_from_sections(user: str) -> dict[str, object]:
    """One decision per section, resting on the section's first sentence copied as it is,
    so the extractor's verbatim check passes on what the fake returns."""
    out: list[dict[str, object]] = []
    for m in _SECTION_RE.finditer(user):
        first = _SENTENCE_SPLIT_RE.split(m["body"].strip(), 1)[0].split("\n", 1)[0].strip()
        if not re.search(r"[^\W\d_]", first):
            continue
        out.append(
            {
                "category": "design:fake",
                "scenario": m["name"],
                "outcome": first[:120],
                "reasoning": first,
                "alternatives": [],
                "quote": first,
                "is_constraint": False,
                "mentions": _CAMEL_RE.findall(first),
            }
        )
    return {"decisions": out}


def _synthesize_typed_record(system: str, user: str) -> dict[str, object]:
    """Parse the schema field list out of the typed-extraction prompt and
    fill it with type-appropriate stub values derived from the user
    prompt where possible."""
    # Enum fields parsed first so they shadow same-named non-enum matches.
    enum_fields: dict[str, list[str]] = {
        m["name"]: [v.strip(' "') for v in m["values"].split(",")]
        for m in _SCHEMA_ENUM_RE.finditer(system)
    }
    fields = {
        m["name"]: m["type"]
        for m in _SCHEMA_FIELD_RE.finditer(system)
        if m["name"] not in enum_fields
    }
    user_words = re.findall(r"[A-Za-z]+", user)[:8]
    user_numbers = re.findall(r"\d+(?:\.\d+)?", user)
    record: dict[str, object] = {}
    # First allowed value occurring in the user prompt; fallback to first allowed.
    for name, allowed in enum_fields.items():
        chosen = next(
            (v for v in allowed if v.lower() in user.lower()),
            allowed[0],
        )
        record[name] = chosen
    for name, type_name in fields.items():
        if type_name == "string":
            for word in user_words:
                if word.lower() in name.lower() or name.lower() in word.lower():
                    record[name] = word
                    break
            else:
                record[name] = user_words[0] if user_words else ""
        elif type_name == "integer":
            record[name] = int(float(user_numbers[0])) if user_numbers else 0
        elif type_name == "number":
            record[name] = float(user_numbers[0]) if user_numbers else 0.0
        elif type_name == "boolean":
            record[name] = bool(user_numbers)
        else:
            record[name] = ""
    entities = [w.lower() for w in re.findall(r"\b[A-Z][a-z]+\b", user)][:6]
    return {"record": record, "entities": entities}


def _agent_rules(user: str) -> list[dict[str, object]]:
    """Under the assistant-rules block: each current assistant sentence that
    opens "I always" / "I never", as an `about: "agent"` fact."""
    current = user.split("current_messages (extract from these):")[-1]
    rules: list[dict[str, object]] = []
    for m in _ASSISTANT_TURN_RE.finditer(current):
        for sentence in _SENTENCE_SPLIT_RE.split(m.group(2).strip()):
            sentence = sentence.strip()
            if not _AGENT_RULE_RE.match(sentence):
                continue
            words = re.findall(r"[a-zA-Z]+", sentence.lower())[1:5]
            rules.append(
                {
                    "key": "assistant_rule_" + "_".join(words),
                    "value": "Assistant rule: " + sentence,
                    "about": "agent",
                    "source_message_indices": [int(m.group(1))],
                    "evidence": sentence,
                }
            )
    return rules


def _fill_profile(system: str, user: str) -> dict[str, object]:
    """Fill each listed property from the newest fact line that names it (the
    property name with `_` read as a space), null when none does."""
    facts = [re.sub(r"^- \[[^\]]*\]\s*", "", line).strip() for line in user.splitlines()]
    out: dict[str, object] = {}
    for m in _PROFILE_PROP_RE.finditer(system):
        name, kind = m["name"], m["kind"]
        hits = [f.rstrip(".") for f in facts if name.replace("_", " ").lower() in f.lower()]
        numbers = re.findall(r"-?\d+(?:\.\d+)?", hits[-1]) if hits else []
        if not hits:
            out[name] = None
        elif kind.startswith("array"):
            out[name] = hits
        elif kind.startswith("integer"):
            out[name] = int(float(numbers[-1])) if numbers else None
        elif kind.startswith("number"):
            out[name] = float(numbers[-1]) if numbers else None
        elif kind.startswith("boolean"):
            out[name] = True
        else:
            out[name] = hits[-1]
    return out


def _extract_facts_from_user_prompt(
    user: str, *, agent_rules: bool = False
) -> dict[str, list[dict[str, object]]]:
    """Synthesize a few plausible facts from the messages body so tests
    can assert on a non-empty round-trip. Picks the first 3 sentences
    that look declarative; with `agent_rules`, the assistant's "I always" /
    "I never" sentences come back as rules instead."""
    rules = _agent_rules(user) if agent_rules else []
    facts: list[dict[str, object]] = list(rules)
    rule_sentences = {str(f["evidence"]).rstrip(".") for f in rules}
    seen: set[str] = set()
    # User prompt is "[role] content\n..." — strip role tags before splitting.
    stripped = re.sub(r"\[[^\]]+\]\s*", "", user).strip()
    for sentence in _SENTENCE_SPLIT_RE.split(stripped):
        sentence = sentence.strip().rstrip(".")
        if len(sentence) < 8 or sentence in rule_sentences:
            continue
        words = re.findall(r"[a-zA-Z]+", sentence.lower())[:4]
        if not words:
            continue
        key = "_".join(words)
        if key in seen:
            continue
        seen.add(key)
        entities_raw = re.findall(r"\b[A-Z][a-z]+\b", sentence)
        entities = list(dict.fromkeys(e.lower() for e in entities_raw))[:6]
        facts.append(
            {
                "key": key,
                "value": sentence + ".",
                "tags": ["fake-extracted"],
                "entities": entities,
            }
        )
        if len(facts) == 3 + len(rules):
            break
    return {"facts": facts}


class FakeLLMClient:
    provider = "fake"

    def __init__(self, model: str = "fake-echo-1") -> None:
        self.model = model

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
        temperature: float | None = 0.0,
    ) -> LLMResult:
        # As a metered provider would: a path that names no customer fails in tests.
        await customer.require()
        # response_format and temperature are no-ops — the anchors already return valid JSON.
        if _STAGE1_ANCHOR in system:
            # Detection stage: assume present so downstream stages run.
            synthesized = '{"present": true}'
        elif _STAGE2_ANCHOR in system:
            # Field-detection stage: return every schema field as detected.
            fields = [m["name"] for m in _PLAIN_FIELD_RE.finditer(system)]
            synthesized = json.dumps({"fields": fields})
        elif _STAGE3_ANCHOR in system or _SCHEMA_ANCHOR in system:
            synthesized = json.dumps(_synthesize_typed_record(system, user))
        elif _PROFILE_ANCHOR in system:
            synthesized = json.dumps(_fill_profile(system, user))
        elif _DECISION_ANCHOR in system:
            synthesized = json.dumps(_decisions_from_sections(user))
        elif _EXTRACTION_ANCHOR in system:
            payload = _extract_facts_from_user_prompt(
                user, agent_rules=_AGENT_RULES_ANCHOR in system
            )
            synthesized = json.dumps(payload)
        else:
            body = user[:200].replace("\n", " ").strip()
            synthesized = f"[fake-synthesis: {self.model}] {body}"
        return LLMResult(
            text=synthesized,
            tokens_in=len(user) // 4,  # rough char-count → token approximation
            tokens_out=len(synthesized) // 4,
            provider=self.provider,
            model=self.model,
        )
