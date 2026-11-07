# Audit log

Audit rows, the `audit_verifier` worker and the checkpoint chain are on by default.

Loom writes one row to `loom.audit_log` for each operation that changes state in a way
someone may have to account for, plus some events the server raises itself. The goal is
to make tampering detectable. Each row carries a SHA-256 of its own content, and a worker
seals closed hourly windows into a hash chain. A ClickHouse administrator can still edit
the table; the next verifier pass shows the edit or deletion. This is an engineering
primitive, not a compliance certification.

## What leaves an audit row

| Source                                                                | `event_type`                                                                                                                                                                                                      | `subject` holds                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Any operation declared `audit=True`                                   | the op name (`memory.delete`, `grants.create`)                                                                                                                                                                    | the op's `audit_subject` when it declares one (no credential, or a hash for text an erasure must reach), else its validated input with every non-naming text value hashed, as JSON                                                                                                                                                                                                                           |
| `memory.set` (point writes)                                           | `memory.set`                                                                                                                                                                                                      | key, memory id, `content_sha256`, `source_class`, verdict, `pinned`. Never the value                                                                                                                                                                                                                                                                                                                         |
| `memory.update`, `memory.update_by_id`                                | the op name                                                                                                                                                                                                       | key, memory id, `content_sha256`, `source_class`, version, and the conflict check's verdict before post-write marking (`duplicate` for a version written as one, empty when the revised row is not embedded: a type that is not, or a `high` value). Never the value                                                                                                                                         |
| `memory.bulk_set`                                                     | `memory.bulk_set`                                                                                                                                                                                                 | one hash-only row per write                                                                                                                                                                                                                                                                                                                                                                                  |
| `memory.import`                                                       | `memory.bulk_set` for the facts it writes; `memory.import` for the category links it writes                                                                                                                       | the bulk write's hash-only row; for the links, `count` and `category_links` (key and category name). Never the text                                                                                                                                                                                                                                                                                          |
| A read of a `high` row, or of evidence held as `high`                 | `memory.sensitive_read` (from `memory.get`, `memory.get_by_id`, `memory.bulk_get`, `memory.history`, `memory.evidence`, `namespace.snapshot`, `erasure.export`, and `memory.export_evidence` with `include_high`) | key and memory id (`memory.history` and `erasure.export` add the version); for released evidence, its kind, session, chunk index and linked keys; for a conversation turn `erasure.export` serves, its agent, thread id and message id; for a thread, its agent and thread id                                                                                                                                |
| Loom retiring memories on its own                                     | `memory.auto_supersede`                                                                                                                                                                                           | `reason`, `count` and `retired`: up to 200 retired rows (id, key, agent, user and session, the row and key each retired behind, the judge's label, `judge_verdict`, `judge_confidence`, `judge_model` and `judge_prompt_version` for a conflict-resolver retirement, and `unconfirmed` (`"true"`) when the re-read could not confirm the retirement). Never the value. More rows split across several events |
| `memory.restore` of a superseded row                                  | `memory.unsupersede`                                                                                                                                                                                              | key, memory id, `superseded_by`, the restored `content_sha256` and `version`                                                                                                                                                                                                                                                                                                                                 |
| A `grants.revoke` row taken under another spelling, or one of several | `grants.revoke`                                                                                                                                                                                                   | `namespace`, the stored `subject`, `named` (the subject the call named), and `landed` (`"unknown"`) when the row's insert raised. Beside the op's own row, which holds the input                                                                                                                                                                                                                             |
| The server itself                                                     | `budget.violated`, `ratelimit.exceeded`, `worker.failed`, `audit.tamper_detected`, …                                                                                                                              | what the event is about                                                                                                                                                                                                                                                                                                                                                                                      |

The **Audit** column of the generated [operations reference](../../reference/operations.md)
lists the ops the transport audits (the `audit` flag on each `Operation`); the memory writes in
the table above write their own row and carry no mark there. No read op is declared
`audit=True`; a read of a `high` row writes `memory.sensitive_read` (above). `audit.list` is
`audit=False`, so reading the trail does not add to it.

The transport writes the row after the handler succeeds (`emit_op_audit` in
`server/transport/_common.py`, for HTTP and MCP). The write is **fail-open**: a failed
insert is logged and loses the row; the operation still succeeds.

For an `audit=True` op the subject is its `audit_subject` when it declares one, else the
validated input as `audit.input_subject` shapes it. Fields that name what the call acted
on or who acted (the identity fields, the principals `subject`, `actor`, `grantee`,
`owner_email`, `requester`, `approver` and `decision_maker`, `version`, and names ending
in an id, key, URI, name, org, agent, namespace or `_by` suffix) and fields typed as a
number, a boolean, a date or time, a Literal or an Enum stay as sent; a field whose name
marks a credential (`audit.is_credential` in `server/observability/audit.py`) is hashed
whatever its suffix. A field holding one nested model follows the same rule, and a list of
models is hashed whole. The principals and the `_by` fields kept as sent are personal data
that an erasure does not remove from `loom.audit_log`. Every other value (a memory value, a
decision's reasoning, a reason, a record's fields, the rows of a restored snapshot) is
stored as `{"sha256", "chars"}`, its SHA-256 and length (the hash a memory's
`content_sha256` would carry, so a moved value's matches it), since erasure does not reach
`loom.audit_log`. Three rows a handler writes itself store text in that shape under the
field's name: `memory.cross_promote`'s and `memory.copy_to`'s `rationale`,
`memory.proposal_accept`'s and `memory.proposal_reject`'s `reason` (a decision the
owner's policy makes records the policy's mode and thresholds instead), and
`triple.follows_row_supersession`'s `closed`, whose actor is the writing principal or
`process:loom`. A hash of a short value can be recovered by guessing; the rule keeps long text out of the trail, not
every word. An op whose input carries a credential (a tracker source's password, a
repository's access token) declares an `audit_subject` that leaves it out. Grant
`loom.audit_log` like the data it describes.

## The row

`loom.audit_log` is a `MergeTree` partitioned by month and ordered by
`(org, namespace, event_type, ts)`, with a bloom-filter index on `request_id`.

| Column                                       | Meaning                                                                                                                                                                                           |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ts`                                         | UTC, millisecond precision                                                                                                                                                                        |
| `event_type`                                 | op name or server event                                                                                                                                                                           |
| `org`, `namespace`                           | the tenant the gate authorized                                                                                                                                                                    |
| `actor`                                      | the token's subject (`anonymous` when auth is off); a memory write's own row (`memory.set`, `memory.bulk_set`, `memory.update`, `memory.update_by_id`) names it `kind:subject`, `-` with no token |
| `subject`                                    | JSON text; the caller decides whether to parse it                                                                                                                                                 |
| `request_id`                                 | joins to `loom.agent_events` and the `X-Request-Id` response header                                                                                                                               |
| `row_hash`                                   | SHA-256 over the canonical row                                                                                                                                                                    |
| `build_commit`, `instance`, `instance_label` | which build and process wrote the row                                                                                                                                                             |
| `consumer`                                   | the client the request spoke for                                                                                                                                                                  |
| `token_id`                                   | the credential that made the request (its `jti`, else a token hash prefix)                                                                                                                        |

The hash covers `ts`, `event_type`, `org`, `namespace`, `actor`, `subject` and
`request_id` as sorted-key ASCII JSON (`_canonical` in `server/observability/audit.py`).
The build, instance, consumer and token stamps are outside the hash, so adding a stamp
column does not invalidate old rows.

## Per-row hashes and checkpoints

Several server replicas write concurrently, so a row-to-row hash chain would fork. Each
row carries its own hash instead, and writes need no coordination. A per-row hash catches
a **modified** row but not a deleted one; the checkpoint chain covers deletions (the
design is in [security architecture](../../architecture/security.md)):

1. When a window of `LOOM_AUDIT_CHECKPOINT_WINDOW_SECONDS` (1 h) has been closed for
   `LOOM_AUDIT_CHECKPOINT_GRACE_SECONDS` (10 min), the `audit_verifier` worker seals it in
   `loom.audit_checkpoints`: the row count, a SHA-256 over the sorted row hashes, and a
   `checkpoint_hash` that includes the previous checkpoint's hash.
2. Every pass recounts the last `LOOM_AUDIT_CHECKPOINT_VERIFY_WINDOWS` (24) sealed windows
   and re-checks the links.

One worker seals, so the chain has a single writer and the request path pays nothing. A
deletion shows only after its window is sealed, and only while the window is recounted.

A recount is classified by `classify` in `server/insights/domain/audit_checkpoints.py`:

| Finding        | Means                                                                               | Tampering?              |
| -------------- | ----------------------------------------------------------------------------------- | ----------------------- |
| `deletion`     | fewer rows than sealed                                                              | yes                     |
| `modification` | same count, different digest                                                        | yes                     |
| `chain`        | a checkpoint's own hash does not recompute, or it does not point at its predecessor | yes                     |
| `late_insert`  | more rows than sealed: a backfill or a very late async insert                       | not by itself; reported |

An empty window is sealed too, with the digest of the empty string, so a row inserted
into it later is a mismatch.

## The verifier worker

`audit_verifier` (`server/insights/workers/audit_verifier.py`) runs every
`LOOM_AUDIT_VERIFIER_INTERVAL_SECONDS` (one hour) on the process holding the workers
lease. Each pass:

1. Re-hashes every row from the last hour (10,000 rows a page, at most 500,000; past the
   cap it reports `truncated`).
2. Verifies sealed windows, then seals closed ones, at most
   `LOOM_AUDIT_CHECKPOINT_MAX_SEAL_PER_PASS` (48) a pass. An error here is logged as a
   partial failure and never blocks the per-row check.
3. Writes **one** `audit.tamper_detected` row for breaks not reported before
   (`subject.break_keys`), so a break is reported once, not every hour.

The pass outcome (`rows_checked`, `breaks`, `windows_sealed`, `windows_verified`,
`checkpoint_breaks`, `new_breaks`, `truncated`) is recorded in `loom.worker_status` like
every worker's; see [workers](../../reference/workers.md).

### Checkpoint lag

`loom.audit.checkpoint_lag_windows` counts the one-hour windows between the newest sealed
`window_end` and now. It is read from `loom.audit_checkpoints` directly, so it reports even
when the sealer is down.

| Reading               | Means                                                                            |
| --------------------- | -------------------------------------------------------------------------------- |
| 0 or 1                | healthy: the chain trails by the grace period                                    |
| rising steadily       | the sealer is stalled: the worker is disabled, failing, or not holding the lease |
| falling               | a backfill after a stall; each pass seals at most 48 windows                     |
| 0 with no checkpoints | nothing has ever been sealed (the gauge reads 0 on an empty table)               |

See [telemetry](./telemetry.md#backlogs-and-process-state) for the rest of the catalog.

## Reading the trail

`audit.list` pages the caller's rows newest first with an opaque keyset cursor. A page never ends
inside one write: the rows one request wrote in the same millisecond come on one page, so
a page can hold more than `limit` rows.

| Field        | Meaning                                                              |
| ------------ | -------------------------------------------------------------------- |
| `org`        | required                                                             |
| `namespace`  | one namespace; omit for every namespace the caller can read          |
| `event_type` | exact match                                                          |
| `since`      | rows with `ts >= since`                                              |
| `limit`      | 1–500, default 50                                                    |
| `cursor`     | `next_cursor` from the previous page; pass the filters again with it |
| `fields`     | project the output rows to these fields                              |

The op needs `viewer` and scopes itself: a platform or org admin sees the whole org,
anyone else the namespaces their token or grants reach. A restricted namespace the caller
cannot pass is hidden. Naming a namespace you cannot read returns an empty page, not a 403.

```bash
curl -s "$LOOM_BASE_URL/v1/audit.list" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org": "acme", "namespace": "support", "event_type": "memory.delete", "limit": 100}'
```

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="auditor") as admin:
    page = await admin.audit.list(event_type="memory.delete", limit=100)
    while page.get("next_cursor"):
        page = await admin.audit.list(
            event_type="memory.delete", cursor=page["next_cursor"], limit=100
        )
```

The admin drawer's **Activity** view reads the same op. It hides server events
(`budget.violated`, `ratelimit.exceeded`, `worker.failed`) behind a toggle and shows the
erasure roll-up beside the list.

To follow one request across the ledger and the audit trail:

```sql
SELECT ts, event_type, actor, subject FROM loom.audit_log WHERE request_id = '<id>';
SELECT * FROM loom.agent_events WHERE request_id = '<id>';
```

### Verifying offline

`dev/verify_audit_chain.py` recomputes every row hash in a window with the worker's
`row_matches_hash`. Exit 0 = all pass, 1 = mismatch, 2 = could not verify. A mismatch is
named by partition, `ts`, `request_id` and stored hash, never by actor, subject or
namespace.

```bash
python dev/verify_audit_chain.py --org acme --ts-since <YYYY-MM-DD> --json
```

## Erasure and the audit log

An erasure keeps the audit trail, including its own audit row; the residue census does not
require `loom.audit_log` to be empty. For a `user_id` scope the export matches `subject` by
substring, since the table has no `user_id` column. See
[retention and erasure](../memory/retention-and-erasure.md).

## Settings

| Setting                                   | Default | Effect                                                                  |
| ----------------------------------------- | ------- | ----------------------------------------------------------------------- |
| `LOOM_AUDIT_VERIFIER_ENABLED`             | `true`  | runs the verifier worker                                                |
| `LOOM_AUDIT_VERIFIER_INTERVAL_SECONDS`    | `3600`  | pass cadence                                                            |
| `LOOM_AUDIT_CHECKPOINT_WINDOW_SECONDS`    | `3600`  | window length                                                           |
| `LOOM_AUDIT_CHECKPOINT_GRACE_SECONDS`     | `600`   | how long a window must be closed before it is sealed                    |
| `LOOM_AUDIT_CHECKPOINT_VERIFY_WINDOWS`    | `24`    | sealed windows recounted each pass                                      |
| `LOOM_AUDIT_CHECKPOINT_MAX_SEAL_PER_PASS` | `48`    | cap on windows sealed per pass                                          |
| `LOOM_BUDGET_VIOLATION_AUDIT_INTERVAL_S`  | `60`    | at most one `budget.violated` row per op per interval (after the burst) |
| `LOOM_BUDGET_VIOLATION_AUDIT_BURST`       | `5`     | violations per window that still write their own row                    |

All settings are in the [configuration reference](../../reference/configuration.md).

## Limits and failure modes

- **Fail-open writes.** A failed audit insert loses the row and never fails the operation.
  A crash can also lose rows still queued in the events stream.
- **Detection, not prevention.** Anyone with write access to the ClickHouse table can
  alter it. The verifier shows the change; it cannot undo it.
- **Bounded look-back.** The per-row check reads the last hour; the checkpoint recount
  covers the last 24 sealed windows. An edit older than both is not re-examined by the
  worker. Run `dev/verify_audit_chain.py` over the older partitions.
- **A late insert is not tampering.** A backfill into an old window is reported as
  `late_insert`.
- **No TTL.** Loom deletes no audit row by age.

## Related

- [Telemetry](./telemetry.md): the ledger, metrics and query attribution.
- [Cost and usage](./cost-and-usage.md).
- [Security architecture](../../architecture/security.md).
- [History and provenance](../memory/history-and-provenance.md): the per-memory version
  history, which is a different mechanism.
