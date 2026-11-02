import { act, cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MentorBriefResult, MyNamespaceEntry, NamespaceAuditResult, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { setFoldOpen } from "../lib/folds";
import { MentorPanel } from "./MentorPanel";
import { NamespaceAuditPanel } from "./NamespaceAuditPanel";

// The mentor and audit panels answer for the namespace on screen. A run started in one
// namespace that resolves after the switch to another is dropped, never shown as the
// new namespace's brief or report.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, mentorBrief: vi.fn(), auditNamespace: vi.fn() };
});
import { auditNamespace, mentorBrief } from "../api";

const viewer: WhoAmI = {
  authenticated: true,
  subject: "v@example.com",
  org: "dev",
  roles: ["VIEWER"],
  namespaces: ["a", "b"],
};
const row = (namespace: string): MyNamespaceEntry => ({
  namespace,
  role: "viewer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
});
const inA = { org: "dev", namespace: "a", agent: "loom-ui" };
const inB = { org: "dev", namespace: "b", agent: "loom-ui" };

const brief = (namespace: string): MentorBriefResult => ({
  org: "dev",
  namespace,
  agent: `writer-in-${namespace}`,
  window_seconds: 7 * 86_400,
  computed_at: "2026-09-20T00:00:00",
  sessions: 1,
  usage: {},
  planes: [],
  hints: [],
  adoption: [],
  agents: [`writer-in-${namespace}`],
  agent_defaulted: false,
  skipped: [],
  healthy: true,
});
const report = (namespace: string, criticals: number): NamespaceAuditResult => ({
  org: "dev",
  namespace,
  computed_at: "2026-09-20T00:00:00",
  window_seconds: 7 * 86_400,
  summary: {
    live_rows: 1,
    live_bytes: 1,
    excerpt_bytes: 0,
    version_rows: 1,
    version_bytes: 1,
    unembedded_rows: 0,
    gets: 0,
    searches: 0,
    sets: 0,
    get_search_ratio: 0,
    inspector_calls: 0,
  },
  findings: [],
  skipped: [],
  healthy: criticals === 0,
  criticals,
  warnings: 0,
  infos: 0,
});

function deferred<T>() {
  let resolve: (v: T) => void = () => {};
  let reject: (e: Error) => void = () => {};
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("panels drop a run for the namespace they moved off", () => {
  beforeEach(() => {
    // The panel starts folded; these tests are about what it shows once opened.
    setFoldOpen("usage-audit", true);
    setFoldOpen("mentor", true);
    // Reset, not clear: a queued Once a failed test left unused must not answer the next.
    vi.resetAllMocks();
    resetNamespaceRoleCache();
    useStore.setState({ authed: true, whoami: viewer, myNamespaces: [row("a"), row("b")], identity: inA });
  });
  afterEach(() => cleanup());

  it("mentor: a's brief resolving after the switch to b is not shown as b's", async () => {
    const late = deferred<MentorBriefResult>();
    vi.mocked(mentorBrief)
      .mockImplementationOnce(() => late.promise)
      .mockImplementationOnce(async () => brief("b"));
    const panel = render(<MentorPanel identity={inA} />);
    fireEvent.click(screen.getByRole("button", { name: /Ask the mentor/ }));
    panel.rerender(<MentorPanel identity={inB} />);
    await act(async () => late.resolve(brief("a")));
    expect(screen.queryByTestId("mentor-summary")).not.toBeInTheDocument();
    expect(screen.queryByText("writer-in-a")).not.toBeInTheDocument();
    // The button is free again, and b's own run lands.
    fireEvent.click(screen.getByRole("button", { name: /Ask the mentor/ }));
    expect(await screen.findByTestId("mentor-summary")).toHaveTextContent("writer-in-b");
  });

  it("audit: a's report resolving after the switch to b is not shown as b's", async () => {
    const late = deferred<NamespaceAuditResult>();
    vi.mocked(auditNamespace)
      .mockImplementationOnce(() => late.promise)
      .mockImplementationOnce(async () => report("b", 0));
    const panel = render(<NamespaceAuditPanel identity={inA} />);
    fireEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    panel.rerender(<NamespaceAuditPanel identity={inB} />);
    await act(async () => late.resolve(report("a", 3)));
    expect(screen.queryByTestId("audit-summary")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    expect(await screen.findByTestId("audit-summary")).toHaveTextContent("Healthy");
  });

  it("audit: a's failure after the switch to b raises no error under b", async () => {
    const late = deferred<NamespaceAuditResult>();
    vi.mocked(auditNamespace).mockImplementationOnce(() => late.promise);
    const panel = render(<NamespaceAuditPanel identity={inA} />);
    fireEvent.click(screen.getByRole("button", { name: /Run audit/ }));
    panel.rerender(<NamespaceAuditPanel identity={inB} />);
    await act(async () => late.reject(new Error("boom")));
    expect(screen.queryByText(/Audit failed/)).not.toBeInTheDocument();
  });
});
