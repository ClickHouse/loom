---
paths:
  - "integrations/**"
  - "skills/**"
---

# Coding-agent plugins

- Edit `integrations/claude-code-plugin/`, then `python scripts/codegen/agent_plugin_sync.py` regenerates the Codex copies; never edit a synced copy.
- A hook change bumps both plugin manifests (`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`) to a version no other open PR uses: two PRs with one version ship only the first.
- Hooks run inside a user's session: they never throw to the agent, never block past their timeout, never print a token, and degrade to silence when the server is unreachable.
- `node --test` in the plugin directory before pushing.
- The skills here ship to users. Their text is prompt: every line costs tokens in every session, so keep them short and exact.
