"""Directory-first reservation and local-first narrowing under one retained owner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from server.cli import cell_control, cell_scim, cell_update_config
from server.cli import cell_update_pg as local
from server.cli import cell_update_probe as probe
from server.core import cell, deployment_config, pg_profile
from server.router import fingerprints
from server.router.directory import CONTROL_FIELDS
from server.settings import settings


def same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


class Run:
    def __init__(
        self, ch: Any, args: argparse.Namespace, router: httpx.URL, transport: Any
    ) -> None:
        self.ch, self.args, self.router, self.transport = ch, args, router, transport
        self.owner: cell_control.Owner
        self.row: dict[str, Any] = {}
        self.state: local.State

    async def call(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await cell_control.request(
            self.router,
            self.args.cell,
            self.transport,
            path,
            body,
            method="GET" if body is None else "POST",
        )

    async def durable(self) -> local.State:
        async def read() -> local.State:
            if await pg_profile.standby_now() != "synchronous":
                raise cell_control._refused("update requires an attached synchronous standby")
            found = await local.read(self.owner.conn, self.args.cell)
            if await pg_profile.standby_now() != "synchronous":
                raise cell_control._refused("the synchronous standby disappeared during update")
            return found

        return await self.owner.transaction(read)

    async def start(self, owner: cell_control.Owner) -> dict[str, Any]:
        self.owner = owner
        self.state = await self.durable()
        self.row = await self.call("")
        local.paired(self.state, self.row)
        self.target: dict[str, Any] | None = None
        if self.args.config:
            self.target = cell_update_config.target_fingerprint(
                cell_update_config.read_target(Path(self.args.config), settings),
                self.state.deployment_id,
                self.row,
            )
        if self.args.config or self.args.finish or self.args.revert:
            _action, _recorded, _current, _pending, target, _desired, _next = self.selection()
            await self.scim_precondition(target)
        elif self.row["is_first"] and self.args.probe_url:
            raise cell_control._refused(
                "the first cell refreshes router-configured addresses without --probe-url"
            )
        elif not self.row["is_first"] and not self.args.probe_url:
            raise cell_control._refused("a later cell probe refresh requires --probe-url")
        self.row = await cell_control.claim(owner.conn, self.call, self.row)
        local.paired(self.state, self.row)
        if self.args.config or self.args.finish or self.args.revert:
            return await self.configuration()
        from server.cli.cell_update_addresses import refresh

        return await refresh(self)

    async def scim_precondition(self, target: dict[str, Any]) -> None:
        from server.cli.cell_attach import _SCAN_BUDGET_S

        await cell_scim.check(
            self.ch,
            self.owner.conn,
            self.row,
            (self.state.current, self.state.pending),
            target=target,
            seconds=_SCAN_BUDGET_S,
            revoke=getattr(self.args, "revoke_unprovisioned", False),
            log=lambda message: print(f"loom cell update: {message}", file=sys.stderr),
        )

    def check_answer(
        self, answer: dict[str, Any], current: dict[str, Any], pending: dict[str, Any] | None
    ) -> None:
        cell_control.preconditions(answer)
        if (
            answer["mutation_generation"] != self.row["mutation_generation"]
            or any(
                not same(answer[name], self.row[name])
                for name in CONTROL_FIELDS
                if name not in ("current_fp", "next_fp")
            )
            or not same((answer["current_fp"], answer["next_fp"]), (current, pending))
        ):
            raise cell_control._refused(
                "directory update acknowledgement differs from its intended result; outcome unknown"
            )

    async def held_orgs(self, current: dict[str, Any], pending: dict[str, Any] | None) -> list[str]:
        orgs = sorted(
            {
                org
                for value in (current, pending)
                if value is not None
                for org in value["fields"].get("configured_orgs", [])
            }
        )
        result = await cell_control.mutate(
            self.owner.conn,
            self.call,
            self.row,
            "/configuration/placements",
            {"orgs": orgs, "keys_fp": deployment_config.fingerprints(settings)["keys_fp"]},
        )
        held = result.get("orgs")
        if (
            not isinstance(held, list)
            or any(type(org) is not str for org in held)
            or len(set(held)) != len(held)
            or not set(held) <= set(orgs)
        ):
            raise cell_control._refused(
                "directory returned an unreadable configured-placement proof"
            )
        if set(held) != set(orgs) and not (
            self.row["is_first"] and self.row["state"] == "attaching"
        ):
            raise cell_control._refused(
                "configured placement is missing; restore directory metadata before updating"
            )
        return held

    async def publish(self, target: dict[str, Any], action: str) -> dict[str, Any]:
        keys = deployment_config.fingerprints(settings)["keys_fp"]
        return await probe.post(
            self, "/configuration", {"action": action, "target_fp": target, "keys_fp": keys}
        )

    def selection(
        self,
    ) -> tuple[
        str,
        bool,
        dict[str, Any],
        dict[str, Any] | None,
        dict[str, Any],
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        action = "config" if self.args.config else "finish" if self.args.finish else "revert"
        recorded = self.row["current_fp"] is not None
        current = self.row["current_fp"] if recorded else self.state.current
        pending = self.row["next_fp"] if recorded else self.state.pending
        target = (
            self.target
            if action == "config"
            else pending
            if action == "finish" and pending is not None
            else current
        )
        assert target is not None
        desired, nxt = fingerprints.configuration_pair(current, pending, target, action)
        if reason := fingerprints.registration_pair_refusal(self.row, desired, nxt):
            raise cell_control._refused(reason)
        return action, recorded, current, pending, target, desired, nxt

    async def configuration(self) -> dict[str, Any]:
        action, recorded, current, pending, target, desired, nxt = self.selection()
        old_pending = pending is not None
        if action == "config" and recorded:
            answer = await self.publish(target, action)
            self.check_answer(answer, desired, nxt)
            self.row = answer
        held = await self.held_orgs(self.row["current_fp"], self.row["next_fp"]) if recorded else []
        self.state = await local.write(self.owner, self.state, self.row, desired, nxt, held)
        self.state = await self.durable()
        if not same((self.state.current, self.state.pending), (desired, nxt)):
            raise cell_control._refused("local acceptance differs after its durable update")
        await self.scim_precondition(target)
        if action != "config" and old_pending:
            from server.cli.cell_update_participants import settle

            await settle(self, target)
            self.state = await self.durable()
            if not same((self.state.current, self.state.pending), (target, None)):
                raise cell_control._refused("local cutoff changed while participants turned over")
            await self.scim_precondition(target)
            # A fresh full-row read refuses a delayed claim before final reservation release.
            latest = await self.call("")
            if not same(cell_control.preconditions(latest), cell_control.preconditions(self.row)):
                raise cell_control._refused(
                    "directory ownership or reservation changed; rerun with a fresh census"
                )
            if recorded:
                answer = await self.publish(target, action)
                self.check_answer(answer, desired, None)
                self.row = answer
        elif action != "config" and recorded:
            answer = await self.publish(target, action)
            self.check_answer(answer, desired, None)
            self.row = answer
        return {
            "cell": self.args.cell,
            "action": action,
            "directory_reserved": recorded,
            "current": cell.digest(desired["fields"]),
            "next": None if nxt is None else cell.digest(nxt["fields"]),
            "changed_fields": sorted(
                name
                for name in set(current["fields"]) | set(target["fields"])
                if not same(
                    current["fields"].get(name, cell.DEFAULTS.get(name)),
                    target["fields"].get(name, cell.DEFAULTS.get(name)),
                )
            ),
            "rollout": (
                "reserved; roll out the target configuration before finish"
                if recorded
                else "local pair prepared; attach must reserve both configurations"
            )
            if action == "config" and nxt is not None
            else "complete",
        }


async def update(
    ch: Any, args: argparse.Namespace, router: httpx.URL, transport: Any = None
) -> dict[str, Any]:
    run = Run(ch, args, router, transport)
    return await cell_control.run("update", run.start)
