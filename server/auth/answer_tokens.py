"""Identify bounded assistant credentials, including their delegated descendants."""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.observability.consumer import ASSISTANT_CONSUMER

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo


ANSWER_JTI_PREFIX = "assistant-answer-"


def is_answer_token(auth: AuthInfo) -> bool:
    return (
        auth.consumer == ASSISTANT_CONSUMER
        or auth.token_id.startswith(ANSWER_JTI_PREFIX)
        or any(parent.startswith(ANSWER_JTI_PREFIX) for parent in auth.parents)
    )
