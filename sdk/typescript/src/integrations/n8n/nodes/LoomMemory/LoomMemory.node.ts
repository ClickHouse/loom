import { BufferWindowMemory } from "@langchain/classic/memory";
import type { INodeType, INodeTypeDescription, ISupplyDataFunctions, SupplyData } from "n8n-workflow";
import { NodeConnectionTypes, NodeOperationError } from "../../runtime.js";
import { client, identityProperties } from "../client.js";
import { LoomChatHistory } from "./LoomChatHistory.js";

export class LoomMemory implements INodeType {
  description: INodeTypeDescription = {
    displayName: "Loom Chat Memory",
    name: "loomMemory",
    icon: { light: "file:loom.svg", dark: "file:loom.svg" },
    group: ["transform"],
    version: 1,
    description: "Keep agent conversation history in Loom",
    subtitle: "Chat history",
    defaults: { name: "Loom Chat Memory" },
    inputs: [],
    outputs: [NodeConnectionTypes.AiMemory],
    outputNames: ["Memory"],
    credentials: [{ name: "loomApi", required: true }],
    codex: { categories: ["AI"], subcategories: { AI: ["Memory"] } },
    properties: [
      ...identityProperties,
      {
        displayName: "Session ID",
        name: "sessionId",
        type: "string",
        default: "={{ $json.sessionId }}",
        required: true,
        description: "Conversation identifier, scoped to this workflow and user",
      },
      {
        displayName: "Context Window Length",
        name: "contextWindowLength",
        type: "number",
        default: 5,
        typeOptions: { minValue: 1, maxValue: 1000 },
        description: "Number of previous interactions to send to the agent",
      },
    ],
  };

  async supplyData(this: ISupplyDataFunctions, itemIndex: number): Promise<SupplyData> {
    const { agent, userId } = await client(this, itemIndex);
    const rawSessionId = this.getNodeParameter("sessionId", itemIndex);
    if ((typeof rawSessionId !== "string" && typeof rawSessionId !== "number") || !String(rawSessionId).trim()) {
      throw new NodeOperationError(this.getNode(), "Session ID must be a non-empty string or number");
    }
    const sessionId = String(rawSessionId);
    const history = new LoomChatHistory(agent, JSON.stringify([this.getWorkflow().id, sessionId]), userId);
    return {
      response: new BufferWindowMemory({
        chatHistory: history,
        k: Number(this.getNodeParameter("contextWindowLength", itemIndex)),
        memoryKey: "chat_history",
        inputKey: "input",
        outputKey: "output",
        returnMessages: true,
      }),
    };
  }
}
