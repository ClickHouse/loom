"""The op and setting names an assistant answer writes that the build neither serves nor
documents. The instructions allow a name only from a tool's result; Loom cannot see those
results (they stay with the agent), so the check holds an answer to what it can decide: a
name is known when this build has it as an op or an environment variable, or when its
documentation writes it. Pure: the app layer hands in what is known."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

# A code span, as Markdown writes one on a line.
_SPAN = re.compile(r"`([^`\n]{1,160})`")
# A dotted op-shaped name, with an optional call after it (`memory.set(key=...)`).
_DOTTED = re.compile(r"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)(?:\(.*\))?")
# An environment variable of Loom's, anywhere in the answer; a trailing `_` names a family.
_ENV = re.compile(r"\bLOOM_[A-Z0-9_]*")


@dataclass(frozen=True)
class Known:
    # Every op this build serves.
    ops: Collection[str]
    # The first segment of each op name (`memory`, `knowledge`): a dotted name in another
    # namespace (`loom.memory`, `agent.yaml`) is not an op name and is not checked.
    groups: Collection[str]
    # Every environment variable this build reads, and the prefixes of the families it
    # reads by prefix.
    env: Collection[str]
    env_prefixes: tuple[str, ...]
    # Every whole identifier the documentation writes, lower-cased (`memory.written`,
    # `loom_client_id`).
    documented: Collection[str]


def candidates(answer: str) -> list[str]:
    """The names in `answer` that the check reads, in the order they first appear."""
    found: list[str] = []
    for span in _SPAN.finditer(answer):
        m = _DOTTED.fullmatch(span[1].strip())
        if m:
            found.append(m[1])
    found.extend(_ENV.findall(answer))
    return list(dict.fromkeys(n for n in found if n != "LOOM_"))


def _documented_family(prefix: str, names: Iterable[str]) -> bool:
    return any(n.startswith(prefix) for n in names)


def unknown(answer: str, known: Known) -> list[str]:
    """The names of `answer` that are none of `known`'s, in order."""
    out: list[str] = []
    for name in candidates(answer):
        lowered = name.lower()
        if name.startswith("LOOM_"):
            if name.endswith("_"):
                ok = _documented_family(name, known.env) or _documented_family(
                    lowered, known.documented
                )
            else:
                ok = (
                    name in known.env
                    or name.startswith(known.env_prefixes)
                    or lowered in known.documented
                )
        else:
            ok = (
                name.split(".", 1)[0] not in known.groups
                or name in known.ops
                or lowered in known.documented
            )
        if not ok:
            out.append(name)
    return out


__all__ = ["Known", "candidates", "unknown"]
