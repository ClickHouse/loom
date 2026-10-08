# Examples

Runnable, self-contained examples for `@clickhouse/loom`. Each was executed against a live Loom before it landed here and the output in its README is the actual run. Each directory stands alone: a README, one script, nothing shared — copy it into your project and start from there.

|     | Example                             | What it shows                                                                          |
| --- | ----------------------------------- | -------------------------------------------------------------------------------------- |
| 🧠  | [agent-memory](./agent-memory/)     | probe → remember → recall → ask with citations → outcome; typed errors                 |
| 🔁  | [session-resume](./session-resume/) | a session resumed in a new process, a parked tool result, a model calling loom's tools |

Every example reads `LOOM_BASE_URL`, `LOOM_API_KEY` and `LOOM_ORG` from the environment and registers a throwaway namespace.
