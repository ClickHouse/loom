# Tasks, decisions and policies

Three planes sit beside memory for coordination between agents. Every op is callable by name on any MCP listing;
which ones the `all` listing shows is set out in [MCP toolsets](../reference/mcp.md#toolsets).

| Plane          | Answers                                                         | Ops                 |
| -------------- | --------------------------------------------------------------- | ------------------- |
| Task inbox     | "This work belongs to another agent. How do I hand it over?"    | `inbox.*`, `task.*` |
| Decisions      | "What did we decide in a case like this, why, and did it work?" | `decision.*`        |
| History policy | "May this agent do this now, given what it just did?"           | `policy.*`          |

**The owner decides.** Loom stores the mode an owner sets and applies it. It never promotes a poster to trusted, never
auto-accepts a triaged task, and never moves a policy rule from shadow to enforce. Where Loom offers evidence (a
poster's history, a rule's shadow ledger), a person or the owner agent reads it and changes the mode. An automatic
trust change would let a writer earn its way past a gate by gaming the signal it watches.

## Task inbox

Each agent has a durable queue. A poster can write into an inbox it cannot read. The inbox owner sets how far to trust
each poster. A claimed task has one owner at a time. Agents pull their work; an optional signed webhook only says
"pull now".

```mermaid
sequenceDiagram
    participant S as Sender (public-bot)
    participant L as Loom inbox private-bot
    participant R as Receiver (private-bot)
    S->>L: task.create (kind, brief, disclosure)
    Note over L: policy: deny / review / auto
    R->>L: task.pull (long poll)
    L-->>R: claimed task + fence
    R->>L: task.heartbeat (fence)
    R->>L: task.complete (fence, outcome)
    S->>L: task.updates (since_seq)
    L-->>S: events: accepted, claimed, done
```

### Statuses

```mermaid
stateDiagram-v2
    [*] --> triage: untrusted poster (review)
    [*] --> todo: trusted poster (auto)
    triage --> todo: task.accept
    triage --> wont_do: task.wont_do
    todo --> in_progress: task.pull / task.claim
    in_progress --> done: task.complete
    in_progress --> failed: task.fail
    in_progress --> deferred: task.defer
    in_progress --> blocked: task.block
    in_progress --> todo: lease expired
    deferred --> todo: resume_at / task.resume
    blocked --> todo: task.unblock
    failed --> todo: task.reopen
    wont_do --> todo: task.reopen
    cancelled --> todo: task.reopen
    done --> [*]
```

- `task.wont_do` (owner) and `task.cancel` (owner, or the poster while nobody owns the task) apply from any unfinished
  status. A finished task (`done`, `wont_do`, `failed`, `cancelled`) answers 409 `TASK_TERMINAL`.
- `task.reopen` (owner) moves `failed`, `wont_do` or `cancelled` back to `todo`.
- Every move off `triage → todo → in_progress → done` carries a `reason`.

### Register an inbox

An admin of the owner namespace registers the inbox and sets trust per `(poster, kind)`:

```python
from loom import Agent

async with Agent(org="acme", namespace="private", agent="admin", api_key=ADMIN_TOKEN) as a:
    await a.tasks.register_inbox(
        "private-bot",
        owner_agent="private-bot",
        posters=["sa:public-bot"],  # may post without holding the namespace
        accept_disclosure=["confidential", "internal"],
        default_lease_seconds=1800,
        max_attempts=3,
    )
    await a.tasks.set_policy(
        "private-bot",
        poster="sa:public-bot",
        kind="security_finding",
        mode="review",
        require_evidence=["inputs.finding_id", "inputs.test"],
    )
    await a.tasks.set_policy("private-bot", poster="sa:public-bot", kind="*", mode="deny")
```

| Mode     | A task from this poster and kind…                                           |
| -------- | --------------------------------------------------------------------------- |
| `deny`   | is refused with 403 `TASK_POSTER_DENIED`.                                   |
| `review` | lands in `triage` until the owner agent or a person accepts or declines it. |
| `auto`   | lands in `todo`, ready to pull.                                             |

- With no row, the inbox owner and a writer on the owner namespace post `auto`; anyone else posts `review`.
- A row keyed on an agent label grants `auto` only to a writer on the owner namespace, because the caller picks its
  label. Key outside posters on their service-account subject (`sa:<name>`).
- `require_evidence` names brief fields a create must carry; a 422 names the missing ones.
- Disclosure levels are `confidential`, `internal` and `public_ok`. A task with a level the inbox does not accept gets
  a 422 naming the accepted values.

`inbox.register` arguments:

| Argument                        | Default     | Meaning                                                         |
| ------------------------------- | ----------- | --------------------------------------------------------------- |
| `default_lease_seconds`         | 1800        | Lease a claim gets unless the pull asks for another.            |
| `max_attempts`                  | 3           | Expired leases before the task is blocked on a person.          |
| `max_defers`                    | 5           | Defers before the task is blocked on a person.                  |
| `triage_escalate_after_seconds` | 14400 (4 h) | Age at which an unanswered triage item is escalated, once.      |
| `webhook_url`                   | empty       | Receives a signed wake-up notice when the inbox has new events. |
| `webhook_secret_ref`            | empty       | Environment variable holding the webhook signing secret.        |

`webhook_url` is an `http` or `https` URL whose host resolves only to public addresses:
each delivery resolves it, refuses loopback, private, link-local (cloud metadata) and
multicast addresses, connects to the address it checked (the name stays the `Host`
header and TLS SNI) and follows no redirect. `webhook_secret_ref` must name one of the
inbox org's own variables, `LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`: ORG is the org id
with lower-case letters upper-cased, digits kept and every other byte (upper-case
letters included) written `_` and two hex digits (org `acme-corp` →
`LOOM_WEBHOOK_SECRET_ACME_2DCORP__ALERTS`, `acme_corp` → `ACME_5FCORP`), NAME is
letters and digits.
`inbox.register` refuses any other name (422), and a row that names one is never read,
so no tenant can have Loom sign with another tenant's secret. No two org ids share an
ORG part.

### Post a task

```python
async with Agent(org="acme", namespace="public", agent="public-bot", api_key=PUBLIC_TOKEN) as a:
    out = await a.tasks.create(
        "private-bot",  # the target inbox
        kind="security_finding",
        title="Out-of-bounds read in the JSON reader",
        brief={
            "objective": "confirm and file in the private repository",
            "inputs": {"finding_id": "f-77", "test": "json_oob.sql"},
        },
        disclosure="confidential",
        idempotency_key="f-77",  # a retried create lands once
    )
    out["task"]["status"]  # "triage", or "todo" when the poster is trusted
```

Two creates by one poster with one `idempotency_key` store one task, even when they race: they run one at a
time under a lease on the key, each answers with the task, and only the one that stored it says `created=true`.
A key is its poster's own: another poster's create with the same key stores its own task. A retry of a create
that was cut off after storing its task writes the `created` event it left out; its missing lease row is the
lease sweep's to write, so the task is claimable after the sweep's 60 s grace and one more pass. When the first
create's own event still lands (it was cancelled mid-insert), the two share one deduplication token and the
readers (`task.history`, `task.updates`, `/v1/task/watch`, webhooks) keep only the first, so a task is created
once in its history and in each cursor's delivery. A create that waits more than 10 s for another with its key
answers 409 `PRECONDITION_FAILED`, and 503 when the lease store (Keeper) does not answer; retry either.

`namespace` is the poster's own; the first argument names the inbox. `create` also takes `priority` (default 3),
`deadline`, `not_before`, `origin` and `parent_task_id`. A task with `not_before` is `todo` but hidden from `pull`
until then, which is how a bot keeps its own "retry later" backlog.

| Refusal                                 | Answer                                                 |
| --------------------------------------- | ------------------------------------------------------ |
| May not post but reads the inbox        | 403 `AUTH_INSUFFICIENT`                                |
| May neither post nor read it            | 404 `RESOURCE_NOT_FOUND`, as for an unregistered inbox |
| Policy denies this poster or kind       | 403 `TASK_POSTER_DENIED`                               |
| Disclosure not accepted                 | 422, with the accepted values                          |
| Brief misses a `require_evidence` field | 422, with the missing field names                      |

### Pull, work, finish

```python
async with Agent(org="acme", namespace="private", agent="private-bot", api_key=PRIVATE_TOKEN) as a:
    async for task in a.tasks.stream("private-bot", kinds=["security_finding"]):
        fence = task["fence"]
        try:
            result = await work(
                task["brief"], heartbeat=lambda: a.tasks.heartbeat(task["task_id"], fence=fence)
            )
            await a.tasks.complete(task["task_id"], fence=fence, outcome={"issue_url": result.url})
        except Exception as e:
            await a.tasks.fail(task["task_id"], fence=fence, reason=str(e)[:500])
```

- **Long poll.** `task.pull` and `task.updates` wait up to 30 s (`wait_seconds`). A create on the same server process
  wakes a waiting pull at once; one on another replica is seen within about 2 s.
- **Leases and fences.** A claim gets a lease and a `fence` token that every owner write presents. `task.heartbeat`
  extends the lease. When a worker dies, the `task_lease_sweep` worker returns the task to `todo`, and after
  `max_attempts` expiries blocks it on a person. A stale fence gets 409 `TASK_FENCE_STALE`. A claim stands only
  when the task, read again after the claim wins its lease, is still `todo` at that fence; otherwise the claim
  gives the lease back as it found it. Each sweep also puts right a lease row a race left out of step with its
  task: a `todo` or triage task with no row gets one, an idle or expired row takes its task's fence (a claim cut
  off after winning leaves one a fence ahead), and an ended task's row goes. It skips rows written in the last
  60 s, which may belong to a create or reopen still in flight; a row the reconcile wrote back for a task a
  cancel was ending goes in the first pass after its 60 s grace. A cancel that lands between a claim's lease
  and its status write is overwritten by the claim, which leaves the task `in_progress`; this race predates
  the reconcile and is not closed by it.
