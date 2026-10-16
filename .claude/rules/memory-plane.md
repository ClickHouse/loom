---
paths:
  - "server/memory/**"
  - "server/knowledge/**"
  - "server/erasure/**"
---

# Memory plane

- Every new version row of `loom.memory` / `loom.shared` is built by `build_memory_version_row` / `build_embedding_row` (`server/memory/store/rows.py`). A hand-built row omits a column and zeros it for every reader under `FINAL`.
- Writes take `actor` and `source_class` from the token (`writer_actor`, `source_class_for`), never from the body, and leave a hash-only audit row.
- Map the behaviour onto Expand / Revise / Contract / Reinforce / Refuse. A write path that bypasses conflict surfacing, the blacklist or the pinned-peer check is a finding.
- An op addressed by org-wide id checks `authz.row_in_scope` after the fetch and answers `found=False` on refusal.
- Tune ranking through settings and prove it with a paired benchmark (`loom-bench`); never change a scalar threshold without one.
- No retention sweep or TTL removes memory its writer did not ask to remove.
