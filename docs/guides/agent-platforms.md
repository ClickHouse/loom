# Offer Loom in an agent platform or factory

An agent platform or factory can offer Loom as an optional shared memory and code
retrieval service through MCP, HTTP, or the Python and TypeScript SDKs. Start with a
running [Loom deployment](../operations/deployment.md), credentials for the intended
namespaces, and an explicit choice of which application stages call it.

## Choose what the integration owns

Keep the framework's execution state and Loom's shared knowledge distinct. A framework
can retain its conversation, checkpoint and orchestration state while Loom holds facts
or documents that other agents and later sessions need. A native adapter can also put
the state its interface supports in Loom; each
[framework adapter](../features/integrations/frameworks.md) documents that boundary
and its optional dependencies. Installing an adapter does not make every framework
memory operation use Loom.

Choose an interface for the application you deliver:

| Integration              | Application responsibility                                                            | Starting point                                                               |
| ------------------------ | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| MCP                      | Configure the endpoint, token, tool listing and model's tool loop                     | [MCP clients](../features/integrations/mcp-clients.md)                       |
| Native framework adapter | Install the matching dependency and bind the supported interface                      | [Frameworks and orchestrators](../features/integrations/frameworks.md)       |
| Direct SDK or HTTP calls | Await recall, validation or recording at defined stages; handle errors and abstention | [Agent integration](agent-integration.md)                                    |
| Model-selected SDK tools | Supply definitions, execute selected calls and return their results to the model      | [Code tools](code-tools.md) and [SDK tool loops](../reference/python-sdk.md) |

