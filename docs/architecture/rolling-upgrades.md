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
   `loom.shared`, `loom.memory_embeddings`, `loom.session_summaries`,
   `loom.entity_summaries` and `loom.documents` are `ReplacingMergeTree(version)`: the
   newest row replaces the whole previous one. Before each insert into them, `ch.insert`
   fills every stored column outside its build's row builder from the newest stored
   version of the row's key (`server/core/rmt_carry.py`), so N-1's touch, revision or
   tombstone keeps the values N wrote to N's new columns. This costs a cached lookup
   while every build agrees with the schema, and one extra keyed read per insert while a
   newer column exists.
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
   A projection failure's name is a plain string that N-1 already tolerates: its repair
   worker leaves a name it does not know active as `unsupported_projection`. So N records
   a triples repair that failed after its insert as `entity_triples_repair`, not
   `entity_triples`, which N-1 would replay by inserting the triples again. A replay of
   an `entity_triples` failure that inserts them and then fails its repair keeps the
   failure: one mutation records that they landed and moves its `entries` and `results`
   to keys N-1 does not read, so N-1 refuses it (no entries) and leaves it active. N
   writes a failure's `abandoned` status because N-1 reads that status as a plain string and
   lists only `active` ones; the `status="abandoned"` list filter works only on N
   replicas. The conflict resolver's owed follow is `triples_follow_owed`, not
   `triples_follow_rows`, for the same reason: N-1's follow does not check that the
   loser is still retired. While N-1 leads, its repair re-reads every failure whose
   name it does not know on every pass (oldest first, each answered
   `unsupported_projection` and left active), so they take places in its batch
   (`LOOM_PROJECTION_REPAIR_MAX_FAILURES_PER_PASS`); past that many, N-1 replays
   nothing else until an N replica leads.
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
9. **Replica-wide state lives in the deployment's stores, never in a process.** Anything a
   replica must agree on (leases, runs, the conflict resolver's queue, the similarity
   calibration, the frontend bundle of every live build) is in a ClickHouse table, so a
   request can land on any replica of either build. Under the Postgres memory authority
   ([storage authority](storage-authority.md)) Postgres holds the memory heads, the
   revision journal, the deliveries, the scope generations and the requests'
   `Idempotency-Key` records, and N and later read and write them there (rule 33); the
   ClickHouse rule holds for everything else. The one exception is the bytes of document
   originals with `LOOM_DOCUMENT_OBJECT_STORE=s3`: they live in an S3-compatible bucket
   that every replica reads the same way, and the catalog row naming each object is in
   ClickHouse. A build that does not read that row's location answers such a download 410
   and cannot erase such an object, so the store is switched on after the rollout, and no
   rollback goes past this build while rows name objects.
