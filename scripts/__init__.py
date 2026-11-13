"""Repository tooling, run from the repository root.

- `gates/`: CI gates (type-error and function-length ratchets, secret scan,
  the SDK wheel smoke test) and the tool that moves ratchet-baseline entries
  with a moved file.
- `codegen/`: generators of checked-in files derived from the code (each has
  a `--check`; CI runs all but the interpreter-dependent library-method
  table), the client-facing wire changelog, and the Langfuse prompt push.
- `ops/`: tools that run against a ClickHouse or a Loom server: the schema
  dump that writes `schemas/schema.sql`, the load benchmark, table rebuilds,
  the call-graph edge audit, the backup drill and the code-intel bootstrap.
- `scip_harness/`: compiler-tier index producers; server workers call them by path.
- `docs_site/`: the documentation site's MkDocs hook, theme override and pinned builder.
"""
