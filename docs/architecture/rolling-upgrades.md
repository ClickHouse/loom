# Rolling upgrades: builds N-1 and N side by side

Every Loom process is a full replica behind one URL, and an upgrade replaces them one at
a time. For the length of a rollout, and again during a rollback, the previous build
(N-1) and the new one (N) serve the same clients against the same ClickHouse, on N's
schema. These rules keep that safe. The operator procedure is in
[upgrades](../operations/upgrades.md).

## The rules

1. **Migrations are additive** and run at boot under the migration lock. N-1 keeps
   reading and writing every table it knew (`tests/unit/test_migrations_additive.py`).
   N-1 booting on N's schema logs one WARNING naming the migrations it does not carry
   (`server/boot.py::warn_newer_schema`) and serves.
2. **A version row keeps columns its build does not know.** `loom.memory`,
   `loom.shared`, `loom.memory_embeddings`, `loom.session_summaries` and
   `loom.entity_summaries` are `ReplacingMergeTree(version)`: the newest row replaces
   the whole previous one. Before each insert into them, `ch.insert` fills every stored
   column outside its build's row builder from the newest stored version of the row's
   key (`server/core/rmt_carry.py`), so N-1's touch, revision or tombstone keeps the
   values N wrote to N's new columns. This costs a cached lookup while every build agrees
   with the schema, and one extra keyed read per insert while a newer column exists.
   - Each process checks `loom._migrations` and the carried tables' metadata time
     every 2 s in the background and then re-reads the column lists.
   - A write that took the lists before the `ALTER` would still drop the column. So
     each process acknowledges the lists it carries by in `loom.carry_acks` (a
     fingerprint per carried table), once no write that took older lists is in flight,
     and a booting process waits, after its migrations and before it serves, until every
     other live process acknowledges the schema it now has (`await_acks`, 120 s, or
     `LOOM_CH_TIMEOUT_WRITE_S` + 60 s when that is longer, then the boot fails naming
     them). A process killed without withdrawing can leave a carried INSERT running after its acknowledgement expires, so each carried query
     id names its process, and the boot also waits while `system.processes` on any
     replica shows a carried insert of a process that no longer acknowledges (when it
     cannot read them, while such a process acknowledged within the longest an insert
     can run). A process withdraws at shutdown (acknowledges no table) only once no
     carried write of it is in flight or pinned, and the boot also waits for a
     withdrawn process whose carried insert still runs. N therefore writes a new
     column only once no writer that would drop it is left. The acknowledgement gates the writes: a carried
     write runs only while this process's last published acknowledgement is younger
     than 15 s (half the 30 s a boot counts it for). Otherwise (never enrolled, a failed
     publish, a pause past expiry) the process publishes, re-reads the lists and
     publishes again before writing, and refuses the write (503, retryable) when any
     step fails, as it does when it cannot read the lists. The watcher re-publishes
     every 7.5 s, so a write rarely renews it, and concurrent writes that find it stale
     wait for one renewal. ClickHouse fences each carried batch on arrival: it is sent
     as `INSERT … SELECT … FROM input(…)` whose `WHERE throwIf(…)` refuses it when, as the
     query starts, the table has a stored column outside the lists the rows were built
     against: a request held in transit (a proxy, a retry) past an ALTER lands no row,
     and the write answers 503 and is retried with fresh lists. A query that started
     earlier is bounded by its `max_execution_time` (60 s when the write class has no
     limit); the guard reads the receiving replica's metadata. Each batch also carries a
     start deadline on the server's clock (send time + 15 s, the clock measured from this
     process): one that starts later is refused, so a request still in transit can start
     only until then. A write that ends in an error or a cancellation after a send stays
     in flight, no newer lists acknowledged, until the server's clock passes its deadline
     plus its `max_execution_time` (when no query of it can run or start); once the
     deadline has passed, `KILL QUERY … SYNC` and a count of 0 in `system.processes` on
     every replica release it earlier. A held write whose queries cannot be counted shows
     on `/v1/health` as `carry_write_pinned`. The acknowledgements keep old writers
     from meeting that refusal during a rollout; the guard makes a late one harmless. It
     costs a carried insert about 5 ms at p50. This fails closed: while
     `loom.carry_acks` cannot be written, every carried write (`memory.set` among them)
     answers 503.
   - A new key's row leaves the column out, so ClickHouse computes its `DEFAULT`.
   - Only state columns may be added to these tables. A column derived from the value
     goes stale when N-1 revises the value, so one that must exist records what it was
     computed from (for example the `content_sha256` it describes), and N can tell it
     is stale.
   - A hand-written `INSERT … SELECT` with a column list is not carried. Only operator
     commands (`loom reembed`) write those; run them when one build serves.