10. **A `task.create` key's exclusion holds only among N's replicas.** `task.create` with an
    idempotency key holds a lease on the poster and its key (`_one_create_per_key`) and
    inserts under the key's dedup token (`insert_created_task`); the coverage watch's
    ready-event delivery holds one on the event's task id (`insert_ready_event`). One
    whose lease ran out before its insert can still race the next, so both insert under
    one dedup token (named by the same poster and key, or the event's task id) and
    storage drops the later row. An erased row's token can outlive it and drop a new row
    too: with nothing stored, the row goes in again under a fresh token. The build before
    takes the delivery's lease and writes its token too, so deliveries exclude across
    builds. Creates do not: the build before names the create's lease and token without
    the poster, so neither build's create of a key excludes the other's, and racing they
    can store two tasks. A build that looks a key up without the poster answers another
    poster's task to a create with the same key until it stops serving.
    Each task is whole (its event and its lease row) and claimable: a create of N cut
    off after its task row is finished by its retry, which under the key's lease writes
    the created event the first one left out, and by the lease sweep, which writes its
    lease row after its grace. The created event takes a fresh seq, so cursor readers
    deliver it, unless the task has events since: then it goes just before the first of
    them and is history only. A first create that was only
    stalled past its lease, or cancelled while its insert ran on, can still land its own
    event. Both inserts carry one token, the task's (`insert_event`), so storage keeps
    whichever lands first. Past the dedup window both can be stored: the backfill is
    marked `backfilled` in its patch, and N's readers drop it when the task has an
    earlier created event, so history shows one creation and a cursor delivers at most
    one. A reader that passed the first one's seq before it landed and reads the
    backfill after misses the creation, as it misses any event landing behind it. A retry
    that reads an N-1 create's task between its row and its event logs such a backfill
    too. N-1's readers do not drop it; that ends once N-1 stops serving.
11. **A link N adds is not yet complete.** N links each value to the triples and edges
    it stated (`entity_triple_rows`, `entity_edge_rows`); N-1 links only keys, just after
    it writes the row. So a value with no row link still holds by its key link when that
    link is no older than the value and no row of the key links the fact, and a close N
    runs keeps what an N-1 row states. A key that rows of both builds link to one fact
    reads as N's: there an N-1 row can lose that fact until it is stated again.
12. **A request field N adds is ignored by N-1** until N-1 stops serving: it answers the
    call as if the field were not sent, naming it in `ignored_args`. So `memory.restore`'s
    preconditions `if_superseded_by` and `only_if_superseded` (the dashboard's Undo sends
    them) do not hold on an N-1 replica, which restores a deleted row or one retired
    behind another memory, and the `relations` of `memory.set`, `memory.update` and
    `memory.update_by_id` are dropped there, so that write graphs none of the facts it
    names. `memory.get_by_id`'s `omit_high_value` does not hold there either: N-1 answers
    a `high` row with its value and hash, and audits that read as a sensitive one. N's
    conflict resolver does not read the `memory.unsupersede` record of an Undo
    made on an N-1 replica, or before the upgrade: it carries neither the restored hash
    nor the restore version. The restored row then reads as a statement made at the Undo,
    newer than the memory it was retired behind, so a confident update verdict can retire
    that memory although the person kept both. Pinning it prevents that.
13. **A name N spells differently is a different node.** N keeps combining marks and
    NFKC-normalises an entity's name (`दिल्ली`, `İstanbul` as `istanbul`); N-1 split them
    into fragments (`द ल ल`, `i stanbul`). ASCII names spell the same in both, and so do
    Latin ones but for compatibility characters, which N folds and N-1 keeps (the
    ligatures `ﬁ` and `ĳ`, full-width letters, superscripts, fractions, `ª`, `º`, `ſ`).
    For the other scripts the two builds write separate nodes until N-1 stops serving,
    and a node N-1 wrote keeps its fragment name until a write states that entity again.
14. **An erasure N-1 runs misses the tables N adds.** N-1's saga and its census do not
    know `entity_triple_rows` and `entity_edge_rows`, so an erasure it runs during the
    rollout leaves their rows in its scope and still records the saga `completed`. Once
    N-1 stops serving, re-verify the erasures completed since the rollout began:
    `loom erasure-re-verify --completed-since <rollout start> --resume` deletes, in each
    one's scope, the rows of those two tables whose memory has no live row and no row
    written since the saga's census (orphan row links). It runs no saga step again and
    touches nothing else: data written in the scope after the erasure finished, its
    links included, stays, tombstoned by `memory.delete` or not.
15. **A marker N writes in a column N-1 reads is not one N-1 reads as its own.** The
    conflict resolver's queue lives on `memory_embeddings.conflict_checked_at`, where an
    N-1 with the per-write judge on reads the epoch itself as a judgement it owes and
    re-runs it. N writes `00:00:01` (owed) and `00:00:02` (judged), which N-1's
    statements read as settled (they match the epoch exactly); N treats N-1's epoch
    marks as owed once N-1's own settle had a minute. N-1's Python is wider: its
    `is_mark` takes any time before 2000 as its pending mark, so where N-1 runs its
    per-write judge (`LOOM_MEMORY_CONFLICT_JUDGE`, off by default) its settle of a row it
    wrote can stamp its settle time over N's mark on that row; N then reads an older
    build's verdict, owed again only while the row is `conflicted`, and that one row may
    go unjudged. While N-1 holds the worker lease (a code-only release keeps it until it
    stops) N's marks wait, and the first N leader judges them. A pin is the other way
    round: N's `memory.pin` holds the key's write lock, N-1's does not, so during the
    rollout an N-1 pin racing an N retirement can leave the pinned row's vector
    superseded until the row's next write (or `loom reconcile-status`).
16. **A triple row N-1 writes arrives at its whole second.** N-1 does not set
    `entity_triples.seen_at` (migration 067), so its rows take the column's default,
    their `last_seen` second (`.000000`). Two values of a single-valued relation
    starting the same day and arriving in the same second, one from each build, then
    order the N-1 row first, whichever arrived later; the next write of either fact
    orders them again.
17. **N-1 does not know N's retirement intents.** A user erasure N-1 runs deletes no
    projection failure (N-1 erases them by namespace only), so a point-write retirement
    intent naming that user in `retired[]` stays until N erases the user again, or the
    intent is resolved and its TTL ends it. A projection repair on N-1 leaves every projection kind N adds
    (rule 3), `triples_follow_owed`, `entity_edges_drop_undeclared`,
    `entity_edges_restore` and `sensitivity_facts_owed` among them, active
    as `unsupported_projection`; N's repair replays them once N-1 stops serving.
18. **A new revocation key** (such as SCIM's `SCIM_PREFIX`): N-1 does not read
    it, so a token it revokes still works on an N-1 replica. The feature that writes
    it is turned on only once no N-1 replica serves: SCIM provisioning
    (`LOOM_SCIM_TOKEN`) after the whole rollout.
19. **A new access rule** (such as collections, [sharing](sharing.md)): N-1 does not
    know that a `col-` namespace opens only through grants, so an org admin's wildcard
    token reads one on an N-1 replica, and a personal collection is one too. An end-user
    token is refused there (N-1 has no key for its `kid`), but on N its first `@me` write
    creates a personal collection. N-1 also rewrites a document (a metadata edit, a
    delete) without the provenance columns, so a user erasure would miss that version; it
    registers a `col-` name that N refuses (the boot warning lists the first 20); and it
    resumes a user purge without purging (it reads no `purge` column). Collections are
    created (`collection.create`, which N-1 does not serve) and end-user issuers
    registered (`LOOM_JWT_END_USER_ISSUERS_JSON`) only once no N-1 replica serves.

20. **Saved conversation messages:** older builds can compact concurrently saved
    turns and rebuild embeddings without carrying their saved state. Clients use
    `conversation.save_message` only once every serving replica runs the build that
    preserves saved messages. A rollback pauses saved-message writes and compaction
    until every replica supports that state again.
21. **An extraction policy** (`extraction_policy.set`): N-1 does not read
    `loom.extraction_policies` and extracts without it. Set a policy only once no N-1
    replica serves. A rollback leaves the rows in place but no replica applies them
    until the build that reads them is back.
22. **A new side effect of a write** (such as memory webhook events): N-1 does not
    perform it. A memory write served by an N-1 replica leaves no
    `loom.memory_webhook_outbox` row, so it sends no event, and an erasure saga run by
    N-1 has no step for the outbox and its census does not count it. Webhooks are
    registered (`webhook.register`) only once no N-1 replica serves, so no outbox row
    exists while an N-1 replica can complete an erasure. A rollback to N-1 after webhooks
    exist leaves the outbox rows written before it: N-1 never reads or erases them, and
    they leave the disk within about 4 days (the outbox is read for 3 days and drops a
    day's part once its last row is 3 days old). Rolling forward again, N's worker sends
    none of an erased user's events, because it skips an event whose key has no history
    left in `loom.memory_versions`, which every build's erasure deletes.
23. **Assistant-turn learnings** (`memory_extract_agent_learnings`): N-1 does not
    erase `loom.learnings_supporters`, where a user's quotes behind a bot's rule live,
    and on `learnings.review` it promotes an `assistant_turn` candidate as the
    approver's own memory. Turn the setting on, and review `assistant_turn` candidates,
    only once no N-1 replica serves. Before a rollback, turn it off and delete the rows
    of `loom.learnings_supporters` and the `assistant_turn` rows of
    `loom.learnings_queue`
    ([extraction](../features/memory/extraction.md#rules-the-assistant-states-about-itself)).
    N-1 has no per-agent filter on knowledge reads, so after a rollback it serves a
    promoted bot rule to every agent in the namespace. To keep a rule to its bot,
    delete the documents tagged `source-kind:assistant_turn` with `knowledge.delete`
    before the rollback; otherwise accept the wider serving.
24. **A namespace guide** (`namespace.guide_set`): N-1 does not serve the guide ops,
    shows no guide in `namespace.browse`, `namespace.get` or the session-start pack,
    and a namespace erasure it runs has no step for `loom.namespace_guides`. So
    `namespace.guide_set` answers 503 `OVERLOADED` while any live replica's schema
    version (`loom.instances`) is below 72, or no heartbeat has been read: no guide is
    written while an N-1 replica could erase its namespace. A rollback to N-1 after
    guides exist leaves them in place, and a namespace erasure N-1 runs then leaves
    them too; run that erasure again once N serves. `tests/unit/test_namespace_guide.py`
    checks the refusal; the mixed-version gate does not. The gate counts the
    deployment's processes (`loom.instances` of its kind): an older process outside it
    on the same ClickHouse, such as a branch server, is not counted.

25. **Platform-admin bots** (`loom.machine_identities.platform_admin`, migration
    `076_machine_platform_admin.sql`): the column's `UInt8 DEFAULT 0` reads 0 for every
    bot made before it. N-1 refuses an admin bot's exchange but accepts a flat admin JWT
    issued by N, and its SSO account routes lack the human-only check. Both OAuth admin
    exchanges and `loom mint-token --platform-admin` therefore refuse until a fresh,
    nonempty fleet view shows every replica at migration 76 or later, including processes
    outside the deployment on the same ClickHouse. Neither path uses the kind-filtered
    replica cache. OAuth answers 503
    `temporarily_unavailable`; the CLI refreshes the fleet before minting and prints no
    token when it is unreadable or contains an older replica. Register and use such bots
    only after the rollout. Before starting any N-1 replica for a rollback, disable admin
    bots and revoke every standalone CLI-minted admin subject or token. N-1 writes the
    column back to 0 when it disables or replaces a bot.

26. **A bot's longest token** (`loom.machine_identities.max_access_token_s`, migration
    `077_machine_access_token_ttl.sql`): the column's `UInt32 DEFAULT 3600` reads an hour
    for every bot made before it. N-1 ignores a request's `expires_in` and issues an hour,
    its `/v1/admin/bots.create` refuses the field (422), and when it saves a bot (a disable,
    a replace, a namespace release that drops one of its roles) it writes 3600 back. This
    can lengthen a token requested or capped below an hour, or shorten one allowed longer.
    So `create_bot` (the admin route and `loom bot create`) refuses a maximum other than
    3600 until a fresh fleet view shows every replica, in every deployment, at migration 77
    or later; an unread or empty view (no server registered yet) refuses too. A request's
    `expires_in` needs no gate: N-1 answers it with an hour. After a rollback, reapply each
    maximum once N serves again (`loom bot create --replace` with the bot's key or trust and
    roles as registered, which also ends its tokens). The mixed-version gate does not check
    this rule.

27. **An erasure saga's audit actor** (`loom.erasure_requests.actor`, migration
    `081_erasure_request_actor.sql`): N-1 inserts ledger rows without the column, so a
    saga it checkpoints reads `''` afterwards, and a later resume by N names the resuming
    process (`erasure-recovery-worker` or the CLI command) on its failed and completed
    rows instead of the principal that asked. N-1 also still writes the body's
    `requester` as the `actor` of the `erasure.*` rows of a request or export it serves,
    or of a saga it resumes. No gate holds the rollout: the cost is the actor named on
    those audit rows, nothing is lost or refused, and it ends once no N-1 replica serves.
    The mixed-version gate does not check this rule.
28. **A new document-source kind** (`recording` or `notion` in
    `loom.knowledge_sources.kind`): N-1 refuses to add one (`BAD_KIND`) and lists and
    inspects one like any source. Its `check` and `rotate` of one fail with a server
    error, since it has no probe for the kind. An N-1 sync worker that takes such a
    source's turn reads it as a shared folder: the credential is not a service account
    key, so the turn ends `credential_invalid` before any request and retires nothing,
    and the next turn by N writes the real status. An N-1 mining step keys a document by
    its revision alone, while N keys a `notion` document by its revision and text hash,
    so a hand-off between the builds mines a `notion` document again. No gate holds the
    rollout: the cost is that status, shown until N's turn, and those extra mining
    calls, and it ends once no N-1 replica serves.
29. **Typed datasets** (`loom.datasets`, `loom.dataset_rows`, migration
    `083_knowledge_datasets.sql`): N-1 ingests a file without its datasets and serves no
    dataset op. A dataset is served only while the source's live document row has the
    version it was published with, so a write or a delete N-1 makes of the source stops
    serving N's datasets as N's own would; N-1's delete or ingest of the source leaves
    their rows, which the source's next ingest or delete by N removes (a delete of a
    source already deleted included). A metadata edit N-1 makes of an ingested source
    carries nothing, unlike N's: its datasets stop being served, and the next ingest
    infers their types again, an earlier approval dropped; ingest the file again after
    the rollout to serve them, and approve again. An erasure N-1 runs does not know the
    two tables, so the rows N wrote in its scope stay and the saga still records
    `completed`: once N-1 stops serving, request the erasures completed since the
    rollout began again, which N's saga runs through every table. No gate holds the
    rollout.
30. **Extracted decisions in the model's words** (`loom decisions extract`, the
    document-source sync's mining step): N records a decision's reasons in the model's
    words and retires a row only when its support check finds it gone twice; N-1
    retires the extractor's row whose reasoning is not a span of the document. So an
    N-1 run on a document N mined (an operator's N-1 command, an N-1 replica's sync
    turn on a changed document, every changed document after a rollback) deletes the
    extractor's unpinned rows N wrote and tags its pinned, attested or acted-on ones
    `source-changed`, and writes its own. The rows are the extractor's own and the next
    run by N writes them again from the document; a `source-changed` tag stays until a
    run by N emits the key and finds it stated. No gate holds the rollout: the cost is
    that churn while N-1 serves, and after a rollback until the document is mined by N.
31. **A new shared-folder file kind** (`docx` in a `gdrive` source's `include`): N
    accepts the kind and writes it only when a caller names it; a config without
    `include`, and the dashboard form's default, take the four kinds N-1 knows. N-1 has
    no `docx` in its file-kind table, so it refuses to add a config that names it
    (`BAD_CONFIG`), its `check` and `rotate` of such a source answer a server error, and
    its sync worker fails such a source's turn before any request: the source's state
    shows status `failed`, error `KeyError`, nothing is retired, and the next turn by N
    syncs it. No gate holds the rollout: the cost falls only on sources whose config
    names `docx`, and it ends once no N-1 replica serves. After a rollback it stays for
    those sources, every file kind of them unsynced, until each is removed; a second
    source that names only `docx` beside one that names the other kinds (the way
    [document sources](../features/knowledge.md#document-sources) add a kind) confines
    that to the `.docx` files. N+1 may take `docx` by default. This departs from rule 3
    the way rule 28 does for a new source kind: holding `add` back a release would hold
    back every folder of `.docx` files, while the cost falls on the sources a maintainer
    opts in, ends with the rollout, and loses nothing. The mixed-version gate does not
    check this rule.
32. **A native document's pictures out of its export** (the `gdrive` sync's `docs`
    conversion): N drops each pasted picture's base64 reference definition from a native
    document's Markdown export; N-1 stores the export as it comes. A document N-1 writes
    is found by N on its next full listing, whose stored-revision read checks each
    stored text for such a definition, and converted again at the same revision. N
    writes every native document with a `text_sha256` in its metadata, so the mining
    step of either build, which keys a document carrying one by its revision and that
    hash, mines the converted text once; a document N-1 writes carries none and is keyed
    by its revision alone, which is new whenever N-1 writes; N-1 rewrites the document
    only when its file changes. No gate holds the rollout: the cost is the base64 a
    document N-1 writes holds until N's next full listing, and the check, which reads
    each document's stored text on every full listing, stays while a build that stores
    raw exports may serve. The mixed-version gate does not check this rule.
33. **The memory authority** (`loom.storage_authority`, migration
    `085_storage_authority.sql`; `schemas/pg_migrations/`): N records the deployment's
    authority at the first boot that runs its migrations (`LOOM_BOOT_MIGRATE`) and refuses
    to serve against another. A build before N does not read the record and writes
    `loom.memory` directly, so once a Postgres deployment is recorded every replica must
    run N or later, and it never rolls back past N. Postgres migrations follow rule 1:
    additive, applied at boot under an advisory lock, and a build at or after N serves on a
    later build's Postgres schema with a warning naming the files it does not carry ([Postgres
    migrations](../development/migrations.md#postgres-migrations)). The mixed-version gate
    does not check this rule.
34. **A user purge deletes the memory rows last** (`memory.retire`): N-1 has no
    `memory.retire`, and its `loom erasure-re-verify --resume` runs a user saga as a
    tombstone, so a user saga N started and N-1 resumes can keep the answers that quoted
    a summary, and a purge can end with the user's rows or the summaries built from them
    only tombstoned; resume user sagas N started from N. A saga's mutation tag names its
    step on N, so a step resumed across builds submits its mutation again; the
    statements are idempotent. No gate holds the rollout, and the mixed-version gate
    does not check this rule.
35. **One token mint** (`service_accounts.create`, `caller_home.minted_home`,
    `token_saves_to`, `me_service_accounts.dev_caller`, `admin_routes.admin_token_mint`).
    - _Where a token saves._ N sends a delegated token's unaddressed calls to the
      namespace its claim names before `*` when it may write there; N-1 sends them to the
      owner's saved pick, as for a bare `*` token. A token of that shape (one from
      `service_accounts.create` with a namespace before `*`, or an `admin/token.mint`
      token whose `namespaces` start with one) writes to either namespace while both
      builds serve, depending on the replica; both are namespaces it may write, and the
      split ends with the rollout. After a rollback such a token follows the pick for
      the rest of its life.
    - _Where a list token saves_ (`saves_to` claim and `loom.service_accounts.saves_to`,
      migration `093_service_account_saves_to.sql`). N records the list token's target in
      the token and its row, and sends its unaddressed calls there, then down its own list,
      never to the owner's saved pick. N-1 ignores the claim: on an N-1 replica the token
      follows the saved pick, as every list token did before, until the rollout ends. A
      list mint N-1 serves drops `saves_to` (both mint routes ignore an unknown field) and
      mints a token without the claim, which follows the saved pick for its life;
      `service_accounts.create` answers where it saves under that rule, and
      `admin/token.mint` answers no `saves_to`. A `*` token's home stays the name before
      `*`, and one that may no longer write there still falls back through the saved
      pick. On N a delegated token, with the claim or without, never saves to a collection
      unasked (one it may only read can still be its default for reads,
      `caller_home._pick_default`): a list token whose only writable entry is a collection
      saves there by default on N-1 and nowhere by default on N, so its calls name where
      they save. N-1 revoking a row writes `saves_to` empty: a revoked token saves nowhere.
    - _Mints refused by the other build, before anything is saved._ N-1 refuses `*` in a
      list, `namespaces: "*"` or none at all (`create_token()`, `loom token create`
      without `--namespace`) and a mint without a name (the dashboard's form with its
      Name left empty, `loom token create` without `--name`). N has no
      `/v1/me/setup/mcp_config`, which N-1's dashboard calls. N refuses `saves_to` beside
      `*` and a collection before `*`, which N-1 mints (dropping `saves_to`). A retry once
      the rollout ends works.
    - _A mint N-1 accepts._ A named list mint is saved by N-1, which answers without
      `saves_to`, `mcp_url`, `reads_everything`, `roles` and `clients`; N's dashboard
      fills those from the page (its own `/mcp`, the claim) and shows the token. A list
      token N-1 mints gets no bound tracker record. N-1's dashboard posting a list mint to
      N sends no `saves_to`, so N records the first writable memory entry, kept for the
      token's life; N's answer names it, and the created dialog shows it.
    - _The list._ N-1 matches a token's maker by the exact subject, so a person signed in
      under another spelling of theirs neither sees nor revokes there a token made under
      the first, and N-1's dashboard lists a `*` token N minted by the names in its claim
      (it reads every namespace all the same).
    - _Who owns an auth-off token._ With auth off and no bearer, N saves a token with
      the single user `you` as its owner and N-1 with `dev`. Each build's Tokens page shows
      the tokens of its own owner (and, as for any owner, those on a namespace it
      administers); every token stays valid. On N, a token N-1 saved is listed and revoked
      by naming its owner: `subject=dev` and the token's `org` (`default` for one made on
      the Tokens page) on `GET /v1/me/service_accounts` and
      `POST /v1/me/service_accounts.revoke`.
36. **The memory a cached `code.ask` answer quoted** (`loom.code_answers.memory_namespace`
    and `memory_keys`, migration `086_code_answer_memory_scope.sql`): N-1 writes an
    answer without them, and a tombstone N-1 writes of an entry N stamped (a stale entry,
    a negative `code.outcome`) drops them, so its text stays where no erasure finds it.
    N-1's saga, census and `memory.redact` do not know the table, so an erasure or redact
    N-1 runs leaves the answers N stamped in its scope and still records `completed`. A
    saga N-1 left `in_progress` runs `code_answers.delete` when N resumes it, since the
    step is not in its `steps_completed`; one N-1 completed is not run again, and
    `loom erasure-re-verify` does not reach this table. So an erasure N runs before the
    cache is cleared can leave text in the cache. N also changes the answer policy in
    the cache key, and each build reads an entry of another prompt version as dead:
    it serves it to nobody and tombstones it on sight. While both serve, a question
    asked of each in turn is synthesized again at every switch, and N-1's tombstones of
    N's entries, which carry no stamp, are the main way the stamp is lost. Where a
    memory namespace is bound, clear the cache once N serves alone
    ([upgrade procedure](../operations/upgrades.md#upgrade-procedure), step 9); after a
    rollback, clear it once N-1 serves alone and again once N serves again. No gate holds
    the rollout.
37. **What a summary of the user's memories fed** (its history, chunks, entity vectors,
    graph rows and shared copies; [erasure saga](memory-architecture.md#erasure-saga)):
    N-1's user saga and census reach only the user's own, so a user erasure N-1 runs
    leaves the summary's rows and records `completed`. Outside a purge, a saga N-1 left
    in progress that N resumes fails N's census when N-1 ran those rows' steps, and the
    recovery worker's next resume runs them again; the shared copies, which the census
    does not count, stay when N-1 passed `memory.retire`. In a purge, N's
    `memory.retire` runs them again, except for the search rows of shared copies of the
    user's own memories when N-1 ran its `shared.tombstone`, which deleted the rows they
    are found by.
    After a purge that passed `memory.retire`, including one N-1 completed, the user's
    keys are gone and nothing finds the summaries' rows.
    Every build before N leaves them, and the ledger does not name the build that ran a
    saga: once N serves alone, request again each user erasure without `purge` that was
    requested before then (the `user_id` rows of `loom.erasure_requests`), and after a
    rollback, those requested since the rollback once N serves again
    ([upgrade procedure](../operations/upgrades.md#upgrade-procedure), step 10, which
    names what the new saga also erases); no gate holds the rollout.
38. **Knowledge document heads** (`loom.knowledge_heads`, Postgres migration
    `002_knowledge_documents.sql`): under the Postgres authority N commits each document
    write as its source's head; N-1 writes `loom.documents` alone. N reads both and
    serves the newer version, and a commit over a head older than the row its writer
    read replaces it, so N serves N-1's writes and a rollback loses none. Two writes of
    one source are refused one against the other only when both run N. N-1's erasure has
    no `pg_knowledge.delete` step and leaves the scope's heads. Its tombstone stamps the
    rows at their version, and at a tie N serves ClickHouse's copy, so the document
    reads as deleted; after a purge the first N read finds no ClickHouse row at the
    head's version and nothing owed, and deletes the head instead of serving it. A
    delivery still owed or parked when N-1 erases its scope stays, and N's delivery
    worker publishes it again; an erased document's text stays in its head. Once N-1
    stops serving, run again with N every erasure that completed while an N-1 replica
    served since N first served, a rollback's included, before an operator retries a
    parked delivery ([upgrade procedure](../operations/upgrades.md#upgrade-procedure),
    step 11). N refuses to boot with `LOOM_RETENTION_DOCUMENTS_DAYS` set under this
    authority (unset it first, step 2) and puts no namespace retention TTL on
    `loom.documents` or `loom.chunks`; while an N-1 replica leads the
    `retention_reconcile` pass it applies a namespace's `retention_days` to both again
    and N's next pass removes it, so a document whose rows age out in that window is
    gone from ClickHouse, and the first N read of it drops its head as a purged one.
    N-1's originals sweep and user erasure read `loom.documents` alone, so while N-1
    runs them they may delete an original only an unpublished N commit names (any owed
    or parked one for a user erasure; for the sweep, one whose original is older than
    `LOOM_DOCUMENT_BLOB_MIN_AGE_SECONDS`): that document's download then answers 410
    once its row lands, until the file is uploaded again. N-1's restore does not wait for
    a delivery still owed, or for one the worker settled whose replaced request may still
    be sending: one that restores a source whose N tombstone landed in ClickHouse before
    its delivery finished (an insert that timed out and landed, a failed completion) has
    its chunks retired again when N's worker publishes that delivery or the replaced
    request's retirement lands, and the document serves no search hits until it is
    written again. No gate holds the rollout, and the mixed-version gate does not check
    this rule.
39. **The chunks a knowledge head serves** (`chunks_revision`, `chunks_version` on
    `loom.knowledge_heads`, Postgres migration `004_knowledge_chunk_heads.sql`): under
    the Postgres authority N's search, outline, synthesis and `memory.stack` knowledge
    reads serve only a head's chunks (a source with no head serves what ClickHouse
    holds) ([knowledge search](storage-authority.md#knowledge-search)); N-1 serves every
    chunk ClickHouse reads as live, as before, so while it serves, a search it answers
    can show an older version still being retired or a write not committed. N-1's
    commits leave both columns as they were, so after one that numbers a new revision
    `chunks_revision` is no longer the head's and N judges that source's chunks by
    revision alone, which also serves a refused writer's chunks of that revision until
    the source's next N write that inserts chunks (a delete, a metadata edit or a
    restore keeps the columns as they are). A head N-1 created holds none. N-1's
    `knowledge.add` and `bulk_add` again keep the earlier version's chunks, which N does
    not serve and retires at the source's next N write that inserts chunks. No gate
    holds the rollout, and the mixed-version gate does not check this rule.
40. **Knowledge ingestion jobs** (`loom.knowledge_jobs`, Postgres migration
    `005_knowledge_jobs.sql`): under the Postgres authority N keeps each job in Postgres
    and projects each committed version into `loom.knowledge_ingestions`, best effort,
    so N-1 may read a job a version behind until N's next save, or until N's worker next
    takes it, and may run again a job N ended whose last projection did not land (a
    published one N-1 finds published); N-1's originals sweep and user erasure read open
    jobs from ClickHouse alone, so while N-1 runs them they may delete the original of
    an N job whose projection has not landed and whose upload has expired, and that job
    then fails `original_gone`; N-1 keeps its jobs in ClickHouse alone. N's status and
    cancel read the newer version of the two stores, and its worker's queue interleaves
    its own due jobs with ClickHouse's, the two leading in turn from one pass to the
    next. A job N-1 queued runs in ClickHouse alone, as N-1 runs it; a run of one
    Postgres holds first takes the version N-1 saved when it is the newer, and waits
    while an earlier open job of its source is held in Postgres or only in ClickHouse,
    so a source's jobs still run one at a time, oldest first. N thus runs, reports and
    cancels the jobs N-1 queued or ran. A cancel N accepts is also written to
    `loom.knowledge_ingest_cancels`, which N-1 reads, and N reads the cancels N-1 writes
    there. The deployment-wide job lease alone keeps a run of each build from
    overlapping the other's: N-1 does not raise a job's `lease_token`, so the token does
    not fence an N run whose lease lapsed against an N-1 taker. N-1's erasure has no
    step for `loom.knowledge_jobs` and leaves the scope's jobs there, and N's worker
    projects an open one back into `loom.knowledge_ingestions` and runs it; once N-1
    stops serving, run such an erasure again with N, as for rule 38 ([upgrade
    procedure](../operations/upgrades.md#upgrade-procedure), step 12). N breaks a tie
    between two jobs of a source created in one millisecond by the id's text, N-1 by its
    UUID, so the two builds may each run one of such a pair at once. No gate holds the
    rollout, and the mixed-version gate does not check this rule.
41. **The erasure receipt** (`loom.erasure_requests.census`, migration
    `091_erasure_census.sql`): N writes every count of a complete census with the verify
    columns of a saga's terminal and failed rows, and the recovery worker's abandonment
    checkpoint carries it forward; a resume writes in-progress rows without it until its
    own census lands. N-1 neither writes nor reads it, so a row N-1 checkpointed reads `''`,
    which N's `erasure.status` answers as no receipt (`census: null`). N also writes the
    purge a saga applies to the `purge` column (false for a single namespace), which N-1
    reads as before, since N-1 never purged a single namespace either. No gate holds the
    rollout, and the mixed-version gate does not check this rule.
42. **The knowledge dataset catalog** (`loom.knowledge_datasets`, Postgres migration
    `007_knowledge_datasets.sql`): under the Postgres authority N commits each catalog
    row there and projects it into `loom.datasets`, best effort, so N-1 may read a
    catalog row a version behind until N's next write of it; N-1 writes `loom.datasets`
    alone. Every read of the catalog by N (`datasets_repo.at_version`:
    `knowledge.dataset_get`, `knowledge.query`, an approval's reads, a publish's and a
    metadata edit's carry, and a publish's check that no approval landed) takes
    Postgres's rows and those only ClickHouse holds, the newer version of a dataset
    winning, so N serves and queries what N-1 published or approved. N's approval
    commits in the transaction that holds the source's head, over the version of the
    catalog row it read: Postgres's, or a newer one N-1 wrote to ClickHouse, which the
    approval then commits to Postgres. N-1's approval writes ClickHouse alone, and N
    reads it as the newer version once the replica it reads has caught up: until then N
    serves Postgres's older row, whose replaced snapshot N-1 may already have deleted,
    so the dataset reads empty, and an approval by N in that window outranks N-1's. Both
    builds take the source's claim for an approval and a publish, so an approval and a
    publish of one source never overlap, whichever builds run them. N-1's deletes
    (`knowledge.delete`, a later publish's retirement) and its erasure remove
    `loom.datasets` rows alone and leave N's catalog rows in Postgres until that source
    is next published or deleted by N: ask such a delete again with N, which removes
    them; once N-1 stops serving, run such an erasure again with N ([upgrade
    procedure](../operations/upgrades.md#upgrade-procedure), step 12). No gate holds the
    rollout, and the mixed-version gate does not check this rule.
43. **A revision's delivery time** (`delivered_at` on `loom.memory_revisions`, Postgres
    migration `010_revision_delivered_at.sql`): under the Postgres authority N stamps a
    revision it delivers more than 15 minutes after its commit, and the payload clearing
    pass takes an unstamped one as delivered within 15 minutes of `committed_at`
    ([payload clearing](storage-authority.md#payload-clearing)). N-1 completes a late
    delivery without the stamp, so a revision it delivered late counts as delivered
    earlier than it was, by up to that delivery's lateness, and stays so. Turn
    `LOOM_PG_PAYLOAD_CLEAR_ENABLED` on no sooner than one clearing horizon after N
    serves alone: every revision N-1 delivered is then past the horizon from its real
    delivery. A rollback to N-1 starts that wait again: turn the switch off before it,
    and on again no sooner than one clearing horizon after N serves alone after the roll
    forward. The switch is off by default, and N-1 runs no clearing pass. N-1's delivery
    cannot send a revision whose payload was cleared. Once a clearing pass has run,
    `loom reconcile` runs only with a build that carries 010, and that build is the one
    the deployment starts on after it; a rollback waits until N owes no claimable
    delivery for a cleared revision, and while N-1 serves a retry of parked deliveries
    leaves the cleared revisions out ([rollback](../operations/upgrades.md#rollback)).
    No gate holds the rollout, and the mixed-version gate does not check this rule.

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
than `default` (rule 5), new revocation keys (rule 18), or a process killed
mid-migration. It skips with exit 0 where docker is unreachable; CI passes `--require`,
which fails there instead.
