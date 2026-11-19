// Dispatch one explicit code lookup without calling an agent model.
// Needs an existing code index and a token with read access. Search uses server embeddings.
import { parseArgs } from "node:util";
import { Datapack } from "@clickhouse/loom";

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    org: { type: "string" },
    "memory-namespace": { type: "string" },
    "code-namespace": { type: "string" },
    kind: { type: "string", default: "symbol" },
    depth: { type: "string" },
    help: { type: "boolean", default: false },
  },
});
if (values.help) {
  console.log("node index.mjs --org ORG --memory-namespace MEMORY --code-namespace CODE [--kind symbol|search|callers|impact] [--depth N] TARGET");
} else {
  if (!values.org || !values["memory-namespace"] || !values["code-namespace"] || positionals.length !== 1) {
    throw new Error("Pass --org, --memory-namespace, --code-namespace and one target; see --help");
  }
  const bot = new Datapack(values.org, values["memory-namespace"], {}).agent("code-assistant");
  const tools = bot.tools({}, { codeNamespace: values["code-namespace"], only: ["loom_code_lookup"], maxResultChars: 12000 });
  // In a model loop, pass tools.definitions("openai") to the provider and dispatch
  // the model's selected name and arguments to execute().
  console.log(await tools.execute("loom_code_lookup", {
    kind: values.kind,
    target: positionals[0],
    ...(values.depth !== undefined ? { depth: Number(values.depth) } : {}),
  }));
}
