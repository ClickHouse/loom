"""Tracker client — the tracker record read live through loom on a QA namespace.

    dups = await agent.tracker.similar(namespace="tracker-clickhouse", issue_number=117352)
    for it in dups["items"]:
        print(it["kind"], it["number"], it["title"], it["why"])

    sig = await agent.tracker.test_signal("02345_keeper_session", namespace="tracker-clickhouse")
    if sig["verdict"] == "flaky":
        ...  # do not bisect the change for this failure

Every call names the tracker namespace it asks: the transport's gate decides who
reads, so a restricted namespace needs an explicit grant.
`tracker_unavailable: true` on any answer means nobody read the record — an
empty answer is never a claim of absence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class TrackerClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def similar(
        self,
        *,
        namespace: str,
        text: str = "",
        issue_number: int = 0,
        frames: list[str] | None = None,
        kinds: list[str] | None = None,
        top_k: int = 10,
        legs: list[str] | None = None,
    ) -> dict[str, Any]:
        """The reports that already say it: duplicate and
        related issues / PRs for a pasted report (`text`), an existing item
        (`issue_number` — its own fields become the query and only older
        items are candidates) or a stack trace (`frames`). Read `legs` on
        the answer (which legs served, which said `not_computed` or
        `restricted_required`), `why` and `relation` (duplicate_of |
        related_to | fixed_by | none — what the tracker graph records) on
        every hit, and `epistemic`, which is always `lower_bound`:
        an empty list is "nothing found"."""
        body: dict[str, Any] = {"namespace": namespace, "top_k": top_k}
        if text:
            body["text"] = text
        if issue_number:
            body["issue_number"] = issue_number
        if frames:
            body["frames"] = list(frames)
        if kinds:
            body["kinds"] = list(kinds)
        if legs:
            body["legs"] = list(legs)
        return await self._agent.transport.post("/v1/tracker.similar", body)

    async def duplicate_check(
        self,
        *,
        namespace: str,
        title: str = "",
        body: str = "",
        frames: list[str] | None = None,
        component: str = "",
        version: str = "",
        mentions: list[int] | None = None,
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
        issue_number: int = 0,
        top_k: int = 10,
        as_of: str = "",
    ) -> dict[str, Any]:
        """Does this draft already exist? `verdict` is
        duplicate_of | related | none — `none` carries no items; every item
        has `confidence` (calibrated probability, null when `epistemic` is
        uncalibrated), `relation` duplicate | related, `relation_recorded`
        (the tracker graph's edge to `issue_number`), `evidence` lines, `why` and
        `features`; `undecided` flags a gray-band top candidate in the same
        component. Call before filing: file on none, attach related as
        see-also, hold a duplicate_of for a reviewer. `paths` / `symbols`
        are the finding's code-path evidence; `as_of` replays at a past
        moment."""
        body_: dict[str, Any] = {"namespace": namespace, "top_k": top_k}
        if title:
            body_["title"] = title
        if body:
            body_["body"] = body
        if frames:
            body_["frames"] = list(frames)
        if component:
            body_["component"] = component
        if version:
            body_["version"] = version
        if mentions:
            body_["mentions"] = [int(n) for n in mentions]
        if paths:
            body_["paths"] = list(paths)
        if symbols:
            body_["symbols"] = list(symbols)
        if issue_number:
            body_["issue_number"] = int(issue_number)
        if as_of:
            body_["as_of"] = as_of
        return await self._agent.transport.post("/v1/tracker.duplicate_check", body_)

    async def links(
        self,
        *,
        namespace: str,
        issue_number: int = 0,
        pr_number: int = 0,
        limit: int = 10,
        include_record: bool = True,
    ) -> dict[str, Any]:
        """The PRs that probably fixed an issue, or the issues a PR probably
        fixed, when no `fixes` edge says so: the daily
        pass's ranked candidates with `score`, `features` and `why`
        (closes | mentions, lexical, frames, component); `epistemic` is
        always `candidate`. `record` carries the tracker graph's own edges read
        live and `confirmed_by_record` marks a hit they agree with;
        `verdict` none = computed, nothing scored; not_computed = the pass
        has not covered the number."""
        body: dict[str, Any] = {"namespace": namespace, "limit": limit}
        if issue_number:
            body["issue_number"] = issue_number
        if pr_number:
            body["pr_number"] = pr_number
        if not include_record:
            body["include_record"] = False
        return await self._agent.transport.post("/v1/tracker.links", body)

    async def item(self, *, namespace: str, numbers: list[int]) -> dict[str, Any]:
        """The tracker record of up to ten issue / PR numbers:
        component, severity, bisect verdict, test presence, the graph
        around each; `unknown` names the numbers QA has no row for."""
        return await self._agent.transport.post(
            "/v1/tracker.item", {"namespace": namespace, "numbers": list(numbers)}
        )

    def _identity(self, namespace: str) -> dict[str, Any]:
        return {"org": self._agent.org, "namespace": namespace or self._agent.namespace}

    async def binding(self, *, namespace: str) -> dict[str, Any]:
        """What `namespace` may ask the tracker plane: the QA
        namespace that answers for it (`tracker_namespace`, itself or the bound
        one), `kind` qa | code | none, `restricted`, `authorized`, and the
        catalogue `templates` the caller may run. `none` is one answer for
        unbound, refused and not-a-QA-namespace alike."""
        return await self._agent.transport.post("/v1/tracker.binding", self._identity(namespace))

    async def release(self, version: str, *, namespace: str = "") -> dict[str, Any]:
        """Blockers and the latest health snapshot of one release (`26.3`)."""
        return await self._agent.transport.post(
            "/v1/tracker.release", {**self._identity(namespace), "version": version}
        )

    async def component(
        self, component: str, *, limit: int = 10, namespace: str = ""
    ) -> dict[str, Any]:
        """One component's risk snapshot and open issues, most severe first."""
        return await self._agent.transport.post(
            "/v1/tracker.component",
            {**self._identity(namespace), "component": component, "limit": limit},
        )

    async def flaky(
        self,
        test_name: str,
        *,
        limit: int = 10,
        include_signal: bool = False,
        window_days: int = 0,
        namespace: str = "",
    ) -> dict[str, Any]:
        """The flaky-test issues that name `test_name` (`verdict` flaky_by_issue |
        no_flaky_signal | tracker_unavailable). `include_signal=True` adds the
        `signal` block of `test_signal` — a read of seconds."""
        body: dict[str, Any] = {
            **self._identity(namespace),
            "test_name": test_name,
            "limit": limit,
        }
        if include_signal:
            body["include_signal"] = True
            body["window_days"] = window_days
        return await self._agent.transport.post("/v1/tracker.flaky", body)

    async def test_signal(
        self,
        test_name: str,
        *,
        window_days: int = 0,
        code_namespace: str = "",
        include_churn: bool = True,
        pr_number: int = 0,
        include_smells: bool = True,
        namespace: str = "",
    ) -> dict[str, Any]:
        """Flaky, regression or infrastructure, from the test's own CI history:
        `verdict` flaky | regression_candidate |
        infra_candidate | no_signal | tracker_unavailable, `why`, `features`
        (runs, fail_rate, flip_rate, entropy, rerun_flips, failing_prs,
        last_true_fail, …), `infra` (storm hours, K), `churn` (home file and
        mapped-source PRs in the ledger), `lower_bound`, `thresholds`.
        `rollup` (what the daily roll-up covered), `streak`
        (the previous-outcome streak of `pr_number`), `smells` (static
        flakiness tokens in the test's source) and `prior` (`next_run_on_pr`,
        the flake indicators). `no_signal` is silence, not health.
        `window_days` 0 = server default."""
        body: dict[str, Any] = {
            **self._identity(namespace),
            "test_name": test_name,
            "window_days": window_days,
            "code_namespace": code_namespace,
            "include_churn": include_churn,
            "pr_number": pr_number,
            "include_smells": include_smells,
        }
        return await self._agent.transport.post("/v1/tracker.test_signal", body)

    async def answer(
        self,
        question: str = "",
        *,
        anchors: dict[str, Any] | None = None,
        limit: int = 10,
        explain_only: bool = False,
        namespace: str = "",
    ) -> dict[str, Any]:
        """Ask the tracker in prose or by anchors."""
        return await self._agent.transport.post(
            "/v1/tracker.answer",
            {
                **self._identity(namespace),
                "question": question,
                "anchors": dict(anchors or {}),
                "limit": limit,
                "explain_only": explain_only,
            },
        )
