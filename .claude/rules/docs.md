---
paths:
  - "docs/**"
  - "README.md"
  - "README.zh-CN.md"
  - "CONTRIBUTING.md"
---

# Documentation

- Describe the system as it is now: no "new", "previously", dates, people, hosts, internal namespaces, ticket or decision numbers (`test_public_copy.py`). History goes in the PR body.
- Generated pages and regions under `docs/reference/` come from their generators (`settings_reference.py`, `docs_reference.py`); never hand-edit them.
- Relative links and `#anchors` must resolve (`test_doc_links.py`).
- Follow the [documentation style guide](../../docs/development/documentation-style.md).
- `README.zh-CN.md` mirrors the README's first screen: a change to its intro, use-case list, results caveats, install steps, model and key setup or plugin commands updates the Chinese page in the same PR (`test_readme_translation.py` pins the results numbers, the commands and the code names in the results caveats; the rest is by review).
