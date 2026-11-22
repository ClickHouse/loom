import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { LoomClient } from "../dist/index.mjs";
import { createTools, parseConfig } from "./tools.mjs";

export default definePluginEntry({
  id: "clickhouse-loom",
  name: "ClickHouse Loom",
  description: "Explicit memory save and search in a configured Loom namespace",
  configSchema: {
    validate(input) {
      try {
        return { ok: true, value: parseConfig(input) };
      } catch (error) {
        return { ok: false, errors: [error.message] };
      }
    },
  },
  register(api) {
    const config = parseConfig(api.pluginConfig);
    const token = process.env[config.tokenEnv]?.trim();
    if (!token) throw new Error(`Set ${config.tokenEnv} in the Gateway environment before enabling ClickHouse Loom`);
    const makeClient = (assertCurrent) =>
      new LoomClient({
        baseUrl: config.baseUrl,
        apiKey: token,
        timeoutMs: 10000,
        logLevel: "off",
        fetch(...args) {
          // SDK pacing and retries must recheck the host's live authority before each send.
          assertCurrent();
          return fetch(...args);
        },
      });
    api.registerTool(
      { contextVersion: 2, create: (context) => createTools(config, context, makeClient) },
      { names: ["clickhouse_loom_save", "clickhouse_loom_search"], optional: true, workspaceAccess: false },
    );
  },
});
