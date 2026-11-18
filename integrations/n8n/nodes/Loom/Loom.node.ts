import {
  NodeConnectionTypes,
  NodeOperationError,
  type IExecuteFunctions,
  type INodeExecutionData,
  type INodeType,
  type INodeTypeDescription,
} from "n8n-workflow";
import { client, identityProperties } from "../client";

export class Loom implements INodeType {
  description: INodeTypeDescription = {
    displayName: "Loom",
    name: "loom",
    icon: { light: "file:loom.svg", dark: "file:loom.svg" },
    group: ["transform"],
    version: 1,
    description: "Remember facts, recall memories and search documents in Loom",
    subtitle: '={{$parameter["operation"]}}',
    defaults: { name: "Loom" },
    usableAsTool: true,
    inputs: [NodeConnectionTypes.Main],
    outputs: [NodeConnectionTypes.Main],
    credentials: [{ name: "loomApi", required: true }],
    properties: [
      {
        displayName: "Operation",
        name: "operation",
        type: "options",
        default: "recall",
        noDataExpression: true,
        options: [
          { name: "Recall", value: "recall", action: "Recall memories" },
          { name: "Remember", value: "remember", action: "Remember a fact" },
          {
            name: "Search Knowledge",
            value: "searchKnowledge",
            action: "Search knowledge documents",
          },
        ],
      },
      ...identityProperties,
      {
        displayName: "Memory Key",
        name: "memoryKey",
        type: "string",
        default: "",
        required: true,
        displayOptions: { show: { operation: ["remember"] } },
      },
      {
        displayName: "Value",
        name: "value",
        type: "string",
        default: "",
        required: true,
        typeOptions: { rows: 4 },
        displayOptions: { show: { operation: ["remember"] } },
      },
      {
        displayName: "Query",
        name: "query",
        type: "string",
        default: "",
        required: true,
        displayOptions: { show: { operation: ["recall", "searchKnowledge"] } },
      },
      {
        displayName: "Limit",
        name: "limit",
        type: "number",
        description: "Max number of results to return",
        default: 50,
        typeOptions: { minValue: 1, maxValue: 100 },
        displayOptions: { show: { operation: ["recall", "searchKnowledge"] } },
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const output: INodeExecutionData[] = [];
    for (let i = 0; i < this.getInputData().length; i++) {
      try {
        const { agent, userId } = await client(this, i);
        const scope = { userId };
        const operation = String(this.getNodeParameter("operation", i));
        let result: object;
        if (operation === "remember") {
          result = await agent.call(
            "memory.set",
            {
              memory_key: String(this.getNodeParameter("memoryKey", i)),
              value: String(this.getNodeParameter("value", i)),
            },
            scope,
          );
        } else {
          const query = String(this.getNodeParameter("query", i));
          const top_k = Number(this.getNodeParameter("limit", i));
          if (operation === "recall") {
            result = await agent.call(
              "memory.search",
              { query, top_k, namespace_scope: "this" },
              { ...scope, agentId: "*" },
            );
          } else if (operation === "searchKnowledge") {
            result = await agent.call("knowledge.search", { query, top_k, namespace_scope: "this" }, scope);
          } else throw new NodeOperationError(this.getNode(), `Unknown operation: ${operation}`);
        }
        output.push({ json: result as INodeExecutionData["json"], pairedItem: { item: i } });
      } catch (error) {
        const failure = error instanceof Error ? error : new Error(String(error));
        if (!this.continueOnFail()) throw new NodeOperationError(this.getNode(), failure, { itemIndex: i });
        output.push({ json: { error: failure.message }, pairedItem: { item: i } });
      }
    }
    return [output];
  }
}