3. **New enum or status values:** N ships readers that accept them, and only N+1
   writes them. N-1's Pydantic `Literal` would answer 500 on a value it does not know.
4. **Wire formats** (cursors, session and run ids, worker resume cursors, JSON stored in
   rows): N reads the old and the new format, and only N+1 writes the new one.
5. **Signing keys:** rotate with `--activate-in` of at least twice the slowest keyring
   refresh interval (the command refuses less), so no process signs with a kid an N-1
   replica cannot verify yet.
6. **Workers:** one leader runs them, elected by a ClickHouse lease. Workers stay
   idempotent under one overlapping in-flight statement, since there are no fencing
   tokens. An older build leaves the lease to a newer one only when their schema
   versions differ (each build's last migration number): a code-only release never
   hands over, and the old leader keeps the workers until it stops. Either way the
   order replicas are replaced in does not matter.
7. **Settings:** a removed setting warns at boot rather than refusing to start. A new
   setting's default accepts what N-1 is configured with.
8. **Retiring a column or table:** stop using it in N, drop it in N+1 or later.
9. **No state outside ClickHouse.** Anything a replica must agree on (leases, runs,
   conflict judgements, the similarity calibration, the frontend bundle of every live
   build) is in a table, so a request can land on any replica of either build.
10. **A new revocation key** (such as SCIM's `SCIM_PREFIX`): N-1 does not read
    it, so a token it revokes still works on an N-1 replica. The feature that writes
    it is turned on only once no N-1 replica serves: SCIM provisioning
    (`LOOM_SCIM_TOKEN`) after the whole rollout.
11. **A new access rule** (such as collections, [sharing](sharing.md)): N-1 does not
    know that a `col-` namespace opens only through grants, so an org admin's wildcard
    token reads one on an N-1 replica, and a personal collection is one too. An end-user
    token is refused there (N-1 has no key for its `kid`), but on N its first `@me` write
    creates a personal collection. N-1 also rewrites a document (a metadata edit, a
    delete) without the provenance columns, so a user erasure would miss that version; it
    registers a `col-` name that N refuses (the boot warning lists the first 20); and it
    resumes a user purge without purging (it reads no `purge` column). Collections are
    created (`collection.create`, which N-1 does not serve) and end-user issuers
    registered (`LOOM_JWT_END_USER_ISSUERS_JSON`) only once no N-1 replica serves.
12. **A new side effect of a write** (such as memory webhook events): N-1 does not
    perform it. A memory write served by an N-1 replica leaves no
    `loom.memory_webhook_outbox` row, so it sends no event; N's worker delivers what N's
    replicas wrote. A receiver that must see every change reads back what it holds once
    the rollout ends. An erasure saga run by N-1 has no step for the outbox and its census
    does not count it, so N's events for an erased user stay in the outbox; N's worker
    sends none of them, because it skips an event whose key has no history left in
    `loom.memory_versions`, which every build's erasure deletes. The outbox is read for 3
    days and drops a day's part once its last row is 3 days old, so the rows leave the
    disk within about 4 days. Until then `webhook.deliveries` still lists those events,
    with their `memory_key` and `memory_id`, so a user or namespace erasure that an N-1
    replica completed during the rollout is run again once only N serves; N's
    `memory_webhook_outbox.delete` step removes the rows.

13. **Saved conversation messages:** older builds can compact concurrently saved
    turns and rebuild embeddings without carrying their saved state. Clients use
    `conversation.save_message` only once every serving replica runs the build that
    preserves saved messages. A rollback pauses saved-message writes and compaction
    until every replica supports that state again.

## What checks them

`scripts/gates/mixed_version.py` (`make test-mixed-version`, the `mixed-version` CI job,
once with a Keeper and once without) boots the base build on an empty ClickHouse, then
this build, which applies its migrations, then the base again on the newer schema, and
a third process of this build with a changed dashboard bundle and one no-op migration
more (N+1). It checks:

- each build answers ready after the cross-boot;
- memories, a run begun on one build and completed on the other, and a conflict verdict
  through the other build's version write read back whole across builds;
- a column added as N+1's migration would survives a version write by each build, made
  as soon as every process has acknowledged the column (rule 2);
- a build on a ledger newer than itself logs the WARNING;
- every asset each dashboard page names loads from a replica of the other build
  (rule 9);
- with every process on `LOOM_BACKGROUND_WORKERS=auto`, exactly one process runs the
  workers, and it is N+1: N, which took the lease first, hands it over to the
  higher schema version (rule 6).

It does not check new enum values (rule 3), wire formats (rule 4), signing keys other
than `default` (rule 5), new revocation keys (rule 10), or a process killed
mid-migration. It skips with exit 0 where docker is unreachable; CI passes `--require`,
which fails there instead.
