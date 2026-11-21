"""Check operation and setting names against this build's public documentation."""

from __future__ import annotations

from server.assistant.domain import names
from server.help.app import docs as help_docs
from server.help.domain import index as help_index


async def unknown_names(answer_text: str) -> list[str]:
    """The op and LOOM_* names `answer_text` writes that this build neither serves nor
    documents; none when the documentation cannot be read, since every name it writes
    would then look unknown."""
    ix = await help_docs.index()
    if ix is None:
        return []
    return names.unknown(answer_text, _known(ix))


# What `_known` built, and for which index: the index lives as long as the process.
_known_for: tuple[help_index.Index, names.Known] | None = None


def _known(ix: help_index.Index) -> names.Known:
    """What this build knows: its ops, its environment variables and its documentation's
    identifiers, built once per index."""
    global _known_for
    if _known_for is not None and _known_for[0] is ix:
        return _known_for[1]
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.settings import Settings
    from server.settings._env import NON_SETTINGS_ENV, NON_SETTINGS_ENV_PREFIXES

    known = names.Known(
        ops=frozenset(OPERATIONS_BY_NAME),
        groups=frozenset(n.split(".", 1)[0] for n in OPERATIONS_BY_NAME),
        env=frozenset(f"LOOM_{f.upper()}" for f in Settings.model_fields) | NON_SETTINGS_ENV,
        env_prefixes=tuple(NON_SETTINGS_ENV_PREFIXES),
        documented=ix.identifiers,
    )
    _known_for = (ix, known)
    return known


__all__ = ["unknown_names"]
