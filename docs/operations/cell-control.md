# Cell control upgrades and recovery

Cell control mutations require generation-aware clients and router handlers, and direct
access to the cell's Postgres. This page covers the runtime prerequisite, unknown
outcomes and registered-cell state cleanup. The protocol is described in
[cells](../architecture/cells.md#control-mutation-fencing).

## Upgrade the control fleet

Apply the router migrations with `loom router migrate`. The additive UUID column has a
server default and remains readable by N-1 data-plane code. Its volatile default may
rewrite the small cells table and takes a table lock; allow a bounded maintenance window.
Schema compatibility does not let old control programs participate in fencing.

Before using generation-aware acceptance mutation, inventory and positively terminate
all old router control handlers and prepare, attach, restore, update, remove and manual
cleanup clients, including paused one-offs and outstanding requests. Replace every
launcher/image so those binaries cannot restart against the stores. Use exact process
and start identities, trustworthy evidence that the exact container ended, or independent
node fencing. A heartbeat expiry, missing pod object or one successful request to a router
replica does not prove this inventory. If the inventory is incomplete, stop/fence every
machine that could still run an old control program before resuming control traffic.
Compatible cell data-plane builds may continue serving during this procedure. Every
old router replica that could execute an operator handler must be positively retired;
continuing its data-plane routing alone does not remove that control capability.

The fencing changes and their dependent CLI changes may be implemented and reviewed as
stacked concerns, merged in dependency order and shipped together. They require no
separate release cut. Running a protected operation requires the capable runtime and
completed control turnover procedure. Missing-generation requests refuse with
`CONTROL_UPGRADE_REQUIRED`; an absent claim route or incomplete operator snapshot tells
the CLI to upgrade the router replica. There is no compatibility fallback or operator
flag that proves a deployment completed its external process inventory.

## Unknown outcomes

A timeout, connection loss, cancelled COMMIT or lost acknowledgement ends the current
attempt. Keep the exact cell and router configuration, inspect the reported state and
rerun the command. The rerun must acquire the local attach lock and make a fresh claim
before it changes acceptance. A duplicate claim creates a fresh UUID; it never restores
a caller's earlier token. Do not replay a saved mutation with a copied current token.

A client connection ending does not prove its Postgres backend ended. If the attach lock
remains held, inspect its exact holder with the query in
[troubleshooting](troubleshooting.md#a-process-with-loom_cell-unset-is-gated). Positively
end that exact owned backend, or wait until it ends. Do not terminate whichever backend
holds a mode-table lock: it may belong to another controller. No control run reconnects
to continue an uncertain attempt.

## Clear a registered unattached cell's state

Use this only for the troubleshooting page's eligible unattached cell with no placements,
or a first cell being backed out while its local mode is still `recording`. A first cell
in local `enforced` mode or any cell with placements goes forward. Complete the control
fleet turnover above, retain the existing stop/unset-`LOOM_CELL` procedure, and prevent
accepting or maintenance processes from restarting with the cell setting during cleanup.
Use the table-owning migration role. Never clear a restored copy beside its live source.

Run the following with this cell's settings, including `LOOM_CELL`, `LOOM_PG_DSN`, its
ClickHouse and bucket configuration, `LOOM_CELL_ROUTER_URL` and the operator credential.
This read-only store pairing check must succeed before the helper claims or clears state.
It does not write participant rows or run the data-plane command gate.

```python
import argparse
import asyncio

import httpx

from server.cli import cell as cell_cli
from server.cli import cell_control
from server.core import pg, pg_profile
from server.core.ch import get_client
from server.settings import settings


async def main():
    ch = get_client()
    await ch.connect()
    try:
        recorded = await pg_profile.recorded_authority(ch)
        if not recorded or recorded["authority"] != "postgres":
            raise RuntimeError("the configured stores do not record Postgres authority")
        pg.expect_deployment((str(recorded["deployment_id"]), int(recorded["epoch"])))
        url = cell_cli._cell_router(argparse.Namespace(router=None, cell=settings.cell))
        if not isinstance(url, httpx.URL):
            raise RuntimeError("the router URL or operator credential was refused")

        async def call(path, body):
            return await cell_control.request(url, settings.cell, None, path, body)

        async def cleanup(owner):
            row = await cell_control.request(url, settings.cell, None, "", method="GET")
            await cell_control.backout(owner, call, row)

        await cell_control.run("state cleanup", cleanup)
    finally:
        await ch.disconnect()


asyncio.run(main())
```

The helper claims first, closes the cell using the existing limit operation, and requires
that operation's durable placement count to remain zero before any local state cleanup.
A queued placement either wins first and refuses cleanup, or observes the closed row and
cannot create a placement. An explicit `loom cell limit --open` now refuses for every
attaching cell, even one already open; a combined open/quota request changes neither
field. This behavior break keeps concurrent limit clients from reopening the cleanup
boundary. Close and quota-only edits remain available. The helper then compares and
deletes only the existing
`cell_first`, `tenancy_mode`, `cell_self` and `tenancy` rows, restores the first-row
triggers and drops only the existing org guard triggers, on the same locked backend.
It takes the migration lock for each bounded local transaction. Even an already-open
retry proves durability before reporting success.

After the close is acknowledged, an abort leaves the cell closed. It never reopens
automatically or deletes a placement.
If a placement won, resume attach while closed: its existing-org placement batches bypass
the ordinary close/quota gate. Complete attach to `serving` before explicitly reopening
with `loom cell limit --open`. Otherwise rerun the cleanup with a fresh claim, then run
`loom cell remove --cell <cell>` with the same cell settings. Removal now holds that
cell's direct Postgres lock and takes a fresh claim for both first and later cells. It
also requires a first cell's durable local state to read `open` before deleting the row.
Only then may the deployment be prepared again.

## Restore the directory offline

A directory backup can contain an earlier generation. Stop and positively terminate or
fence all control clients and router processes, including outstanding control requests,
and prevent restarts before restoring it. While control traffic remains stopped, execute
this on the restored directory and wait for its synchronous commit:

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '10s';
UPDATE loom_router.cells SET mutation_generation = gen_random_uuid();
COMMIT;
```

Bound the client's complete transaction/COMMIT wait too. An uncertain outcome does not
permit APIs to resume: establish the restored writer's actual completion, retry the
rotation while still offline and obtain a known durable commit. Rotate every restored
row before restarting capable router/control binaries. Never copy generations from a
cell backup or rely on an audit retention window to fence restored requests.