- **Other moves.** `task.defer` (with `resume_at`), `task.block` (`blocked_on`: `sender`, `human`, `external`),
  `task.wont_do`, and `task.progress` (a note, no state change).
- **Triage.** `task.pull(status="triage")` returns review items unclaimed; decide with `task.accept` or
  `task.wont_do`. A poster may not accept its own task (403 `TASK_SELF_ACCEPT`). Nothing expires or auto-accepts a
  triaged task.

### Follow the outcome

The sender reads its own tasks' events with a cursor:

```python
upd = await a.tasks.updates(since_seq=cursor, wait_seconds=30)
for ev in upd["events"]:  # accepted, claimed, done, wont_do, blocked, ...
    reconcile(ev)
if upd["events"]:
    cursor = upd["next_seq"]  # save only after reconcile succeeds
```

A crash between the read and the save replays events, so `reconcile` must tolerate repeats. The cursor stays about
3 s behind the newest events so a row still landing on another replica is not skipped.

### Operations

| Group                     | Ops                                                                                                                    |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Inbox setup (admin)       | `inbox.register`, `inbox.policy_set`, `inbox.principals`; `inbox.list`, `inbox.policy_list` (viewer)                   |
| Posting (sender)          | `task.create`, `task.updates`, `task.cancel`                                                                           |
| Working (owner)           | `task.pull`, `task.claim`, `task.heartbeat`, `task.progress`, `task.complete`, `task.fail`, `task.defer`, `task.block` |
| Triage and repair (owner) | `task.accept`, `task.wont_do`, `task.unblock`, `task.resume`, `task.reopen`, `task.update`, `task.reassign`            |
| Reading                   | `task.get`, `task.list`, `task.counts`, `task.history`                                                                 |

