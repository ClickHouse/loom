"""loom — Python SDK for the Loom Control Plane.

Primary surface:
- `Datapack(org, namespace)` — the encapsulating unit for an agentic app
- `Agent(org, namespace, agent)` — one runnable agent inside a datapack
- `login()` / `logout()` / `whoami()` — sign in once (`loom login`); every
  Agent given no api_key then uses the stored, self-refreshing session

A Datapack holds the (org, namespace) tuple plus platform defaults
(base_url, api_key, timeout) so `.agent(name)` spawns Agents without
repeating those. Every Agent has a client per wire-op group:
`.memory / .working_memory / .knowledge / .conversations / .profile /
.context / .facts / .learnings / .conflicts / .blacklist / .grants /
.erasure / .cost / .audit / .namespaces / .procedure / .run /
.tasks / .policy / .decision / .calibration / .briefs / .code / .tracker`,
and `.assistant` asks the Loom assistant (`stream` / `ask`).
"""

from loom._version import __version__
from loom.agent import Agent
from loom.assistant import AssistantAnswer, AssistantCitation, AssistantText
from loom.capabilities import Capabilities
from loom.client import OpsClient
from loom.credentials import login, logout, whoami
from loom.datapack import Datapack
from loom.errors import (
    INVALID_RESPONSE_CODE,
    NETWORK_ERROR_CODE,
    AuthInsufficient,
    AuthInvalid,
    BackendUnavailable,
    ErrorCategory,
    IdempotentConflict,
    LoomError,
    NotFound,
    PreconditionFailed,
    RateLimited,
    ValidationError,
)
from loom.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from loom.session import Resumed, Session
from loom.sync import SyncAgent
from loom.token import ROLE_RANK, TokenClaims, decode_token_claims
from loom.tools import LOOM_TOOL_SPECS, AgentTools, tool_definitions
from loom.transport import CallResult, RateLimit

__all__ = [
    "INVALID_RESPONSE_CODE",
    "LOOM_TOOL_SPECS",
    "NETWORK_ERROR_CODE",
    "ROLE_RANK",
    "Agent",
    "AgentTools",
    "AssistantAnswer",
    "AssistantCitation",
    "AssistantText",
    "AuthInsufficient",
    "AuthInvalid",
    "BackendUnavailable",
    "CallResult",
    "Capabilities",
    "Datapack",
    "ErrorCategory",
    "IdempotentConflict",
    "LoomError",
    "NotFound",
    "OpsClient",
    "PreconditionFailed",
    "DEFAULT_RETRY_POLICY",
    "RateLimit",
    "RateLimited",
    "Resumed",
    "RetryPolicy",
    "Session",
    "SyncAgent",
    "TokenClaims",
    "ValidationError",
    "__version__",
    "decode_token_claims",
    "login",
    "logout",
    "tool_definitions",
    "whoami",
]
