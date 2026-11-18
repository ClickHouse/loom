# Microsoft Agent Framework context provider

`LoomContextProvider` implements the framework's context-provider interface over
the asynchronous Python SDK. The optional extra admits only the tested framework
minor series; the core SDK and server do not import the framework.

Before a run, the provider searches the latest user text and adds matching facts
to the framework's instructions. After the run, it sends new user and assistant
text to `memory.set_from_messages` in bounded batches. It runs that hook once per
turn, including a framework middleware loop. The framework keeps its own history;
this provider stores durable facts rather than replacing a history provider.

## Invariants

The user and framework session id derive a Loom session, preventing two users'
equally named sessions from sharing extracted chunks. An omitted user reads all namespace memories the token permits and writes
shared rows. Explicit user ids cannot have surrounding whitespace. Namespace grants authorize access; a user parameter is a filter.
The server supplies actor and source provenance and applies ordinary conflict,
erasure and rolling-schema rules. No database, cache or local history is added.
Errors propagate through the framework's provider hooks, including errors after
a model reply. Concurrent writes follow the server's normal conflict rules.

## Conversation protection

A provider serves one logical conversation family. It retains the application-owned
protection handles present at construction and callback entry, including ambient
contributors and each later bound client handle. Native user and session ids route
requests; they do not create or replace this authority. Restore reopens the existing
capsule before constructing the provider.

Owned recall and extraction run in the SDK's automatic capture context. The transport
rechecks every retained family after credentials and before dispatch, observes replies
before conversion, and durably marks all contributors before releasing a marked reply.
The first marked recall may reach the model. Later automatic work refuses, and each
extraction batch is checked before the next one can start. A refusal after a model reply
propagates through the native provider lifecycle; it cannot undo the answer or tool effects.
The provider adds no capability declaration or restored-host qualification.

## Source entry points

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: native provider and agent behavior.
- `tests/integration/test_sdk_agent_framework_memory.py`: live extraction and recall.

[Framework setup](../features/integrations/frameworks.md#microsoft-agent-framework)
