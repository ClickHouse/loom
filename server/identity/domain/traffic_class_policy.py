"""LLM tier eligibility by traffic class.

`LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` is `{class: {"llm_cache": bool,
"strong": bool, "mid": bool}}`. `llm_cache` overrides
the exact-prompt cache's deployment default for the class (a per-request pin
still wins); `strong` and `mid` say whether the cascade may escalate to those tiers — false means
the class answers on the cheap tier alone. Every tier is eligible for every
class unless the map says otherwise, because eligibility moves answers and
the plan's rule is precision first; the shipped map only turns the
exact-prompt cache on for `bench`, where it measured answer-identical.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from server.settings import settings

log = logging.getLogger("loom.traffic_class")


@dataclass(frozen=True)
class ClassLLMPolicy:
    llm_cache: bool | None = None  # None = the deployment default decides
    strong: bool = True
    mid: bool = True

    def tier_allowed(self, tier: str) -> bool:
        t = str(tier or "").strip().lower()
        if t == "strong":
            return self.strong
        if t == "mid":
            return self.mid
        return True  # cheap is always eligible


NEUTRAL = ClassLLMPolicy()

_RAW: str | None = None
_PARSED: dict[str, ClassLLMPolicy] = {}


def _policies() -> dict[str, ClassLLMPolicy]:
    global _RAW, _PARSED
    raw = str(settings.llm_policy_by_traffic_class)
    if raw == _RAW:
        return _PARSED
    parsed: dict[str, ClassLLMPolicy] = {}
    if raw.strip():
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("not a JSON object")
            for cls, values in obj.items():
                if not isinstance(values, dict):
                    raise ValueError(f"{cls!r}: expected an object")
                for key, value in values.items():
                    known = ("llm_cache", "strong", "mid")
                    if key not in known or not isinstance(value, bool):
                        raise ValueError(f"{cls!r}.{key!r}: expected a boolean tier flag")
                parsed[str(cls).strip().lower()] = ClassLLMPolicy(
                    llm_cache=values.get("llm_cache"),
                    strong=bool(values.get("strong", True)),
                    mid=bool(values.get("mid", True)),
                )
        except (ValueError, TypeError) as exc:
            log.warning("LOOM_LLM_POLICY_BY_TRAFFIC_CLASS ignored: %s", exc)
            parsed = {}
    _RAW = raw
    _PARSED = parsed
    return parsed


def llm_policy_for(traffic_class: str) -> ClassLLMPolicy:
    """The policy for one class; the neutral policy for '' or an unlisted class."""
    if not traffic_class:
        return NEUTRAL
    return _policies().get(str(traffic_class).strip().lower(), NEUTRAL)


__all__ = ["NEUTRAL", "ClassLLMPolicy", "llm_policy_for"]
