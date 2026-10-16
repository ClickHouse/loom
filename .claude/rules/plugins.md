---
paths:
  - "integrations/**"
  - "skills/**"
---

# Integrations and coding-agent plugins

- Edit `integrations/claude-code-plugin/`, then `python -m scripts.codegen.agent_plugin_sync` regenerates the Codex copies; never edit a synced copy.
- A behaviour change to any file the plugin ships (`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`) bumps both plugin manifests (`integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json`) to a version no other open PR uses: two PRs with one version ship only the first.
- Hooks run inside a user's session: they never throw to the agent, never block past their timeout, never print a token, and degrade to silence when the server is unreachable.
- `node --test` in the plugin directory before pushing; a skill change also runs `claude plugin eval integrations/claude-code-plugin` ([evals](../../integrations/claude-code-plugin/evals/README.md)), and a new skill adds a case.
- Anything a hook sends to the server goes through `redactSecrets` / `redactHead` (`integrations/claude-code-plugin/lib/loom-client.mjs`), redacting before truncating; a new capture path that skips them is a finding.
- An integration or its README is held to an open-source bar: the happy path runs end to end as written; an experimental feature says so and names the versions it was tested on; an upstream bug is filed upstream and linked; a cheap CI check keeps the example from rotting.
- The root `skills/loom-code/` skill is not synced: its version is `.claude-plugin/plugin.json` plus its `metadata.version`, released with the server ([release process](../../docs/development/release-process.md#where-the-version-lives)).
- The skills here ship to users. Their text is prompt: every line costs tokens in every session, so keep them short and exact.
