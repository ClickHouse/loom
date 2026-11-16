import { Agent } from "@clickhouse/loom";
import type { IExecuteFunctions, INodeProperties, ISupplyDataFunctions } from "n8n-workflow";

export const identityProperties: INodeProperties[] = [
  { displayName: "Organization", name: "org", type: "string", default: "", required: true },
  { displayName: "Namespace", name: "namespace", type: "string", default: "", required: true },
  { displayName: "Agent Name", name: "agent", type: "string", default: "n8n", required: true },
  {
    displayName: "User ID",
    name: "userId",
    type: "string",
    default: "",
    description:
      "The end user whose memories to read and write. Leave empty to write shared memories and recall all users allowed by the token.",
  },
];

export async function client(context: IExecuteFunctions | ISupplyDataFunctions, item: number) {
  const credentials = await context.getCredentials("loomApi");
  const text = (name: string) => String(context.getNodeParameter(name, item)).trim();
  const userId = text("userId");
  if (userId === "-") throw new Error("User ID must name a user or be empty");
  const org = text("org");
  const namespace = text("namespace");
  const name = text("agent");
  if (!org || !namespace || !name || name === "-" || name === "*") {
    throw new Error("Organization, namespace and agent name must be set");
  }
  return {
    agent: new Agent(org, namespace, name, {
      baseUrl: String(credentials.baseUrl),
      apiKey: String(credentials.apiKey),
      logLevel: "off",
    }),
    userId: userId || undefined,
  };
}
