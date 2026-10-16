---
paths:
  - "server/code/**"
---

# Code index

- A code change to a module in `extractor_stamp.EXTRACTOR_MODULES` or a grammar in `STAMPED_GRAMMARS` re-runs that stage over every indexed repository (hours of ingest). Comments and docstrings are not stamped. Touch their code only when the extraction itself changes, and say so in the PR.
- Those modules are excluded from formatting; do not reformat them.
- Lane parsers outside the stamp re-ingest their files with `loom code backfill --paths-file`.
- Git operations on mirrors and checkouts: mirrors are never pruned or garbage-collected by Loom code (checkouts borrow their objects through alternates); a fetch names the ref it needs, not a bare sha.
- Guide: [adding-language-support](../../docs/development/adding-language-support.md).
