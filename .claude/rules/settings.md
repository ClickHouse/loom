---
paths:
  - "server/settings/**"
---

# Settings

- One mixin per area, `server/settings/_<area>.py`, composed into one `Settings`; the env name is `LOOM_<FIELD>`. Every setting that names a model lives in `_models.py`.
- A new field's comment says what it controls and what its values mean, in one or two lines.
- Read it as `settings.<field>`; `getattr(settings, "<field>", default)` fails `test_settings_attribute_access.py`.
- Regenerate the reference: `python -m scripts.codegen.settings_reference`.
- The Helm chart, the operator guides, the compose files and `deploy/standalone/env.example` name settings by hand: a rename or removal changes every `git grep -nw LOOM_<FIELD>` hit that names this setting. Leave generated output to its generator: the generated pages and regions under `docs/reference/` (`configuration.md`, `operations/`, the `BEGIN GENERATED` regions) come from `settings_reference` and `docs_reference`, and the copies in `integrations/codex-plugin/` from `agent_plugin_sync`; rerun each one whose output names the setting.
