---
title: How to choose AI software factory tools
description: Compare AI software factory tools for planning, coding, review, CI, deployment and shared memory. Score a pilot on release gates and cost per accepted change.
---

# How to choose AI software factory tools

An AI software factory coordinates agents and existing development tools to take work
from an issue to a tested change. Choose tools by what each stage needs to produce,
then replay real tasks to check that the handoffs and approval rules work together.

## Match tools to the work

| Stage          | Tool to choose                         | Evidence it should produce                                                                                                                          |
| -------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Plan work      | Issue tracker and workflow coordinator | An issue with acceptance criteria, an owner, dependencies and a record of assignments.                                                              |
| Implement      | Coding agent and isolated workspace    | A diff against a known base revision, files changed and the commands the agent ran.                                                                 |
| Review         | Pull request review and policy checks  | A review tied to the exact diff, with required approvals and unresolved findings visible.                                                           |
| Verify         | CI and test harness                    | Test results tied to the proposed revision, with failures and retries recorded. Include acceptance tests maintained outside the implementation run. |
| Release        | Deployment and rollback tooling        | The artifact and revision deployed, the approval record and the rollback target.                                                                    |
| Reuse evidence | Shared agent memory and source lookup  | Decisions and lessons with an identifiable writer, source pointers and access scoped to the team or repository.                                     |

Start with the issue tracker, repository, CI and deployment tools the team already
uses. If an existing tool can produce a required record and another stage can read
it, keep it. Where roles run under separate credentials, verify that a reviewer can
read the implementation handoff and that an unrelated identity cannot read it.

Loom covers shared agent memory, indexed source and evidence handoffs. Your issue
tracker owns the issue, CI owns its check results, and the deployment tool owns its
release record. Store pointers to those records next to the lessons and decisions
that a future run can reuse. See the [software factory memory walkthrough](../use-cases/multi-agent.md#memory-for-a-software-factory)
for the implementation and review roles and their scoped credentials.

## Run a pilot on one repository

1. **Choose representative work.** Include a small fix, a change that depends on an
   earlier decision, and a change whose relevant source has moved. Pick a starting
   revision and acceptance tests for each before any agent writes code.
2. **Record a baseline.** Run the tasks with your current tools and review rules.
   When evaluating a memory tool, keep the model, repository revision, starting
   instructions and approval rules fixed. Give the memory-enabled runs only
   evidence available before the task began.
3. **Test a clean handoff.** Give a fresh review session the issue and revision.
   Check that it can find the relevant decision, open its source, and see what
   still needs verification. Change a source definition that an earlier lesson
   describes and check whether the reviewer revisits the claim.
4. **Test a failed gate.** Give the review stage a revision with a failing required
   check. Check that the release workflow keeps it blocked and that another agent
   can resume from the issue, revision and check result.
5. **Compare outcomes.** Use the [pilot scorecard](#software-factory-pilot-scorecard)
   to turn test, review, time and cost records into a rollout decision.

For example, start a retry-policy change with a prior lesson linked to the code
that defines which errors can be retried. After implementation, the reviewer
should be able to open the current definition, check the effect of the change,
and see the CI result for the proposed revision. The reviewer's job includes
deciding whether the prior lesson still applies.

For a memory pilot, use the [cross-client recall and access checks](../getting-started/evaluate.md#shared-memory-across-clients)
and the [code-linked lesson exercise](../use-cases/memory-linked-to-code.md#check-a-lesson-after-a-code-edit).
Use the [software factory handoff output](../use-cases/multi-agent.md#check-the-handoff-output)
to see what a reviewer can verify about the source and the saved lesson.
Use the [paired task scorecard](../getting-started/evaluate.md#compare-coding-agents-with-and-without-memory)
to see which tasks passed with memory, without it, or in both setups.
Use the [cost comparison procedure](../use-cases/coding-agent-cost.md#compare-completed-tasks-including-overhead)
to include index construction, retrieval and unsuccessful runs in the cost.

## Software factory pilot scorecard

Set the rollout criteria before the pilot. Keep one record per issue and tool setup
with the starting revision, the proposed revision, the acceptance test and approval
records, elapsed time, reviewer and rework minutes, and total cost including retries.
Count a change as accepted when the proposed revision passes the required tests and
receives the required review approval.

| Measure                        | How to score it                                                                                                                                 |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Acceptance rate                | Accepted changes divided by issues attempted. Keep timeouts and abandoned issues in the denominator.                                            |
| Elapsed time                   | Measure from issue assignment to acceptance or stop. Keep each outcome with its duration so quick failures stay visible.                        |
| Human time per accepted change | Divide all review and rework minutes by accepted changes. Include time spent on failed attempts.                                                |
| Cost per accepted change       | Divide total pilot cost by accepted changes. Include model calls, hosting, indexing and retries. Document how each setup shares one-time costs. |

Report the human-time and cost ratios as unavailable if a setup accepts zero changes.
Record permission and failed-release checks separately. Pause rollout if an unrelated
identity can read the handoff or a revision can advance past a failed required check.
Compare the outcomes and costs of the setups that meet those release and access gates.
