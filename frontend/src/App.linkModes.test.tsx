// Behind a proxy that adds Authorization, or with auth off, the page sends no token of its
// own: a #token= link is scrubbed and dropped, with no confirmation and nothing stored.
import { cleanup, render, screen } from "./test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { mode } = vi.hoisted(() => ({ mode: { proxy: false, authOff: false } }));
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, isProxyMode: () => mode.proxy, isAuthDisabled: () => mode.authOff };
});
vi.mock("./views/registry", () => ({ preloadView: vi.fn(), VIEW_LOADERS: {} }));
vi.mock("./lib/navigation", () => ({ goToSso: vi.fn(), reloadPage: vi.fn() }));

import App from "./App";
import { useStore } from "./lib/state";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64(JSON.stringify({ alg: "HS256" }))}.${b64(JSON.stringify(payload))}.sig`;
}

describe("a link token where the page sends none", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    useStore.setState({ authed: false, gateReason: null, whoami: null, myNamespaces: null, identity: null });
    Element.prototype.scrollTo = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ authenticated: false }), { status: 200 })),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    mode.proxy = false;
    mode.authOff = false;
    window.history.replaceState(null, "", "/");
  });

  for (const which of ["proxy", "authOff"] as const) {
    it(`is dropped unasked and scrubbed (${which})`, async () => {
      mode[which] = true;
      const mallory = jwt({ sub: "mallory", org: "acme" });
      window.history.replaceState(null, "", `/settings#token=${mallory}`);
      render(<App />);
      await new Promise((r) => setTimeout(r, 200));
      expect(localStorage.getItem("loom_token_v1")).toBeNull();
      expect(window.location.href).not.toContain("token=");
      expect(screen.queryByText(/Sign in from a link/)).toBeNull();
      expect(useStore.getState().gateReason).toBeNull();
    });
  }
});
