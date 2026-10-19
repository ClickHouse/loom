---
paths:
  - "server/settings/**"
---

# Settings

- One mixin per area, `server/settings/_<area>.py`, composed into one `Settings`; the env name is `LOOM_<FIELD>`. Every setting that names a model lives in `_models.py`.
- A new field's comment says what it controls and what its values mean, in one or two lines.
- Read it as `settings.<field>`; `getattr(settings, "<field>", default)` fails `test_settings_attribute_access.py`. Server code never reads `os.environ` for configuration (`test_env_reads.py`).
- `Annotated[T, LIVE]` makes a field take a runtime override (`server/settings/_live.py`): only for a field read at call time, never captured at boot or used as a calibration fallback (`test_live_settings.py`, which pins the LIVE set).
- A changed default is a `DefaultChange` in `server/settings/_history.py`, with the release and the reason (`test_settings_defaults.py`).
- Regenerate the reference and the settings-file schema: `python -m scripts.codegen.settings_reference` and `python -m scripts.codegen.settings_schema`.
- The Helm chart, the operator guides, the compose files and `deploy/standalone/env.example` name settings by hand: a rename or removal changes every `git grep -nw LOOM_<FIELD>` hit that names this setting. Leave generated output to its generator: the generated pages and regions under `docs/reference/` (`configuration.md`, `operations/`, the `BEGIN GENERATED` regions) and the root `llms.txt` come from `settings_reference` and `docs_reference`, and the copies in `integrations/codex-plugin/` from `agent_plugin_sync`; rerun each one whose output names the setting.
