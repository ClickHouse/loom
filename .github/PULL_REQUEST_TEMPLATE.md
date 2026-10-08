## Why

The problem this solves and why this is the fix. The diff says what changed;
this says why. Link the issue if there is one.

## Scope and risk

What this touches, what it deliberately leaves alone, and what could break:
concurrency, stored data, auth, hooks running in a user's session.

## Compatibility, rollout and rollback

- Wire: name every op or field added, changed or removed, and any changed default.
- Operator steps: new settings, a backfill, a re-index, the order to deploy in. Say "none" if none.
- Rollback: what reverting leaves behind (rows, migrations, generated files).

## Tests

What you ran and what it showed. For behaviour with no test, say why.

## Checklist

- [ ] `make format-check`, `make lint`, `make typecheck` and `make test-unit` pass; the integration suite passes if server behaviour changed.
- [ ] Generated files are regenerated in this PR, not by hand: the SDK op tables, `docs/reference/configuration.md` and the `docs/reference/` tables (`python scripts/codegen/docs_reference.py`), the reach manifest, `schemas/schema.sql` (`make schema-dump`) and the Codex plugin copies (`python3 scripts/codegen/agent_plugin_sync.py`).
- [ ] A new migration is additive and numbered after the latest on `main`; no merged migration is edited.
- [ ] A plugin hook change bumps both plugin manifests' version, and no other open PR uses that version.
- [ ] The frontend builds (`npm --prefix frontend run build`) and its tests pass, if `frontend/` changed.
- [ ] Docs under `docs/` describe the system as it now is, and this description names any operator steps and wire changes.
- [ ] Repository text has no dates, people, host names, internal namespaces, ticket or decision numbers; the history goes in this description (`tests/unit/test_public_copy.py`).
- [ ] Commits are signed off (`git commit -s`), as CONTRIBUTING.md asks.
