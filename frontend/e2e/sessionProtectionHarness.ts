export { post, postFile, postStreaming, getJson, getResponse } from "../src/api";
export { DashboardSessionProtectionStore } from "../src/lib/dashboardSessionProtection";
export { createDashboardTransport } from "../src/lib/dashboardTransport";
export { mcpRead } from "../src/api/mcpSources";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { MCPSourceSetup } from "../src/flows/MCPSourceSetup";
import { AddMemoryDialog } from "../src/flows/AddMemoryDialog";

export function mountSourceSetup() {
  const target = document.createElement("div");
  document.body.append(target);
  const root = createRoot(target);
  root.render(createElement(MCPSourceSetup, {
    org: "acme", callerKey: "owner", collectionVersion: "grants",
    onSaved: () => {}, onClose: () => {},
  }));
  return () => { root.unmount(); target.remove(); };
}

export function mountMemory() {
  const target = document.createElement("div");
  document.body.append(target);
  const root = createRoot(target);
  root.render(createElement(AddMemoryDialog, {
    open: true, identity: { org: "acme", namespace: "col-team", agent: "loom-ui" },
    onAdded: () => {}, onOpenChange: () => {},
  }));
}
