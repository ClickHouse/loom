---
paths:
  - "server/code/**"
---

# Code index

- A code change to a module in `extractor_stamp.EXTRACTOR_MODULES` or a grammar in `STAMPED_GRAMMARS` re-runs its stage (`STAGES`: chunk, symbol, edge, guard) for the language families it touches in every indexed repository (hours of ingest). The stamp hashes code tokens only (`extractor_stamp.logic_tokens`), so comments and docstrings can be edited freely. Touch their code only when the extraction itself changes, and say so in the PR.
- Those modules are excluded from formatting; do not reformat them.
- Lane parsers outside the stamp re-ingest their files with `loom code backfill --paths-file`.
- Mirrors are garbage-collected only by `deploy/cron/repo-gc.sh`, which never prunes a repository another checkout borrows from through alternates. A commit fetched by sha is pinned under `refs/loom/merges/` so gc keeps it; a new fetch path pins what it fetches the same way.
- Guide: [adding-language-support](../../docs/development/adding-language-support.md).
