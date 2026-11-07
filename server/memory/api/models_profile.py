"""profile.* — named, char-limited, agent-editable always-loaded sections.

Set/append/replace are thin shims over memory.set/get pinning
memory_type='profile'. They exist so MCP exposes named tools agents can
discover by intent ("rewrite my persona" → profile.replace) rather than
knowing memory.set with memory_type='profile' is the right call. Reads
(get/list) aren't shimmed — they ride the existing memory ops.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from server.memory.api.models_common import _Identity
from server.memory.domain.delete_marks import caller_trace

# Matches the SDK constant.
DEFAULT_CHAR_LIMIT = 8192

# --------------------------------------------------------------------
# Inputs / outputs
# --------------------------------------------------------------------


class ProfileSetInput(_Identity):
    # The section's name (its memory key), e.g. `persona`; setting it again replaces its text.
    label: str
    # The section's whole new text.
    value: str
    # The section's size cap, measured on the JSON-encoded value; a longer value is a 422.
    char_limit: int = DEFAULT_CHAR_LIMIT
    # Where the section came from; a trace starting with `memory.delete_version:` is
    # refused (that op's tombstone marker). A worker that folds facts with an LLM
    # writes "profile.fold:<label>:<memory ids>" so provenance classes the row `derived`;
    # a client writing a persona leaves it empty (`asserted`).
    source_trace: str = ""
    # Write only if the section's current content hash equals this (409 otherwise);
    # empty = no check.
    if_match_sha256: str = ""

    @field_validator("source_trace")
    @classmethod
    def _source_trace(cls, v: str) -> str:
        return caller_trace(v)  # memory.set's own check, before the handler builds its input


class ProfileAppendInput(_Identity):
    # The section to append to; a missing section is created with the default size cap.
    label: str
    # Text added at the end of the section.
    addition: str
    # Put between the existing text and `addition`; unused when the section is new.
    separator: str = "\n"


class ProfileReplaceInput(_Identity):
    # The section to edit; a missing section is a 404.
    label: str
    # Text to find; every occurrence is replaced, and absent text is a 422.
    old: str
    # Replacement text; empty deletes the occurrences of `old`.
    new: str


class ProfileRenderInput(_Identity):
    """Render every section in (org, namespace) into one prompt fragment.

    Default scope reads sections from EVERY agent in the namespace (so a
    persona authored under agent='loom-ui' is visible when rendering under
    agent='claude-desktop'); scope='this_agent' restricts to the caller's
    own sections.
    """

    format: str = "xml"  # "xml" | "markdown"
    labels: list[str] | None = None  # if set, render only these labels in this order
    scope: str = "namespace"  # "namespace" (default; fan-out) | "this_agent"
    # How far the render reaches across USERS (same grammar as
    # memory.search.read_scope, the token's claim is the ceiling). Under
    # own_user / own_session only this user's (session's) sections and the
    # namespace-level ones (user_id '-') render; another user's LLM-folded
    # profile never does. "namespace" = every row (the default).
    read_scope: Literal["namespace", "own_user", "own_session"] = "namespace"


class ProfileRenderOutput(BaseModel):
    # The sections as one prompt fragment in the chosen `format`; empty when there are none.
    rendered: str
    # How many sections were rendered.
    section_count: int
    # Length of `rendered` in characters, markup included.
    total_chars: int
    # The rendered sections' labels, in render order.
    labels: list[str] = []


class ProfileMutationOutput(BaseModel):
    # Version of the memory row this write created.
    version: int
    # Id of the memory row the section is stored in.
    memory_id: str = ""
    # Content hash of the section after the write; pass it as `if_match_sha256` next time.
    content_sha256: str = ""
    namespace: str = ""
    # The section that was written.
    label: str = ""
    # The section's size cap after the write.
    char_limit: int | None = None
    # Length of the JSON-encoded value after the write, the size `char_limit` caps;
    # the gap is the headroom left.
    value_length: int = 0


class ProfileActivityInput(_Identity):
    """Recent profile reads per agent — "which AI client read my profile, when?"."""

    lookback_days: int = 7  # capped at 30 server-side to bound the partition scan


class ProfileActivityEntry(BaseModel):
    agent: str
    last_activated_at: str  # ISO-8601 UTC
    event_count: int
    # Which read paths this agent used — loom_activate (a
    # typed "use loom"), profile.render (the SDK), profile.structured (a
    # structured profile), context.pack (the plugin's session start),
    # mcp.initialize (the MCP handshake).
    operations: list[str] = []


class ProfileActivityOutput(BaseModel):
    # Agents that read the profile in the window (at most 20), most recent first;
    # the dashboard's own reads are left out.
    entries: list[ProfileActivityEntry] = []


class ProfileSchemaSetInput(BaseModel):
    org: str
    namespace: str
    # A JSON Schema object of 1-64 `properties`, each with a `type` and a `description`
    # (at most 16 KiB); replaces the namespace's schema, and a change to its properties
    # makes every profile stale.
    json_schema: dict[str, Any]


class ProfileSchemaInput(BaseModel):
    org: str
    namespace: str


class ProfileSchemaOutput(BaseModel):
    # False when the namespace has no profile schema.
    found: bool
    # The schema as the admin set it, in their key order; {} when not found.
    json_schema: dict[str, Any] = {}
    # SHA-256 of the schema's properties (not its titles or order), which each profile names.
    schema_sha256: str = ""
    # The actor (`human:<subject>` or `process:<subject>`) that last set or cleared it;
    # empty when not found, or for a token narrowed to one user.
    updated_by: str = ""
    # When it was last set or cleared; None when not found.
    updated_at: datetime | None = None


class ProfileStructuredInput(_Identity):
    # The token's read_scope is the ceiling; the op always reads the one user_id named
    # (or pinned), and own_session is refused.
    read_scope: Literal["namespace", "own_user"] = "namespace"


class ProfileStructuredOutput(BaseModel):
    # ready: current, or all null with no facts; stale: memories or schema changed, or a fact
    # was not served at the build; pending: no live profile (unbuilt, expired, redacted or
    # high); no_schema: none set or it no longer parses.
    status: Literal["ready", "stale", "pending", "no_schema"]
    # Every property of the schema, null where the memories do not settle it ({} under
    # `no_schema`).
    profile: dict[str, Any] = {}
    # When the build read the profile's sources (a lock or unlock of the key keeps it);
    # None when no row holds it (pending, or ready for a user with no facts).
    built_at: datetime | None = None
    # The memory row that holds the profile, whose history is the profile's history; empty
    # when no row holds it (pending, or ready for a user with no facts).
    memory_id: str = ""
    # The row's version, higher for each rebuild; 0 when no row holds it (pending, or ready
    # for a user with no facts).
    version: int = 0
    # The same version as a decimal string, exact for a JavaScript caller (the number is
    # above 2^53, which JSON.parse rounds): send this one to memory.delete_version. Empty
    # when no row holds it (pending, or ready for a user with no facts).
    version_text: str = ""
    # The hash of the schema the namespace has now.
    schema_sha256: str = ""
