# Connect Loom to a local model and a coding agent

This guide connects Loom to a local language model served by Ollama and to Cline in Visual Studio Code. Loom exposes its MCP tools to Cline so the agent can save and retrieve persistent memories across conversations.

## Requirements

- macOS with Docker Desktop installed and running.
- Python 3.12 or later.
- Node.js and `npx`, to run the MCP remote bridge.
- Ollama installed.
- Visual Studio Code with the Cline extension installed.
- The Loom repository cloned locally, with its development dependencies installed.

## 1. Start Ollama

Pull the model used in this guide:

```bash
ollama pull qwen2.5:3b
```

Confirm that the model is available and that Ollama responds:

```bash
ollama list
curl http://localhost:11434/api/tags
```

Loom's extraction prompts can contain about 15,000 tokens. Start Ollama with a context length of `32768` so it can process them without truncating the prompt:

```bash
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
```

Keep this terminal running while using Loom and Cline.

## 2. Configure Loom to use Ollama

From the root of the Loom checkout, open `.env` and set these values:

```dotenv
LOOM_LLM_PROVIDER=local
LOOM_LLM_MODEL=qwen2.5:3b
LOOM_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
LOOM_EMBEDDING_MODEL=local
LOOM_EMBEDDING_DIM=384
```

Keep the other settings in `.env`. Do not replace the file with `.env.example`: `loom up` creates `.env` when it is missing and preserves an existing file.

The URL uses `host.docker.internal` because Loom runs in Docker and Ollama runs on the Mac host. The local embedder keeps embedding requests on the machine as well.

Start the development stack:

```bash
loom up
```

Wait for the server to become ready, then check its health:

```bash
curl --fail --silent --show-error http://localhost:7777/v1/health
```

The response should report `"status":"ok"`, `"llm_provider":"local"`, and `"embedding_model":"local"`.

If the dashboard has saved model settings that override environment variables, open **Models & keys**, choose **Use environment configuration**, and restart the stack.

## 3. Configure Cline to use Ollama

In Visual Studio Code, open Cline's settings and select Ollama as the API provider.

Use these model settings:

- **Model:** `qwen2.5:3b`
- **Base URL:** `http://localhost:11434`

Cline runs on macOS rather than inside the Loom container, so it uses `localhost` to reach Ollama.

## 4. Connect Cline to Loom

Open Cline's MCP server configuration and add this server entry:

```json
{
  "mcpServers": {
    "loom": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:7777/mcp?toolset=coding",
        "--allow-http"
      ]
    }
  }
}
```

The `mcp-remote` bridge lets Cline connect to Loom's streamable-HTTP MCP endpoint. The `coding` toolset exposes the tools used by coding agents. `--allow-http` permits the local HTTP connection.

Restart Cline or reload its MCP servers. Confirm that the Loom server appears as connected and that its tools are available.

## 5. Verify persistent memory across conversations

Start a Cline conversation and ask it to save a persistent memory in Loom. Give the memory a unique key, for example `cline-local-model-check`, and include a short value that you can recognize.

For example:

> Save a persistent Loom memory with the key `cline-local-model-check` and the value `Ollama qwen2.5:3b is connected to Loom through Cline on my local Mac.`

After the agent confirms the save, start a **new Cline conversation** and ask it to retrieve the memory using that exact key:

> Look up the Loom memory with the key `cline-local-model-check` and tell me its value.

The new conversation should be able to retrieve the value saved by the previous one. This verifies that the memory is stored in Loom rather than only in the original conversation.

## Tested versions

The workflow was tested with:

- macOS 15.7.3
- Python 3.13.0
- Docker Compose 5.0.1
- Ollama 0.40.17
- `qwen2.5:3b`
- Visual Studio Code with Cline 4.1.27
- Loom 1.3.0