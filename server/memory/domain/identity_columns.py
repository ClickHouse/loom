"""The memory columns that name who a row is about or came from, as the namespace
tallies count them."""

from __future__ import annotations

from typing import Literal

# `agent` the writing agent, `actor` the stamped writer, `user_id` the end user.
IdentityColumn = Literal["agent", "actor", "user_id"]
