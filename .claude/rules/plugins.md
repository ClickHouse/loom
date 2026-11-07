---
paths:
  - "integrations/**"
  - "skills/**"
  - ".claude-plugin/**"
---

# Integrations and coding-agent plugins

- Edit `integrations/claude-code-plugin/`, then `python -m scripts.codegen.agent_plugin_sync` copies its `hooks/*.mjs`, `bin/`, `lib/`, `skills/` and `assets/` to the Codex plugin; never edit a synced copy. Each plugin's manifest, `hooks/hooks.json` and `.mcp.json`, and the Codex `plugin-defaults.json`, differ on purpose and are edited in their own directory.
- A behaviour change to any file the plugin ships (`hooks/`, `lib/`, `bin/`, `skills/`, `.mcp.json`, the Codex `plugin-defaults.json`) bumps both plugin manifests (`integrations/claude-code-plugin/.claude-plugin/plugin.json`, `integrations/codex-plugin/.codex-plugin/plugin.json`) to a version no other open PR uses: two PRs with one version ship only the first.
- A hook, `lib/` or `bin/` change that calls an op or a field the server does not serve yet says so in its changelog entry ("needs the next server release"), so the release notes list it: that release's pull request raises both manifests' `minServerVersion` to its own version and bumps both plugin versions (clients get a manifest only through a new version); a pull request on `main` before it never raises it (`tests/unit/test_codex_plugin.py` keeps the two equal and no newer than the server). Between such a hook's merge and that release, a server of the previous release gets no warning.
- Hooks run inside a user's session: they never throw to the agent, never block past their timeout, never print a token, and degrade to silence when the server is unreachable.
- Before pushing, run the `integrations/` row of `loom-pre-push` §3, which lists every check CI runs here; a change to a plugin skill also runs `claude plugin eval integrations/claude-code-plugin` ([evals](../../integrations/claude-code-plugin/evals/README.md)), and a new skill adds a case.
- Anything a hook sends to the server goes through `redactSecrets` / `redactHead`, redacting before truncating, and a diff through `diffHeaders`, which sends only its paths, hunk ranges and function names (`integrations/claude-code-plugin/lib/loom-client.mjs`); a new capture path that skips them is a finding.
- An integration or its README is held to an open-source bar: the happy path runs end to end as written; an experimental feature says so and names the versions it was tested on; an upstream bug is filed upstream and linked; a cheap CI check keeps the example from rotting.
- The root `skills/loom-code/` and `skills/loom/` skills are not synced: their version is `.claude-plugin/plugin.json` plus each `metadata.version`, released with the server; `skills/loom/` is also what `mentor.guide` serves, so the image copies it ([release process](../../docs/development/release-process.md#where-the-version-lives)).
- The skills here ship to users. Their text is prompt: every line costs tokens in every session, so keep them short and exact.
