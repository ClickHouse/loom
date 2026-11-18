import type { ICredentialType, INodeProperties } from "n8n-workflow";

export class LoomApi implements ICredentialType {
  name = "loomApi";
  displayName = "Loom API";
  icon = "file:loom.svg" as const;
  authenticate = {
    type: "generic" as const,
    properties: { headers: { Authorization: "=Bearer {{$credentials.apiKey}}" } },
  };
  test = { request: { baseURL: "={{$credentials.baseUrl}}", url: "/v1/me" } };
  documentationUrl = "https://clickhouse.github.io/loom/getting-started/quickstart/";
  properties: INodeProperties[] = [
    {
      displayName: "Server URL",
      name: "baseUrl",
      type: "string",
      default: "",
      placeholder: "https://loom.example.com",
      required: true,
      description: "The Loom deployment reachable from the n8n worker",
    },
    {
      displayName: "API Token",
      name: "apiKey",
      type: "string",
      default: "",
      typeOptions: { password: true },
      required: true,
      description: "A token with writer access to the namespace",
    },
  ];
}
