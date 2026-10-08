---
type: tool_used
tool: mcp__plugin_loom_loom__loom_memory_set
input_match: '"value"\s*:\s*"[A-Za-z0-9+/]{200,}={0,2}"'
max: 0
---

Awkward characters are stored as they are; a value is never base64.
