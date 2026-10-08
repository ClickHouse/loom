---
type: tool_used
tool: mcp__plugin_loom_loom__loom_memory_set
input_match: '"memory_key"\s*:\s*"[^"]*(?:[-_/.]v\d+|rev(?:ision)?[-_/]?\d+)"'
max: 0
---

A revision rewrites the same key (Loom keeps the versions); no `…/v2`, `…-rev3` keys and hence no hand-rolled index row saying which one is current.
