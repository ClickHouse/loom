---
paths:
  - "docs/**"
  - "README.md"
  - "i18n/**"
  - "CONTRIBUTING.md"
  - "llms-install.md"
---

# Documentation

- Describe the system as it is now: no "new", "previously", dates, people, hosts, internal namespaces, ticket or decision numbers (`test_public_copy.py`). History goes in the PR body.
- Generated pages and regions under `docs/reference/` come from their generators (`settings_reference.py`, `docs_reference.py`); never hand-edit them.
- Relative links and `#anchors` must resolve (`test_doc_links.py`).
- Follow the [documentation style guide](../../docs/development/documentation-style.md).
- `i18n/<lang>/` contains machine translations of tracked `README.md` and `docs/**/*.md`, including generated references. Edit English or its generator, never translations by hand. `.github/workflows/i18n.yml` queues bounded batches after English merges to main; ordinary PRs may have pending coverage. `python -m scripts.i18n.translate check` reports missing, stale and obsolete output; `check --skip-stale` still validates present files and links. Verified replacements wait in `i18n/.pending/<lang>/` until their link dependencies permit publication. The updater reuses validated generated data from `i18n/update`, never branch code, and deletes only stamped obsolete output after inbound links are repaired. Paid calls require an explicit cumulative allowance and durable reservations ([translations](../../CONTRIBUTING.md#translations)). After a prompt change, request `update --all`; another model also needs the runner role's policy changed.
