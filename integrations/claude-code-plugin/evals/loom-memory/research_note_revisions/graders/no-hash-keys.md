---
type: tool_used
tool: mcp__plugin_loom_loom__loom_memory_set
input_match: '"memory_key"\s*:\s*"(?:[^"]*[/:])?[0-9a-fA-F]{24,}"'
max: 0
---

No `memory_key` whose last segment is a 24+ hex run: keys are readable paths (`<scope>/<kind>/<id>`), never content hashes — the audit's `hash_keys` finding.
