"""Field-level fold of a typed record onto the active record it supersedes.

"Newest wins, whole record" for two writes of one logical key would let an
incoming record with an empty field clobber a filled one, and count two
spellings of one value as a change. The fold rules, in order, per field:

  1. incoming empty, prior filled  -> keep prior          (filled_from_prior)
  2. prior empty                   -> take incoming       (added)
  3. equivalent after NFKC, casefold and whitespace collapse
                                   -> take incoming, no change (agreed)
  4. both lists                    -> union, prior order first, dedupe on
                                      the canonical form  (unioned)
  5. otherwise                     -> incoming wins, and the pair is a
                                      CONFLICT the caller records (changed)

Identity fields never reach here differently: the canonical key already
agreed, or the write would not be a supersession. The loser of rule 5
stays queryable through memory.record.history (the superseded row keeps
its record_json) and through loom.memory_record_conflicts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

_WS = re.compile(r"\s+")


def canonical(value: Any) -> str:
    """Equivalence form of one field value: NFKC, casefold, whitespace
    collapsed; lists sort their canonical members; dicts serialise sorted."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(float(value)) if isinstance(value, float) else str(value)
    if isinstance(value, list | tuple | set):
        return "|".join(sorted(c for c in (canonical(v) for v in value) if c))
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).casefold()
    s = unicodedata.normalize("NFKC", str(value)).casefold()
    return _WS.sub(" ", s).strip()


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool | int | float):
        return False
    return canonical(value) == ""


@dataclass
class FoldResult:
    merged: dict[str, Any]
    agreed: list[str] = field(default_factory=list)
    filled_from_prior: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    unioned: list[str] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "agreed": list(self.agreed),
            "filled_from_prior": list(self.filled_from_prior),
            "added": list(self.added),
            "unioned": list(self.unioned),
            "changed": [dict(c) for c in self.changed],
        }


def fold(prior: dict[str, Any], incoming: dict[str, Any]) -> FoldResult:
    """Fold `incoming` onto `prior` field by field (rules in the module
    docstring). Field order: prior's, then incoming's new fields."""
    merged: dict[str, Any] = {}
    out = FoldResult(merged=merged)
    names = list(prior) + [n for n in incoming if n not in prior]
    for name in names:
        p, i = prior.get(name), incoming.get(name)
        if is_empty(i):
            if is_empty(p):
                merged[name] = i if name in incoming else p
            else:
                merged[name] = p
                out.filled_from_prior.append(name)
            continue
        if is_empty(p):
            merged[name] = i
            out.added.append(name)
            continue
        if canonical(p) == canonical(i):
            merged[name] = i
            out.agreed.append(name)
            continue
        if isinstance(p, list) and isinstance(i, list):
            seen = {canonical(v) for v in p}
            union = list(p)
            for v in i:
                c = canonical(v)
                if c and c not in seen:
                    seen.add(c)
                    union.append(v)
            merged[name] = union
            out.unioned.append(name)
            continue
        merged[name] = i
        out.changed.append({"field": name, "prior": p, "incoming": i})
    return out


def restate(prior: dict[str, Any], incoming: dict[str, Any]) -> FoldResult:
    """`incoming` as the whole record: nothing is kept from `prior`. Reports what agreed,
    was added or changed, like `fold`."""
    out = FoldResult(merged=dict(incoming))
    for name, i in incoming.items():
        p = prior.get(name)
        if is_empty(i):
            continue
        if is_empty(p):
            out.added.append(name)
        elif canonical(p) == canonical(i):
            out.agreed.append(name)
        else:
            out.changed.append({"field": name, "prior": p, "incoming": i})
    return out
