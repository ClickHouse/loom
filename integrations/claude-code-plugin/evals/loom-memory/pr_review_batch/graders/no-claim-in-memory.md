---
type: tool_used
tool: mcp__plugin_loom_loom__loom_memory_set
input_match: '"memory_key"\s*:\s*"[^"]*(?:claim|lease|lock)'
max: 0
---

The reviewer's claim on a PR is coordination state (working memory), never a `memory.set` row polled by `memory.get`.
