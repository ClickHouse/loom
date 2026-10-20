"""What a request's `rerank` or a namespace's `rerank_endpoint` may name.

Loom never posts candidate text to a URL a caller sends. The choices are ""
(the default), "none", a built-in reranker (`builtin:openai[:<model>]`,
`builtin:cohere[:<model>]`), or the name of an endpoint the operator
configured in LOOM_RERANK_ENDPOINTS_JSON.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

from server.settings import settings
from server.settings._providers import is_builtin_cohere

BUILTIN_OPENAI = "builtin:openai"
BUILTIN_COHERE = "builtin:cohere"


def is_builtin(name: str) -> bool:
    """`builtin:openai` / `builtin:cohere`, optionally with `:<model>`."""
    return any(name == b or name.startswith(b + ":") for b in (BUILTIN_OPENAI, BUILTIN_COHERE))


def is_rerank_choice(name: str) -> bool:
    """True when `name` is one a request or a namespace may send."""
    return name in ("", "none") or is_builtin(name) or name in settings.rerank_endpoint_map()


def check_rerank_name(value: str) -> str:
    """The stripped choice, or ValueError (a 422) listing what may be sent."""
    name = value.strip()
    why = settings.gateway_required_by()
    if why and is_builtin_cohere(name):
        # Cohere's own API takes no gateway; the reranker would fail open unseen.
        raise ValueError(
            f"builtin:cohere is refused while {why} is set: rerank with "
            "builtin:openai or an endpoint behind the gateway"
        )
    if is_rerank_choice(name):
        return name
    configured = ", ".join(sorted(settings.rerank_endpoint_map())) or "none configured"
    raise ValueError(
        "rerank takes a name, not a URL: '', 'none', 'builtin:openai[:<model>]', "
        f"'builtin:cohere[:<model>]' or a configured endpoint ({configured})"
    )


RerankName = Annotated[str, AfterValidator(check_rerank_name)]
