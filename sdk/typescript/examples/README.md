# Examples

Runnable, self-contained examples for `@clickhouse/loom`. The memory and session
examples include output from a live Loom run. The code lookup example is covered by
mocked SDK tests and claims no live result. Each directory contains its own README and
script.

|     | Example                             | What it shows                                                                          |
| --- | ----------------------------------- | -------------------------------------------------------------------------------------- |
| 🧠  | [agent-memory](./agent-memory/)     | probe → remember → recall → ask with citations → outcome; typed errors                 |
| 🔁  | [session-resume](./session-resume/) | a session resumed in a new process, a parked tool result, a model calling loom's tools |
|     | [code-lookup](./code-lookup/)       | a read-only code dispatcher with fixed scope and bounded evidence                      |

The memory and session examples read `LOOM_BASE_URL`, `LOOM_API_KEY` and `LOOM_ORG` and
register a throwaway namespace. Code lookup reads the server and token from the SDK's
environment, takes the organization and namespaces as arguments, and uses an existing index.