The Python SDK exposes them as `agent.tasks`. Over MCP, `?toolset=tasks` lists the ones a working agent uses:
`task.pull`, `task.create`, `task.complete`, `task.fail`, `task.wont_do`, `task.defer`, `task.heartbeat`,
`task.updates`, `task.list`, `task.get`, `inbox.list`.

The dashboard's **Tasks** page lists every inbox of the namespace, with tiles for what needs a person, rows grouped by
next step, Accept / Won't do inline, and a panel per task (`task.update` with `if_match_version`). A writer moves a
task through every transition the state machine has: from the panel's buttons (Start is `task.claim` under a one-day
lease held by `loom-ui`; Mark done and Fail are `task.complete` / `task.fail`, fenced with the current lease), or by
dragging a row or card onto another group, which runs the one-step move there (into To do accepts, unblocks, resumes
or reopens; into In progress starts; into Waiting blocks; into Closed marks done or won't do). Groups a task cannot
reach in one step are dimmed while it is held. Inbox settings pick posters from the identities Loom already knows in
the org (`inbox.principals`, admin).

### Settings

| Setting                             | Default | Effect                                                                                                                       |
| ----------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_TASK_LEASE_SWEEP_ENABLED`     | true    | Runs the sweeper: expired leases, deferred tasks reaching `resume_at`, stale triage, lease rows out of step with their task. |
| `LOOM_TASK_WEBHOOK_DELIVER_ENABLED` | true    | Posts one signed notice per new event batch to an inbox's `webhook_url`.                                                     |

See [configuration: task inbox](../reference/configuration.md#task-inbox).

## Decisions

A decision record holds a choice that matters later: a vendor, an architecture, a policy exception.
`decision.record` takes `category`, `scenario`, `reasoning`, `outcome`, `alternatives`, `confidence`, and the keys it
was `caused_by` or `influenced_by`. It writes:

- a typed record of the built-in `decision` schema;
- a searchable memory row of type `decision` under the same key;
- an edge from each `caused_by` / `influenced_by` key to the decision.

A decision recorded in a repository's trunk code namespace also reaches MCP `loom_search` from a checkout of that
repository, for a caller who may read the namespace ([unified search](unified-search.md#reading-the-memory-section)).

The memory row's value is one statement,
`<scenario>. Decided <outcome> because <reasoning>. Rejected: <alternative>; …`, then `Realized: …` once
`decision.outcome` has recorded one. The category is not in it: it stays on the record and in the row's
`decision:category:<category>` tag, which is what a filter by category reads. A reasoning that is the outcome word for
word (case, whitespace and the closing punctuation aside) records a ban or an invariant stated with no reason: the
statement reads `Decided <outcome>.` and `context.pack` gives no `because`. Every part is stored as the record keeps
it, whole within the record's limits (scenario 2,000 characters, outcome 1,000, reasoning 4,000, realized outcome
2,000, up to ten alternatives of 300 each). A longer alternative is cut at a space, or between two CJK characters, and
ends in `…`, so no half name reads as a file. A statement can run to about 12,000 characters. `memory.for_file` shows
it whole, or names it when it would pass the block's ceiling, and search embeds its excerpt, the first 1,200 stored
characters less its ` (#<key>)` suffix, so it opens with the scenario; a longer statement is also stored as chunks,
which search reads too.

`stage` is `decided` (the default) or `proposed`: an option put forward and not yet chosen or built, such as a proposal,
a plan or an action item. A proposed decision's statement reads `Proposed <outcome> because <reasoning>.` and its row
carries the `decision:proposed` tag, so the pre-edit block and search present it as a proposal. A proposed ban keeps its
`constraint` tag: the pre-edit block still shows it first, worded as a proposal, and reads `Proposed <outcome>.` when no
reason is stated.

The stage is kept on the row, not the record: `context.pack` lists a proposed decision as `→ proposed: <outcome>`, and
`decision.precedents` and `decision.chain` answer its `stage`, read from the tag on the row that holds the record (a
decision whose outcome is recorded reads as decided).

`decision.outcome` drops the stage, since an outcome records that the decision was acted on: the statement then reads
`Decided … Realized: …`. Recording the decision again with `stage: "decided"` drops it too. `decision.record` ignores a
caller's own `decision:proposed` tag; `memory.update`, which replaces a row's tags, edits the tag like any other, and
the readers follow it.

A `loom decisions extract` run keeps the stage of a row whose words stay the same ([Extract decisions from a
document](#extract-decisions-from-a-document), **Stage**), so a proposal later marked accepted keeps its rows proposed.
Mark such a decision decided by sending the body a `--dry-run` reports for it (it carries the extractor's identity, so
it replaces the row) with `stage: "decided"`, record its outcome with `decision.outcome`, or delete the document's
extracted rows that are not pinned or attested and extract it again, which reads every row's stage afresh.

- **Checks**: the blacklist reads every stored field in full; the sensitivity and the attribute labels read the stored
  category, scenario, outcome, reasoning, alternatives and realized outcome (not `decision_maker` or `run_id`, which
  the row never holds).
- **Files**: `files` sets the row's files as on `memory.set`; unset, they are detected from the stored fields, never
  from the realized outcome. `memory.for_file` serves the decision before an edit of a file it names, as it is,
  `(pinned)` when pinned and `(source changed)` once tagged `source-changed`
  ([how the block reads](../concepts/memory.md#tags-entities-and-files)).
- **Source**: `source_resource_id` and `source_class` set those columns as on `memory.set`; `source_resource_id`
  cites the document the decision was taken from.
- **Outcome**: `decision.outcome` keeps the row's files, source, declared class and the caller's tags. It drops the
  `decision:proposed` tag, sets the `decision` tags again and works out the server's `sensitivity:` and `attribute:`
  labels again.

A decision is identified by its key, not its text. The write-time conflict check never merges a decision into another
decision's row, files it as a duplicate of one, lets one replace it or lets a pinned one refuse it, however alike they
read; a close decision under another key is flagged as a conflict. The conflict resolver may later retire one of a
close pair it judges an update of the other; that is recorded as `memory.auto_supersede`, and `memory.restore` undoes
it.

`category` plus `scenario` identify a decision. Deciding the same scenario again supersedes the earlier record
(`superseded_prior=true`); history keeps it. The key is derived as a record's identity key is (see [typed
records](memory/writing.md#typed-records)): a scenario in any script records, and a category or scenario with no
letter or digit (punctuation, symbols or marks alone) is refused with 400 `MISSING_IDENTITY_FIELD`. A decision
recorded again word for word writes the same statement, which `memory.set` takes as a restatement: it reinforces the
row, and a pinned decision accepts it. The restatement compares the statement's bytes, so a row stored under another
statement form is revised rather than restated by the same fields, whether they come from `decision.record` or from
`decision.outcome`, which rebuilds the statement too. A row whose value is locked refuses either with 423 `LOCKED`
until the lock ends or is released; an attested row takes the write, and its attestation reads stale until
`memory.verify` binds it again; a pinned row refuses `decision.record` until it is unpinned, and the pin is set again
only through `memory.pin`, while `decision.outcome` keeps the pin. When the writer of a pinned decision decides it
otherwise, the call is refused with 409 `PINNED_CONFLICT` and nothing is written; an admin unpins it with
`memory.unpin` first. The pin guards only its writer's row: another identity's decision on the same scenario still
supersedes the record. The new record holds only what the new decision states: fields it leaves out, and an earlier
`realized_outcome` or `success`, are not carried over, whether it is written by `decision.record` or
`memory.record.write`. A `decision.outcome` without `success` keeps the record's verdict.

The record holds the text of its last writer, the agent, user and session that recorded it, so a delete of that
writer's decision row retires it; an expired row still counts as live. Another identity's row under the same key was
superseded in the record plane and does not keep the record.

- **Retires the record**: `memory.delete` or `memory.delete_version` of the writer's decision row, including
  `memory.move` with `keep_redirect=false` and `memory.apply` with `action="delete"`, which delete through
  `memory.delete`; `conflicts.resolve` and `memory.dedup_resolve` for the side they tombstone; `blacklist.add`
  with `sweep=true` and `blacklist.sweep`. `decision.chain`, `decision.precedents` and `decision.outcome` stop
  serving it, and `memory.record.history` shows the last version with `status="tombstoned"`. Two retirements that
  race can each write a tombstoned version, so history may list the tombstone twice.
- **After a retirement**: `memory.restore`, or `namespace.rollback` past the delete, revives the row but not the
  record; call `decision.record` again. A row moved with `keep_redirect=false` is no longer a decision: its new key
  has no record, and `decision.record` with the same category and scenario records it again under its derived key.
- **Leaves the record active**: a delete of another identity's row; `memory.clear` and `namespace.rollback`, which
  tombstone rows another way; `memory.move` with `keep_redirect=true`, whose redirect row still counts as the
  writer's live row under the old key; a `memory.set` under the writer's identity that rewrites the key with another
  `memory_type`; the expiry sweep's tombstone of an expired writer row.
- **A record with no decision row**: `memory.record.write` on the `decision` schema writes the record alone.
  `decision.chain` serves it, and `decision.outcome` answers not-found for it; record decisions with `decision.record`.
- **`decision.outcome`**: writes under the record's writer, keeping a pinned row's pin, and answers not-found when
  the writer's row is not live. Its row write is conditional on the row it read: it answers 409 `PRECONDITION_FAILED`
  when the decision changes during the call or another conditional write holds the key, and 503 when the key's lease
  store is unreachable; call it again. A re-record that keeps the decision's text leaves the row's value as it was,
  so the outcome also checks the writer's record before its row write and again before its record write, and answers
  the same 409 when it moved; caught at the second check, the row holds the outcome's statement and outcome tags
  until the call is made again, and that call counts as a restatement of the row. The condition is on the row's
  value, not its pin: a `memory.unpin` that lands between the outcome's read and its write is undone, as the outcome
  writes the row pinned again.

| Operation             | Role   | Does                                                                                               |
| --------------------- | ------ | -------------------------------------------------------------------------------------------------- |
| `decision.record`     | writer | Record a decision and its causal edges.                                                            |
| `decision.precedents` | viewer | Semantic search over past decisions (at most 25), optionally by `category` or `with_outcome_only`. |
| `decision.chain`      | viewer | Walk causal edges `upstream`, `downstream` or `both`, up to 4 hops.                                |
| `decision.outcome`    | writer | Record `realized_outcome` and `success`; credit or charge the memories the deciding run relied on. |

Pass the `run_id` the decision was made under: `decision.outcome` finds the memories to credit through that run's
`memories_used` ([runs](runs.md)).

```python
rec = await agent.decision.record(
    category="storage",
    scenario="retention for raw transcripts in the support namespace",
    reasoning="Replay needs 30 days; legal hold is handled by export, not by retention.",
    outcome="keep transcripts 30 days",
    alternatives=["keep forever", "7 days"],
    confidence=0.8,
    influenced_by=["policy/legal/hold"],
)
prior = await agent.decision.precedents("how long do we keep transcripts?", category="storage")
await agent.decision.outcome(rec["memory_key"], "no replay gaps in the quarter", success=True)
```

### Extract decisions from a document

`loom decisions extract` reads one [knowledge](knowledge.md) document and records the decisions it states. It runs
in-process with no token, so it is an operator command, and it calls the configured LLM per window of the
document's sections: a read (twice for a window read again, below) and a support check, plus one more check per window
when a stored decision looks gone, also in a dry run. Its model calls are not gated by the
namespace's cost cap. A model call held for the org (its model calls switched off, or refused or deferred by the
inference gateway), for a model read or for an embedding a write needs, stops the run: nothing more is written,
nothing is retired, and it exits 1. The document-source sync runs the same extraction on each document it writes,
within a share of the namespace's cost cap ([mining decisions](knowledge.md#mining-decisions)).

```bash
loom decisions extract --org O --namespace NS --source-uri URI            # dry run: prints what it would write
loom decisions extract --org O --namespace NS --source-uri URI --apply    # writes
loom decisions extract --org O --namespace NS --source-uri URI --repo OWNER/NAME --into source --apply
```

- **Repository**: by default the document's namespace is where its code names are looked up and its decisions are
  written, which fits a document added to a repository's code namespace. A document in a person's or a team's
  namespace names the repository with `--repo OWNER/NAME` (or a remote URL), matched by owner/name as `code.repos`
  matches a checkout's remote (a fork's remote is not followed to its upstream: name the upstream); its names are
  then looked up in that repository's trunk code namespace, and `--into` chooses where the rows go:
  - `--into source` (the default for a document outside a code namespace): the document's namespace, each row tagged
    `repo:<id>`, the tag the coding plugins send to `memory.for_file`, so a session whose own namespace (the plugin's
    `namespace`) is the document's gets the row before editing a file it names. The pre-edit hook reads only that
    namespace and the repository's trunk, so a team's shared document whose readers work from their own namespaces
    goes in with `--into repo`. A document in the trunk itself gets untagged rows, read there by every editor.
  - `--into repo` (the default for a document in a code namespace): the repository's trunk code namespace, where every
    reader of the repository gets the row before an edit. `source_resource_id` still cites the document in its own
    namespace; there is no per-document access, so a repository reader who cannot read the document's namespace sees
    the row's decision but is refused the document it cites. The rows are the trunk's: erasing the document's namespace
    leaves them, and `memory.delete` in the trunk removes them.
  - **Tags**: under `--into source` for a document outside the trunk, a row of the extractor's the run keeps as it is
    (pinned, attested or with a recorded outcome, still stated or not judged, or one the retire step keeps because it
    is still stated or was not judged), and a row it tags `source-changed`, gains the `repo:<id>` tag by a tags-only
    write; another writer's row gains none.
  - **Access**: the run reads the document's namespace and the repository's, and writes where the rows go. With an
    auth context bound it checks each (viewer, viewer, and writer, or viewer for a dry run) and refuses before any
    model call; the CLI runs with none, as the operator.
  - **Rows from an earlier run**: every memory row the reconcile and retire steps read is in the namespace the rows go
    to, so the run refuses before any model call when this document's rows are in another namespace a run may write
    them to (the document's namespace, the named repository's trunk, or, for a document in a branch's code namespace,
    its repository's trunk), or carry another repository's `repo:<id>` tag (any, for a run without `--repo` or with a
    repository that has no remote): going on would leave the document's decisions in two places, or linked to the
    wrong repository. The refusal names a linked repository by `owner/name` when the caller may read it, and a caller
    who cannot read that other namespace is refused. Two cases the run does not see: rows an `--into repo` run wrote
    to a repository other than the one whose code namespace holds the document (any repository, for a document
    outside a code namespace), when a later run drops `--repo` or names another repository, so pass the same `--repo`
    again; and two runs on one document at once with different `--into`, which can both find the other namespace
    empty.
  - **Exit 1** before any model call, besides those refusals, for:
    - a repository that is not configured in the org, or that the caller may not read (one answer for both);
    - a repository with no remote to tag rows with, under `--into source` for a document outside the trunk;
    - `--into` without `--repo`;
    - `--into source` for a document in another repository's code namespace (its rows would link to the named
      repository's code and be read in the other's);
    - the repositories added from the dashboard could not be read.

- **Windows**: the document's live chunks for the current embedding model, grouped by section, at most `--window-chars`
  per call (default `LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS`, 24,000): a document that fits is read whole, so a
  reason the document gives in another section than the choice (a problem statement, a measurement, a list of missing
  cases) is read with it. The default keeps a dense design document's reply, about as many tokens as the window, inside
  the read's output cap (`decision_extract` in `LOOM_LLM_MAX_TOKENS_OVERRIDES`); a reply cut at the cap loses its
  window, and the dropped line says to lower `--window-chars` (the sync reads the setting) or raise the cap. A document
  chunked under another embedding model has no chunks: the command says to run `loom rechunk` first and writes nothing.
  A `--source-uri` with no live document exits 1 and writes nothing. The prompt lists the category and scenario of up to
  `LOOM_MEMORY_DECISION_EXTRACT_KNOWN_MAX` (default 30) decisions the extractor already holds for the document and asks
  the model to reuse them for a decision the text still records, so a re-run reads the same keys; the old outcome is not
  shown, so a changed outcome is read from the text as it is now.
- **Read**: for each decision the model writes its outcome, the reasons the document gives and the options it rejects
  (with the reason given, or none), in its own concise words, and each of these claims cites one to three short quotes
  of the document (`server/memory/domain/decision_extract.py`, `SYSTEM`). A decision the document states with no reason
  is written with none. The current or former approach a new design replaces is a rejected option of the new design;
  a measurement, a recap, an agenda or a benchmark result is no decision. When the reply holds no decision and the
  window's text is more than a caption (80 characters once images and link markup are removed), the window is read
  once more with the same prompt, reported as `read again`. When the document has a title, the window's text opens
  with a `[document: <title>]` line (its first 200 characters).
- **Grounding**: each quote must be in the text the model read: whitespace runs as one space, letter case ignored, and
  up to three label lines read across between two words, as a model copies a sentence a slide or diagram export splits
  (`find_span`). A found quote is kept as the document's own words on one line, its label lines left out, and one
  under 12 characters rests on nothing. A reason or rejected option none of whose quotes is found is left out
  (`reason '...' left out: none of its quotes is in the document text`); a decision none of whose outcome quotes is
  found is dropped (`none of the outcome's quotes is in the document text`). An outcome quote in a section with
  `recap`, `summary` or `background` as a word of a heading at any level of its breadcrumb locates no decision; a
  decision with no other outcome quote is dropped (`the quote is in a recap, summary or background section`), while a
  reason may rest on such a section's text.
- **Support check**: a second call over the same window reads each kept claim with its quotes and supports it only
  when the quotes, read in the context of the document, state or directly imply it; summarising and combining quotes is
  fine, adding a reason, a cause, a number, a name or a rejected option is not. A claim it does not support is left out
  (`... left out: the support check found it not stated`), and a decision whose outcome it does not support is dropped.
  It does not judge whether a decision was decided or only proposed: that is the read's stage. The same call reads each
  of the extractor's stored decisions on the document (up to the known-decision cap) against the text: still stated,
  and which candidate restates it. A stored decision is stated when any window's check states it, and found no longer
  stated only when every window says so; it is then read once more without candidates, and counts as no longer stated
  only when every window's second check agrees. Anything else (a check that fails or comes back unreadable, a reply
  that leaves the decision out) gives no verdict, and a check that is lost also loses its window's candidates.
- **Image-only sections**: a section whose own text has no words (a heading alone or over an image), or holds an image
  and at most 80 characters of other text (a caption), is not sent to the model and gets the line
  `image-only, rationale not extracted`; a heading alone over its subsections is not one. The text above the first
  heading, or a document with no heading, is skipped the same way when it holds an image and at most a caption; it is
  reported as `(untitled)`. An image is a Markdown image (inline, or by reference as `![diagram][fig1]`; a shortcut
  reference such as `![spill flow]` is not read, since it looks like a macro such as `vec![0; n]`), an HTML image or a
  placeholder such as `[image]`, `[Image 3]` or `[image: ...]`. The caption's characters are counted without HTML tags,
  link targets, reference labels, the empty brackets a link around an image leaves, and bare URLs, so a badge wrapped in
  a link adds none; a tag is a lower-case name on one line, so `x < 10k` and `Option<A>` stay text. Image markup inside
  code (`![d](f.png)` or `<img>` in a code span or block) is text. A chunk with no words is not sent either.
- **Duplicates and limits per section**: a candidate under a key, or with an outcome (whitespace and letter case aside),
  an earlier candidate of the run has is dropped, as are a candidate that does not fit `decision.record` and one past
  `--max-per-section` (default 3) for the section its first outcome quote is in.
- **Stage**: the model marks each candidate `decided` or `proposed`; anything but a plain `decided` (a proposal, an RFC,
  a plan, future work, a follow-up, or no answer) is recorded with `stage: "proposed"`, because a proposal served as a
  choice made misleads an agent more than a choice served as a proposal. A stored decision the text still states keeps
  its stage, and the run's note names the stage the document reads as when it differs. A pinned or attested row is
  never re-recorded by a run, so its stage changes only by hand.
- **Write**: through `decision.record` as agent `loom-decisions` (user and session `-`), the reasons joined with
  `; ` as the reasoning (the outcome itself for a decision with none, which the statement reads as no stated reason),
  each rejected option as `<option> — rejected: <reason>` or the option alone, confidence 0.5,
  `source_class="derived"`, `source_resource_id` `knowledge:<namespace>/<source_uri>#<section breadcrumb>`, tags
  `source-kind:design_doc`, `constraint` for a ban or an invariant and `repo:<id>` under `--into source` for a document
  outside the repository's trunk. The row's `actor` is `process:cli:decisions`. Each `decision.record`, and each
  `memory.delete_version` that deletes a row, leaves the audit row the transport writes for that op, with the actor
  `process:cli:decisions`; the tag write, `memory.update_by_id`, declares none, as over HTTP.
- **Files**: each backticked or qualified name and each bare class name the document text holds is looked up with
  `code.symbol` in the document's namespace, or in the repository's trunk with `--repo`; exactly one defining file (a
  class preferred over its constructor) goes into `files`, several or none add nothing. A name only tests or Markdown
  pages define links nothing: a decision governs code, and an edit of a test or a page would show it instead. The dry
  run says `only a test or a page defines it` and lists the name as unresolved (`only tests or pages`) for a human to
  add the code file, or as a setting when it is snake_case and a page documents it; a snake_case name only tests define
  is a helper and is not listed. Which paths count as tests is `governs` in `server/memory/domain/decision_extract.py`:
  common test, mock and end-to-end layouts; one it misses keeps its link. A repository path a cited quote
  writes out (`src/Storages/MergeTreeData.cpp`) is added as well, as `memory.set` detects one. When the code index
  cannot be read for a name, a decision already recorded keeps the files its row has, and a new one is written with the
  files that did resolve. Pull request numbers and setting names are printed as unresolved in a dry run, for a human to
  add; a backticked snake_case name is looked up first and reported as a setting when the index does not define it. A
  name defined in several files is printed as unresolved, for a human to pick the file.
- **Reconcile**: the document says otherwise when the support check finds the key's stored decision no longer
  stated; the model's wording alone is no change. A decision still stated is recorded again only for its row fields,
  in its stored words. A row is frozen against re-extraction when it is pinned or attested in its current form
  (`memory.verify`). A decision the document still states keeps its stored words even when a revision adds a reason
  or a rejected option to it: the check asks whether the text still says what the row says, not whether it says more.
  By the row the key's active record belongs to:
  - **The extractor's own, not pinned or attested, from this document**: still stated with the same `files`,
    `source_resource_id`, `constraint` tag and `repo:` tag: no write. No longer stated: recorded again as the text now
    reads (the record is superseded, history keeps it); still stated with other row fields (a file that resolves once
    the code is indexed, a renamed heading, a first run with `--repo`): recorded again in its stored words.
  - **No verdict** (a stored row past the known-decision cap, one a reply left out or answered with neither true nor
    false, or one whose check was lost): a pinned, attested or acted-on row is left as it is; any other is recorded
    again when the run gives its key, as the text now reads when the words differ, or in its stored words and stage
    when only its files, source or tags changed.
  - **The extractor's own, pinned or attested, citing this document**: no longer stated: no write; the full
    `decision.record` body is printed with the row's agent, user and session, and the row is tagged `source-changed`
    unless it already carries the tag. To accept the body, a maintainer unpins a pinned row with `memory.unpin`
    (`decision.record` on a pinned writer row answers 409 `PINNED_CONFLICT`) and sends it; an attested row takes the
    body, and its attestation then reads stale until `memory.verify` binds it again.
  - **Another writer's row, or a row from another document**: neither revised nor tagged; its body is printed. The
    support check reads only the extractor's own rows.
  - **Its outcome recorded** (`decision.outcome`): never recorded again, since `decision.record` would rebuild it
    without the outcome; one no longer stated is tagged `source-changed`, as a pinned or attested row is.
  - **A restatement**: a candidate the support check names as restating a stored decision it does not find gone is
    not written (`restates <key>, not found gone`) when its key is new or is the key of a stored decision not found
    stated: the stored row and its stage stand, and the row under the candidate's key is retired when found gone and
    keeps its stored words when it has no verdict. This comes before the re-record of a row no longer stated and of
    a row with no verdict.
  - **No record under the key**: of the `LOOM_MEMORY_DECISION_EXTRACT_RESTATEMENT_TOP_K` (default 20) decision keys
    closest to the candidate (cosine only), a pinned or attested one under another key that reads the same at or above
    the namespace's conflict threshold skips the candidate: `already recorded by <agent> under <key>`. The query is the
    candidate's statement, which leaves the category out, so a pinned or attested decision filed under another
    category counts. Otherwise the decision is recorded. The fetch is capped at 500 rows, so a large value or
    `LOOM_MEMORY_SEARCH_PER_KEY_CAP=0` can read fewer keys.
- **The tag write**: `memory.update_by_id` by the row's `memory_id`, tags only, conditional on the value read: a value
  changed since, by `decision.outcome` or a re-record, is left alone and reported as a race. It is a new version by
  the extractor: the row's pin, value and attestation stay (the value's hash does not change, so the attestation still
  binds it), and its actor and age move to it, so the pre-edit line reads `by cli:decisions, just now`. It is a full
  revision, as any `memory.update_by_id` is: the row is embedded again and meets the conflict check, so a decision
  with a close peer is marked `conflicted` (`memory.for_file` still lists it unless `include_conflicted=false`), and a
  stale conflict mark is cleared.
- **Races**: `decision.record` takes no precondition, so a `decision.outcome` that lands between the run's read and
  its re-record is replaced with the rest of the record; the tag write's precondition covers the value, not a
  tags-only change in the gap.
- **Retire**: a `loom-decisions` row, not pinned or attested, citing the document, that is no longer stated (above) is
  deleted with `memory.delete_version`, which also retires its record; a pinned or attested one, or one whose outcome
  `decision.outcome` recorded, is tagged `source-changed` instead. One whose decision this run recorded under another
  key in its place is reported as replaced by that key. A row still stated stays, whether or not the run emitted it
  again, and so does a row with no verdict (one past the known-decision cap, or one a check left out or lost). The
  verdict reads the extractor's own record: a key whose active record another identity wrote is left alone. A
  decision whose statement spans two windows of a long document can read as no longer stated in each.
- **Citing the document**: a row cites it when its `source_resource_id` starts
  `knowledge:<document namespace>/<source_uri>#`, the namespace that holds the document (not where the row is), so a
  heading holding `#` (`C# bindings`) is no obstacle, unless a document `<source_uri>#<part>` it also starts with was
  ever ingested in that namespace: `doc#v2#Intro` cites `doc#v2` once that document exists.
- **Clearing `source-changed`**: when the run emits a candidate under a tagged row's key and the support check finds
  the row stated again, the run drops the tag, pinned or not, as long as the row cites this document. The retire step
  never drops it.
  By hand, a maintainer drops it with `memory.update_by_id` on the row's `memory_id` with `tags` less
  `source-changed`, or re-records the decision (after `memory.unpin`) or deletes it.
- **Refusals and races**: a write the server refuses (a blacklisted decision, a row pinned during the run, a busy key, a
  store timeout) is reported with its code, and one the embedding provider's error stops (`memory.set` and
  `memory.search` embed in-process) with the error's name; a write that finds its row deleted or rewritten since the run
  read it, or a key whose row is deleted between the run's reads, is reported as such, a race the next run settles.
  Either way the run goes on with the next candidate and the retire step.
- **Limits**: `--max-per-section` is at least 1 and `--window-chars` at least 500; a smaller value is refused. A reply
  cut at the model's token limit is reported with the cap it hit (`decision_extract`, 12000, or `decision_support`,
  8000, unless `LOOM_LLM_MAX_TOKENS_OVERRIDES` sets it) and the advice to lower either flag or raise that entry; a
  window's read, or its second read after an empty reply, or its support check, that fails (a provider error, a
  timeout) loses that window only.
