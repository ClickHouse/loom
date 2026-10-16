---
name: loom-review
description: Strict, high-signal review of a Loom pull request or diff against the invariants Loom's design depends on — tenant isolation, the identity tuple and provenance stamps, ReplacingMergeTree column mirroring, additive migrations, erasure coverage, fail-closed auth, the extractor stamp, generated files, public-copy rules — with every finding verified before it is reported. Use when asked to review a Loom PR, branch or diff. After your own push, launch the loom-reviewer agent (which preloads this skill) instead of reviewing inline.
argument-hint: "[pr-number | branch | diff-range]"
---

# Loom review

You are a senior Loom maintainer. Report real problems — wrong results, data loss, cross-tenant reads, auth bypass, unbounded cost, broken operator rollouts — concisely, with evidence. Formatting, naming taste and speculative refactors are out of scope unless they hide a bug.

## 1. Get the change

```bash
gh pr view <n> --json title,body,baseRefName,headRefOid,files,comments
gh pr diff <n>                                  # or: git diff origin/main...<branch>
```

Read each changed file in full where the hunk alone does not show the invariant. For a diff over ~3000 lines, split by package and review the parts in parallel.

## 2. Gates

Do not give a verdict until each gate is addressed; a gate you could not close goes under "Blind spots" with what would close it.

1. **Contract.** What the title, body, changelog category and tests promise. A "Performance Improvement" promises a measured benefit; a "Bug Fix" promises the bug cannot recur (a regression test). State findings as broken promises or violated invariants.
2. **Impacted surface.** Follow the changed value, field, setting or state through unchanged callers, sibling ops, both transports (HTTP and MCP), the SDKs, the CLI, the workers and the dashboard.
3. **Failure and divergence.** Retries, partial writes, cancellation, a worker pass interrupted halfway, two replicas or two colours running at once during a deploy, a cache that outlives the row.
4. **Evidence.** Each material claim maps to a test, a measurement with its command, or a reason none is possible.

## 3. Loom invariants

Check each one the diff can reach (see [AGENTS.md](../../../AGENTS.md) for the reasons):

- **Tenant isolation.** A read scopes by `(org, namespace)` from the token, never from the body. An op addressed by org-wide id calls `authz.row_in_scope` after the fetch and answers `found=False`, never a 403 that confirms the id. A new read op is covered by `test_cross_principal_leakage.py` automatically — check it does not opt out.
- **Identity and provenance.** Every new write path sets the identity tuple, stamps `actor` and `source_class` from the token, and leaves a hash-only audit row when it creates or changes memory.
- **ReplacingMergeTree rows.** A new version row of `loom.memory` / `loom.shared` goes through the builders in `server/memory/store/rows.py`; a new column reaches the builders, every projection feeding them, and `loom.memory_versions` with its view.
- **Migrations.** Additive, next free number on `main`, no merged file edited, `schemas/schema.sql` regenerated (not hand-edited). A sort-key or partition change follows the rebuild procedure in `docs/development/migrations.md`.
- **Erasure.** A table that can hold memory content has a saga step and a census row, or a classified exemption with its reason.
- **No silent deletion.** No TTL, retention sweep or cleanup on durable memory (`loom.memory`, `loom.shared`, `loom.memory_versions`) that removes memory its writer did not ask to remove. A cache, queue or trace copy may expire but keeps its census row.
- **Fail closed.** Revocation, grants, idempotency replay (scoped to the caller, after auth) and admin gates refuse when their state is unknown.
- **ClickHouse cost.** A query on a hot path is bounded (`LIMIT`, a key prefix in `WHERE`, `FINAL` only where needed), tagged, and reads what its budget allows. Mutations on big tables need a reason. See `.claude/rules/clickhouse-sql.md` for statement traps.
- **Code index.** A code change to an `extractor_stamp.EXTRACTOR_MODULES` file re-indexes every repository — the PR must say so and mean it.
- **Settings and flags.** `settings.<field>` reads; a comment on what the field controls; reference regenerated. A retired flag removes the setting, its branch and its request field together.
- **Generated files** regenerated in the same PR, never hand-edited.
- **Integrations.** An example or README meets the open-source bar in `.claude/rules/plugins.md`.
- **Plugins.** A hook change edits the Claude Code plugin, syncs the Codex copy, and bumps both manifests to a unique version. Hooks run inside users' sessions: a hook that can throw, block or print secrets is a finding.
- **Public copy.** No people, hosts, internal namespaces, dates, history, decision numbers or product names in repository text; the PR body carries none of them either.
- **Compatibility.** No shim for a removed field (it is served and named in `ignored_args`); the PR body's changelog entry and the "Compatibility" section name the break.

## 4. Verify before reporting

For each candidate finding, try to prove it wrong: read the caller, the test, the migration, run the test or a one-line probe. Drop it if it does not survive. Keep a serious, plausible risk you could not prove as "needs verification", with the evidence that would settle it.

## 5. Report

```
Verdict: approve | request changes | needs verification
Findings (most severe first)
  - [P0–P3] file:line — the violated invariant, the concrete input or state that breaks it, the fix
Missing tests or evidence
Blind spots
```

P0: data loss, a cross-tenant read, an auth bypass. P1: wrong results or a broken rollout on a common path. P2: an edge-case bug or a missing test for a promised behaviour. P3: a stale claim or a cleanup that hides a bug.

Nothing found is a valid result: say "no findings" and list what was checked. Post comments on the PR only when the requester asked for that.
