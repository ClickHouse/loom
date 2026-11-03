import { render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axeViolations } from "../test/axe";
import { ErrorBoundary, buildDiagnostics } from "./ErrorBoundary";
import { setPageOwner, setToken } from "../api";

// A render error shows a message, Reload and Copy diagnostics instead of a blank page.

function Boom({ when }: { when: boolean }) {
  if (when) throw new Error("card exploded");
  return <div>fine</div>;
}

describe("ErrorBoundary", () => {
  let consoleError: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    // React logs the caught error and the boundary logs once more; neither
    // is the failure under test.
    consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ build_commit: "abc1234" }), { status: 200 })),
    );
  });
  afterEach(() => {
    consoleError.mockRestore();
    vi.unstubAllGlobals();
  });

  it("renders its children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <Boom when={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("fine")).toBeInTheDocument();
  });

  it("shows the message, Reload and Copy diagnostics when a child throws — and passes axe", async () => {
    const { container } = render(
      <ErrorBoundary>
        <Boom when={true} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.getByText("card exploded")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy diagnostics" })).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it("copies message, stack, route and build commit", async () => {
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    history.replaceState(null, "", "/memories?x=1");
    render(
      <ErrorBoundary>
        <Boom when={true} />
      </ErrorBoundary>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
    expect(writeText).toHaveBeenCalledTimes(1);
    const text = writeText.mock.calls.at(0)?.[0];
    expect(text).toBeDefined();
    expect(text).toContain("message: card exploded");
    expect(text).toContain("route: /memories?x=1");
    expect(text).toContain("build: abc1234");
    expect(text).toContain("stack:");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("reads health with no token when the stored one names someone other than the page's person", async () => {
    const part = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
    setPageOwner({ authenticated: true, subject: "alice", org: "o" });
    setToken(`${part({ alg: "HS256" })}.${part({ sub: "bob", org: "o" })}.sig`);
    try {
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText: vi.fn(async () => undefined) },
        configurable: true,
      });
      render(
        <ErrorBoundary>
          <Boom when={true} />
        </ErrorBoundary>,
      );
      await userEvent.click(screen.getByRole("button", { name: "Copy diagnostics" }));
      const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith("/v1/health"));
      // The read was made, and made without the token.
      expect(call).toBeDefined();
      expect(new Headers(call?.[1]?.headers).get("Authorization")).toBeNull();
    } finally {
      setPageOwner(null);
      localStorage.clear();
    }
  });

  it("says `unknown` for the build when /v1/health is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const text = await buildDiagnostics(new Error("x"), null);
    expect(text).toContain("build: unknown");
  });

  it("says `unknown` for the build when /v1/health's build_commit is not a string", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ build_commit: 1234 }), { status: 200 })),
    );
    const text = await buildDiagnostics(new Error("x"), null);
    expect(text).toContain("build: unknown");
    expect(text).not.toContain("build: 1234");
  });
});
