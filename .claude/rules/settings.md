---
paths:
  - "server/settings/**"
---

# Settings

- One mixin per area, `server/settings/_<area>.py`, composed into one `Settings`; the env name is `LOOM_<FIELD>`. Every setting that names a model lives in `_models.py`.
- A new field's comment says what it controls and what its values mean, in one or two lines.
- Read it as `settings.<field>`; `getattr(settings, "<field>", default)` fails `test_settings_attribute_access.py`.
- Regenerate the reference: `python -m scripts.codegen.settings_reference`.
- The Helm chart, the operator guides and `deploy/standalone/env.example` name settings by hand: a rename or removal changes every `git grep -nw LOOM_<FIELD>` hit that names this setting, outside the generated `docs/reference/` and `integrations/codex-plugin/`.
