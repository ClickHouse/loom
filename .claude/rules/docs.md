---
paths:
  - "docs/**"
  - "README.md"
  - "CONTRIBUTING.md"
---

# Documentation

- Describe the system as it is now: no "new", "previously", dates, people, hosts, internal namespaces, ticket or decision numbers (`test_public_copy.py`). History goes in the PR body.
- Generated pages and regions under `docs/reference/` come from their generators (`settings_reference.py`, `docs_reference.py`); never hand-edit them.
- Relative links and `#anchors` must resolve (`test_doc_links.py`).
- Follow the [documentation style guide](../../docs/development/documentation-style.md).
