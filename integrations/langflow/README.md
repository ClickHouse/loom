# Langflow

A Langflow custom component that saves and searches Loom memory and searches
knowledge documents through the Python SDK.

The component is a Langflow \`Component\` with typed inputs for the namespace, the
user, and the query, and an action dropdown: \`save\`, \`search memory\`, or
\`search documents\`. It constructs a \`SyncAgent\` and calls \`memory.set\`,
\`memory.search\`, or \`knowledge.search\`. The user input is sent as \`user_id\` on
the memory write and search. Knowledge search is namespace-scoped. Data stays in
your Loom deployment; the component adds no store.

## Install

These steps were checked against Langflow 1.13.3.

Install the Python SDK into the same environment as Langflow, from this checkout:

```bash
pip install /path/to/loom/sdk/python
```

Or install the published package with `pip install clickhouse-loom`.

Load [`loom_component.py`](loom_component.py) as a Langflow custom component:
paste the file into a new custom component, or point Langflow's custom
components path at this directory, then start Langflow:

```bash
export LANGFLOW_COMPONENTS_PATH=/path/to/loom/integrations/langflow
langflow run
```

The component appears as **Loom**. It needs a reachable Loom server (a
[quickstart](../../docs/getting-started/quickstart.md) listens on
`http://localhost:7777`) and a namespace-scoped writer token.

## Inputs

| Input      | Role                                                                     |
| ---------- | ------------------------------------------------------------------------ |
| Action     | `save`, `search memory`, or `search documents`                           |
| Org        | Organization; may be empty when the token fills it                       |
| Namespace  | Namespace the component reads and writes                                 |
| User       | End user for `memory.set` and `memory.search`; empty reads the namespace |
| Query      | Value to save, or the search query                                       |
| Memory key | Required when the action is `save`                                       |
| Top K      | Number of search hits (default 5)                                        |
| Loom URL   | Server URL (default `http://localhost:7777`)                             |
| API key    | Writer token; may also come from `LOOM_API_KEY` or the Loom profile      |
| Agent      | Agent name stamped on writes (default `langflow`)                        |

## A flow

1. Start a Loom quickstart at `http://localhost:7777` and create a writer token.
2. In Langflow 1.13.3, add a Chat Input, a Loom component, and a Chat Output.
   Connect Chat Input to the Loom Query input, and the Loom Result to Chat Output.
3. On the Loom component, set Action to `save`, Namespace to your namespace, User
   to the end-user id, Memory key to a stable key such as `pref.seat`, and Org,
   API key, and Loom URL from the quickstart.
4. Run the flow with a message such as "aisle seat". The Result is a JSON object
   with `action`, `memory_id`, `version`, and `content_sha256`.
5. Add a second Loom component with the same Org, Namespace, User, API key, and
   URL, Action `search memory`, and Query from Chat Input. Run with "which
   seat". The Result is a JSON object with `action`, `abstain`, and `hits`
   (`memory_key`, `value`, `similarity`). A `search documents` run returns
   `action` and `hits` (`source_uri`, `content`, `similarity`).

Unit tests under `tests/unit/test_langflow_component.py` drive each action with
the SDK pointed at an `httpx.MockTransport`, so they do not need Langflow or a
live Loom server. A recorded transcript from a live Langflow session against a
quickstart is not included here.
