"""Fake LLM client for unit tests and local dev — no network, deterministic.

Returns shaped JSON keyed off an anchor phrase in the system prompt, so the
extraction pipelines run end-to-end without a real LLM; falls back to a
synthesis marker otherwise.
"""

from __future__ import annotations

import json
import re

from server.llm.client import LLMResult

_EXTRACTION_ANCHOR = "evidence-bound memory extractor"
_SCHEMA_ANCHOR = "extract one structured record"
_STAGE1_ANCHOR = "decide whether the source messages contain"
_STAGE2_ANCHOR = "decide which schema fields are CLEARLY mentioned"
_STAGE3_ANCHOR = "extract values for specific fields from source"
_SCHEMA_FIELD_RE = re.compile(r'- "(?P<name>[^"]+)": (?P<type>\w+)')
_SCHEMA_ENUM_RE = re.compile(
    r'- "(?P<name>[^"]+)": one of \[(?P<values>[^\]]+)\]',
)
# Plain-arg form used in Stage 2/3 prompts:  - field_name: type_spec
_PLAIN_FIELD_RE = re.compile(r"-\s+(?P<name>\w+):\s+(?P<type>[\w\s]+)$", re.M)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


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


def _extract_facts_from_user_prompt(user: str) -> dict[str, list[dict[str, object]]]:
    """Synthesize a few plausible facts from the messages body so tests
    can assert on a non-empty round-trip. Picks the first 3 sentences
    that look declarative."""
    facts: list[dict[str, object]] = []
    seen: set[str] = set()
    # User prompt is "[role] content\n..." — strip role tags before splitting.
    stripped = re.sub(r"\[[^\]]+\]\s*", "", user).strip()
    for sentence in _SENTENCE_SPLIT_RE.split(stripped):
        sentence = sentence.strip().rstrip(".")
        if len(sentence) < 8:
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
        if len(facts) == 3:
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
        elif _EXTRACTION_ANCHOR in system:
            payload = _extract_facts_from_user_prompt(user)
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
