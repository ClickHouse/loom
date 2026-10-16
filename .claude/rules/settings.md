---
paths:
  - "server/settings/**"
---

# Settings

- One mixin per area, `server/settings/_<area>.py`; every setting that names a model lives in `_models.py`.
- A new field's comment says what it controls and what its values mean, in one or two lines.
- Read it as `settings.<field>`; `getattr(settings, "<field>", default)` fails `test_settings_attribute_access.py`.
- Regenerate the reference: `python scripts/codegen/settings_reference.py`.
- Retiring a flag deletes the field, its branch and any request field carrying it in one change.
