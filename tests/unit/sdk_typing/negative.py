"""Typing fixture: each line marked `# expect: <error-code>`
must be REJECTED by mypy --strict with that code, and no other line may
error. Never executed."""

from __future__ import annotations

from loom import Agent, RetryPolicy, SyncAgent
from loom.ops import MemorySearchBody, MemorySearchInput, spec

agent = Agent("acme", "support", "bot", api_key="k")

bad_field: MemorySearchBody = {"query": "x", "top_k": "five"}  # expect: typeddict-item
missing_required: MemorySearchBody = {"top_k": 5}  # expect: typeddict-item
unknown_key: MemorySearchBody = {"query": "x", "nope": 1}  # expect: typeddict-unknown-key
no_identity: MemorySearchInput = {"query": "x"}  # expect: typeddict-item
policy = RetryPolicy(max_attempts="3")  # expect: arg-type
result: str = agent.call  # expect: assignment
sync_agent = SyncAgent("acme", "support", "bot", api_key="k")
sync_agent.probe(opps=["memory.search"])  # expect: call-arg
not_awaited: dict[str, object] = agent.call("memory.search", {"query": "x"})  # expect: assignment
unawaited_set = agent.memory.set("k", "v", on_expire="never")  # expect: arg-type
unawaited_search = agent.memory.search("q", top_k="3")  # expect: arg-type
role = spec("memory.get").role  # expect: union-attr
