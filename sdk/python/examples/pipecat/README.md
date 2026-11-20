# Pipecat text memory

`LoomMemoryService` adds recall and finalized-turn capture to a Pipecat text
pipeline. ClickHouse maintains this integration as part of Loom's Python SDK.
Memory stays in your Loom deployment; the adapter has no local memory store.

The [single-file example](../pipecat_text_memory.py) uses a fixed reply. Recall
runs before that reply, and capture receives the completed assistant turn after
it. Pipecat needs no audio or model provider for this example. Loom can still
call its configured embedding and extraction providers, with their credentials
and costs.

## Install

Use Python 3.11 or later and an application virtual environment. Install the SDK
source used by the native text check:

```bash
uv venv .venv-pipecat
uv pip install --python .venv-pipecat/bin/python \
  "clickhouse-loom[pipecat] @ git+https://github.com/ClickHouse/loom@3c9ae8e1dde82685ee0e27bfe29b1c8d66cb956f#subdirectory=sdk/python"
```

Save the linked single-file example as `pipecat_text_memory.py` beside your
application. The SDK extra declares Pipecat 1.16.x; the native check used 1.16.0.
This source pin identifies that checked SDK and example, rather than promising
that a different installed package has the same runtime qualification.

## Application requirements

- A reachable Loom deployment and an operator-issued token that permits
  `memory.search` and `memory.set_from_messages` in the intended namespace.
- Your application's existing trusted `SessionProtection` family and pointer
  file. The example opens that file; it never creates or repairs a family. The
  application must reuse its existing family when restoring a conversation.
- A user identifier and a stable conversation identifier. These route memory
  work; they do not grant permission or establish protection authority.

Keep credentials and pointer metadata out of prompts, recordings and logs.
The token's organization and namespace determine access. An identifier received
from model output is not trusted application authority. See the
[adapter source](../../loom/integrations/pipecat.py)
for the service contract.

## Place the processors

Beside the example file, an application with an existing asynchronous `Agent`
can construct the pipeline as follows:

```python
from loom import Agent
from loom.integrations.pipecat import LoomMemoryService
from pipecat.pipeline.pipeline import Pipeline
from pipecat_text_memory import CannedReply


def memory_pipeline(agent: Agent, user_id: str, conversation_id: str) -> Pipeline:
    memory = LoomMemoryService(
        agent, user_id=user_id, session_id=conversation_id, defer=False
    )
    return Pipeline(
        [memory.recall_processor(), CannedReply(), memory.capture_processor()]
    )
```

The complete example supplies `PipelineWorker` and `WorkerRunner`, queues a text
context and an end frame, and returns the context from `run_turn`. In a pipeline
with aggregators, recall belongs after the user context aggregator and before
the LLM; capture belongs after the assistant aggregator.

## Run the example

Set `LOOM_API_KEY` through your application's secret workflow. Configure the
remaining values for the same deployment and conversation:

```bash
export LOOM_BASE_URL="https://loom.example"
export LOOM_ORG="app-org"
export LOOM_NAMESPACE="app-memory"
export LOOM_AGENT="pipecat-text"
export LOOM_USER_ID="user-42"
export LOOM_CONVERSATION_ID="conversation-42"
export LOOM_SESSION_POINTER_FILE="$PWD/application-pointer.json"
.venv-pipecat/bin/python pipecat_text_memory.py
```

The pointer must already exist and belong to the application. To observe recall,
store a relevant memory for the same user in the permitted namespace before
running; the example does not seed one. Its printed completion message does not
prove recall or storage. Inspect the returned native context and persisted user
and assistant messages independently. A `LoomError` or automatic-capture
suppression can skip memory work while frames continue.

## Tested scope

Authenticated hosted checks with Pipecat 1.16.0, a real asynchronous `Agent` and
fake server providers verify recalled seed content in the native context and two
persisted native messages. They call Python `main()` with a fresh application
family and pointer. They do not launch the CLI command above as a separate
process or qualify restoring an existing or protected family. Audio,
interruption handling and a completed video are outside that evidence.

## License and history

The SDK and integration use [Apache-2.0](../../LICENSE).
See the [integration changelog](CHANGELOG.md) and
[SDK release history](https://github.com/ClickHouse/loom/releases).
