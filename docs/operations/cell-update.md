# Update a cell's configuration or addresses

`loom cell update` reserves a configuration before the cell accepts it. Run it with
that cell's stores, `LOOM_CELL`, and the router operator credential configured. It
holds the cell's ATTACH lock on one direct Postgres session. Prepare, attach,
restore, another update, and the documented state cleanup cannot run concurrently.
An interrupted command exits unsuccessfully; rerun the same action after correcting
the reported cause.

## Reserve a configuration

Write a nonsecret settings file:

```yaml
version: 1
settings:
  tracker_org: operations
```

```bash
loom cell update --cell first --config target.yaml
```

The file overlays the CLI's effective settings. Every supplied setting is validated,
including values outside the fingerprint. Secret fields are refused. The command
neither installs the file nor changes the running processes' configuration. It
prints fingerprint digests and changed field names.

The deployment id, registered public and OAuth host, and bucket and prefix cannot
change. The root-secret fingerprint must match the directory's locked value.

A SCIM-on target requires this cell's valid SCIM token, OIDC login and required
authentication. The CLI freshly checks active accounts, live refresh sessions and
personal service-account creators against the target provisioning policy before
claiming the directory generation, after durable local acceptance, and after
participant turnover before releasing the reservation. It uses the same bounded
[provisioning census as attach](cell-attach.md); a cached admission decision does
not establish this precondition. Run the CLI with the target's SCIM credentials
and authentication settings configured.

Provision every required identity before updating. Alternatively, add
`--revoke-unprovisioned` to `--config`, `--finish` or `--revert` to end tokens
issued before the update for subjects that cannot meet its SCIM policy. Accepted
SCIM cutoffs are reported immediately and remain if a later check fails; the
command does not roll them back. Correct the reported cause and rerun the same
action. This flag does not apply to address refresh. Reverting to SCIM-off current
does not require provisioning.

After attach has begun, the directory reserves both current and next values and
places newly configured orgs before the cell's Postgres accepts next. Overlap checks
include both values of every other attached or attaching cell. A different pending
value refuses. Repeating the identical candidate repairs an interrupted local
write without placing an org twice.

New configured placements obey the cell's closed flag and quota. Existing winners
in that cell remain valid while closed or full. An org held elsewhere refuses the
whole reservation. A later cell still attaching refuses configured-org changes;
a first cell attaching after begin accepts them through the normal placement gate.

Before attach/begin has recorded the directory fingerprint, update claims the
registration's generation and changes only the local pair. It places no org and
reserves no issuer or pattern. Attach checks and reserves both local values later.

## Finish or revert

Install and roll out the reserved configuration, then run:

```bash
loom cell update --cell first --finish --wait 600
```

To keep current instead:

```bash
loom cell update --cell first --revert --wait 600
```

These forms first repair local tenancy for actual same-cell configured placements
from both directory values. Their operator read compares the exact generation,
registration, pair, and root-secret fingerprint, writes an audit and WAL fence,
and waits for synchronous commit. Public lookup's first-cell fallback is not proof
that an immutable placement exists. A missing placement outside the attaching
first cell requires directory metadata recovery.

Finalization durably narrows local acceptance first. The directory keeps both
reservations until every participant captured by a complete post-cutoff census
has permanently retired or its exact process is proved dead with no outstanding
ClickHouse query. Captured target processes also turn over. A target-serving
replacement must join a live instance, a marked cell heartbeat, and a guarded
participant outside the captured set. A workers-only process cannot satisfy this.

Every configured ClickHouse replica must answer. Replicated participant tables
synchronize after the local cutoff; the census unions their newest rows without
age filters, sampling, caches, or partial-result limits. Heartbeat expiry,
`drained`, a missing row, an empty query list alone, a request deadline, or deletion
of a pod object does not prove permanent retirement. An uninspectable process or
unreadable topology refuses finalization.

Online finalization requires the [participant lifecycle baseline](../architecture/cells.md#participant-lifecycle-baseline)
and [controller fencing](../architecture/cells.md#control-mutation-fencing). Positively
terminate or fence every legacy server, worker, one-off command and control writer,
including paused boots, before using this protocol. Prevent old binaries from
returning. Inventory actual process/container termination or independent node
fencing; an expired heartbeat or missing API object is insufficient. The CLI
cannot establish this external inventory from its census. Unknown legacy identities
remain blockers. No command flag asserts or bypasses this prerequisite.

`--wait` is finite and between 0 and 86400 seconds. Zero still makes a bounded
initial attempt. Local transactions and complete census reads have their own
bounded attempts. If turnover times out, local acceptance stays single and the
directory stays dual. Keep the target replacement serving, resolve the named
participants and rerun the same action. Each rerun takes a fresh census. Retrying
`--config` can widen local acceptance again and requires another cutoff and census
before finalization. All directory placements and local tenancy rows remain after
revert, including orgs introduced by the pending value.

| Interruption                                                   | Recorded state                            | Recovery                                             |
| -------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------------- |
| Directory reservation commits before local write               | Directory dual; local pair may be old     | Repeat config, finish, or revert explicitly          |
| Local cutoff commits before turnover or final directory commit | Directory dual; local single              | Repeat the selected finish or revert                 |
| Generation or ATTACH backend changes                           | An in-flight directory reply may be stale | Stop; a fresh invocation claims again and recomputes |
| Final directory singleton already matches local                | Both single                               | The no-op still proves synchronous local durability  |

## Refresh addresses

For a later cell:

```bash
loom cell update --cell second --probe-url https://second-probe.example.test
```

For the first cell, roll out the intended `LOOM_ROUTER_UPSTREAM_URL` and
`LOOM_ROUTER_UPSTREAM_PROBE_URL` to **every router first**, then run without an
action flag:

```bash
loom cell update --cell first
```

The contacted router supplies its normalized first-cell addresses. The probe must
echo fresh Postgres, ClickHouse, and configured bucket nonces and the registered
deployment id, bucket and prefix. A main listener cannot substitute for the probe.
The first cell's main upstream must independently report that deployment id. The
router rechecks its configured addresses at commit. A later refresh changes only
its probe URL; it cannot move the registered host, stores or upstream.

A generation and address comparison excludes stale requests; it does not establish
fleet-wide configuration agreement. An old router can read the current expected
row and reverse the addresses if it still runs the old configuration. Keep the
old endpoints reachable until every router demonstrably refreshes its routing
table or restarts **after** the directory change. A configuration rollout alone
does not flush cached routes. During a directory outage a router can keep its last
routing table indefinitely. The command cannot certify all-router agreement.

Configuration actions, finish, revert and probe refresh are mutually exclusive.
Probe readiness retries use `--wait`; invalid proofs refuse immediately. Nonce
cleanup uses a value-qualified Postgres delete and a Keeper compare-and-delete.
Bucket cleanup reads and compares this run's value before an unconditional delete.
If the ATTACH backend is lost, a successor can replace the object between that read
and delete and lose its nonce. Rerun the affected proof; bucket cleanup does not
guarantee preservation of a successor's nonce.
