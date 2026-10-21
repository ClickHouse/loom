"""extraction_policy.set / get / clear — wire models.

An org admin's rules for what extraction never stores ("never store
credentials, payment card data or health data"), for the whole org or added
for one namespace. Every model call that turns raw text into memories follows
them, above the request's `extraction_instructions`.

`scope` is required: a transport may fill `namespace` from the caller's
session, so the namespace alone never decides whether the org's policy or a
namespace's is meant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.domain.extraction_policy import MAX_CHARS

Scope = Literal["org", "namespace"]


class ExtractionPolicySetInput(BaseModel):
    org: str
    # `org` sets the org's own policy, which every namespace follows (`namespace` ignored);
    # `namespace` sets the rules that namespace adds to it.
    scope: Scope
    # The namespace a `namespace`-scoped policy is for; ignored for scope `org`. It covers
    # that namespace only, not its child collections; a rule for a subtree goes in the org's.
    namespace: str = ""
    # The rules in plain language, at most 4,096 characters; replaces the scope's policy.
    policy: str = Field(..., min_length=1, max_length=MAX_CHARS)


class ExtractionPolicyGetInput(BaseModel):
    org: str
    # `org` reads the org's own policy; `namespace` reads `namespace`'s.
    scope: Scope
    # The namespace whose policy to read; ignored for scope `org`.
    namespace: str = ""


class ExtractionPolicyClearInput(BaseModel):
    org: str
    # `org` removes the org's own policy; `namespace` removes that namespace's, leaving
    # the org's in force.
    scope: Scope
    # The namespace whose policy to remove; ignored for scope `org`.
    namespace: str = ""


class ExtractionPolicyOutput(BaseModel):
    org: str
    # `org` or `namespace`, as asked.
    scope: Scope
    # The namespace for scope `namespace`; "" for scope `org`.
    namespace: str = ""
    # This scope's policy; "" when none is set.
    policy: str = ""
    # Who set it; "" when none is set.
    updated_by: str = ""
    # When it was set (UTC); None when none is set.
    updated_at: datetime | None = None
    # What extractions in this scope run under: the org's policy, then the
    # namespace's; "" when neither is set.
    effective: str = ""