For a factory, place retrieval before planning or implementation when its result is
required. Let investigation tools answer additional questions during the run. Record
the outcome after the task's own checks. The existing
[invocation paths](agent-integration.md#who-invokes-loom) distinguish application calls,
plugin hooks and model choices; no particular factory controller is required.

## Call Loom explicitly from an SDK

Install the [Python SDK](../reference/python-sdk.md) and set `LOOM_BASE_URL` to the
deployment and `LOOM_API_KEY` to a token with read access to `team-context`. Replace
the example organization and namespace with scopes that token permits:

```python
import asyncio
import os

from loom import Agent


async def main() -> None:
    async with Agent(
        org="example",
        namespace="team-context",
        agent="triage-worker",
        base_url=os.environ["LOOM_BASE_URL"],
        api_key=os.environ["LOOM_API_KEY"],
    ) as agent:
        result = await agent.call(
            "memory.search",
            agent="*",
            query="Which checks are required before a change?",
            top_k=3,
        )
        print(result)


asyncio.run(main())
```

This application call runs without asking a model to select a tool. Inspect the returned
hits or `abstain` before adding evidence to the prompt; an abstention is an expected
outcome when no memory qualifies. Authentication or transport errors need their own
handling. `agent="*"` searches across writers within the token's permitted scope;
it does not widen namespace or user access. The [memory operation reference](../reference/operations/memory.md#memorysearch)
defines the response. Use `call_with_meta` when recording the request ID and timings
([request correlation](../features/integrations/index.md#joining-a-request-across-systems)).

For code investigation, use the existing Python and TypeScript
[`loom_code_lookup` examples](code-tools.md). The application fixes the code namespace;
the model chooses `symbol`, `search`, `callers` or `impact`. The model's ordinary tool
loop must still dispatch the selected call. An application can instead call the
[code operations](../reference/operations/code.md) directly by their dotted names.

## Diagnose agents that never call the tools

Check a representative task from connection through use of evidence:

1. Confirm that the client can discover the intended tool and that the token can read
   its namespace. A successful connection alone does not test either condition.
2. Make one explicit, relevant call. Check for a usable result, abstention, denied access
   or a provider error before changing the model's instructions.
3. Check whether the running client supplied tool definitions and executed selected
   calls. A raw model request does not run coding-client lifecycle hooks.
4. Give the model a short policy saying when to use each tool and when existing evidence
   is sufficient. Reuse the [coding-agent policy and diagnostics](../features/integrations/claude-code.md#claude-does-not-call-loom-tools).
5. Record application calls, hook retrieval and model-selected calls separately. Confirm
   that the returned evidence reached the model. Count errors, unused results and
   zero-call tasks as outcomes, then [evaluate answer quality](../getting-started/evaluate.md).

If a stage must retrieve or validate something, enforce that dependency in application
code. Instructions guide discretionary tool use; they do not enforce a required gate.

## Give a CTO an evaluation they can check

Evaluate the infrastructure decision on the platform's own repositories and tasks.
The [benchmark coverage map](../benchmarks/coverage.md) separates published results from
harnesses without published measurements. Follow its links to the committed artifacts,
models, datasets, reproduction commands and limitations. A published retrieval score
does not establish a task-success or cost gain for a different agent platform.

Use the existing [evaluation guide](../getting-started/evaluate.md) for indexing,
labelled retrieval cases and deployment smoke tests. Then write a paired protocol
before collecting results:

1. Freeze repository commits, task inputs, models, prompts, test commands and execution
   budgets. Include triage, design and implementation tasks, unanswerable questions,
   changed code, permission-denied cases and tasks needing no shared context. Label
   expected source ranges or caller sets without exposing reference answers to the agent.
2. Run a baseline using the platform's usual checkout, framework memory and local tools.
   Run the treatment with the same inputs and access plus the intended Loom integration.
   Keep reference fixes and held-out tests outside both agent checkouts. Use fresh
   sessions; prevent one arm's writes from teaching the other arm evaluation answers.
3. Record each task's actual calls, request IDs, outcomes and evidence delivered to the
   model. Separate explicit application calls, hooks and model-selected tools. A run with
   no calls remains a non-use outcome. A failed or misconfigured request is an integration
   failure; report it separately from a grounded retrieval miss.
4. Score task success with the same objective tests or a fixed human rubric. Check answer
   claims and citations against the frozen source, and score retrieval separately from
   synthesis. Review false answers, stale lessons, denied reads and appropriate abstention.
5. Measure end-to-end wall time and model tokens for both arms. Include tool-definition
   overhead, failed requests, retries, Loom's provider work and validation time. Report
   indexing, storage and operation costs separately from per-task cost. Mark unavailable
   usage or prices as unknown; an absent cost field is not a zero-cost result.
6. Report task count, paired outcomes, category results, repeated-run variation and
   uncertainty. Keep raw failures and non-use cases. Small samples and mixed outcomes
   support a bounded decision or another experiment, not a general accuracy claim.

Give the decision maker a short scorecard with links to the underlying task records:

| Decision                                 | Evidence to retain                                                                                |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Are answers or completed tasks better?   | Baseline and treatment outputs, objective test results, valid citations, misses and uncertainty   |
| Did agents use the intended integration? | Per-task calls and evidence receipt, including non-use and unusable results                       |
| Is the benefit worth the cost?           | Total task latency and model cost, construction cost, operating cost and expected workload volume |
| Can the team operate it?                 | Allowed and denied reads, provider failures, recovery, retention, erasure and upgrade checks      |

Set acceptance thresholds before running the protocol. No improvement follows merely
from installing Loom or completing this procedure. Real model and indexing providers
can incur charges; the protocol is a proposed evaluation, not a measured result.

## Deliver a customer deployment

A platform provider can integrate a customer-operated Loom service or package the
documented [container or Kubernetes deployment](../operations/deployment.md) with its
application. Agree who operates storage, model providers, backups, upgrades and
credential rotation. Test the same release and adapter dependencies that will ship;
main-branch documentation can describe changes absent from an installed release. Use
the [release history](https://github.com/ClickHouse/loom/releases) and
[supported versions](../operations/supported-versions.md) to choose the matching source.

Give each service identity only the namespaces and roles it needs
([tokens and grants](tokens-and-grants.md)). Repository indexing credentials and Loom
read authorization are separate controls. A chat connector login does not grant a
native service token access to a repository. Test allowed reads, denied reads, revocation
and [repository-job isolation](../operations/code-index-operations.md#repository-job-sandbox)
before delivery. Set retention and
[erasure](../features/memory/retention-and-erasure.md) requirements for shared content.

Make optional integration behavior explicit in the platform: whether a customer has
enabled it, which stages depend on it, and what users see when it is unavailable. Treat
required validation failures as failures; report omitted optional context rather than
silently claiming it was used. Evaluate the deployment with the customer's own tasks
and [published evidence](../benchmarks/coverage.md).

These are technical delivery options. They do not establish a partner program, joint
delivery commitment, support agreement, SLA, pricing or commercial terms. Those terms,
including who owns customer support and escalation, require a separate agreement with
the parties involved.
