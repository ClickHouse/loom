import { protectedBrowserTransport } from "../../../sdk/typescript/src/browser-protection.js";
import type { SessionProtection } from "../../../sdk/typescript/src/session-protection.js";
import { OP_BY_NAME } from "../../../sdk/typescript/src/generated/ops.js";
import { dashboardSessionProtection } from "./dashboardSessionProtection";
import { ADMIN_SETTINGS_READ_ROUTES } from "../api/adminReadRoutes";

/** Explicit intent belongs to one invocation. Never retain it in a client or callback. */
export type DashboardRequestIntent = "automatic" | "explicit";

function isReadOnlyOperation(input: string, init: RequestInit): boolean {
  if (init.method !== "POST" || !input.startsWith("/v1/")) return false;
  if (Object.values(ADMIN_SETTINGS_READ_ROUTES).some((path) => path === input)) return true;
  const name = input.slice(4);
  return Object.hasOwn(OP_BY_NAME, name) && OP_BY_NAME[name as keyof typeof OP_BY_NAME].readOnly === true;
}

export function createDashboardTransport(options: {
  baseUrl: string;
  family: () => Promise<SessionProtection>;
  fetch: (input: string, init: RequestInit) => Promise<Response>;
}) {
  // The same promise and handle survive request, credential, namespace and conversation changes.
  let retained: ReturnType<typeof options.family> | undefined;
  return async (input: string, init: RequestInit, intent: DashboardRequestIntent = "automatic", download = false) => {
    retained ??= options.family();
    const transport = protectedBrowserTransport({
      baseUrl: options.baseUrl,
      sessionProtection: await retained,
      fetch: options.fetch,
    });
    if (download) return transport.downloadFetch(input, init);
    const read = init.method === "GET" || isReadOnlyOperation(input, init);
    return intent === "explicit" || read ? transport.fetch(input, init) : transport.automaticFetch(input, init);
  };
}

let retained: ReturnType<typeof createDashboardTransport> | undefined;

export function dashboardFetch(
  input: string,
  init: RequestInit,
  intent: DashboardRequestIntent = "automatic",
  download = false,
): Promise<Response> {
  retained ??= createDashboardTransport({
    baseUrl: window.location.origin,
    family: dashboardSessionProtection,
    fetch: (url, options) => globalThis.fetch(url, options),
  });
  return retained(input, init, intent, download);
}
