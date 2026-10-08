---
type: tool_used
tool: mcp__plugin_loom_loom__loom_memory_set
input_match: '"memory_key"\s*:\s*"[^"]*(?:lease|lock|freeze)'
max: 0
---

The lease / pointer is never a durable memory row — the audit's `coordination_as_memory` finding.