- **Model and exit code**: the run refuses before any model call when it would read the document with the fake model,
  whose decisions pass grounding by construction: a keyed provider (`LOOM_LLM_PROVIDER=openai` or `anthropic`)
  with no API key set, or no provider configured at all. `LOOM_LLM_PROVIDER=fake` set explicitly runs on it on purpose.
  Otherwise a window the fake model answers (a fallback model, `LOOM_LLM_FALLBACK_MODEL`, with no key) is dropped as
  unread. A run whose every window's model call failed or came back unreadable, or answered nothing and failed when read
  again, reads nothing and retires nothing. The command exits 1 when it could not run on the document (no document, no
  chunks, no usable model, a refusal under **Repository** above, the extractor's own rows unreadable before any model
  call, no window read), 2 when a write, or a read it needed, was refused or failed (a dry run's reads count too, and
  so does a window or a support check the model could not answer readably while others were read), else 0; a race
  does not change the exit code.

The quotes ground the claims and are not stored: the record keeps the decision in the model's words and cites the
document's section. An extracted row is served as it is; nobody needs to review it. Pinning is optional: to freeze a
row against re-extraction, read its identity with `memory.get` and call `memory.pin` under it, or attest its current
value with `memory.verify`; a later run whose document no longer says it then tags the row `source-changed` instead of
revising or deleting it. Before an edit,
`memory.for_file` marks a pinned extracted row `(pinned)` and one tagged `source-changed` `(source changed)`, as it does
every decision row ([Decisions](#decisions)). Extracted into a repository's trunk code namespace, the rows also reach
`loom_search` from a checkout of the repository, as every decision recorded there does
([unified search](unified-search.md#reading-the-memory-section)); that surface marks no pin state, nor whether its
source changed, so an extracted row reads there as any decision does.

## History policies

A role says who may call what. A history policy adds rules over the caller's own recent history in the agent event
trace (`loom.agent_events`): "only after a read", "no more than 200 writes an hour". A policy can only take permission
away; a call that fails its role check never reaches the policy gate.

A rule is `effect` + `actions` + `condition`:

- `effect`: `forbid` denies when the condition holds; `require` denies when it does not.
- `actions`: op names or globs (`memory.delete`, `memory.*`).
- `condition`: a tree of past-only leaves combined with `all`, `any` and `not`.

| Leaf             | Holds when                                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------------------------ |
| `formerly`       | A matching event happened within the window.                                                                 |
| `count_within`   | The count of matching events compares (`gt`, `gte`, `lt`, `lte`, `eq`) to `value`.                           |
| `not_since`      | An `anchor` event happened within the window and the named event has not happened since the latest anchor.   |
| `breadth_within` | The distinct keys the principal was shown compare to a `value` or a `fraction` of the namespace's live rows. |

Each leaf takes `within` (`30s`, `15m`, `1h`, `7d`; capped by `LOOM_POLICY_MAX_WINDOW_SECONDS`), `operation`, and
`key`: `agent` (default), `session` or `user` (`breadth_within` defaults to `actor`, the verified token subject). The
compiler pins the caller, so a rule never reads another principal's events. `args_equal` compares event arguments to
a literal or to `$args.<field>` of the current call.

```python
await agent.policy.set(
    "read-before-delete",
    effect="require",
    actions=["memory.delete"],
    condition={
        "op": "formerly",
        "within": "1h",
        "operation": "memory.get",
        "args_equal": {"memory_key": "$args.memory_key"},
    },
    description="An agent deletes only what it has just looked at.",
)
verdict = await agent.policy.check(
    operation="memory.delete", agent="cleanup-bot", args={"memory_key": "notes/old"}
)
# verdict: {decision, would_enforce, mode, write_shaped, determining_rules, errors, rules, latency_ms}
```

### Modes

`LOOM_POLICY_MODE` sets the deployment's mode:

| Mode            | Effect                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------- |
| `off` (default) | The gate reads and writes nothing.                                                                |
| `shadow`        | Rules run and each decision is appended to `loom.policy_decisions`; nothing blocks.               |
| `enforce`       | A denying rule answers 403 `POLICY_FORBIDDEN` with the rules and per-leaf values that decided it. |

A rule's own `mode` (`inherit`, `shadow`, `enforce`) overrides the deployment's, except when the deployment is `off`.
Moving a rule to enforce is the owner's step, taken after reading `policy.decisions`.

In `enforce`, a rule that cannot be evaluated denies a write-shaped call (writer role or above) and allows a read,
reporting the error. The gate never applies to `policy.*` or `ops.*`, so an operator can always fix a rule. A change
that widens what agents may do also writes a `policy.widened` audit row.

| Operation          | Role   | Does                                                                   |
| ------------------ | ------ | ---------------------------------------------------------------------- |
| `policy.set`       | admin  | Create or replace a rule; returns `leaf_count` and whether it widened. |
| `policy.delete`    | admin  | Remove a rule.                                                         |
| `policy.list`      | viewer | The namespace's rules and the deployment's mode.                       |
| `policy.check`     | viewer | Dry-run a call against the stored rules or against `rules` you pass.   |
| `policy.decisions` | viewer | Read the decision ledger, filtered by `allow` / `deny`.                |

The dashboard's **Policies** page (admin drawer) manages rules, dry-runs a call and reads the ledger. Settings:
`LOOM_POLICY_MODE`, `LOOM_POLICY_MAX_WINDOW_SECONDS` (default 86,400) and `LOOM_POLICY_CACHE_TTL_SECONDS` (default 30;
other replicas see a rule change within it), in [configuration: auth](../reference/configuration.md#auth).

## Limits

- Claims are exclusive but not transactional with the work. Completing a task and writing its result are two calls;
  make the result write idempotent.
- `decision.chain` stops at 4 hops; `decision.precedents` returns at most 25.
- Policy evaluation is past-only: the current call is not in the trace yet, so a witness is always an earlier event.

## Related

- [Runs](runs.md): the run ledger decisions credit through.
- [Namespaces and sharing](namespaces-and-sharing.md): owner namespaces and grants.
- [Tokens and grants](../guides/tokens-and-grants.md): service-account subjects for `posters`.
- [Headless bots](../guides/headless-bots.md).
- [The dashboard](dashboard.md): the Tasks board and the Policies page.
- [Security architecture](../architecture/security.md): where the policy gate sits.
