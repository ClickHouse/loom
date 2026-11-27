# Cost budgets

Namespace and named-user caps use the rolling 24-hour cost ledger in `agent_events`.
A positive namespace policy overrides its corresponding server default; zero inherits,
and zero on both tiers disables that cap. A named user's cap is independent of the
namespace ceiling. Admission refuses paid synthesis when either is reached, and model
routing uses the larger spend fraction. `memory.ask`, including its whole-document
route, `code.ask`, `knowledge.synthesize` and `code.search` query expansion supply the
logical user recorded in the ledger. Namespace-wide workers keep their namespace accounting;
a token subject is never substituted for an unattributed user. The extraction queue
is the exception: a replay's row carries the user the deferred request named, so its
spend counts toward that user's cap and can refuse that user's paid synthesis. These
soft controls do not reserve spend or establish a billing identity.

`cost.budget_status` shares the policy and spend read with the guards. User mode requires
a namespace and reports the user's cap and verdict, plus `namespace_over_cap`. Exact
user spend and its percentage are `null` unless the caller is a verified platform admin
without namespace-role restrictions. This holds even when every event is ordinary;
varying the presence of a number would reveal marked activity. Missing auth grants no
private spend access. Internal accounting, admission and budget pressure include all
spend, including marked requests. Namespace and org totals remain numeric.
Transport admission enforces a token's pinned user. Namespace and org modes retain their
existing read scopes. The [cost guide](../features/observability/cost-and-usage.md#daily-spend-caps)
describes configuration and warning timing.

An optional `budget_alert_inbox` must name an existing inbox owned by the budget's
namespace in the same org. Registration validates it and delivery rechecks ownership.
Point checks and status polls schedule bounded background warnings through the existing
task event stream, with at most 32 pending deliveries per process. Deliveries for one
org run serially within the process so a namespace and user warning do not compete
with each other. There is no discovery worker. The event ID includes org, namespace, inbox, selected user, cap, warning share
and UTC day. A distributed claim and strongly consistent event read serialize the
check and insert; a deduplication token plus readback handles an uncertain retry.
Notification failures cannot change a cap verdict.

User warning payloads carry the selected user, cap and warning share, with
`spent_24h_usd: null`. This applies to the inbox, polling, streams and webhook, even
when an operator's check triggers delivery: the initiator's authority does not establish
the recipient's authority. Namespace warnings retain their namespace spend total.
The user cap verdict and warning threshold crossing reveal bounds on spend; an admin
who changes caps can infer tighter bounds. This is an accepted policy signal alongside
the [marked-spend totals exception](document-sources.md), not full noninterference.
It grants no access to marked work, models or outcomes.

Task updates and watch streams apply the caller's user and session scope to warnings
before pagination. An incomplete scope receives no warnings; ordinary task events stay
shared within the inbox. The registered inbox's webhook receives its namespace stream.

Every delivery and task-event erasure takes the same org-wide warning-writer lease.
Inserts use the existing server-side start deadline and execution bound; the lease TTL
exceeds the working window plus the full send bound. An uncertain insert or cancellation
leaves that lease to expire, so erasure cannot acquire it until every possible send has
finished. Erasure keeps its tagged mutation, polling and resume protocol. Ledger checks
before and after publication invalidate an observation reached by erasure; post-write
cleanup names only that delivery's exact event and sequence. An unreadable ledger
refuses publication.

Warning rows carry the full identity tuple, a process actor and derived source class.
User erasure deletes their task events and the residue census verifies the deletion;
namespace erasure covers both tasks and events. Added columns have defaults for older
writers. Warning production waits for every live replica to support the migration, so
an older erasure implementation cannot miss user-bearing warning rows during a rolling
upgrade. Unknown fleet state defers publication until a later check.

## Activation and rollback

User caps and budget warnings require every serving build to support migration 107.
Keep `LOOM_DEFAULT_DAILY_USER_COST_CAP_USD` and every namespace's
`daily_user_cost_cap_usd` at zero, and `budget_alert_inbox` empty, while any deployment
sharing the stores runs an older build or its version is unknown. Do not rely on user
caps during that interval. Apply the migration and upgrade every serving process before
setting these policies. The warning fleet check defers production on an old or unknown
fleet; it does not remove warnings already stored.

Save the global user-cap default and each namespace's user cap and alert inbox before
rollback. Rollback requires a maintenance interval:

1. Stop all request-serving and background processes that can check budgets or schedule
   warnings, across every deployment sharing the stores. Stop status polling too.
   Turning off background workers or clearing an inbox policy alone does not stop
   deliveries already scheduled with that inbox.
2. Drain scheduled deliveries and settle every in-flight or uncertain write before
   cleanup. Wait for warning-writer leases to be released or expire and for their
   bounded late-start and server execution windows to end. An interrupted drain or an
   empty running-query list does not prove that a delayed send cannot still arrive.
3. Erase only `loom.task_events` rows whose `event` is `budget.warning`. Wait for the
   deletion to complete on every storage replica and verify that no warning rows remain
   on any of them before starting the first older process. An unreadable replica or an
   unsettled write blocks rollback. Namespace or user erasure is not a substitute: it
   also deletes unrelated data.
4. Keep user caps and warnings disabled while older builds serve. After every serving
   process supports migration 107 again, restore the saved global default and reapply
   the namespace user caps and alert inboxes with `namespace.register`. Read the
   namespace policies and check the effective caps before resuming warning production.

This limits activation during mixed-version operation and trades availability for
privacy during rollback. Older task readers do not filter private warnings, and older
user erasure does not delete them. Older namespace registrations also omit the user-cap
and alert-inbox columns; their replacement rows reset these policies to their defaults.
The namespace registry does not carry those columns through older writes. The operator
must therefore preserve and restore policy rather than rely on it surviving rollback.

## Source entry points

- `server/observability/budget.py`: `budget_snapshot` reads policy and rolling spend for the guards and status.
- `server/tasks/app/budget_notifications.py`: `deliver` owns warning claims and erasure fences.
- `server/tasks/store/repo.py`: `events_since` applies warning scope before cursor pagination.
- `server/tasks/app/reads.py` and `server/tasks/app/watch_routes.py`: polling and streaming readers bind that scope.
- `server/erasure/store/steps_ledgers.py`: warning-writer leases protect task-event deletion.

## Invariants

- A named user's allowance never raises the namespace ceiling, and attribution never guesses a token subject.
- A narrowed reader receives no warning outside its user and session scope.
- An uncertain warning write keeps erasure fenced until its possible send has finished.
- Notification failure never changes the cap decision.
