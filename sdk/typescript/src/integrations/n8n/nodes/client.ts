import type { IExecuteFunctions, INodeProperties, ISupplyDataFunctions } from "n8n-workflow";
import { Agent } from "../../../agent.js";

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

function trimUserId(value: string): string {
  // Server reads also strip NEL and the four information separators.
  const isWhitespace = (character: string) =>
    character.trim() === "" || character === "\u0085" || (character >= "\u001c" && character <= "\u001f");
  let start = 0;
  let end = value.length;
  while (start < end && isWhitespace(value[start])) start++;
  while (end > start && isWhitespace(value[end - 1])) end--;
  return value.slice(start, end);
}

export async function client(context: IExecuteFunctions | ISupplyDataFunctions, item: number) {
  const credentials = await context.getCredentials("loomApi");
  const text = (name: string) => String(context.getNodeParameter(name, item)).trim();
  const userId = trimUserId(text("userId"));
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
