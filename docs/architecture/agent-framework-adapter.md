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

## Source entry points

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: native provider and agent behavior.
- `tests/integration/test_sdk_agent_framework_memory.py`: live extraction and recall.

[Framework setup](../features/integrations/frameworks.md#microsoft-agent-framework)
