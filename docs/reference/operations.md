# Operations reference

An operation (op) is one named unit of work on the Loom wire, such as `memory.set` or
`code.search`. Each op is declared once, in the operation registry. The HTTP route, the MCP
tool, the OpenAPI document, the capability catalogue and both SDKs' op tables are derived from
that declaration. The table at the end of this page is generated from the same registry.

## The registry

Each op is an `Operation` (`server/operations/base.py`) in a group module's `OPERATIONS` list,
an `ops*.py` module in its domain's `api/` package (`server/memory/api/ops_memory.py`,
`server/code/api/ops.py`). `server/operations/registry.py` joins the groups in
`_OPERATION_MODULES` into `ALL_OPERATIONS`, which the transports iterate. An `Operation`
carries:

| Field                                   | Meaning                                                                                                                                |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                                  | Dotted `<group>.<verb>`. The group is the text before the first dot.                                                                   |
| `input_model`, `output_model`           | Pydantic models: the request body and the 200 response.                                                                                |
| `handler`                               | The async function that runs the op, in its domain's `app/` package (`server/<domain>/app/`).                                          |
| `required_role`                         | The minimum role the caller's token must hold on the namespace.                                                                        |
| `idempotent`                            | Safe to retry. Idempotent ops accept an idempotency key; see [HTTP API](./http-api.md#idempotency).                                    |
| `audit`                                 | A successful call writes a row to `loom.audit_log`.                                                                                    |
| `writes`                                | Set on the few viewer-callable, idempotent ops that still change stored state (a feedback row, a watch), so they are not read-only.    |
| `maturity`                              | `stable` or `experimental`. It decides one thing: the [MCP `all` listing](#mcp-all-listing).                                           |
| `budget_p99_ms`, `shape_budgets_p99_ms` | The target p99 latency, overall and per request shape. A successful call over budget emits a `budget.violated` event.                  |
| `skip_namespace_authz`                  | The op acts _on_ a namespace (join, leave, register) rather than in one; the handler checks membership. The role check still runs.     |
| `deployment_scoped`                     | The op reads facts about the deployment (build, registry), not tenant data; a namespace-scoped token passes with its best role.        |
| `row_scoped`                            | The op is addressed by an org-wide id (`memory_id`, `erasure_id`). The handler authorizes the row's namespace and hides a foreign row. |

[Adding an operation](../development/adding-an-operation.md) walks through declaring one.

## Names and addresses

One op has one name and one address per surface:

| Surface    | Address                                                                          | Example                      |
| ---------- | -------------------------------------------------------------------------------- | ---------------------------- |
| HTTP       | `POST /v1/<name>` (`Operation.http_path`)                                        | `POST /v1/memory.set`        |
| MCP        | the tool `loom_` + the name with dots as underscores (`Operation.mcp_tool_name`) | `loom_memory_set`            |
| Python SDK | a group-client method, or `await agent.call("<name>", {...})`                    | `await bot.memory.set(...)`  |
| TypeScript | `client.call("<name>", input)` or the camel-cased group accessor                 | `client.memory.bulkSet(...)` |

The ledger (`loom.agent_events`), the audit log and the latency budgets record the dotted
name, so a `GROUP BY operation` lines up with this page.

## Reading the table

- **Operation** is the dotted name.
- **Summary** is the first sentence of the input model's docstring, else of the op's MCP
  description (`server/transport/mcp_descriptions.py`), else of the handler's docstring. The
  full contract is the schema.
- **Role** is the minimum role: `viewer` < `writer` < `admin` < `org_admin` <
  `platform_admin` (`Role` in `server/operations/base.py`). `org_admin` and `platform_admin`
  are covered in [Tokens and grants](../guides/tokens-and-grants.md).
- **Kind** is one of:
  - `read`: `Operation.read_only` is true (idempotent, viewer, not audited, not `writes`). MCP
    sets `readOnlyHint` only on these, and the `readonly` toolset lists only these.
  - `idempotent`: changes state; a retry with the same input is safe.
  - `write`: a state transition; a blind retry can apply it twice unless the op takes a
    content-hash precondition (`if_match_sha256`).
- **Listed under `all`** is `yes` when an MCP connection on the `all` toolset lists the op.
- **Audit** is `yes` when a successful call leaves an `audit_log` row.

## MCP `all` listing

`LOOM_MCP_TOOLSET=all` (or `?toolset=all`) lists every op that is not hidden from MCP and is
either `stable` or on the core or atomic roster (`listed_under_all` in
`server/operations/toolsets.py`). The `code.*` ops also need `LOOM_MCP_CODE_TOOLS=true`.

An op left out of that listing behaves like any other op. It is callable by name over MCP or
through `loom_op`, at `POST /v1/<op>`, and from both SDKs. Role checks, rate limits, audit and
serving do not depend on `maturity`.

The label is set by hand in the op's declaration. The reach census
(`scripts/codegen/op_reach_census.py`, gated by `tests/unit/test_op_reach_census.py`) reports
each op's reach and, with `--call-counts`, its calls; it never rewrites the declaration.

## Toolsets

A toolset decides which ops an MCP connection lists in `tools/list`, never what it can call.
The rosters, the synthetic tools and what hidden ops answer are in
[MCP toolsets](./mcp.md#toolsets). `ops.capabilities` reports each op's `toolsets`.

## Schemas

The table gives a summary. The exact fields come from the op's Pydantic models:

| Source                                                          | Gives                                                                                                                       |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `GET /v1/api/openapi.json` (no token)                           | The full input and output JSON Schema of every op, for code generation.                                                     |
| `POST /v1/ops.capabilities` (any token)                         | Per op: `request_fields`, `response_fields`, role, budget, idempotency, audit, `mcp_tier`, `toolsets`, plus `build_commit`. |
| `loom export-actions --format json` or `--format cedar`         | An MCP `tools/list`-shaped manifest with `inputSchema`, `outputSchema` and a `loom` block per tool, or a Cedar schema.      |
| `sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts` | Generated per-op `TypedDict`s and TypeScript interfaces, plus the `OPS` spec table.                                         |

The MCP listing trims each schema: `org` and every field marked `MCP_HIDDEN`
(`server/operations/base.py`) are left out, because every listed property costs tokens on every
turn. A hidden field is still accepted by name. See
[Exporting the action schema](./mcp.md#exporting-the-action-schema) for the manifest and Cedar
output, and [HTTP API](./http-api.md#discovering-what-a-deployment-serves) for reading the
catalogue at start-up.

## How the table is generated

`scripts/codegen/docs_reference.py` imports `ALL_OPERATIONS`, groups ops by the text before
the first dot, and rewrites the region between the markers below. `make lint` runs it with
`--check`, so a registry change that does not regenerate this page fails CI. Edit the op's
docstring or declaration, never the table.

Each group also has a generated field page, `operations/<group>.md` (for example
[operations/memory.md](operations/memory.md)), with every input and output field's type,
default and meaning. The meaning is the comment above the field in its Pydantic model.

```bash
python scripts/codegen/docs_reference.py          # rewrite the regions
python scripts/codegen/docs_reference.py --check  # exit 1 when a region is stale
```

## Operations

<!-- BEGIN GENERATED: operations -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

347 operations in 39 groups. Every op is served at `POST /v1/<op>` and as the MCP tool `loom_<op>` (dots become underscores).

Columns: **Role** is the minimum role (`viewer` < `writer` < `admin` < `org_admin` < `platform_admin`); **Kind** is `read` (idempotent, viewer, unaudited, writes nothing — MCP `readOnlyHint`), `idempotent` (safe to retry) or `write`; **Listed under `all`** says whether an MCP connection on the `all` toolset lists the op in `tools/list` (`code.*` ops only with `LOOM_MCP_CODE_TOOLS=true`); an op marked `no` is still callable by name or through `loom_op`, unless it is hidden from MCP; **Audit** marks ops that leave an `audit_log` row.

Groups: [`admin`](#admin) · [`agents`](#agents) · [`audit`](#audit) · [`blacklist`](#blacklist) · [`calibration`](#calibration) · [`code`](#code) · [`collection`](#collection) · [`conflicts`](#conflicts) · [`consolidations`](#consolidations) · [`context`](#context) · [`conversation`](#conversation) · [`cost`](#cost) · [`decision`](#decision) · [`erasure`](#erasure) · [`extraction_policy`](#extraction_policy) · [`facts`](#facts) · [`grants`](#grants) · [`inbox`](#inbox) · [`knowledge`](#knowledge) · [`learnings`](#learnings) · [`memory`](#memory) · [`memory_schema`](#memory_schema) · [`mentor`](#mentor) · [`namespace`](#namespace) · [`observe`](#observe) · [`ops`](#ops) · [`org`](#org) · [`policy`](#policy) · [`procedure`](#procedure) · [`profile`](#profile) · [`profile_schema`](#profile_schema) · [`run`](#run) · [`search`](#search) · [`session`](#session) · [`task`](#task) · [`tracker`](#tracker) · [`ui`](#ui) · [`webhook`](#webhook) · [`working_memory`](#working_memory)

### admin

Fields for every `admin` op: [operations/admin.md](operations/admin.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`admin.users`](operations/admin.md#adminusers) | Aggregate subjects from namespace_grants + namespaces.owner_email, join in last-active from the audit log, mark configured platform admins. | org_admin | idempotent | no |  |

### agents

Fields for every `agents` op: [operations/agents.md](operations/agents.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`agents.connected`](operations/agents.md#agentsconnected) | The agents active in a window: last seen, operations and cost per agent. | viewer | read | yes |  |

### audit

Fields for every `audit` op: [operations/audit.md](operations/audit.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`audit.list`](operations/audit.md#auditlist) | Query the audit log (tamper-evident per-row hashes). | viewer | read | no |  |

### blacklist

Fields for every `blacklist` op: [operations/blacklist.md](operations/blacklist.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`blacklist.add`](operations/blacklist.md#blacklistadd) | Add a pattern that prevents matching content from being saved to memory. | writer | idempotent | yes | yes |
| [`blacklist.list`](operations/blacklist.md#blacklistlist) | List patterns that suppress memory storage. | viewer | read | no |  |
| [`blacklist.remove`](operations/blacklist.md#blacklistremove) | Remove a previously-added blacklist pattern. | admin | idempotent | no | yes |
| [`blacklist.sweep`](operations/blacklist.md#blacklistsweep) | Tombstone the live rows an existing pattern matches. | writer | idempotent | no | yes |

### calibration

Fields for every `calibration` op: [operations/calibration.md](operations/calibration.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`calibration.get`](operations/calibration.md#calibrationget) | Resolve the active calibration for (org, namespace): per-namespace, then cluster-wide, then scalar fallback, with the source tier reported. | viewer | read | no |  |

### code

Fields for every `code` op: [operations/code.md](operations/code.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`code.ask`](operations/code.md#codeask) | Ask a QUESTION about this codebase and get a grounded answer with [file:line] citations — 'how does TTL drop old rows', 'where is the memory limit enforced'. | viewer | read | yes |  |
| [`code.backport_gap`](operations/code.md#codebackport_gap) | Which release branches still carry the bug this fix removed, and the exact PR to cherry-pick there? | viewer | read | no |  |
| [`code.backport_gaps`](operations/code.md#codebackport_gaps) | The set-wide backport report: which merged fixes labelled for backport does a release branch still owe? | viewer | read | no |  |
| [`code.bisect_candidates`](operations/code.md#codebisect_candidates) | Rank PRs that may have introduced a bug; Loom does not test or reproduce it. | viewer | read | no |  |
| [`code.blame_range`](operations/code.md#codeblame_range) | Which merged PRs last touched these lines, between release A and B? | viewer | read | no |  |
| [`code.calibrate`](operations/code.md#codecalibrate) | Derive code-specific abstention thresholds from the authored probe corpus (query/positive/hard-distractor triples over the indexed repo) and persist them as this namespace's calibration. | admin | write | no | yes |
| [`code.callees`](operations/code.md#codecallees) | What does this symbol call — forward call edges (depth<=3), tier-marked like loom_code_callers. | viewer | read | no |  |
| [`code.callers`](operations/code.md#codecallers) | Who calls this symbol — exact reverse call edges (depth<=3), compiler-resolved where available; `edge_kinds: ['inherits']` narrows the walk to what implements or extends a type. | viewer | read | yes |  |
| [`code.context_pack`](operations/code.md#codecontext_pack) | Briefing at the START of a task, instead of exploring file by file. | viewer | read | yes |  |
| [`code.context_packs`](operations/code.md#codecontext_packs) | List the stored per-PR context packs of a repository, optionally for one PR. | viewer | read | no |  |
| [`code.conventions`](operations/code.md#codeconventions) | The repository's recurring maintainer checks as facts over one PR's diff: a setting added or re-defaulted without a SettingsChangesHistory entry; the same setting's docs mention; a new… | viewer | read | no |  |
| [`code.coverage`](operations/code.md#codecoverage) | Report how much of a repository the index covers at its head: per language, per tier, and the gaps. | viewer | read | yes |  |
| [`code.coverage.unwatch`](operations/code.md#codecoverageunwatch) | Cancel a coverage watch on a commit. | viewer | idempotent | no |  |
| [`code.coverage.watch`](operations/code.md#codecoveragewatch) | Ask for a task-inbox message when the index covers a commit, instead of polling for it. | viewer | idempotent | no |  |
| [`code.coverage_claim`](operations/code.md#codecoverage_claim) | Is this already tested? | viewer | read | no |  |
| [`code.enclosing`](operations/code.md#codeenclosing) | Batched `path:line` -> enclosing function. | viewer | read | yes |  |
| [`code.examples`](operations/code.md#codeexamples) | Real call sites of a symbol, ranked test-covered-first — a current, tested usage example beats the deprecated one grep finds first. | viewer | read | no |  |
| [`code.files`](operations/code.md#codefiles) | Which indexed files have this NAME — an rg-style glob (`tests/queries/0_stateless/05138_*`, `/*Parquet*.cpp`), a basename stem (`05138_with_fill` matches the stem exactly or as its leading prefix… | viewer | read | no |  |
| [`code.first_build`](operations/code.md#codefirst_build) | Which CI build first carried this commit? | viewer | read | no |  |
| [`code.grep`](operations/code.md#codegrep) | Exact-match lookup over the indexed corpus. | viewer | read | yes |  |
| [`code.history`](operations/code.md#codehistory) | The merged PRs that most recently touched a symbol or path: number, title, author, labels, and what reviewers said about that file — up to `reviews_per_pr` (default 3, 0 = none) human review… | viewer | read | yes |  |
| [`code.impact`](operations/code.md#codeimpact) | Symbol-shaped (name/symbol_id) or diff-shaped (files/diff) blast radius. | viewer | read | yes |  |
| [`code.index_errors`](operations/code.md#codeindex_errors) | List the files the code index failed to parse or ingest, with the error and the commit. | viewer | read | yes |  |
| [`code.index_status`](operations/code.md#codeindex_status) | Health of the code index: indexed head commit, compiler-tier commit, row counts, watermarks, and the weekly tables' state (symbol_stats_rows/at, unit_risk_rows/at; 0 = not written yet). | viewer | read | yes |  |
| [`code.map`](operations/code.md#codemap) | PageRank-ordered orientation map of the indexed repository with per-file summary cards, token-budgeted. | viewer | read | no |  |
| [`code.outcome`](operations/code.md#codeoutcome) | Tell loom what you did with a code answer, by its trace_run_id: 'used_in_answer', 'opened_file' (targets = the paths), 'fell_back_grep' or 'ignored'. | viewer | idempotent | no |  |
| [`code.outline`](operations/code.md#codeoutline) | "What is in this file?" — the question a whole-file read answers at the price of the whole file. | viewer | read | yes |  |
| [`code.path_guards`](operations/code.md#codepath_guards) | The conditions that stand on the call path from `from_symbol` to `to_symbol`: the verify(reaches) chain, and for each hop the guards the call site sits under inside its caller… | viewer | read | no |  |
| [`code.pr_check`](operations/code.md#codepr_check) | A PR draft asks whether a PR like it already exists. | viewer | read | no |  |
| [`code.recent_commits`](operations/code.md#coderecent_commits) | List the most recently indexed commits of a repository, decorated with subject and time from the clone. | viewer | read | yes |  |
| [`code.refresh`](operations/code.md#coderefresh) | CI calls this right after a merge lands: run one ingest pass now instead of waiting for the poll interval. | writer | write | no | yes |
| [`code.reindex`](operations/code.md#codereindex) | Operator repair: move the git watermark (e.g. back N commits after a bad pass, or to a specific sha). | admin | write | no | yes |
| [`code.repo.add`](operations/code.md#coderepoadd) | Add a repository's branch as a new read-only index. | viewer | write | yes | yes |
| [`code.repo.add_branch`](operations/code.md#coderepoadd_branch) | Index another branch of a repository the caller maintains. | admin | write | yes | yes |
| [`code.repo.remote_branches`](operations/code.md#codereporemote_branches) | What a Git remote shows before anything is written — every branch with its head, the default branch, the ones that look like release branches, and the ones indexed here already — so the add dialog… | viewer | read | yes |  |
| [`code.repo.remove`](operations/code.md#codereporemove) | Retire a repository index a person added. | admin | idempotent | yes | yes |
| [`code.repo.set_access`](operations/code.md#codereposet_access) | A maintainer sets a repository's access — its classification (`standard` \| `restricted`) and join policy (`open` \| `approval` \| `closed`). | admin | idempotent | no | yes |
| [`code.repo.set_source`](operations/code.md#codereposet_source) | A maintainer sets the trackers this repository reads (records of issues, PRs, tests and incidents registered with `tracker.source.add`), in order. | admin | idempotent | no | yes |
| [`code.repos`](operations/code.md#coderepos) | Lists the code namespaces (repo@branch entries) configured on this deployment that the caller is authorized to read, across orgs — each row carries its org. | viewer | read | yes |  |
| [`code.review_brief`](operations/code.md#codereview_brief) | A merged PR's review brief: the point read of what the ingest built at merge — touched symbols with callers and fan-in, coverage verdicts, setting facts, regression history, blame of the deleted… | viewer | read | no |  |
| [`code.scip_ingest`](operations/code.md#codescip_ingest) | Accept one part of a SCIP index upload (compiler-tier symbols and references) for a batch. | writer | idempotent | no | yes |
| [`code.scip_status`](operations/code.md#codescip_status) | Report the progress of a SCIP upload batch: parts received and apply status. | viewer | read | no |  |
| [`code.search`](operations/code.md#codesearch) | Search the shared code index of this repository (fresh to the last merged PR). | viewer | read | yes |  |
| [`code.setting_facts`](operations/code.md#codesetting_facts) | One ClickHouse setting, the four places that define it: its declaration and tier (production / important / beta / experimental / obsolete), every default change with the release it shipped in… | viewer | read | no |  |
| [`code.symbol`](operations/code.md#codesymbol) | Exact symbol lookup: definition, signature, doc comment, and body of a function/class/enum by (qualified) name — compiler-grade where available (tier='scip'), resolved across C++ overloads… | viewer | read | yes |  |
| [`code.symbol_history`](operations/code.md#codesymbol_history) | The regression history of one function: the merged PRs that touched it (with linked issues, reverts, inline review comments inside its span), and — through the tracker namespace bound to this code… | viewer | read | no |  |
| [`code.test_gate`](operations/code.md#codetest_gate) | The pre-PR question in one call: for THIS change, which tests must run, and which touched code no test reaches. | viewer | read | no |  |
| [`code.test_home`](operations/code.md#codetest_home) | Where does a new test case belong? | viewer | read | no |  |
| [`code.tests_for`](operations/code.md#codetests_for) | Which stateless tests carry EVERY named facet: the tests that `SET` a setting, call a function, create an `ENGINE`, read a `system.*` table, use a `FORMAT`, carry a `-- Tags:` tag, or expect an… | viewer | read | no |  |
| [`code.trace`](operations/code.md#codetrace) | Paste a stack trace / backtrace; get each frame resolved to its definition in one call — the incident-workflow entry point. | viewer | read | yes |  |
| [`code.verify`](operations/code.md#codeverify) | Verify one closed-form claim against the index and return a three-valued verdict. | viewer | read | yes |  |
| [`code.verify_citations`](operations/code.md#codeverify_citations) | Every citation a draft carries, resolved against the index before the draft is posted somewhere public and irreversible. | viewer | read | no |  |

### collection

Fields for every `collection` op: [operations/collection.md](operations/collection.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`collection.create`](operations/collection.md#collectioncreate) | Create a collection: a namespace read and written only through grants. | viewer | idempotent | no | yes |
| [`collection.delete`](operations/collection.md#collectiondelete) | Delete a collection with what it holds: a purging erasure of the namespace (documents, chunks, originals, memory leave the disk), then it leaves the directory and the tree, and its name is never… | admin | idempotent | no | yes |
| [`collection.list`](operations/collection.md#collectionlist) | List the collections the caller can read, by name. | viewer | read | no |  |
| [`collection.move`](operations/collection.md#collectionmove) | Put a collection under another one, or detach it. | admin | idempotent | no | yes |

### conflicts

Fields for every `conflicts` op: [operations/conflicts.md](operations/conflicts.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`conflicts.allowlist_list`](operations/conflicts.md#conflictsallowlist_list) | List (memory_key, memory_key) pairs marked as known-non-conflicting so the conflicts worker stops flagging them. | viewer | read | no |  |
| [`conflicts.allowlist_remove`](operations/conflicts.md#conflictsallowlist_remove) | Revoke an allow decision; the pair will re-flag on future writes. | admin | idempotent | no | yes |
| [`conflicts.list`](operations/conflicts.md#conflictslist) | List unresolved conflicts where two memories contradict each other. | viewer | read | no |  |
| [`conflicts.propose_revision`](operations/conflicts.md#conflictspropose_revision) | Return non-destructive revise/contract/allow suggestions. | viewer | read | no |  |
| [`conflicts.resolve`](operations/conflicts.md#conflictsresolve) | Pick the winner of a conflict (keep A, keep B, keep both, merge). | admin | idempotent | no | yes |

### consolidations

Fields for every `consolidations` op: [operations/consolidations.md](operations/consolidations.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`consolidations.list`](operations/consolidations.md#consolidationslist) | Namespace-scoped consolidation-run history. agent + user_id are optional — runs aggregate across them when omitted. | viewer | read | no |  |

### context

Fields for every `context` op: [operations/context.md](operations/context.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`context.fetch`](operations/context.md#contextfetch) | Read back a parked result by key: a character slice (offset/limit) or only the lines matching a regex (pattern, with context_lines). | viewer | read | no |  |
| [`context.offload`](operations/context.md#contextoffload) | Park a large tool result (log, page, diff, query output) outside the model context. | writer | idempotent | no |  |
| [`context.pack`](operations/context.md#contextpack) | Startup context pack for this session, once near the start of a substantial task: profile, relevant memories, matching procedures, optionally docs/conversations, in one prompt-sized bundle. | viewer | read | yes |  |

### conversation

Fields for every `conversation` op: [operations/conversation.md](operations/conversation.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`conversation.append`](operations/conversation.md#conversationappend) | Log one chat turn (role + content) to the active thread. | writer | idempotent | yes |  |
| [`conversation.compact`](operations/conversation.md#conversationcompact) | Summarize old turns into one embedded system turn and tombstone the originals. | writer | idempotent | no | yes |
| [`conversation.create`](operations/conversation.md#conversationcreate) | Open a thread container. | writer | idempotent | yes |  |
| [`conversation.delete`](operations/conversation.md#conversationdelete) | Soft-delete one thread: its messages, thread row and embeddings no other agent holds. | admin | idempotent | no | yes |
| [`conversation.get`](operations/conversation.md#conversationget) | Fetch one thread by id: thread metadata + optional messages. | viewer | read | no |  |
| [`conversation.list`](operations/conversation.md#conversationlist) | Enumerate stored conversation threads for an agent / namespace. | viewer | read | no |  |
| [`conversation.list_saved_messages`](operations/conversation.md#conversationlist_saved_messages) | Page through the messages a user saved, newest first, with no query: the first view of a "Saved" panel before anything is typed. | viewer | read | no |  |
| [`conversation.save_message`](operations/conversation.md#conversationsave_message) | Save one chat message a user picked for search, whether or not the namespace records transcripts. | writer | idempotent | no |  |
| [`conversation.saved_status`](operations/conversation.md#conversationsaved_status) | Which of a conversation's messages a user has saved, in one call: a chat client draws every message's saved state when it opens the conversation. | viewer | read | no |  |
| [`conversation.search`](operations/conversation.md#conversationsearch) | Rank conversation messages by embedding similarity to `query`. | viewer | read | no |  |
| [`conversation.search_saved_messages`](operations/conversation.md#conversationsearch_saved_messages) | Hybrid search over only the messages one user saved with conversation.save_message, not the whole transcript. | viewer | read | no |  |
| [`conversation.set_logging`](operations/conversation.md#conversationset_logging) | Toggle whether this namespace records conversation transcripts. | admin | idempotent | no | yes |
| [`conversation.threads`](operations/conversation.md#conversationthreads) | Recent conversation threads of a namespace, newest first. | viewer | read | yes |  |
| [`conversation.unsave_message`](operations/conversation.md#conversationunsave_message) | Withdraw one message from a user's saved-message search. | writer | idempotent | no |  |
| [`conversation.update`](operations/conversation.md#conversationupdate) | Update one thread's title/metadata/run_id. | writer | idempotent | no |  |

### cost

Fields for every `cost` op: [operations/cost.md](operations/cost.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`cost.agent_summary`](operations/cost.md#costagent_summary) | The coding agent's own spend (loom.agent_turn_usage) over a date window — the series `cost.summary` does not cover, which prices what LOOM spends upstream. | viewer | read | yes |  |
| [`cost.budget_status`](operations/cost.md#costbudget_status) | This namespace's upstream spend in the last 24 h against its cap. | viewer | read | yes |  |
| [`cost.record_turns`](operations/cost.md#costrecord_turns) | The coding agent's own spend for one session, a batch of turns per call. | writer | idempotent | no |  |
| [`cost.summary`](operations/cost.md#costsummary) | Cost rollup over a date window. | viewer | read | no |  |

### decision

Fields for every `decision` op: [operations/decision.md](operations/decision.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`decision.chain`](operations/decision.md#decisionchain) | Trace a decision's causal chain: upstream (what caused or influenced it) and/or downstream (what it went on to cause), up to 4 hops over the authored graph. | viewer | read | no |  |
| [`decision.outcome`](operations/decision.md#decisionoutcome) | Close the loop on a decision once reality has answered: record what actually happened and whether it worked (success). | writer | write | no | yes |
| [`decision.precedents`](operations/decision.md#decisionprecedents) | BEFORE deciding, ask what was decided in similar situations: semantic search over past decisions (optionally within a category), each with its reasoning, rejected alternatives, and — when known… | viewer | read | no |  |
| [`decision.record`](operations/decision.md#decisionrecord) | Record a DECISION as a first-class object — call it whenever you choose between options that matter later (a vendor, a design, a fix, a rollback). | writer | idempotent | no | yes |

### erasure

Fields for every `erasure` op: [operations/erasure.md](operations/erasure.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`erasure.export`](operations/erasure.md#erasureexport) | GDPR Article 20 portability. | admin | idempotent | yes | yes |
| [`erasure.list`](operations/erasure.md#erasurelist) | Audit-shaped roll-up of the org's erasures the caller may see: the org-wide view for an org admin, the namespace sagas of the caller's own namespaces otherwise. | viewer | read | no |  |
| [`erasure.request`](operations/erasure.md#erasurerequest) | GDPR Article 17 forgetting. | writer | idempotent | yes | yes |
| [`erasure.status`](operations/erasure.md#erasurestatus) | Check the status of an in-flight erasure request (queued, running, completed, failed). | viewer | read | no |  |

### extraction_policy

Fields for every `extraction_policy` op: [operations/extraction_policy.md](operations/extraction_policy.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`extraction_policy.clear`](operations/extraction_policy.md#extraction_policyclear) | Remove the scope's policy. | org_admin | idempotent | no | yes |
| [`extraction_policy.get`](operations/extraction_policy.md#extraction_policyget) | The scope's policy and the one its extractions run under. | org_admin | idempotent | no |  |
| [`extraction_policy.set`](operations/extraction_policy.md#extraction_policyset) | Set the scope's policy, replacing the one it held. | org_admin | idempotent | no | yes |

### facts

Fields for every `facts` op: [operations/facts.md](operations/facts.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`facts.expire_by_adapter`](operations/facts.md#factsexpire_by_adapter) | Tombstone graph-level triples emitted by a specific extractor (e.g. a buggy adapter) without touching the parent memories. | admin | write | no | yes |
| [`facts.list`](operations/facts.md#factslist) | Subject-relation-object triples from the entity graph, filtered by subject / object / relation / entity; optional source keys. | viewer | read | yes |  |
| [`facts.path`](operations/facts.md#factspath) | Find how two entities are connected: a bounded bidirectional breadth-first search over the same validity-filtered edge set `facts.traverse` walks, meeting in the middle. | viewer | read | no |  |
| [`facts.related`](operations/facts.md#factsrelated) | Return the entities that co-occur with `input.entity` via any relation, ranked by total co_occur (DESC). | viewer | read | no |  |
| [`facts.traverse`](operations/facts.md#factstraverse) | Walk the entity co-occurrence graph N hops out from a seed, one bounded query per hop. | viewer | read | yes |  |

### grants

Fields for every `grants` op: [operations/grants.md](operations/grants.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`grants.approve`](operations/grants.md#grantsapprove) | Approve (or reject) a pending grant request. | admin | idempotent | no | yes |
| [`grants.create`](operations/grants.md#grantscreate) | Grant another subject (user or service-account) viewer / writer / admin role on a namespace. | admin | idempotent | yes | yes |
| [`grants.list`](operations/grants.md#grantslist) | List active grants on a namespace — who has access, at which role. | viewer | read | yes |  |
| [`grants.pending_for_me`](operations/grants.md#grantspending_for_me) | Every pending request in the namespaces the caller administers, in one statement. | viewer | read | no |  |
| [`grants.revoke`](operations/grants.md#grantsrevoke) | Remove a subject's grant on a namespace. | admin | idempotent | no | yes |
| [`grants.share_agent`](operations/grants.md#grantsshare_agent) | Let another agent of the caller's trust read, or write, the caller's own namespace. | writer | idempotent | no | yes |
| [`grants.unshare_agent`](operations/grants.md#grantsunshare_agent) | Take back what the caller lent another agent of its trust. | writer | idempotent | no | yes |

### inbox

Fields for every `inbox` op: [operations/inbox.md](operations/inbox.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`inbox.list`](operations/inbox.md#inboxlist) | Inboxes the caller may see: those owned by ``namespace`` (the authorized namespace) plus, with ``mine_everywhere``, every inbox in the org whose owner namespace the caller can read or that lists the… | viewer | read | no |  |
| [`inbox.policy_list`](operations/inbox.md#inboxpolicy_list) | List an inbox's trust policies. | viewer | read | no |  |
| [`inbox.policy_set`](operations/inbox.md#inboxpolicy_set) | Set the trust mode an inbox owner chose for a poster and task kind (Loom stores it, never changes it). | admin | idempotent | no | yes |
| [`inbox.principals`](operations/inbox.md#inboxprincipals) | The identities loom already knows in the org — the poster picker's source. | admin | idempotent | no |  |
| [`inbox.register`](operations/inbox.md#inboxregister) | ``namespace`` is the owner namespace (the caller must be its admin). | admin | idempotent | no | yes |

### knowledge

Fields for every `knowledge` op: [operations/knowledge.md](operations/knowledge.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`knowledge.add`](operations/knowledge.md#knowledgeadd) | WHAT: Ingest a document into the knowledge base for later retrieval via loom_search. | writer | idempotent | yes |  |
| [`knowledge.bulk_add`](operations/knowledge.md#knowledgebulk_add) | Batched ingestion: many documents in one CH round-trip. | writer | idempotent | yes |  |
| [`knowledge.bulk_delete`](operations/knowledge.md#knowledgebulk_delete) | Tombstone multiple documents by source_uri in one lightweight UPDATE. | writer | idempotent | no | yes |
| [`knowledge.dedup_list`](operations/knowledge.md#knowledgededup_list) | List cross-URI doc duplicates and (optionally) chunk-overlap clusters. | admin | idempotent | no |  |
| [`knowledge.dedup_resolve`](operations/knowledge.md#knowledgededup_resolve) | Resolve one document duplicate pair. | admin | idempotent | no | yes |
| [`knowledge.delete`](operations/knowledge.md#knowledgedelete) | Soft-delete a document by source_uri: tombstones all live chunks and version-bumps the documents row. | writer | idempotent | yes | yes |
| [`knowledge.get`](operations/knowledge.md#knowledgeget) | Fetch a single document by source_uri. | viewer | read | yes |  |
| [`knowledge.list`](operations/knowledge.md#knowledgelist) | Paginated listing of documents in a namespace, for curation tools. | viewer | read | yes |  |
| [`knowledge.outline`](operations/knowledge.md#knowledgeoutline) | A document's table of contents, as recorded on its chunks at ingest, and optionally one section's assembled text. | viewer | read | no |  |
| [`knowledge.reads`](operations/knowledge.md#knowledgereads) | How often agents opened each document over the last `days` (`knowledge.get` ledger rows; dashboard, assistant and bench traffic left out). | viewer | read | no |  |
| [`knowledge.search`](operations/knowledge.md#knowledgesearch) | Search the user's uploaded document library — bills, contracts, receipts, resumes, policies, manuals, technical docs. | viewer | read | no |  |
| [`knowledge.source.add`](operations/knowledge.md#knowledgesourceadd) | Register a document source on a repository's trunk code namespace: a kind, the ids it reads and a read-only credential. | admin | write | no | yes |
| [`knowledge.source.check`](operations/knowledge.md#knowledgesourcecheck) | Probe a registered document source with its stored credential now (folder checks and a listing), nothing written. | admin | idempotent | no |  |
| [`knowledge.source.inspect`](operations/knowledge.md#knowledgesourceinspect) | One registered document source: its config, its sync state and the documents it wrote (the 200 most recently ingested). | viewer | read | no |  |
| [`knowledge.source.list`](operations/knowledge.md#knowledgesourcelist) | The document sources registered on a repository's code namespace, with each one's sync state. | viewer | read | no |  |
| [`knowledge.source.remove`](operations/knowledge.md#knowledgesourceremove) | Retire a registered document source: its registry row goes inactive and its credential is cleared. | admin | write | no | yes |
| [`knowledge.source.rotate`](operations/knowledge.md#knowledgesourcerotate) | Replace a registered document source's credential. | admin | write | no | yes |
| [`knowledge.source.sync_now`](operations/knowledge.md#knowledgesourcesync_now) | Record a request to sync one document source in its sync state: the sync worker starts its next pass with it. | admin | idempotent | no | yes |
| [`knowledge.synthesize`](operations/knowledge.md#knowledgesynthesize) | Complete a task over WHOLE documents, not a search slice: every active chunk of the named documents is read in order, in windows that respect section boundaries, each window is noted once, and the… | viewer | idempotent | no | yes |
| [`knowledge.update_metadata`](operations/knowledge.md#knowledgeupdate_metadata) | Edit doc-level metadata (title, description, tags). | writer | idempotent | no | yes |
| [`knowledge.upsert`](operations/knowledge.md#knowledgeupsert) | Same fields as knowledge.add. | writer | idempotent | no |  |

### learnings

Fields for every `learnings` op: [operations/learnings.md](operations/learnings.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`learnings.list`](operations/learnings.md#learningslist) | List learnings candidates by status (pending by default, promoted, rejected). | viewer | read | no |  |
| [`learnings.quality`](operations/learnings.md#learningsquality) | Per-namespace approve-ratio rollup of the learnings queue. | viewer | read | no |  |
| [`learnings.review`](operations/learnings.md#learningsreview) | Approve or reject a pending learning. | admin | idempotent | no | yes |

### memory

Fields for every `memory` op: [operations/memory.md](operations/memory.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`memory.agents_list`](operations/memory.md#memoryagents_list) | Distinct agent names that have ever written into (org, namespace). agent + user_id are forced sentinels — we aggregate ACROSS them to discover which agents exist. | viewer | read | yes |  |
| [`memory.aggregate`](operations/memory.md#memoryaggregate) | Templated SQL aggregation over typed records in loom.memory. | viewer | read | no |  |
| [`memory.apply`](operations/memory.md#memoryapply) | One scope grammar for every write that can touch more than one row. | writer | write | yes | yes |
| [`memory.ask`](operations/memory.md#memoryask) | Ask a question and get a grounded answer synthesized from the caller's recalled memories (or, with allow_synthesis=False, just the memories). | viewer | read | yes |  |
| [`memory.assign_category`](operations/memory.md#memoryassign_category) | Link a memory to one of the namespace's registered categories. | writer | idempotent | no | yes |
| [`memory.auto_retirements`](operations/memory.md#memoryauto_retirements) | The memories Loom retired on its own (the conflict resolver, `newest_wins`), newest record first, inside the request's read scope. | viewer | read | no |  |
| [`memory.backlinks`](operations/memory.md#memorybacklinks) | What points at one memory: its version chain, the digests whose lineage names it, the typed links in and out of it, how many evidence turns support it, and the memories that share its entities. | viewer | read | no |  |
| [`memory.brief`](operations/memory.md#memorybrief) | A task or question to orient on. | viewer | read | yes |  |
| [`memory.bulk_get`](operations/memory.md#memorybulk_get) | Fetch many memory entries by key in one CH round-trip. | viewer | read | no |  |
| [`memory.bulk_set`](operations/memory.md#memorybulk_set) | Insert many memory entries in one batched CH insert. | writer | idempotent | no |  |
| [`memory.clear`](operations/memory.md#memoryclear) | Bulk-tombstone memories in one scope. | admin | idempotent | yes | yes |
| [`memory.consolidate`](operations/memory.md#memoryconsolidate) | Periodic consolidation pass — merges fragmented memories about the same subject that slip past hash-dedup and extraction-time dedup. | writer | write | yes | yes |
| [`memory.copy_to`](operations/memory.md#memorycopy_to) | Self-service copy of a memory into another namespace. | writer | write | no | yes |
| [`memory.cross_promote`](operations/memory.md#memorycross_promote) | Cross-namespace promotion with provenance. | viewer | write | no | yes |
| [`memory.dedup_collapse`](operations/memory.md#memorydedup_collapse) | Collapse every duplicate pair of a namespace onto its canonical — no LLM, every key kept, nothing tombstoned. | writer | idempotent | no | yes |
| [`memory.dedup_list`](operations/memory.md#memorydedup_list) | List clusters of status='duplicate' rows in a namespace. | admin | idempotent | no |  |
| [`memory.dedup_resolve`](operations/memory.md#memorydedup_resolve) | Resolve one duplicate pair. | admin | idempotent | no | yes |
| [`memory.delete`](operations/memory.md#memorydelete) | WHAT: Tombstone a memory by key (14-day recovery in the dashboard). | writer | idempotent | yes | yes |
| [`memory.delete_version`](operations/memory.md#memorydelete_version) | Tombstone one version of a key under this exact identity, or nothing when the row under it has moved on. | writer | idempotent | no | yes |
| [`memory.demonstrate`](operations/memory.md#memorydemonstrate) | Test-time learning from memory: the nearest memories tagged as examples (default tag `example`, label carried as `label:<value>`), a similarity-weighted vote over their labels, the predicted label… | viewer | read | no |  |
| [`memory.demote`](operations/memory.md#memorydemote) | Revert a shared memory back to private. | admin | idempotent | no | yes |
| [`memory.digests`](operations/memory.md#memorydigests) | List the session digests of a namespace. | viewer | read | yes |  |
| [`memory.evidence`](operations/memory.md#memoryevidence) | List the source turns a memory was extracted from, with the supporting span and whether each link was cited by the extractor, matched lexically, or fell back to the whole window. | viewer | read | yes |  |
| [`memory.export_evidence`](operations/memory.md#memoryexport_evidence) | The raw chunks and session digests behind a namespace's memories, as portable rows (paged). | viewer | read | yes |  |
| [`memory.exposure_summary`](operations/memory.md#memoryexposure_summary) | Per-(actor, agent) distinct memory keys returned by reads of a namespace over a window — how many distinct records each token has extracted — computed for every token, from… | admin | idempotent | no |  |
| [`memory.extraction_retry_mine`](operations/memory.md#memoryextraction_retry_mine) | Replay your failed deferred memory.set_from_messages batches once more, by queue_id. | writer | idempotent | no |  |
| [`memory.extraction_status`](operations/memory.md#memoryextraction_status) | State of deferred set_from_messages batches. | viewer | read | no |  |
| [`memory.extraction_status_mine`](operations/memory.md#memoryextraction_status_mine) | Your deferred memory.set_from_messages batches, by the queue_id each call returned. | viewer | read | no |  |
| [`memory.for_file`](operations/memory.md#memoryfor_file) | What Loom knows about ONE file, shaped for the moment before an edit: memories whose files[] facet names the path (repo-relative or bare filename both match). | viewer | read | yes |  |
| [`memory.get`](operations/memory.md#memoryget) | WHAT: Fetch one memory by exact `memory_key`. | viewer | read | yes |  |
| [`memory.get_by_id`](operations/memory.md#memoryget_by_id) | Fetch a memory by its UUID (not memory_key). | viewer | read | yes |  |
| [`memory.health`](operations/memory.md#memoryhealth) | Knowledge-base health for (org, namespace): how much of what is stored is ever recalled, what is silent, what earns its keep. agent + user_id are sentinels — the report aggregates across them. | viewer | read | yes |  |
| [`memory.history`](operations/memory.md#memoryhistory) | Enumerate the version history of one memory_key. | viewer | read | yes |  |
| [`memory.import`](operations/memory.md#memoryimport) | Import items another memory system distilled, through every check a write makes. | writer | idempotent | no |  |
| [`memory.import_curated`](operations/memory.md#memoryimport_curated) | Split a curated markdown doc into sections, write each via handle_memory_set, returning per-section verdicts. | writer | write | no | yes |
| [`memory.import_evidence`](operations/memory.md#memoryimport_evidence) | Write exported evidence chunks and digests into a namespace, re-embedded here and linked to their memory keys. | writer | write | yes | yes |
| [`memory.inspect`](operations/memory.md#memoryinspect) | Inspect one user, agent or session of a namespace, read-only. | viewer | read | no |  |
| [`memory.keys`](operations/memory.md#memorykeys) | Every live row's identity and key in one read: a set an exporter can check its paged listing against, since a row that moves while pages are read can be skipped. | viewer | read | no |  |
| [`memory.lineage`](operations/memory.md#memorylineage) | Assemble the provenance chain for a SHARED memory. | viewer | read | no |  |
| [`memory.link`](operations/memory.md#memorylink) | Author an explicit edge between two memories (or two entities). | writer | write | yes | yes |
| [`memory.list`](operations/memory.md#memorylist) | List memory keys under this identity, optionally filtered. | viewer | read | yes |  |
| [`memory.lock`](operations/memory.md#memorylock) | Lock one row against a class of writes. | writer | write | yes | yes |
| [`memory.move`](operations/memory.md#memorymove) | Rename `from_key` to `to_key`, leaving the old key answering. | writer | write | yes | yes |
| [`memory.outcome`](operations/memory.md#memoryoutcome) | External outcome signal for a memory row. | writer | write | no | yes |
| [`memory.outcome_run`](operations/memory.md#memoryoutcome_run) | Apply one outcome to many recalled memories in a single call — the bulk form of memory.outcome, so callers skip a per-key loop. | writer | write | yes | yes |
| [`memory.outline`](operations/memory.md#memoryoutline) | A namespace's table of contents, read from structure alone: row counts by type, the dated sessions with their digest's first line, the hub entities by degree, the communities when a pass has… | viewer | read | no |  |
| [`memory.owner_report`](operations/memory.md#memoryowner_report) | The owner's view of their memory: what they own, who read it, how it was used. | viewer | read | no |  |
| [`memory.pin`](operations/memory.md#memorypin) | WHAT: Save a standing fact and protect it. | writer | idempotent | yes |  |
| [`memory.promote`](operations/memory.md#memorypromote) | Promote a single source memory to the namespace's shared layer. | admin | write | no | yes |
| [`memory.proposal_decide`](operations/memory.md#memoryproposal_decide) | Accept (→ active, served by default) or reject a list of proposals. | writer | idempotent | no | yes |
| [`memory.proposals`](operations/memory.md#memoryproposals) | List facts on trial (written with memory.set proposed=true) with the graded runs that relied on each — passes, fails — and what the namespace's proposal policy says (`ready`: accept / reject). | viewer | read | no |  |
| [`memory.provenance`](operations/memory.md#memoryprovenance) | Resolve a memory's origin for user-facing provenance. | viewer | read | yes |  |
| [`memory.recall`](operations/memory.md#memoryrecall) | Search the raw conversation turns behind this namespace's memories — the messages `memory.set_from_messages` extracted from — by text and by time. | viewer | read | yes |  |
| [`memory.record.conflicts`](operations/memory.md#memoryrecordconflicts) | Field-level disagreements recorded when a typed record superseded the active record for its logical key: which field, both values, both record ids and both citing memories. | viewer | read | no |  |
| [`memory.record.history`](operations/memory.md#memoryrecordhistory) | Every version of one typed record, newest first. | viewer | read | yes |  |
| [`memory.record.query`](operations/memory.md#memoryrecordquery) | Typed records of one schema by field filters and predicates, as of a time. | viewer | read | yes |  |
| [`memory.record.write`](operations/memory.md#memoryrecordwrite) | Write one typed record (schema name + fields, optional validity window); a newer record for the same key supersedes the old. | writer | write | yes | yes |
| [`memory.redact`](operations/memory.md#memoryredact) | Scrub the value of a historical memory row, preserving its audit entry. | admin | idempotent | no | yes |
| [`memory.reindex`](operations/memory.md#memoryreindex) | Re-embed a namespace's memories whose vectors were made by another embedding model (or all of them with `force`). | admin | idempotent | no | yes |
| [`memory.reject`](operations/memory.md#memoryreject) | Mark a memory as explicitly rejected (extractor / user review decided the candidate fact is wrong), so it isn't re-extracted. | writer | idempotent | no | yes |
| [`memory.related`](operations/memory.md#memoryrelated) | Find memories sharing entities with the source memory. | viewer | read | yes |  |
| [`memory.resolve_orphan_duplicates`](operations/memory.md#memoryresolve_orphan_duplicates) | Scan (org, namespace) for orphaned duplicate rows and re-activate them. | writer | idempotent | yes | yes |
| [`memory.restore`](operations/memory.md#memoryrestore) | Bring back a deleted memory (within 14 days) or one Loom retired on its own. | writer | idempotent | yes | yes |
| [`memory.search`](operations/memory.md#memorysearch) | Semantic search over the agent's own memory. | viewer | read | no |  |
| [`memory.search_trace`](operations/memory.md#memorysearch_trace) | Same shape as memory.search; returns the vector leg's per-candidate scoring breakdown (no fusion, assembly or reranker). | viewer | read | no |  |
| [`memory.search_trace_log`](operations/memory.md#memorysearch_trace_log) | Read loom.search_trace_log for a namespace (requires the telemetry flag). | viewer | read | yes |  |
| [`memory.set`](operations/memory.md#memoryset) | Save a fact to Loom — shared across every AI tool the user runs; native memory is the Claude-only scratchpad. | writer | idempotent | yes |  |
| [`memory.set_from_messages`](operations/memory.md#memoryset_from_messages) | Run LLM extraction on `messages` and write the extracted facts. | writer | write | yes |  |
| [`memory.stack`](operations/memory.md#memorystack) | Compose a token-budgeted recall bundle for agent wake-up. | viewer | read | no |  |
| [`memory.statuses`](operations/memory.md#memorystatuses) | The current status of some memories, by id: which automatic retirements still stand (the Review queue's Auto-resolved tab reads it). | viewer | read | no |  |
| [`memory.sweep_expired`](operations/memory.md#memorysweep_expired) | Run one expiry-sweep pass now — the same pass the `memory_expiry_sweep` worker runs on its interval, scoped to an org and optionally a namespace. | admin | idempotent | no | yes |
| [`memory.timeline`](operations/memory.md#memorytimeline) | Return memories about an entity (or matching a tag filter) in strict temporal order — newest first, with optional since/until windows. | viewer | read | no |  |
| [`memory.touch`](operations/memory.md#memorytouch) | Change only a memory's expiry: set a new TTL (`ttl_seconds`) or clear it (`clear_expiry`), for one key or up to 1,000 (`memory_keys`), without rewriting its value, entities, conflict state or… | writer | write | no |  |
| [`memory.unlock`](operations/memory.md#memoryunlock) | Lift a memory.lock (admin). | admin | idempotent | no | yes |
| [`memory.unpin`](operations/memory.md#memoryunpin) | Remove the write-time pin from one live memory. | admin | idempotent | no | yes |
| [`memory.update`](operations/memory.md#memoryupdate) | Key-addressed update: the fact changed or got more specific. | writer | write | yes |  |
| [`memory.update_by_id`](operations/memory.md#memoryupdate_by_id) | Update a memory by UUID with an optimistic concurrency check. | writer | idempotent | yes |  |
| [`memory.verify`](operations/memory.md#memoryverify) | Attest to (or revoke the attestation on) one memory. | writer | idempotent | yes | yes |

### memory_schema

Fields for every `memory_schema` op: [operations/memory_schema.md](operations/memory_schema.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`memory_schema.approve`](operations/memory_schema.md#memory_schemaapprove) | Approve (make active) or reject a proposed record schema. | admin | write | no | yes |
| [`memory_schema.list`](operations/memory_schema.md#memory_schemalist) | List a namespace's record schemas, active ones only unless `include_proposed`. | viewer | read | no |  |
| [`memory_schema.propose`](operations/memory_schema.md#memory_schemapropose) | Register a record schema as `proposed`, for an admin to approve or reject. | writer | write | no | yes |
| [`memory_schema.register`](operations/memory_schema.md#memory_schemaregister) | Register a typed-record schema (named fields) for a namespace; typed records are then written against it. | admin | write | yes | yes |

### mentor

Fields for every `mentor` op: [operations/mentor.md](operations/mentor.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`mentor.brief`](operations/mentor.md#mentorbrief) | One agent's usage profile and the store's hints. | viewer | read | yes |  |
| [`mentor.guide`](operations/mentor.md#mentorguide) | The agent guide this server ships; the same for every caller. | viewer | read | no |  |

### namespace

Fields for every `namespace` op: [operations/namespace.md](operations/namespace.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`namespace.audit`](operations/namespace.md#namespaceaudit) | Usage report for one namespace: is it being used as a memory, or as an object store? | viewer | read | yes |  |
| [`namespace.browse`](operations/namespace.md#namespacebrowse) | Discover public namespaces in the org; anyone can join returned rows via namespace.join. | viewer | read | yes |  |
| [`namespace.cancel_request`](operations/namespace.md#namespacecancel_request) | Withdraw the caller's own pending elevation request. | viewer | idempotent | no | yes |
| [`namespace.delete`](operations/namespace.md#namespacedelete) | Tombstone a namespace and stop accepting writes against it. | admin | idempotent | no | yes |
| [`namespace.get`](operations/namespace.md#namespaceget) | Fetch one namespace's metadata + settings (owner, visibility, feature flags, retention). | viewer | read | yes |  |
| [`namespace.guide_get`](operations/namespace.md#namespaceguide_get) | The namespace's guide: Markdown its writers keep on what it holds, its key layout and what its tags mean. | viewer | read | no |  |
| [`namespace.guide_history`](operations/namespace.md#namespaceguide_history) | The guide's revisions, newest first, with summary, note, writer and length. | viewer | read | no |  |
| [`namespace.guide_set`](operations/namespace.md#namespaceguide_set) | Save a new revision of the namespace's guide after you change one of its conventions: the whole text, a `note`, and `expected_revision` = the revision you read. | writer | idempotent | no | yes |
| [`namespace.inventory`](operations/namespace.md#namespaceinventory) | What one namespace holds and who appears in it, with what was not counted. | viewer | read | no |  |
| [`namespace.join`](operations/namespace.md#namespacejoin) | Self-add as a viewer on a public namespace. | viewer | idempotent | no | yes |
| [`namespace.leave`](operations/namespace.md#namespaceleave) | Drop the caller's grant on a namespace. | viewer | idempotent | no | yes |
| [`namespace.list`](operations/namespace.md#namespacelist) | Enumerate every namespace the caller can see (own + granted + public). | org_admin | idempotent | no |  |
| [`namespace.list_categories`](operations/namespace.md#namespacelist_categories) | Enumerate registered categories with item counts. | viewer | read | no |  |
| [`namespace.list_mine`](operations/namespace.md#namespacelist_mine) | List namespaces the caller is a member of (own + granted). | viewer | read | yes |  |
| [`namespace.list_ontology`](operations/namespace.md#namespacelist_ontology) | List the ontology terms (entity and relation kinds) registered for a namespace. | viewer | read | no |  |
| [`namespace.mark_seen`](operations/namespace.md#namespacemark_seen) | Acknowledge a new-invitation indicator on a namespace. | viewer | idempotent | no |  |
| [`namespace.register`](operations/namespace.md#namespaceregister) | Create a shared memory workspace within an org; caller becomes admin. | viewer | idempotent | yes | yes |
| [`namespace.register_category`](operations/namespace.md#namespaceregister_category) | Register a category in a namespace. | writer | write | no | yes |
| [`namespace.register_ontology`](operations/namespace.md#namespaceregister_ontology) | Register an entity type or relation verb for a namespace. | admin | idempotent | yes | yes |
| [`namespace.restore`](operations/namespace.md#namespacerestore) | Restore a snapshot payload into a target namespace. | viewer | idempotent | no | yes |
| [`namespace.rollback`](operations/namespace.md#namespacerollback) | Return every memory head in a namespace to the version it had at `as_of`. | admin | write | no | yes |
| [`namespace.search_categories`](operations/namespace.md#namespacesearch_categories) | Cosine-rank the category registry against a query. | viewer | read | no |  |
| [`namespace.snapshot`](operations/namespace.md#namespacesnapshot) | Freeze a namespace's curated state as a portable JSON payload. | admin | idempotent | no | yes |
| [`namespace.summary`](operations/namespace.md#namespacesummary) | Aggregated read-only metadata for one namespace, for a single-shot dashboard tile (category/memory counts, top entities, recent activity). | viewer | read | yes |  |
| [`namespace.taxonomy`](operations/namespace.md#namespacetaxonomy) | The de-facto taxonomy of a namespace — tags + entities across its memories. | viewer | read | yes |  |
| [`namespace.tune`](operations/namespace.md#namespacetune) | Draft changes to a namespace's extraction rules, categories and guide from what its rows and the audit show. | viewer | read | no |  |
| [`namespace.validate`](operations/namespace.md#namespacevalidate) | Integrity report for one namespace, in one call, shaped for the agent that just wrote to it. | viewer | read | yes |  |

### observe

Fields for every `observe` op: [operations/observe.md](operations/observe.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`observe.crystallize`](operations/observe.md#observecrystallize) | Mark a session's pending observations ready for extraction into memories (queues only). | writer | idempotent | yes |  |
| [`observe.record`](operations/observe.md#observerecord) | Record one tool observation (input, output, files, error) into the episodic plane; no LLM call. | writer | idempotent | yes |  |

### ops

Fields for every `ops` op: [operations/ops.md](operations/ops.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`ops.capabilities`](operations/ops.md#opscapabilities) | Identity is optional: a client discovers the surface with whatever token it holds (role check only; no namespace is addressed). | viewer | read | yes |  |
| [`ops.health`](operations/ops.md#opshealth) | Run the deployment's self-checks (schema contracts, embedding stubs, ClickHouse parts, op mix, procedure plane, workers) and report each as ok, warn or fail. | org_admin | idempotent | no |  |
| [`ops.projection_failures.list`](operations/ops.md#opsprojection_failureslist) | List recorded projection failures: derived rows (embeddings, graph edges, summaries) a write could not produce. | org_admin | idempotent | no |  |
| [`ops.projection_failures.resolve`](operations/ops.md#opsprojection_failuresresolve) | Mark projection failures resolved by id without retrying them. | org_admin | idempotent | no | yes |
| [`ops.projection_failures.retry`](operations/ops.md#opsprojection_failuresretry) | Retry open projection failures for a namespace, optionally one projection kind. | org_admin | idempotent | no | yes |

### org

Fields for every `org` op: [operations/org.md](operations/org.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`org.model_calls`](operations/org.md#orgmodel_calls) | The org's current setting; enabled and unlocked when it was never set. | org_admin | idempotent | no |  |
| [`org.set_model_calls`](operations/org.md#orgset_model_calls) | Switch the org's model calls off or on. | org_admin | idempotent | no | yes |

### policy

Fields for every `policy` op: [operations/policy.md](operations/policy.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`policy.check`](operations/policy.md#policycheck) | A hypothetical call: what would the rules decide right now? | viewer | read | yes |  |
| [`policy.decisions`](operations/policy.md#policydecisions) | List the recorded history-policy decisions for a namespace, optionally one verdict. | viewer | read | no |  |
| [`policy.delete`](operations/policy.md#policydelete) | Remove a history-policy rule (tombstoned, so its past decisions stay explainable). | admin | idempotent | no | yes |
| [`policy.list`](operations/policy.md#policylist) | The namespace's policy rules and the enforcement mode. | viewer | read | yes |  |
| [`policy.set`](operations/policy.md#policyset) | Create or replace a namespace history-policy rule (effect, actions, condition). | admin | idempotent | no | yes |

### procedure

Fields for every `procedure` op: [operations/procedure.md](operations/procedure.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`procedure.add`](operations/procedure.md#procedureadd) | Register a procedural memory — a 'how to do X' recipe with structured (name, situation, steps) fields. | writer | write | yes |  |
| [`procedure.evolve`](operations/procedure.md#procedureevolve) | Consolidate a procedure's accumulated run traces into an improved graph: rank the proven path by SUCCESSFUL traversals, flag steps no run ever used as dead ends, flag steps and transitions that sit… | writer | write | no |  |
| [`procedure.fork`](operations/procedure.md#procedurefork) | Copy a procedure from a namespace the caller can read into the request namespace, with its evidence reset. | writer | write | no | yes |
| [`procedure.list`](operations/procedure.md#procedurelist) | Enumerate registered procedures in the namespace (optionally filtered by memory_key prefix or tags). | viewer | read | no |  |
| [`procedure.locate`](operations/procedure.md#procedurelocate) | Where am I in this procedure, and what comes next? | viewer | read | no |  |
| [`procedure.match`](operations/procedure.md#procedurematch) | Find procedures whose `situation` field is semantically closest to the caller's described situation. | viewer | read | yes |  |
| [`procedure.record_run`](operations/procedure.md#procedurerecord_run) | Report one execution of a procedure so loom can learn which path actually works. | writer | write | no |  |
| [`procedure.revise`](operations/procedure.md#procedurerevise) | Propose an edited topology for a procedure — you are the refiner. | writer | write | no | yes |

### profile

Fields for every `profile` op: [operations/profile.md](operations/profile.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`profile.activity`](operations/profile.md#profileactivity) | Recent profile reads per agent — "which AI client read my profile, when?". | viewer | read | yes |  |
| [`profile.append`](operations/profile.md#profileappend) | WHAT: Atomic append to an existing profile section. | writer | write | yes |  |
| [`profile.render`](operations/profile.md#profilerender) | Render every section in (org, namespace) into one prompt fragment. | viewer | read | yes |  |
| [`profile.replace`](operations/profile.md#profilereplace) | WHAT: Substring rewrite within a profile section. | writer | idempotent | no |  |
| [`profile.set`](operations/profile.md#profileset) | WHAT: Create or replace a profile section at `label`. | writer | idempotent | yes |  |
| [`profile.structured`](operations/profile.md#profilestructured) | The user's structured profile and whether it is current. | viewer | read | no |  |

### profile_schema

Fields for every `profile_schema` op: [operations/profile_schema.md](operations/profile_schema.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`profile_schema.clear`](operations/profile_schema.md#profile_schemaclear) | Clear the schema: no profile is built or rebuilt for the namespace, and reads answer `no_schema`. | admin | idempotent | no | yes |
| [`profile_schema.get`](operations/profile_schema.md#profile_schemaget) | The namespace's profile schema, its hash, and who set it when. | viewer | read | no |  |
| [`profile_schema.set`](operations/profile_schema.md#profile_schemaset) | Set the namespace's profile schema. | admin | idempotent | no | yes |

### run

Fields for every `run` op: [operations/run.md](operations/run.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`run.begin`](operations/run.md#runbegin) | Open a RUN — one execution of a task — BEFORE you start working on it. | writer | idempotent | yes | yes |
| [`run.complete`](operations/run.md#runcomplete) | Close a run opened with run.begin — call EXACTLY ONCE when the task ends. | writer | write | yes | yes |
| [`run.get`](operations/run.md#runget) | Replay one run: goal, status, final response, the memories it relied on, the memory_keys it wrote, how many memories it recalled and observed, and what its model turns cost. | viewer | read | yes |  |
| [`run.grade`](operations/run.md#rungrade) | A verdict on a CLOSED run, usually days after it — a maintainer confirmed the finding, CI passed, a reviewer rejected it. | writer | idempotent | no | yes |
| [`run.list`](operations/run.md#runlist) | A page of a namespace's agent runs and their match count (`total`), filtered and sorted. | viewer | read | no |  |

### search

Fields for every `search` op: [operations/search.md](operations/search.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`search.query`](operations/search.md#searchquery) | Search every plane of the caller's Loom content in one call. | viewer | read | yes |  |

### session

Fields for every `session` op: [operations/session.md](operations/session.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`session.brief_delete`](operations/session.md#sessionbrief_delete) | Delete a session brief by brief_key when the user asks for it to be forgotten. | writer | idempotent | no | yes |
| [`session.brief_get`](operations/session.md#sessionbrief_get) | Load a brief: by `brief_key`, else the newest brief a given session wrote (`from_session`), else the newest for `project`. | viewer | read | no |  |
| [`session.brief_list`](operations/session.md#sessionbrief_list) | List the session briefs in this namespace, newest first: key, revision, age, agent and goal, one line each. | viewer | read | no |  |
| [`session.brief_save`](operations/session.md#sessionbrief_save) | Revise (or start) one brief. | writer | idempotent | no | yes |

### task

Fields for every `task` op: [operations/task.md](operations/task.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`task.accept`](operations/task.md#taskaccept) | Accept a triaged task into the inbox's backlog (owner side), optionally setting priority and constraints. | writer | write | no |  |
| [`task.block`](operations/task.md#taskblock) | Mark a task blocked, releasing its lease, with the reason and what it is blocked on. | writer | write | no |  |
| [`task.cancel`](operations/task.md#taskcancel) | A transition anyone entitled may make, with a reason; ``fence`` is required only when the caller is the current owner of an in_progress task. | writer | write | no |  |
| [`task.claim`](operations/task.md#taskclaim) | Claim a todo task under a lease; the returned fence guards every later progress, block or finish. | writer | write | no |  |
| [`task.complete`](operations/task.md#taskcomplete) | WHAT: Finish a task you claimed: pass the fence from task.pull and an outcome (links, ids, a short result). | writer | write | no |  |
| [`task.counts`](operations/task.md#taskcounts) | Count tasks by status for one inbox or every inbox of a namespace. | viewer | read | no |  |
| [`task.create`](operations/task.md#taskcreate) | ``namespace`` is the poster's own namespace; ``inbox`` the target. | writer | idempotent | no |  |
| [`task.defer`](operations/task.md#taskdefer) | WHAT: Put a claimed task back with a resume_at; the sweeper returns it to the queue at that time. | writer | write | no |  |
| [`task.fail`](operations/task.md#taskfail) | WHAT: Mark a claimed task failed with a reason; the owner side may reopen it, and the inbox's max_attempts decides when it is blocked on a human instead. | writer | write | no |  |
| [`task.get`](operations/task.md#taskget) | One task by `task_id`: its state, lease, poster, result and history. | viewer | read | no |  |
| [`task.heartbeat`](operations/task.md#taskheartbeat) | WHAT: Renew the lease on a task you claimed (fence from task.pull) and optionally leave a progress note. | writer | idempotent | no |  |
| [`task.history`](operations/task.md#taskhistory) | Return a task's event history (posted, accepted, claimed, progress, finished, ...). | viewer | read | no |  |
| [`task.list`](operations/task.md#tasklist) | Tasks in ``inbox`` (owner side) or, with ``posted_by_me``, the tasks the caller posted anywhere in the org. | viewer | read | no |  |
| [`task.progress`](operations/task.md#taskprogress) | Record a progress note on a claimed task and extend its lease. | writer | write | no |  |
| [`task.pull`](operations/task.md#taskpull) | ``namespace`` is the inbox's owner namespace; the caller must be a writer there. | writer | write | no |  |
| [`task.reassign`](operations/task.md#taskreassign) | Owner side: cancel here, re-post into another inbox as a child task (the destination's trust policy applies to the original poster). | writer | write | no |  |
| [`task.reopen`](operations/task.md#taskreopen) | A transition anyone entitled may make, with a reason; ``fence`` is required only when the caller is the current owner of an in_progress task. | writer | write | no |  |
| [`task.resume`](operations/task.md#taskresume) | A deferred task back to ``todo`` now, ahead of its ``resume_at``. | writer | write | no |  |
| [`task.unblock`](operations/task.md#taskunblock) | Owner side, or the poster when the task is blocked on the sender. | writer | write | no |  |
| [`task.update`](operations/task.md#taskupdate) | Edit the brief. | writer | write | no |  |
| [`task.updates`](operations/task.md#taskupdates) | The sender's cursor: events on tasks this caller posted (or, with ``inbox``, every event in an inbox the caller owns) after ``since_seq``. | viewer | read | no |  |
| [`task.wont_do`](operations/task.md#taskwont_do) | A transition anyone entitled may make, with a reason; ``fence`` is required only when the caller is the current owner of an in_progress task. | writer | write | no |  |

### tracker

Fields for every `tracker` op: [operations/tracker.md](operations/tracker.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`tracker.answer`](operations/tracker.md#trackeranswer) | Answer a tracker question in prose — or from an artefact's anchors — by running the catalogue's templates: escalation / incident context, a customer across channels, incidents / escalations /… | viewer | read | no |  |
| [`tracker.binding`](operations/tracker.md#trackerbinding) | What this namespace may ask the tracker plane: a QA namespace answers itself; a code namespace answers the tracker namespace bound to it (`LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`), passed through… | viewer | read | no |  |
| [`tracker.component`](operations/tracker.md#trackercomponent) | One component's risk snapshot and its open issues, most severe first. | viewer | read | no |  |
| [`tracker.contract`](operations/tracker.md#trackercontract) | The record contract as the register page shows it before anyone connects: every table loom reads, required or optional, each column's family, the type a view writes, whether a default is safe, what… | viewer | read | no |  |
| [`tracker.duplicate_check`](operations/tracker.md#trackerduplicate_check) | Does this draft already exist? | viewer | read | no |  |
| [`tracker.flaky`](operations/tracker.md#trackerflaky) | Is this test a known flaky? | viewer | read | no |  |
| [`tracker.item`](operations/tracker.md#trackeritem) | The tracker record of up to ten issue / PR numbers: component, severity, bisect verdict and branches still affected, test presence, CI result, reviewers, backport lineage, and the fixes / backports… | viewer | read | no |  |
| [`tracker.links`](operations/tracker.md#trackerlinks) | Which PR probably fixed this issue — or which issue this PR probably fixed — when nobody wrote the link down. | viewer | read | no |  |
| [`tracker.release`](operations/tracker.md#trackerrelease) | Release readiness for one version (`26.3`): the blocker rows with their status and the latest health snapshot. | viewer | read | no |  |
| [`tracker.similar`](operations/tracker.md#trackersimilar) | The reports that already say it: duplicate and related issues / PRs for a pasted report (`text`), an existing item (`issue_number` — its own title, body, component, versions and error code become… | viewer | read | no |  |
| [`tracker.source.add`](operations/tracker.md#trackersourceadd) | Register a tracker source: a ClickHouse endpoint and database holding the record contract (docs/features/tracker/record-contract.md), a read-only credential, and the tracker namespaces that read it. | viewer | write | no | yes |
| [`tracker.source.check`](operations/tracker.md#trackersourcecheck) | Run the record-contract check on a configured tracker source now: the same read the boot gate runs, its verdict cached. | viewer | read | no |  |
| [`tracker.source.inspect`](operations/tracker.md#trackersourceinspect) | Connect with a read-only credential and say what the database holds — every table the user can read, its columns and types — plus a first mapping of it onto loom's logical model. | viewer | read | no |  |
| [`tracker.source.list`](operations/tracker.md#trackersourcelist) | The tracker sources of an org: every configured source with its contract verdict, breaker and freshness, and the tracker namespaces reading it — each with the directory row's effective join rule and… | viewer | read | no |  |
| [`tracker.source.remove`](operations/tracker.md#trackersourceremove) | Retire a registered tracker source and its namespaces. | viewer | idempotent | no | yes |
| [`tracker.source.rotate`](operations/tracker.md#trackersourcerotate) | Replace a registered source's credential. | viewer | write | no | yes |
| [`tracker.source.set_mapping`](operations/tracker.md#trackersourceset_mapping) | Change a registered tracker's mapping, with the same checks as add; `mapping: null` returns it to the views case. | viewer | idempotent | no | yes |
| [`tracker.test_signal`](operations/tracker.md#trackertest_signal) | Flaky, regression or infrastructure? | viewer | read | no |  |

### ui

Fields for every `ui` op: [operations/ui.md](operations/ui.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`ui.vitals`](operations/ui.md#uivitals) | Record one dashboard page's web vitals; the per-op ledger row the transport writes carries the fields. | viewer | idempotent | no |  |

### webhook

Fields for every `webhook` op: [operations/webhook.md](operations/webhook.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`webhook.deliveries`](operations/webhook.md#webhookdeliveries) | The namespace's webhook events of the last 3 days and their delivery state. | admin | idempotent | no |  |
| [`webhook.list`](operations/webhook.md#webhooklist) | The namespace's memory webhooks. | admin | idempotent | no |  |
| [`webhook.register`](operations/webhook.md#webhookregister) | Register a URL that is told when memory in the namespace changes. | admin | idempotent | no | yes |
| [`webhook.remove`](operations/webhook.md#webhookremove) | Remove one memory webhook; none of its events is sent after the call, save a POST already in flight. | admin | idempotent | no | yes |

### working_memory

Fields for every `working_memory` op: [operations/working_memory.md](operations/working_memory.md).

| Operation | Summary | Role | Kind | Listed under `all` | Audit |
| --- | --- | --- | --- | --- | --- |
| [`working_memory.append`](operations/working_memory.md#working_memoryappend) | Append to a working-memory block. | writer | write | yes |  |
| [`working_memory.delete`](operations/working_memory.md#working_memorydelete) | Delete one working-memory block. | writer | idempotent | yes |  |
| [`working_memory.get`](operations/working_memory.md#working_memoryget) | Fetch one working-memory block by key. | viewer | read | no |  |
| [`working_memory.render`](operations/working_memory.md#working_memoryrender) | Render working-memory blocks as XML or markdown. context.pack already includes this section; direct render is mainly for SDK or custom orchestration code. | viewer | read | yes |  |
| [`working_memory.replace`](operations/working_memory.md#working_memoryreplace) | Replace exact text inside a working-memory block. | writer | idempotent | yes |  |
| [`working_memory.set`](operations/working_memory.md#working_memoryset) | Replace one small working-memory block. | writer | idempotent | yes |  |

<!-- prettier-ignore-end -->
<!-- END GENERATED: operations -->

## Related

- [HTTP API](./http-api.md): request gates, headers, idempotency, pagination, rate limits.
- [MCP](./mcp.md): the tool list, toolsets, results and errors over MCP.
- [Errors](./errors.md): every error code an op can answer with.
- [Python SDK](./python-sdk.md) and [TypeScript SDK](./typescript-sdk.md).
- [Adding an operation](../development/adding-an-operation.md).
