"""org.set_model_calls / org.model_calls — wire models.

An org admin switches the org's model calls off (or back on): while off, no
chat, embedding, rerank or classify call is made for the org's data, on a
request or by a background job, and its queued work waits. A platform admin
can lock the setting: the org's own admins can still switch it off, but not
back on.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OrgSetModelCallsInput(BaseModel):
    org: str
    # False switches the org's model calls off; true switches them back on.
    enabled: bool
    # A note on why, kept with the setting; at most 500 characters.
    reason: str = Field(default="", max_length=500)
    # Platform admins only: true locks the setting (the org's own admins may switch
    # it off, not back on), false unlocks it; unset leaves the lock as it is.
    locked: bool | None = None


class OrgModelCallsInput(BaseModel):
    org: str


class OrgModelCallsOutput(BaseModel):
    org: str
    # Whether the org's data may go to a model; true when it was never set.
    enabled: bool
    # The note given with the last change; "" when none.
    reason: str = ""
    # Who made the last change; "" when it was never set.
    updated_by: str = ""
    # When the setting last changed (UTC), None when it was never set; while off,
    # when it was switched off, whatever else changed since.
    updated_at: datetime | None = None
    # Whether a platform admin locked the setting: its admins cannot switch it on.
    locked: bool = False
    # The platform admin who set the lock; "" when unlocked.
    locked_by: str = ""
